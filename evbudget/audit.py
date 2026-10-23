"""Audits: find double charges and charges that look wrong.

Every check works on the whole history, because "unusual" only means something next to what's normal.
A check can be limited to a set of transactions (the ones just imported), and anything marked OK is
remembered so a rerun stays quiet.

The bank exports have no time of day, card number, or place, so the checks lean on what is there:
who was paid, how much, and how the pattern compares with the past.
"""

import json
import re
import statistics
from collections import defaultdict
from datetime import date, timedelta
from typing import Any, Callable, Dict, Iterable, List, Optional, Set

from . import store
from .envelopes import KNOWN_INTERVALS
from .textutil import clean_vendor_for_lookup

# id, group, title, plain-language help, on for "Run audit", on for every import
CHECKS = [
    ("dup_same_day", "duplicates", "Same payee, same amount, same day",
     "The same charge twice in one day. Could be a double charge, or two real purchases.", True, True),
    ("dup_near_days", "duplicates", "Same payee, same amount, within 3 days",
     "A repeat of a charge a day or two later: a pending charge plus its posted copy, or a payment sent twice.", True, True),
    ("dup_import_twins", "duplicates", "Saved twice from overlapping files",
     "The same bank line saved twice because two downloads covered the same days.", True, True),
    ("dup_amount_date", "duplicates", "Same date and amount, different payees",
     "Two charges that match on date and amount only. Noisy, but catches a payee that was renamed.", False, False),
    ("uncategorized", "tidy", "Spending with no envelope",
     "Expenses that haven't been given an envelope yet.", True, False),
    ("new_person_paid", "unusual", "First payment to a new person",
     "The first Zelle, Venmo, PayPal, Cash App or bank transfer to someone you haven't paid before. Scams often start here.", True, True),
    ("price_change", "unusual", "Recurring charge changed price",
     "A subscription or bill that suddenly costs 20% or more different from its usual amount.", True, True),
    ("new_payee_large", "unusual", "New payee with a large charge",
     "A first-ever payment to a merchant that is large, or far above what that envelope usually sees.", True, True),
    ("tiny_new_payee", "unusual", "Tiny charge from a new payee",
     "A first charge of $2 or less. Thieves test a stolen card with a small amount before a big one.", False, True),
    ("new_recurring", "unusual", "A new repeating charge",
     "A payee that has started charging you the same amount on a regular schedule.", False, True),
]
CHECK_IDS = [c[0] for c in CHECKS]
SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}

DEFAULT_LARGE_AMOUNT = 500
DEFAULT_LOOKBACK_DAYS = 120

# Ways money goes to a person: (pattern, how to find the person's name in the text)
P2P_PATTERNS = [
    (re.compile(r"\bZELLE\s+(?:PAYMENT\s+)?TO\s+(.+?)(?:\s+ON\s+\d|\s+REF\b|\s+\d{3,}|$)"), "Zelle"),
    (re.compile(r"\bONLINE\s+TRANSFER\s+TO\s+(.+?)(?:\s+(?:WELLS|REF|XXXX|WAY2SAVE|ON\s+\d)|$)"), "Transfer"),
    (re.compile(r"\bCASH\s*APP\*?\s*(.+?)(?:\s+\d{3,}|$)"), "Cash App"),
]
P2P_SERVICES = [("VENMO", "Venmo"), ("PAYPAL INST XFER", "PayPal"), ("PAYPAL", "PayPal")]

# A payee whose first payment falls this soon after your data begins is just "the start of the data", not new.
HISTORY_START_GRACE_DAYS = 45


def default_selection(kind: str) -> List[str]:
    idx = 4 if kind == "manual" else 5
    return [c[0] for c in CHECKS if c[idx]]


# ---------------------------------------------------------------------------
# Settings and reviews
# ---------------------------------------------------------------------------


def _json_list(conn: Any, key: str, fallback: List[str]) -> List[str]:
    raw = store.get_setting(conn, key)
    try:
        data = json.loads(raw) if raw else None
    except ValueError:
        data = None
    return [x for x in data if x in CHECK_IDS] if isinstance(data, list) else fallback


