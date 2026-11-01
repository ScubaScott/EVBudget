"""Matching and renaming helpers, and the context a classification runs in.

The rules themselves (what they look for and what they do) are in rule_engine.py, which is also where a
transaction is classified; classify() here is the entry point everything calls.
"""

import functools
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import income, store
from .textutil import collapse_ws, vendor_key_tokens

KINDS = ("expense", "refund", "paycheck", "other_income", "transfer", "cc_payment")
INCOME_KINDS = ("paycheck", "other_income")
EXCLUDED_KINDS = ("transfer", "cc_payment")
MATCH_MODES = ("contains", "word", "regex")


# ---------------------------------------------------------------------------
# Rule matching
# ---------------------------------------------------------------------------

@functools.lru_cache(maxsize=512)
def _compiled(pattern: str, flags: int = re.IGNORECASE):
    return re.compile(pattern, flags)


def rule_matches(rule: Dict[str, Any], texts: Sequence[str]) -> bool:
    """True if the rule's phrase matches any of the (upper-case) texts."""
    phrase = (rule.get("phrase") or "").strip()
    if not phrase:
        return False
    mode = rule.get("match_mode") or "contains"
    upper = phrase.upper()
    for text in texts:
        if mode == "contains":
            if upper in text:
                return True
        elif mode == "word":
            if re.search(r"\b" + re.escape(upper) + r"\b", text):
                return True
        elif mode == "regex":
            try:
                if _compiled(phrase).search(text):
                    return True
            except re.error:
                return False
        elif mode == "vendor":
            # Known-vendor keys match when contained either way round.
            if upper in text or (len(text) >= 4 and text in upper):
                return True
    return False


_REF_BRACED = re.compile(r"\$\{(\d+)\}")
_REF_DOLLAR = re.compile(r"\$(\d+)")
_REF_ANY = re.compile(r"\\g<(\w+)>|\\(\d+)|\$\{(\d+)\}|\$(\d+)")


def translate_replacement(repl: str) -> str:
    """Let people write $1 or ${1} as well as \\1 for a captured group."""
    return _REF_DOLLAR.sub(r"\\g<\1>", _REF_BRACED.sub(r"\\g<\1>", repl))


def check_rename_rule(pattern: str, repl: str) -> None:
    """Raise ValueError if the pattern isn't valid or the replacement names a group the pattern doesn't have."""
    try:
        compiled = re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        raise ValueError(f"Invalid pattern: {exc}")
    for m in _REF_ANY.finditer(repl):
        named, num = m.group(1), next((g for g in m.groups()[1:] if g), None)
        if named is not None and not named.isdigit():
            if named not in compiled.groupindex:
                raise ValueError(f"The pattern has no group called '{named}'")
        else:
            n = int(named if named is not None else num)
            if n > compiled.groups:
                raise ValueError(f"The replacement uses group {n}, but the pattern only has {compiled.groups} group(s). Put (parentheses) around the part to keep.")


def rename_for(rule: Dict[str, Any], hay_raw: str, name: str) -> Optional[str]:
    """The new payee name if this rule renames `name`, otherwise None.

    A pattern rule replaces just the part it matches (so use ^...$ or (.*) to rewrite the whole name),
    and the replacement can use captured groups. A plain rule replaces the whole name.
    """
    repl = rule.get("set_payee")
    if not repl:
        return None
    if (rule.get("match_mode") or "contains") == "regex":
        try:
            pat = _compiled(rule["phrase"])
        except re.error:
            return None
        if not pat.search(name):
            return None
        try:
            new = pat.sub(translate_replacement(repl), name, count=1)
        except (re.error, IndexError):
            return None
        return collapse_ws(new) or None
    if rule_matches(rule, (hay_raw, name.upper())):
        return repl.strip()
    return None


# ---------------------------------------------------------------------------
# Recent purchases, for matching refunds back to what was bought
# ---------------------------------------------------------------------------

