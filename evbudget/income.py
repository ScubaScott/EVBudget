"""Paycheck matching and bonus detection."""

import statistics
from typing import Any, Dict, List, Optional, Tuple

TYPICAL_WINDOW = 6


def typical_amount(
    history: List[Tuple[str, float]],
    before_date: str,
    override: Optional[float] = None,
    window: int = TYPICAL_WINDOW,
) -> Optional[float]:
    """Median of the last `window` pay dates before `before_date`.

    `history` is a list of (ISO date, amount) for regular paychecks. When an
    employer splits one pay date across several deposits, only the largest
    deposit on that date counts, so a small side deposit can't pull the
    typical amount down. A manual override wins.
    """
    if override:
        return override
    biggest_per_date: Dict[str, float] = {}
    for d, a in history:
        if d < before_date and a > biggest_per_date.get(d, float("-inf")):
            biggest_per_date[d] = a
    if not biggest_per_date:
        return None
    recent = [biggest_per_date[d] for d in sorted(biggest_per_date)[-window:]]
    return float(statistics.median(recent))


def subtype_for(amount: float, typical: Optional[float], threshold_pct: float) -> str:
    """`bonus` when a paycheck is more than threshold_pct above the typical amount."""
    if typical and amount > typical * (1 + threshold_pct / 100.0):
        return "bonus"
    return "regular"


class PaycheckHistory:
    """Regular paychecks per income source, used to judge the next one."""

    def __init__(self) -> None:
        self._by_source: Dict[int, List[Tuple[str, float]]] = {}

    def add(self, source_id: int, date_iso: str, amount: float) -> None:
        self._by_source.setdefault(source_id, []).append((date_iso, amount))

    def typical_before(self, source_id: int, date_iso: str, override: Optional[float] = None) -> Optional[float]:
        return typical_amount(self._by_source.get(source_id, []), date_iso, override)

    @classmethod
    def from_db(cls, conn: Any) -> "PaycheckHistory":
        hist = cls()
        rows = conn.execute(
            """
            SELECT income_source_id, date, amount FROM transactions
            WHERE txn_kind = 'paycheck' AND income_subtype = 'regular'
              AND is_included = 1 AND income_source_id IS NOT NULL
            """
        ).fetchall()
        for r in rows:
            hist.add(int(r[0]), r[1], float(r[2]))
        return hist


def match_income_source(sources: List[Dict[str, Any]], *texts: str) -> Optional[Dict[str, Any]]:
    """First active income source whose match phrase appears in any of the texts."""
    for src in sources:
        phrase = (src.get("match_phrase") or "").strip().upper()
        if phrase and any(phrase in t for t in texts):
            return src
    return None


# ---------------------------------------------------------------------------
# Income sources, planning income, and reports
# ---------------------------------------------------------------------------

from datetime import date, timedelta  # noqa: E402

from . import recurrence  # noqa: E402


def source_stats(conn: Any) -> List[Dict[str, Any]]:
    """Income sources with the typical paycheck, monthly amount, and next pay date."""
    out = []
    for src in conn.execute(
        """SELECT s.*, p.name AS person_name FROM income_sources s
           LEFT JOIN people p ON p.id = s.person_id ORDER BY s.id"""
    ).fetchall():
        row = dict(src)
        hist = [
            (r[0], float(r[1])) for r in conn.execute(
                """SELECT date, amount FROM transactions WHERE income_source_id = ? AND txn_kind = 'paycheck'
                   AND income_subtype = 'regular' AND is_included = 1""", (row["id"],))
        ]
        override = (row["typical_amount_cents"] or 0) / 100.0 or None
        typical = typical_amount(hist, "9999-12-31", override)
        per_year = recurrence.occurrences_per_year(row["cadence_freq"], row["cadence_interval"])
        last = conn.execute(
            "SELECT date, amount FROM transactions WHERE income_source_id = ? AND txn_kind = 'paycheck' AND is_included = 1 "
            "ORDER BY date DESC LIMIT 1", (row["id"],)).fetchone()
        anchor = recurrence.parse(row["cadence_anchor_date"])
        next_pay = None
        if anchor and per_year:
            after = recurrence.parse(last["date"]) if last else date.today() - timedelta(days=1)
            nxt = recurrence.next_occurrence(row["cadence_freq"], row["cadence_interval"], anchor, after)
            next_pay = nxt.isoformat() if nxt else None
        row.update(
            typical=typical, typical_source="manual" if override else ("computed" if typical else None),
            periods_per_year=per_year, monthly_amount=round((typical or 0) * per_year / 12, 2),
            regular_count=len(hist), last_paycheck_date=last["date"] if last else None,
            last_paycheck_amount=float(last["amount"]) if last else None, next_pay_date=next_pay,
        )
        out.append(row)
    return out


def planning_income(conn: Any) -> Dict[str, Any]:
    """Monthly income to budget from: typical regular paycheck x paychecks per year / 12, per active source."""
    sources = [s for s in source_stats(conn) if s["active"]]
    by_person: Dict[str, float] = {}
    for s in sources:
        name = s["person_name"] or "Unassigned"
        by_person[name] = round(by_person.get(name, 0.0) + s["monthly_amount"], 2)
    return {"monthly_total": round(sum(by_person.values()), 2), "by_person": by_person, "sources": sources}


