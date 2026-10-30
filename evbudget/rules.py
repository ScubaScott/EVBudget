"""Classification engine.

Every transaction goes through one pipeline. The first stage that matches
decides the transaction's kind:

  1. include_override  keep something that would otherwise be excluded
  2. exclude           credit card payments and transfers between our accounts
  3. paycheck          positive amount matching an income source
  4. positive, not a paycheck: income/refund rules, then envelope rules,
     then a similar recent purchase, then "other income" for review
  5. negative amount: check heuristic, envelope rules, default Shopping
"""

import functools
import html
import re
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from . import income, store
from .textutil import collapse_ws, vendor_key_tokens

KINDS = ("expense", "refund", "paycheck", "other_income", "transfer", "cc_payment")
INCOME_KINDS = ("paycheck", "other_income")
EXCLUDED_KINDS = ("transfer", "cc_payment")
STAGES = ("include_override", "exclude", "income", "refund", "envelope", "payee")
MATCH_MODES = ("contains", "word", "regex", "vendor")

ORIGIN_LABELS = {"user": "User Learned Rule", "vendor": "Known Vendor DB", "core": "Core Rule"}


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


class RuleSet:
    """Active rules in the order they should be tried.

    Personal rules are tried before shared ones, so what someone sets for themselves wins. A shared rule
    that sends a purchase to an envelope this budget doesn't have is skipped and noted in `skipped`;
    classify() turns that into a review flag.
    """

    def __init__(self, rules: Iterable[Dict[str, Any]], known_envelopes: Optional[Iterable[str]] = None):
        self.rules = sorted(
            (dict(r) for r in rules if r.get("active", 1)),
            key=lambda r: (r.get("scope") == "shared", r.get("priority", 0), r.get("id") or 0),
        )
        self.known_envelopes = None if known_envelopes is None else {e.strip().lower() for e in known_envelopes}
        self.skipped: List[Dict[str, Any]] = []
        self.last_match: Optional[Dict[str, Any]] = None  # the rule first() last returned, for explaining a result

    def _envelope_missing(self, rule: Dict[str, Any]) -> bool:
        env = (rule.get("set_envelope") or "").strip().lower()
        return bool(
            self.known_envelopes is not None and rule.get("scope") == "shared" and env and env not in self.known_envelopes
        )

    def first(self, stages: Sequence[str], hay_raw: str, hay_name: str) -> Optional[Dict[str, Any]]:
        for rule in self.rules:
            if rule["stage"] not in stages:
                continue
            # An envelope rule without an envelope would shadow the ones behind it.
            if rule["stage"] == "envelope" and not rule.get("set_envelope"):
                continue
            # Heuristic envelope rules only look at the cleaned payee, as the old importer did.
            if rule["stage"] == "envelope" and rule.get("origin") in ("core", "vendor"):
                texts: Tuple[str, ...] = (hay_name,)
            else:
                texts = (hay_raw, hay_name)
            if rule_matches(rule, texts):
                if self._envelope_missing(rule):
                    self.skipped.append(rule)
                    continue
                self.last_match = rule
                return rule
        return None

    def rewrite_name(self, hay_raw: str, name: str) -> str:
        """Apply rename rules in order. Pattern rules chain (each works on the previous result);
        a plain rename rule is final."""
        for rule in self.rules:
            new = rename_for(rule, hay_raw, name)
            if new is None:
                continue
            name = new
            if (rule.get("match_mode") or "contains") != "regex":
                break
        return name


def _match_label(rule: Dict[str, Any]) -> str:
    return rule.get("label") or ORIGIN_LABELS.get(rule.get("origin") or "", "Core Rule")


def rule_detail(rule: Optional[Dict[str, Any]]) -> str:
    """Which rule decided a result, in words: its label, the phrase it looks for, and who it applies to."""
    if not rule:
        return ""
    bits = [_match_label(rule)]
    if rule.get("phrase"):
        mode = "pattern" if (rule.get("match_mode") or "contains") == "regex" else "contains"
        bits.append(f'{mode} "{rule["phrase"]}"')
    if rule.get("scope"):
        bits.append("everyone" if rule["scope"] == "shared" else "just me")
    return " · ".join(bits)


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
        rules: RuleSet,
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
    rule_rows = [dict(r, scope="personal") for r in conn.execute("SELECT * FROM rules WHERE active = 1")]
    known_envelopes = None
    if store.has_shared(conn):
        rule_rows += [dict(r, scope="shared") for r in conn.execute("SELECT * FROM shared.rules WHERE active = 1")]
        known_envelopes = [r[0] for r in conn.execute("SELECT name FROM envelopes WHERE active = 1")]
    rules = RuleSet(rule_rows, known_envelopes)
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
    """Classify one transaction. Returns the fields the transactions table needs."""
    ctx.rules.skipped = []
    ctx.rules.last_match = None
    out = _classify(ctx, raw_name, payee, amount, date_iso, check_num, trans_type)
    out["match_detail"] = rule_detail(ctx.rules.last_match) or out["match_source"]
    if ctx.rules.skipped:
        missing = ctx.rules.skipped[0]["set_envelope"]
        out["needs_review"] = 1
        out["match_source"] = f"{out['match_source']} (shared rule skipped: no '{missing}' envelope in this budget)"
    return out