def get_config(conn: Any) -> Dict[str, Any]:
    return {
        "checks": [{"id": c[0], "group": c[1], "title": c[2], "help": c[3]} for c in CHECKS],
        "manual": _json_list(conn, "audit_manual_checks", default_selection("manual")),
        "import": _json_list(conn, "audit_import_checks", default_selection("import")),
        "large_amount": float(store.get_setting(conn, "audit_large_amount") or DEFAULT_LARGE_AMOUNT),
        "lookback_days": int(store.get_setting(conn, "audit_lookback_days") or DEFAULT_LOOKBACK_DAYS),
        "auto_open": (store.get_setting(conn, "audit_auto_open") or "1") == "1",
    }


def save_config(conn: Any, data: Dict[str, Any]) -> None:
    for key, setting in (("manual", "audit_manual_checks"), ("import", "audit_import_checks")):
        if key in data:
            store.set_setting(conn, setting, json.dumps([x for x in data[key] if x in CHECK_IDS]))
    if "large_amount" in data:
        amount = float(data["large_amount"])
        if amount <= 0:
            raise ValueError("The large amount must be more than zero")
        store.set_setting(conn, "audit_large_amount", str(amount))
    if "lookback_days" in data:
        days = int(data["lookback_days"])
        if days < 1:
            raise ValueError("Look back at least 1 day")
        store.set_setting(conn, "audit_lookback_days", str(days))
    if "auto_open" in data:
        store.set_setting(conn, "audit_auto_open", "1" if data["auto_open"] else "0")


def dismiss(conn: Any, items: List[Dict[str, str]]) -> int:
    now = date.today().isoformat()
    n = 0
    for it in items:
        check, key = str(it.get("check") or ""), str(it.get("key") or "")
        if check in CHECK_IDS and key:
            conn.execute("INSERT OR REPLACE INTO audit_reviews (check_id, item_key, reviewed_at) VALUES (?, ?, ?)", (check, key, now))
            n += 1
    return n


def _reviewed(conn: Any) -> Set[Any]:
    return {(r["check_id"], r["item_key"]) for r in conn.execute("SELECT check_id, item_key FROM audit_reviews")}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def payee_key(name: str) -> str:
    """A payee with store numbers and order codes removed, so 'AMAZON MKTPL*AB12' and 'AMAZON MKTPL*CD34' match."""
    text = clean_vendor_for_lookup(name or "").upper()
    text = text.split("*")[0] if len(text.split("*")[0].strip()) >= 3 else text.replace("*", " ")
    tokens = [t for t in re.split(r"\s+", text) if t and not any(ch.isdigit() for ch in t)]
    return " ".join(tokens).strip()


def recipient_of(raw: str) -> Optional[str]:
    """Who a person-to-person payment went to, as 'Zelle: LEE FEEDLUND'. None if it isn't one."""
    text = " ".join((raw or "").upper().split())
    for pattern, label in P2P_PATTERNS:
        m = pattern.search(text)
        if m:
            who = m.group(1).strip(" -*")
            if who:
                return f"{label}: {who}"
    for needle, label in P2P_SERVICES:
        if needle in text:
            return label
    return None


def _severity(amount: float, high: float = 100, medium: float = 25) -> str:
    a = abs(amount)
    return "high" if a >= high else "medium" if a >= medium else "low"