def extra_paycheck_months(conn: Any, start: date, end: date) -> Dict[str, List[str]]:
    """Months with more paychecks than usual (e.g. 3 on a biweekly schedule), by source name."""
    result: Dict[str, List[str]] = {}
    for s in conn.execute("SELECT * FROM income_sources WHERE active = 1"):
        anchor = recurrence.parse(s["cadence_anchor_date"])
        per_year = recurrence.occurrences_per_year(s["cadence_freq"], s["cadence_interval"])
        if not anchor or not per_year:
            continue
        usual = int(per_year / 12 + 1e-9)
        counts: Dict[str, int] = {}
        for d in recurrence.occurrences_between(s["cadence_freq"], s["cadence_interval"], anchor, start, end):
            counts[d.strftime("%Y-%m")] = counts.get(d.strftime("%Y-%m"), 0) + 1
        for month, n in counts.items():
            if n > usual >= 1:
                result.setdefault(month, []).append(s["name"])
    return result


def _month_range(start: date, end: date) -> List[str]:
    months, y, m = [], start.year, start.month
    while (y, m) <= (end.year, end.month):
        months.append(f"{y}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return months


def income_summary(conn: Any, start: str, end: str) -> Dict[str, Any]:
    """Everything the Income tab shows for [start, end] (ISO dates, inclusive)."""
    rows = conn.execute(
        """SELECT t.date, t.amount, t.txn_kind, t.income_subtype, t.income_source_id, t.person_id,
                  t.income_label, p.name AS person_name
           FROM transactions t LEFT JOIN people p ON p.id = t.person_id
           WHERE t.is_included = 1 AND t.date >= ? AND t.date <= ?
             AND t.txn_kind IN ('paycheck', 'other_income', 'expense', 'refund')
           ORDER BY t.date""", (start, end)).fetchall()

    start_d, end_d = date.fromisoformat(start), date.fromisoformat(end)
    months = _month_range(start_d, end_d)
    series_names: List[str] = []
    monthly = {m: {"month": m, "series": {}, "income": 0.0, "spend": 0.0} for m in months}
    person_totals: Dict[str, float] = {}
    regular_total = bonus_total = other_total = 0.0
    other_by_label: Dict[str, float] = {}

    for r in rows:
        month, amt = r["date"][:7], float(r["amount"])
        bucket = monthly.get(month)
        if bucket is None:
            continue
        kind = r["txn_kind"]
        if kind in ("expense", "refund"):
            bucket["spend"] = round(bucket["spend"] - amt, 2)
            continue
        who = r["person_name"] or "Unassigned"
        if kind == "paycheck":
            if r["income_subtype"] == "bonus":
                key, bonus_total = "Bonus", bonus_total + amt
            else:
                key, regular_total = f"{who} regular", regular_total + amt
            person_totals[who] = person_totals.get(who, 0.0) + amt
        else:
            key, other_total = "Other income", other_total + amt
            label = r["income_label"] or "Other"
            other_by_label[label] = other_by_label.get(label, 0.0) + amt
        if key not in series_names:
            series_names.append(key)
        bucket["series"][key] = round(bucket["series"].get(key, 0.0) + amt, 2)
        bucket["income"] = round(bucket["income"] + amt, 2)

    extra = extra_paycheck_months(conn, start_d, end_d)
    for m in months:
        monthly[m]["extra_paycheck_sources"] = extra.get(m, [])

    trend = []
    for s in conn.execute(
        "SELECT s.id, s.name, p.name AS person FROM income_sources s LEFT JOIN people p ON p.id = s.person_id ORDER BY s.id"
    ):
        pts = [
            {"date": r["date"], "amount": float(r["amount"]), "subtype": r["income_subtype"] or "regular"}
            for r in conn.execute(
                "SELECT date, amount, income_subtype FROM transactions WHERE income_source_id = ? AND txn_kind = 'paycheck' "
                "AND is_included = 1 AND date >= ? AND date <= ? ORDER BY date", (s["id"], start, end))
        ]
        trend.append({"source_id": s["id"], "name": s["name"], "person": s["person"], "points": pts})

    # Average regular income per complete month inside the range
    this_month = date.today().strftime("%Y-%m")
    complete = [m for m in months if m < this_month]
    regular_in_complete = sum(v for m in complete for k, v in monthly[m]["series"].items() if k.endswith(" regular"))
    plan = planning_income(conn)
    next_pay: Dict[str, str] = {}
    for s in plan["sources"]:
        if s["next_pay_date"]:
            who = s["person_name"] or s["name"]
            if who not in next_pay or s["next_pay_date"] < next_pay[who]:
                next_pay[who] = s["next_pay_date"]

    return {
        "start": start, "end": end, "months": months, "series_names": series_names,
        "monthly": [monthly[m] for m in months], "trend": trend,
        "cards": {
            "income_by_person": {k: round(v, 2) for k, v in person_totals.items()},
            "regular_total": round(regular_total, 2), "bonus_total": round(bonus_total, 2),
            "other_total": round(other_total, 2), "other_by_label": {k: round(v, 2) for k, v in other_by_label.items()},
            "total_income": round(regular_total + bonus_total + other_total, 2),
            "observed_regular_monthly": round(regular_in_complete / len(complete), 2) if complete else None,
            "planning_monthly": plan["monthly_total"], "planning_by_person": plan["by_person"],
            "next_pay_dates": next_pay,
        },
    }