def _classify(
    ctx: ClassifyContext, raw_name: str, payee: str, amount: float, date_iso: str, check_num: str, trans_type: str
) -> Dict[str, Any]:
    hay_raw = collapse_ws(html.unescape(raw_name or "")).upper()
    hay_name = payee.upper()

    out: Dict[str, Any] = {
        "txn_kind": "expense",
        "is_included": True,
        "exclusion_reason": "",
        "envelope": "",
        "name": payee,
        "match_source": "",
        "income_source_id": None,
        "person_id": None,
        "income_subtype": None,
        "income_label": None,
        "needs_review": 0,
        "reimburses_txn_id": None,
    }

    out["name"] = ctx.rules.rewrite_name(hay_raw, payee)

    # 1. Include overrides (e.g. allowance transfers to the kids).
    rule = ctx.rules.first(("include_override",), hay_raw, hay_name)
    if rule:
        out["txn_kind"] = rule.get("set_kind") or ("expense" if amount < 0 else "refund")
        out["envelope"] = rule.get("set_envelope") or ""
        out["match_source"] = _match_label(rule)
        return out

    # 2. Exclusions: card payments and transfers between our own accounts.
    if (trans_type or "").strip().upper() == "PAYMENT":
        out.update(
            txn_kind="cc_payment", is_included=False,
            exclusion_reason="Credit Card Payment / Autopay", match_source="Core Rule",
        )
        return out
    rule = ctx.rules.first(("exclude",), hay_raw, hay_name)
    if rule:
        default_kind = "expense" if amount < 0 else "transfer"
        out.update(
            txn_kind=rule.get("set_kind") or default_kind,
            is_included=False,
            exclusion_reason=rule.get("exclusion_reason") or "User Learned Rule",
            match_source=_match_label(rule),
        )
        return out

    # 3. Paychecks.
    if amount > 0:
        src = income.match_income_source(ctx.income_sources, hay_raw, hay_name)
        if src:
            override = (src.get("typical_amount_cents") or 0) / 100.0 or None
            typical = ctx.history.typical_before(src["id"], date_iso, override)
            threshold = src.get("bonus_threshold_pct") or ctx.bonus_threshold_pct
            subtype = income.subtype_for(amount, typical, float(threshold))
            if subtype == "regular":
                ctx.history.add(src["id"], date_iso, amount)
            out.update(
                txn_kind="paycheck",
                income_source_id=src["id"],
                person_id=src.get("person_id"),
                income_subtype=subtype,
                match_source=f"Income Source: {src.get('name') or 'Paycheck'}",
            )
            return out
        return _classify_positive(ctx, out, hay_raw, hay_name, payee, amount, date_iso)

    return _classify_expense(ctx, out, hay_raw, hay_name, check_num)


def _classify_positive(
    ctx: ClassifyContext, out: Dict[str, Any], hay_raw: str, hay_name: str,
    payee: str, amount: float, date_iso: str,
) -> Dict[str, Any]:
    review = 0

    # 4.1 Explicit income / refund rules.
    rule = ctx.rules.first(("income", "refund"), hay_raw, hay_name)
    if rule:
        review = 1 if rule.get("set_needs_review") else 0
        if rule.get("set_kind"):
            out.update(
                txn_kind=rule["set_kind"],
                envelope=rule.get("set_envelope") or "",
                income_label=rule.get("set_income_label"),
                match_source=_match_label(rule),
                needs_review=review,
            )
            return out

    # 4.2 A user, vendor, or core envelope rule matches: it's a refund to that envelope.
    rule2 = ctx.rules.first(("envelope",), hay_raw, hay_name)
    if rule2 and rule2.get("set_envelope"):
        out.update(
            txn_kind="refund", envelope=rule2["set_envelope"],
            match_source=f"Refund: {_match_label(rule2)}", needs_review=review,
        )
        return out

    # 4.3 A similar recent purchase tells us where the money belongs.
    env = ctx.expenses.find(date_iso, payee, ctx.refund_lookback_days)
    if env:
        out.update(txn_kind="refund", envelope=env, match_source="Refund: Recent Purchase", needs_review=review)
        return out

    # 4.4 Unknown positive amount: park as other income and ask for review.
    out.update(
        txn_kind="other_income", income_label=payee[:60],
        match_source="Other Income (Review)", needs_review=1,
    )
    return out


def _classify_expense(
    ctx: ClassifyContext, out: Dict[str, Any], hay_raw: str, hay_name: str, check_num: str
) -> Dict[str, Any]:
    out["txn_kind"] = "expense"
    if check_num and check_num.strip():
        out.update(envelope="Bills: One Time", match_source="Check Heuristic")
        return out
    rule = ctx.rules.first(("envelope",), hay_raw, hay_name)
    if rule and rule.get("set_envelope"):
        out.update(envelope=rule["set_envelope"], match_source=_match_label(rule))
        return out
    out.update(envelope="Shopping", match_source="Default Fallback")
    return out


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