class Ctx:
    def __init__(self, conn: Any, lookback_days: int, large_amount: float, scope: Optional[Set[str]], today: date):
        self.today = today
        self.cutoff = (today - timedelta(days=lookback_days)).isoformat()
        self.large = large_amount
        self.scope = scope
        self.rows = [dict(r) for r in conn.execute(
            """SELECT txn_id, date, name, raw_name, amount, envelope, txn_kind, is_included, account_type, source_file, bill_id
               FROM transactions ORDER BY date, txn_id""")]
        self.by_id = {r["txn_id"]: r for r in self.rows}
        self.spend = [r for r in self.rows if r["is_included"] and r["txn_kind"] == "expense" and r["amount"] < 0]
        for r in self.spend:
            r["key"] = payee_key(r["name"])
            r["recipient"] = recipient_of(r["raw_name"] or r["name"])
        first = [r["date"] for r in self.rows if r["is_included"]]
        self.data_start = date.fromisoformat(min(first)) if first else today
        self.bill_text = set()
        for b in conn.execute("SELECT name, match_phrase FROM bills"):
            for t in (b["name"], b["match_phrase"]):
                if t:
                    self.bill_text.add(" ".join(t.upper().split()))
        self.env_median: Dict[str, float] = {}
        per_env: Dict[str, List[float]] = defaultdict(list)
        for r in self.spend:
            per_env[(r["envelope"] or "").strip().lower()].append(-r["amount"])
        for env, vals in per_env.items():
            if len(vals) >= 10:
                self.env_median[env] = statistics.median(vals)

    def in_scope(self, *txn_ids: str) -> bool:
        if self.scope is not None:
            return any(t in self.scope for t in txn_ids)
        return True

    def recent(self, txn_date: str) -> bool:
        """With no scope, only look at the lookback window; with a scope the scope decides."""
        return self.scope is not None or txn_date >= self.cutoff

    def not_at_history_start(self, first_date: str) -> bool:
        return (date.fromisoformat(first_date) - self.data_start).days > HISTORY_START_GRACE_DAYS

    def brief(self, txn_id: str) -> Dict[str, Any]:
        r = self.by_id[txn_id]
        return {"txn_id": txn_id, "date": r["date"], "name": r["name"], "amount": float(r["amount"]),
                "envelope": r["envelope"] or "", "source_file": r["source_file"] or ""}

    def finding(self, check: str, ids: List[str], severity: str, reason: str, key: Optional[str] = None) -> Dict[str, Any]:
        ids = sorted(ids, key=lambda t: (self.by_id[t]["date"], t))
        return {"check": check, "key": key or "|".join(sorted(ids)), "severity": severity, "reason": reason,
                "txns": [self.brief(t) for t in ids], "date": self.by_id[ids[-1]]["date"]}

    def first_payments(self, keyfn: Callable[[Dict[str, Any]], Optional[str]]) -> Dict[str, Dict[str, Any]]:
        firsts: Dict[str, Dict[str, Any]] = {}
        for r in self.spend:
            k = keyfn(r)
            if k and k not in firsts:
                firsts[k] = r
        return firsts


def _files(row: Dict[str, Any]) -> Set[str]:
    return {f.strip() for f in (row["source_file"] or "").split(",") if f.strip()}


def _twin_ids(ctx: Ctx) -> Set[str]:
    """Rows that match another row on date, bank text, and amount but came from different files."""
    groups: Dict[Any, List[Dict[str, Any]]] = defaultdict(list)
    for r in ctx.rows:
        groups[(r["date"], r["raw_name"], round(r["amount"], 2), r["account_type"])].append(r)
    twins: Set[str] = set()
    for rs in groups.values():
        if len(rs) < 2:
            continue
        for i, a in enumerate(rs):
            for b in rs[i + 1:]:
                if not (_files(a) & _files(b)):
                    twins.update((a["txn_id"], b["txn_id"]))
    return twins


# ---------------------------------------------------------------------------
# The checks
# ---------------------------------------------------------------------------


def check_dup_import_twins(ctx: Ctx) -> List[Dict[str, Any]]:
    groups: Dict[Any, List[Dict[str, Any]]] = defaultdict(list)
    for r in ctx.rows:
        groups[(r["date"], r["raw_name"], round(r["amount"], 2), r["account_type"])].append(r)
    out = []
    for rs in groups.values():
        pair = [(a, b) for i, a in enumerate(rs) for b in rs[i + 1:] if not (_files(a) & _files(b))]
        for a, b in pair:
            if not ctx.in_scope(a["txn_id"], b["txn_id"]):
                continue
            counts = a["is_included"] and b["is_included"] and a["txn_kind"] in ("expense", "paycheck", "other_income", "refund")
            files = sorted(_files(a) | _files(b))
            out.append(ctx.finding(
                "dup_import_twins", [a["txn_id"], b["txn_id"]], "high" if counts else "low",
                f"Saved twice from different files ({' and '.join(files)})" + ("" if counts else ". Not counted in your totals, so low risk.")))
    return out