class ExpenseIndex:
    def __init__(self) -> None:
        self._rows: List[Tuple[str, Tuple[str, ...], str]] = []

    def add(self, date_iso: str, name: str, envelope: str) -> None:
        tokens = vendor_key_tokens(name)
        if tokens and envelope:
            self._rows.append((date_iso, tokens, envelope))

    def find(self, date_iso: str, name: str, lookback_days: int) -> Optional[str]:
        """Envelope of the most recent similar purchase in the lookback window."""
        tokens = vendor_key_tokens(name)
        if not tokens:
            return None
        try:
            earliest = (datetime.strptime(date_iso, "%Y-%m-%d") - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
        except ValueError:
            return None
        best_full: Optional[Tuple[str, str]] = None
        best_first: Optional[Tuple[str, str]] = None
        for d, toks, env in self._rows:
            if d > date_iso or d < earliest:
                continue
            if toks == tokens:
                if best_full is None or d >= best_full[0]:
                    best_full = (d, env)
            elif toks[0] == tokens[0] and len(tokens[0]) >= 5:
                if best_first is None or d >= best_first[0]:
                    best_first = (d, env)
        if best_full:
            return best_full[1]
        if best_first:
            return best_first[1]
        return None


class ClassifyContext:
    def __init__(
        self,
        rules: Any,
        income_sources: List[Dict[str, Any]],
        history: income.PaycheckHistory,
        expenses: ExpenseIndex,
        bonus_threshold_pct: float = 50.0,
        refund_lookback_days: int = 120,
    ):
        self.rules = rules
        self.income_sources = income_sources
        self.history = history
        self.expenses = expenses
        self.bonus_threshold_pct = bonus_threshold_pct
        self.refund_lookback_days = refund_lookback_days


def load_context(conn: Any) -> ClassifyContext:
    """Build a classification context from the database."""
    from .rule_engine import RuleBook

    rule_rows = [dict(r) for r in conn.execute("SELECT * FROM rules WHERE active = 1")]
    if store.has_shared(conn):
        rule_rows += [dict(r) for r in conn.execute("SELECT * FROM shared.rules WHERE active = 1")]
    known_envelopes = [r[0] for r in conn.execute("SELECT name FROM envelopes WHERE active = 1")]
    rules = RuleBook(rule_rows, known_envelopes)
    sources = [dict(r) for r in conn.execute("SELECT * FROM income_sources WHERE active = 1 ORDER BY id")]
    expenses = ExpenseIndex()
    for r in conn.execute(
        "SELECT date, name, envelope FROM transactions WHERE is_included = 1 AND txn_kind = 'expense' AND amount < 0"
    ):
        expenses.add(r[0], r[1], (r[2] or "").strip())
    return ClassifyContext(
        rules=rules,
        income_sources=sources,
        history=income.PaycheckHistory.from_db(conn),
        expenses=expenses,
        bonus_threshold_pct=float(store.get_setting(conn, "bonus_threshold_pct")),
        refund_lookback_days=int(store.get_setting(conn, "refund_lookback_days")),
    )


# ---------------------------------------------------------------------------
# The pipeline
# ---------------------------------------------------------------------------

def classify(
    ctx: ClassifyContext,
    *,
    raw_name: str,
    payee: str,
    amount: float,
    date_iso: str,
    check_num: str = "",
    trans_type: str = "",
) -> Dict[str, Any]:
    """Classify one transaction. Returns the fields the transactions table needs, plus `rules`:
    which rules applied and what each did."""
    from . import rule_engine

    return rule_engine.classify(
        ctx, ctx.rules, raw_name=raw_name, payee=payee, amount=amount, date_iso=date_iso,
        check_num=check_num, trans_type=trans_type,
    )


# ---------------------------------------------------------------------------
# Reimbursement matching (Venmo and similar)
# ---------------------------------------------------------------------------

_RATIOS = [(1.0, "full amount"), (0.5, "half"), (1 / 3, "a third"), (0.25, "a quarter")]


def reimbursement_candidates(
    conn: Any, date_iso: str, amount: float, exclude_txn_id: str = "", window_days: int = 60, limit: int = 25,
) -> List[Dict[str, Any]]:
    """Recent included expenses that this reimbursement could be paying back.

    Only expenses at least as large as the reimbursement qualify. Ones where the
    reimbursement is the full amount, half, a third, or a quarter come first.
    """
    try:
        end = datetime.strptime(date_iso, "%Y-%m-%d")
    except ValueError:
        return []
    start = (end - timedelta(days=window_days)).strftime("%Y-%m-%d")
    reimb = abs(amount)
    rows = conn.execute(
        """
        SELECT e.txn_id, e.date, e.name, e.amount, e.envelope,
               (SELECT COALESCE(SUM(r.amount), 0) FROM transactions r
                 WHERE r.reimburses_txn_id = e.txn_id AND r.is_included = 1) AS reimbursed
        FROM transactions e
        WHERE e.is_included = 1 AND e.txn_kind = 'expense' AND e.amount < 0
          AND e.date >= ? AND e.date <= ? AND ABS(e.amount) >= ? AND e.txn_id != ?
        """,
        (start, date_iso, reimb - 0.005, exclude_txn_id),
    ).fetchall()

    out = []
    for r in rows:
        exp = abs(float(r["amount"]))
        best_err, best_label = None, ""
        for ratio, label in _RATIOS:
            err = abs(reimb - ratio * exp) / exp if exp else 1.0
            if best_err is None or err < best_err:
                best_err, best_label = err, label
        days_ago = (end - datetime.strptime(r["date"], "%Y-%m-%d")).days
        out.append({
            "txn_id": r["txn_id"], "date": r["date"], "name": r["name"], "amount": float(r["amount"]),
            "envelope": r["envelope"] or "", "days_ago": days_ago,
            "already_reimbursed": round(float(r["reimbursed"]), 2),
            "likely": bool(best_err is not None and best_err <= 0.02),
            "split": best_label if best_err is not None and best_err <= 0.02 else "",
        })
    out.sort(key=lambda c: (not c["likely"], c["days_ago"]))
    return out[:limit]
