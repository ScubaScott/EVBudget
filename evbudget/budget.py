"""Budget suggestions, the budget-vs-income summary, and fill templates."""

import math
import statistics
from datetime import date
from typing import Any, Dict, List, Optional

from . import envelopes as env_mod
from . import income, store
from .envelopes import ValidationError, to_cents, to_dollars


def _round_up_5(x: float) -> float:
    return float(math.ceil(x / 5.0 - 1e-9) * 5) if x > 0 else 0.0


def _prev_months(today: date, count: int) -> List[str]:
    """The `count` complete calendar months before today's month, oldest first."""
    months, y, m = [], today.year, today.month
    for _ in range(count):
        m -= 1
        if m == 0:
            y, m = y - 1, 12
        months.append(f"{y}-{m:02d}")
    return list(reversed(months))


def _first_full_month(conn: Any) -> Optional[str]:
    """First month with usable data. A month whose data starts after the 7th is treated as partial."""
    row = conn.execute("SELECT MIN(date) FROM transactions WHERE is_included = 1").fetchone()
    if not row or not row[0]:
        return None
    first = row[0]
    month = first[:7]
    if int(first[8:10]) > 7:
        y, m = int(month[:4]), int(month[5:])
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
        month = f"{y}-{m:02d}"
    return month


def _monthly_spend(conn: Any, months: List[str]) -> Dict[str, Dict[str, float]]:
    """Net spend (expenses less refunds) per envelope name (lower-case) per month."""
    out: Dict[str, Dict[str, float]] = {}
    if not months:
        return out
    for r in conn.execute(
        """SELECT LOWER(TRIM(COALESCE(envelope, ''))) AS env, substr(date, 1, 7) AS month, -SUM(amount) AS spend
           FROM transactions WHERE is_included = 1 AND txn_kind IN ('expense', 'refund')
             AND substr(date, 1, 7) >= ? AND substr(date, 1, 7) <= ? GROUP BY env, month""",
        (months[0], months[-1])):
        out.setdefault(r["env"], {})[r["month"]] = float(r["spend"])
    return out


def suggestions(conn: Any, today: Optional[date] = None) -> Dict[str, Any]:
    """Ball-park monthly budgets from past spending, plus how they compare with income."""
    today = today or date.today()
    spend_n = int(store.get_setting(conn, "suggest_window_spending_months"))
    bill_n = int(store.get_setting(conn, "suggest_window_bill_months"))
    first = _first_full_month(conn)
    spend_months_all = _prev_months(today, spend_n)
    bill_months_all = _prev_months(today, bill_n)
    spend_months = [m for m in spend_months_all if first and m >= first]
    bill_months = [m for m in bill_months_all if first and m >= first]
    spend_data = _monthly_spend(conn, bill_months_all)

    result: Dict[str, Any] = {}
    plan_total = 0.0
    for e in env_mod.list_envelopes(conn, today):
        if not e["active"]:
            continue
        by_month = spend_data.get(e["name"].lower(), {})
        if e["kind"] == "savings":
            result[str(e["id"])] = {"basis": "none", "suggested": None, "confidence": None}
            plan_total += e["effective_budget"]
            continue

        if e["budget_derived"]:
            vals12 = [by_month.get(m, 0.0) for m in bill_months]
            actual = (sum(vals12) / 12.0) if vals12 else 0.0
            result[str(e["id"])] = {
                "basis": "bills", "suggested": e["bills_monthly"], "actual_12mo_avg": round(actual, 2),
                "confidence": "high", "months": len(vals12),
            }
            plan_total += e["effective_budget"]
            continue

        if e["kind"] == "bill":
            vals = [by_month.get(m, 0.0) for m in bill_months]
            total12 = sum(vals)
            avg = total12 / 12.0
            result[str(e["id"])] = {
                "basis": "annualized", "avg": round(avg, 2), "total": round(total12, 2),
                "suggested": _round_up_5(avg), "months": len(vals), "partial_history": len(vals) < 12,
                "confidence": "high" if len(vals) >= 12 else "medium" if len(vals) >= 6 else "low",
            }
        else:
            vals = [by_month.get(m, 0.0) for m in spend_months]
            if vals:
                avg, med = sum(vals) / len(vals), statistics.median(vals)
            else:
                avg = med = 0.0
            n = len(vals)
            result[str(e["id"])] = {
                "basis": "average", "avg": round(avg, 2), "median": round(med, 2),
                "min": round(min(vals), 2) if vals else 0.0, "max": round(max(vals), 2) if vals else 0.0,
                "suggested": _round_up_5(avg), "months": n,
                "confidence": "high" if n >= 6 else "medium" if n >= 3 else "low",
            }
        plan_total += e["effective_budget"]

    # Actual average monthly spend across every envelope (and unassigned spending)
    actual_total = 0.0
    if spend_months:
        for env_months in spend_data.values():
            actual_total += sum(env_months.get(m, 0.0) for m in spend_months)
        actual_avg: Optional[float] = round(actual_total / len(spend_months), 2)
    else:
        actual_avg = None
    plan = income.planning_income(conn)
    surplus = round(plan["monthly_total"] - plan_total, 2)
    return {
        "suggestions": result,
        "summary": {
            "planning_income": plan["monthly_total"], "planning_by_person": plan["by_person"],
            "total_budgeted": round(plan_total, 2), "surplus": surplus, "actual_avg_spend": actual_avg,
            "go_live_date": store.get_setting(conn, "go_live_date") or None,
        },
    }