def check_dup_same_day(ctx: Ctx) -> List[Dict[str, Any]]:
    twins = _twin_ids(ctx)
    groups: Dict[Any, List[Dict[str, Any]]] = defaultdict(list)
    for r in ctx.spend:
        if r["key"] and r["txn_id"] not in twins and abs(r["amount"]) >= 1:
            groups[(r["key"], round(r["amount"], 2), r["date"])].append(r)
    return [
        ctx.finding("dup_same_day", [r["txn_id"] for r in rs], _severity(rs[0]["amount"]),
                    f"{len(rs)} charges of {abs(rs[0]['amount']):,.2f} from the same payee on {rs[0]['date']}")
        for rs in groups.values() if len(rs) >= 2 and ctx.in_scope(*[r["txn_id"] for r in rs]) and ctx.recent(rs[0]["date"])
    ]


def check_dup_near_days(ctx: Ctx) -> List[Dict[str, Any]]:
    twins = _twin_ids(ctx)
    groups: Dict[Any, List[Dict[str, Any]]] = defaultdict(list)
    for r in ctx.spend:
        if r["key"] and not r["recipient"] and r["txn_id"] not in twins and abs(r["amount"]) >= 15:
            groups[(r["key"], round(r["amount"], 2))].append(r)
    out = []
    for rs in groups.values():
        for a, b in zip(rs, rs[1:]):
            gap = (date.fromisoformat(b["date"]) - date.fromisoformat(a["date"])).days
            if 1 <= gap <= 3 and ctx.in_scope(a["txn_id"], b["txn_id"]) and ctx.recent(b["date"]):
                out.append(ctx.finding("dup_near_days", [a["txn_id"], b["txn_id"]], _severity(a["amount"]),
                                       f"The same {abs(a['amount']):,.2f} charge {gap} day{'s' if gap != 1 else ''} apart"))
    return out


def check_dup_amount_date(ctx: Ctx) -> List[Dict[str, Any]]:
    groups: Dict[Any, List[Dict[str, Any]]] = defaultdict(list)
    for r in ctx.spend:
        if abs(r["amount"]) >= 1:
            groups[(r["date"], round(r["amount"], 2))].append(r)
    out = []
    for rs in groups.values():
        if len(rs) >= 2 and len({r["key"] for r in rs}) > 1 and ctx.in_scope(*[r["txn_id"] for r in rs]) and ctx.recent(rs[0]["date"]):
            out.append(ctx.finding("dup_amount_date", [r["txn_id"] for r in rs], "low",
                                   f"{len(rs)} charges of {abs(rs[0]['amount']):,.2f} on {rs[0]['date']} from different payees"))
    return out


def check_uncategorized(ctx: Ctx) -> List[Dict[str, Any]]:
    return [
        ctx.finding("uncategorized", [r["txn_id"]], "medium", "No envelope assigned")
        for r in ctx.spend if not (r["envelope"] or "").strip() and ctx.in_scope(r["txn_id"]) and ctx.recent(r["date"])
    ]


def _first_time(ctx: Ctx, keyfn: Callable[[Dict[str, Any]], Optional[str]]) -> List[Dict[str, Any]]:
    """First-ever payments (by the given key) that are new enough to matter."""
    return [
        r for r in ctx.first_payments(keyfn).values()
        if ctx.not_at_history_start(r["date"]) and ctx.in_scope(r["txn_id"]) and ctx.recent(r["date"])
    ]


def check_new_person_paid(ctx: Ctx) -> List[Dict[str, Any]]:
    out = []
    for r in _first_time(ctx, lambda x: x["recipient"]):
        out.append(ctx.finding(
            "new_person_paid", [r["txn_id"]], _severity(r["amount"], high=200, medium=50),
            f"First payment to {r['recipient']} ({abs(r['amount']):,.2f})", key=r["txn_id"]))
    return out


def check_new_payee_large(ctx: Ctx) -> List[Dict[str, Any]]:
    out = []
    for r in _first_time(ctx, lambda x: None if x["recipient"] else x["key"]):
        amt = abs(r["amount"])
        norm = " ".join((r["name"] or "").upper().split())
        if r["bill_id"] or any(t and (t in norm or norm in t) for t in ctx.bill_text):
            continue
        env_med = ctx.env_median.get((r["envelope"] or "").strip().lower())
        far_above = env_med is not None and amt >= max(100.0, 5 * env_med)
        if amt >= ctx.large or far_above:
            why = f"First payment to this payee, {amt:,.2f}"
            if far_above and amt < ctx.large:
                why += f", about {amt / env_med:.0f} times the usual in {r['envelope']}"
            out.append(ctx.finding("new_payee_large", [r["txn_id"]], "high" if amt >= 2 * ctx.large else "medium", why, key=r["txn_id"]))
    return out


def check_tiny_new_payee(ctx: Ctx) -> List[Dict[str, Any]]:
    return [
        ctx.finding("tiny_new_payee", [r["txn_id"]], "medium", f"A first-ever charge of just {abs(r['amount']):,.2f}. Could be a card test.", key=r["txn_id"])
        for r in _first_time(ctx, lambda x: None if x["recipient"] else x["key"]) if 0 < abs(r["amount"]) <= 2.0
    ]


def _steady(ctx: Ctx, rs: List[Dict[str, Any]]) -> Optional[tuple]:
    """The (target days, wobble) schedule a list of payments follows, if it follows one."""
    dates = [date.fromisoformat(r["date"]) for r in rs]
    gaps = [(b - a).days for a, b in zip(dates, dates[1:])]
    if not gaps:
        return None
    med = statistics.median(gaps)
    spec = next((s for s in KNOWN_INTERVALS if abs(med - s[0]) <= s[3]), None)
    if spec and sum(1 for g in gaps if abs(g - spec[0]) <= spec[3]) / len(gaps) >= 0.8:
        return spec
    return None


def check_price_change(ctx: Ctx) -> List[Dict[str, Any]]:
    groups: Dict[str, Dict[str, Dict[str, Any]]] = defaultdict(dict)
    for r in ctx.spend:
        if r["key"] and not r["recipient"]:
            prev = groups[r["key"]].get(r["date"])
            if prev is None or abs(r["amount"]) > abs(prev["amount"]):
                groups[r["key"]][r["date"]] = r          # same-day repeats count once
    out = []
    for by_date in groups.values():
        rs = [by_date[d] for d in sorted(by_date)]
        if len(rs) < 4 or _steady(ctx, rs[:-1]) is None:
            continue
        for i in range(3, len(rs)):
            r, history = rs[i], [-x["amount"] for x in rs[max(0, i - 6):i]]
            usual = statistics.median(history)
            if usual < 3 or (max(history) - min(history)) / usual > 0.15:
                continue
            change = (-r["amount"] - usual) / usual
            if abs(change) >= 0.20 and abs(-r["amount"] - usual) >= 1 and ctx.in_scope(r["txn_id"]) and ctx.recent(r["date"]):
                word = "up" if change > 0 else "down"
                out.append(ctx.finding(
                    "price_change", [r["txn_id"]], "high" if change >= 0.5 else "medium" if change > 0 else "low",
                    f"{abs(r['amount']):,.2f}, {word} {abs(change) * 100:.0f}% from the usual {usual:,.2f}", key=r["txn_id"]))
    return out