def accept_suggestions(conn: Any, items: List[Dict[str, Any]]) -> int:
    """Set monthly budgets from [{envelope_id, amount}]. Envelopes whose budget comes from bills are skipped."""
    changed = 0
    for it in items:
        row = conn.execute("SELECT id, kind FROM envelopes WHERE id = ?", (it.get("envelope_id"),)).fetchone()
        if row is None or row["kind"] == "system":
            continue
        has_bills = conn.execute("SELECT 1 FROM bills WHERE envelope_id = ? AND active = 1", (row["id"],)).fetchone()
        if row["kind"] == "bill" and has_bills:
            continue
        cents = to_cents(it.get("amount", 0))
        if cents < 0:
            raise ValidationError("A budget can't be negative")
        conn.execute("UPDATE envelopes SET monthly_budget_cents = ? WHERE id = ?", (cents, row["id"]))
        changed += 1
    return changed


# ---------------------------------------------------------------------------
# Fill templates
# ---------------------------------------------------------------------------


def get_templates(conn: Any) -> List[Dict[str, Any]]:
    """One entry per active income source with its saved fill lines and typical paycheck."""
    out = []
    for s in income.source_stats(conn):
        if not s["active"]:
            continue
        t = conn.execute("SELECT * FROM fill_templates WHERE income_source_id = ?", (s["id"],)).fetchone()
        lines = []
        if t:
            lines = [
                {"envelope_id": r["envelope_id"], "amount": to_dollars(r["amount_cents"])}
                for r in conn.execute(
                    "SELECT envelope_id, amount_cents FROM fill_template_lines WHERE template_id = ? ORDER BY sort_order",
                    (t["id"],))
            ]
        out.append({
            "income_source_id": s["id"], "source_name": s["name"], "person_name": s["person_name"],
            "typical": s["typical"], "periods_per_year": s["periods_per_year"], "lines": lines,
            "saved": bool(t),
        })
    return out


def save_template(conn: Any, source_id: int, lines: List[Dict[str, Any]]) -> None:
    if conn.execute("SELECT 1 FROM income_sources WHERE id = ?", (source_id,)).fetchone() is None:
        raise ValidationError("Pay source not found")
    seen = set()
    clean = []
    for order, ln in enumerate(lines):
        env_id = ln.get("envelope_id")
        row = conn.execute("SELECT id, kind FROM envelopes WHERE id = ?", (env_id,)).fetchone()
        if row is None or row["kind"] == "system":
            raise ValidationError("A line points at an envelope that doesn't exist")
        if row["id"] in seen:
            raise ValidationError("An envelope appears twice in the template")
        seen.add(row["id"])
        cents = to_cents(ln.get("amount", 0))
        if cents < 0:
            raise ValidationError("Fill amounts can't be negative")
        clean.append((row["id"], cents, order))
    now = date.today().isoformat()
    t = conn.execute("SELECT id FROM fill_templates WHERE income_source_id = ?", (source_id,)).fetchone()
    if t:
        template_id = t["id"]
        conn.execute("UPDATE fill_templates SET updated_at = ? WHERE id = ?", (now, template_id))
        conn.execute("DELETE FROM fill_template_lines WHERE template_id = ?", (template_id,))
    else:
        template_id = conn.execute(
            "INSERT INTO fill_templates (income_source_id, name, updated_at) VALUES (?, 'Fill template', ?)",
            (source_id, now)).lastrowid
    for env_id, cents, order in clean:
        conn.execute(
            "INSERT INTO fill_template_lines (template_id, envelope_id, amount_cents, sort_order) VALUES (?, ?, ?, ?)",
            (template_id, env_id, cents, order))


def generate_template(conn: Any, source_id: int, today: Optional[date] = None) -> Dict[str, Any]:
    """Suggested lines for one pay source: each envelope's monthly budget x this source's share of income,
    spread over its paychecks in a month, rounded to whole dollars. Nothing is saved."""
    plan = income.planning_income(conn)
    mine = next((s for s in plan["sources"] if s["id"] == source_id), None)
    if mine is None or not mine["periods_per_year"]:
        raise ValidationError("This pay source has no schedule or typical paycheck yet")
    total = sum(s["monthly_amount"] for s in plan["sources"]) or 0.0
    share = (mine["monthly_amount"] / total) if total else 0.0
    per_month = mine["periods_per_year"] / 12.0
    lines = []
    for e in env_mod.list_envelopes(conn, today):
        if not e["active"] or e["effective_budget"] <= 0:
            continue
        amount = round(e["effective_budget"] * share / per_month)
        if amount > 0:
            lines.append({"envelope_id": e["id"], "amount": float(amount)})
    return {
        "lines": lines, "total": float(sum(l["amount"] for l in lines)), "typical": mine["typical"],
        "share": round(share, 4),
    }