def check_new_recurring(ctx: Ctx) -> List[Dict[str, Any]]:
    groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for r in ctx.spend:
        if r["key"] and not r["recipient"]:
            groups[r["key"]].append(r)
    out = []
    for key, rs in groups.items():
        if len(rs) < 2 or not ctx.not_at_history_start(rs[0]["date"]):
            continue
        if any(t and (t in key or key in t) for t in ctx.bill_text):
            continue
        spec = _steady(ctx, rs)
        amounts = [-r["amount"] for r in rs]
        if spec is None or spec[0] < 28 or min(amounts) < 2 or (max(amounts) - min(amounts)) / statistics.median(amounts) > 0.05:
            continue
        last = rs[-1]
        if len(rs) <= 4 and ctx.in_scope(last["txn_id"]) and ctx.recent(last["date"]) and ctx.recent(rs[0]["date"]):
            out.append(ctx.finding("new_recurring", [r["txn_id"] for r in rs], "medium",
                                   f"{len(rs)} charges of about {statistics.median(amounts):,.2f}, roughly every {spec[0]} days, starting {rs[0]['date']}",
                                   key=last["txn_id"]))
    return out


RUNNERS: Dict[str, Callable[[Ctx], List[Dict[str, Any]]]] = {
    "dup_same_day": check_dup_same_day, "dup_near_days": check_dup_near_days, "dup_import_twins": check_dup_import_twins,
    "dup_amount_date": check_dup_amount_date, "uncategorized": check_uncategorized, "new_person_paid": check_new_person_paid,
    "price_change": check_price_change, "new_payee_large": check_new_payee_large, "tiny_new_payee": check_tiny_new_payee,
    "new_recurring": check_new_recurring,
}


def run(
    conn: Any, check_ids: Iterable[str], lookback_days: Optional[int] = None, large_amount: Optional[float] = None,
    scope_ids: Optional[Iterable[str]] = None, today: Optional[date] = None, include_reviewed: bool = False,
) -> Dict[str, Any]:
    """Run the chosen checks. Returns findings (worst first) and a count per check."""
    cfg = get_config(conn)
    ctx = Ctx(conn, lookback_days or cfg["lookback_days"], large_amount or cfg["large_amount"],
              None if scope_ids is None else set(scope_ids), today or date.today())
    done = _reviewed(conn)
    findings: List[Dict[str, Any]] = []
    ran: List[str] = []
    hidden = 0
    for cid in [c for c in CHECK_IDS if c in set(check_ids)]:
        ran.append(cid)
        for f in RUNNERS[cid](ctx):
            if not include_reviewed and (cid, f["key"]) in done:
                hidden += 1
                continue
            findings.append(f)
    findings.sort(key=lambda f: (SEVERITY_ORDER[f["severity"]], f["check"], f["date"]), reverse=False)
    counts: Dict[str, int] = defaultdict(int)
    for f in findings:
        counts[f["check"]] += 1
    return {"findings": findings, "counts": dict(counts), "ran": ran, "hidden_reviewed": hidden, "total": len(findings)}


def run_for_import(conn: Any, txn_ids: List[str]) -> Dict[str, Any]:
    """The checks you chose to run on every import, looking only at the rows just saved."""
    cfg = get_config(conn)
    if not txn_ids or not cfg["import"]:
        return {"findings": [], "counts": {}, "ran": [], "hidden_reviewed": 0, "total": 0}
    store.set_setting(conn, "audit_last_import_ids", json.dumps(txn_ids))
    conn.commit()
    return run(conn, cfg["import"], scope_ids=txn_ids)


def pending_from_last_import(conn: Any) -> Dict[str, Any]:
    """What the last import's checks found that hasn't been marked OK yet."""
    try:
        ids = json.loads(store.get_setting(conn, "audit_last_import_ids") or "[]")
    except ValueError:
        ids = []
    cfg = get_config(conn)
    if not ids or not cfg["import"]:
        return {"findings": [], "total": 0, "counts": {}, "ran": [], "hidden_reviewed": 0}
    existing = {r[0] for r in conn.execute(f"SELECT txn_id FROM transactions WHERE txn_id IN ({','.join('?' for _ in ids)})", ids)}
    return run(conn, cfg["import"], scope_ids=sorted(existing))
