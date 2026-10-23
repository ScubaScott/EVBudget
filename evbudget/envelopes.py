"""Envelope settings, bills with repeat schedules, and the go-live opening balances."""

import statistics
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from . import recurrence, store

KINDS = ("spending", "bill", "savings")
ROLLOVER_MODES = ("rollover", "reset")
AMOUNT_MODES = ("fixed", "estimated")


class ValidationError(ValueError):
    """Bad input from the user; the API turns this into a 400."""


def to_cents(value: Any) -> int:
    try:
        return int(round(float(value) * 100))
    except (TypeError, ValueError):
        raise ValidationError("An amount is not a valid number")


def to_dollars(cents: Optional[int]) -> float:
    return round((cents or 0) / 100.0, 2)


# ---------------------------------------------------------------------------
# Bills
# ---------------------------------------------------------------------------


def bill_monthly_cents(bill: Dict[str, Any]) -> int:
    """What a bill costs per month on average: amount x times per year / 12."""
    per_year = recurrence.occurrences_per_year(bill["freq"], bill["interval"])
    return int(round((bill["amount_cents"] or 0) * per_year / 12))


def next_due_dates(freq: str, interval: int, anchor: Optional[str], after: date, count: int = 3) -> List[str]:
    a = recurrence.parse(anchor or "")
    if not a:
        return []
    out: List[str] = []
    cursor = after
    for _ in range(count):
        nxt = recurrence.next_occurrence(freq, interval, a, cursor)
        if not nxt:
            break
        out.append(nxt.isoformat())
        cursor = nxt
    return out


def _bill_view(row: Any, today: date) -> Dict[str, Any]:
    d = dict(row)
    d["amount"] = to_dollars(d["amount_cents"])
    d["monthly_amount"] = to_dollars(bill_monthly_cents(d))
    d["upcoming"] = next_due_dates(d["freq"], d["interval"], d["anchor_date"], today - timedelta(days=1))
    d["overdue"] = bool(d["active"] and d["next_due_date"] and d["next_due_date"] < today.isoformat())
    return d


def list_bills(conn: Any, envelope_id: Optional[int] = None, today: Optional[date] = None) -> List[Dict[str, Any]]:
    today = today or date.today()
    sql = "SELECT * FROM bills"
    args: tuple = ()
    if envelope_id:
        sql += " WHERE envelope_id = ?"
        args = (envelope_id,)
    return [_bill_view(r, today) for r in conn.execute(sql + " ORDER BY envelope_id, name", args)]


def save_bill(conn: Any, data: Dict[str, Any], today: Optional[date] = None) -> int:
    today = today or date.today()
    name = str(data.get("name", "")).strip()
    if not name:
        raise ValidationError("A bill needs a name")
    env = conn.execute("SELECT id, kind FROM envelopes WHERE id = ?", (data.get("envelope_id"),)).fetchone()
    if env is None or env["kind"] == "system":
        raise ValidationError("Choose an envelope for this bill")
    freq = data.get("freq") or "monthly"
    if freq not in recurrence.FREQUENCIES:
        raise ValidationError("Unknown repeat schedule")
    try:
        interval = max(1, int(data.get("interval") or 1))
        tolerance = int(data["amount_tolerance_pct"]) if data.get("amount_tolerance_pct") not in (None, "") else None
    except (TypeError, ValueError):
        raise ValidationError("A number field is not a valid number")
    amount_mode = data.get("amount_mode") or "fixed"
    if amount_mode not in AMOUNT_MODES:
        raise ValidationError("Unknown amount type")
    amount_cents = to_cents(data.get("amount", 0))
    if amount_cents < 0:
        raise ValidationError("Amount can't be negative")
    anchor = str(data.get("anchor_date") or "").strip() or None
    if anchor and recurrence.parse(anchor) is None:
        raise ValidationError("The date must be a real date (YYYY-MM-DD)")
    next_due = str(data.get("next_due_date") or "").strip() or None
    if next_due and recurrence.parse(next_due) is None:
        raise ValidationError("The next due date must be a real date (YYYY-MM-DD)")
    if not next_due and anchor:
        a = recurrence.parse(anchor)
        found = recurrence.next_occurrence(freq, interval, a, today - timedelta(days=1))
        next_due = (found or a).isoformat()
    args = (
        int(env["id"]), name, (str(data.get("match_phrase") or "").strip() or None), amount_mode, amount_cents,
        freq, interval, anchor, next_due, tolerance, 0 if data.get("active") in (0, False) else 1,
    )
    bill_id = data.get("id")
    if bill_id:
        cur = conn.execute(
            """UPDATE bills SET envelope_id=?, name=?, match_phrase=?, amount_mode=?, amount_cents=?, freq=?, interval=?,
                      anchor_date=?, next_due_date=?, amount_tolerance_pct=?, active=? WHERE id=?""",
            args + (int(bill_id),),
        )
        if cur.rowcount == 0:
            raise ValidationError("Bill not found")
        return int(bill_id)
    return int(conn.execute(
        """INSERT INTO bills (envelope_id, name, match_phrase, amount_mode, amount_cents, freq, interval, anchor_date,
                              next_due_date, amount_tolerance_pct, active) VALUES (?,?,?,?,?,?,?,?,?,?,?)""", args,
    ).lastrowid)


def delete_bill(conn: Any, bill_id: int) -> None:
    conn.execute("DELETE FROM bills WHERE id = ?", (int(bill_id),))


def bill_history(conn: Any, envelope_id: int, today: Optional[date] = None) -> List[Dict[str, Any]]:
    """Payees paid from an envelope, with a guess at how often each repeats. Used to pre-fill a new bill."""
    env = conn.execute("SELECT name FROM envelopes WHERE id = ?", (envelope_id,)).fetchone()
    if env is None:
        return []
    groups: Dict[str, List[Any]] = {}
    for r in conn.execute(
        """SELECT date, name, amount FROM transactions WHERE is_included = 1 AND txn_kind = 'expense'
           AND LOWER(TRIM(envelope)) = LOWER(?) ORDER BY date""", (env["name"],)):
        groups.setdefault((r["name"] or "").strip(), []).append(r)
    out = []
    for name, rows in groups.items():
        dates = [date.fromisoformat(r["date"]) for r in rows]
        gaps = [(b - a).days for a, b in zip(dates, dates[1:]) if (b - a).days > 0]
        median_gap = statistics.median(gaps) if gaps else None
        freq, interval = "monthly", 1
        if median_gap is not None:
            if median_gap <= 10:
                freq, interval = "weekly", max(1, round(median_gap / 7))
            elif median_gap <= 45:
                freq, interval = "monthly", 1
            elif median_gap <= 130:
                freq, interval = "monthly", max(2, round(median_gap / 30.4))
            elif median_gap <= 250:
                freq, interval = "monthly", 6
            else:
                freq, interval = "yearly", 1
        elif len(rows) == 1:
            freq, interval = "yearly", 1
        out.append({
            "name": name, "payments": len(rows), "last_date": rows[-1]["date"],
            "last_amount": round(abs(float(rows[-1]["amount"])), 2),
            "typical_amount": round(statistics.median(abs(float(r["amount"])) for r in rows), 2),
            "freq": freq, "interval": interval,
        })
    out.sort(key=lambda g: (-g["payments"], g["name"]))
    return out[:12]


# ---------------------------------------------------------------------------
# Finding bills you already pay
# ---------------------------------------------------------------------------

# (days between payments, frequency, interval, allowed wobble in days)
KNOWN_INTERVALS = [
    (7, "weekly", 1, 3), (14, "weekly", 2, 3), (30, "monthly", 1, 5), (61, "monthly", 2, 6),
    (91, "monthly", 3, 7), (182, "monthly", 6, 10), (365, "yearly", 1, 12),
]


def _norm(text: str) -> str:
    return " ".join((text or "").upper().split())


def dismissed_candidates(conn: Any) -> List[str]:
    import json
    try:
        data = json.loads(store.get_setting(conn, "dismissed_bill_candidates") or "[]")
        return data if isinstance(data, list) else []
    except ValueError:
        return []


def dismiss_candidate(conn: Any, key: str) -> None:
    import json
    keys = dismissed_candidates(conn)
    if key and key not in keys:
        keys.append(key)
        store.set_setting(conn, "dismissed_bill_candidates", json.dumps(keys))


def discover_bills(conn: Any, today: Optional[date] = None) -> List[Dict[str, Any]]:
    """Payees paid at a steady interval and a steady amount that don't have a bill yet.

    A payee needs at least 3 payments on different days. Most gaps must sit near a known
    interval (weekly, every 2 weeks, monthly, every 2, 3 or 6 months, yearly) and most
    amounts within 15% of the usual one. Payees that stopped paying are left out.
    """
    from .textutil import clean_vendor_for_lookup

    today = today or date.today()
    existing = set()
    for b in conn.execute("SELECT name, match_phrase FROM bills"):
        existing.add(_norm(b["name"]))
        if b["match_phrase"]:
            existing.add(_norm(b["match_phrase"]))
    dismissed = set(dismissed_candidates(conn))
    env_ids = {r["name"].strip().lower(): (r["id"], r["name"]) for r in conn.execute("SELECT id, name FROM envelopes WHERE kind != 'system' AND active = 1")}

    groups: Dict[str, Dict[str, Any]] = {}
    for r in conn.execute(
        "SELECT date, name, amount, envelope FROM transactions WHERE is_included = 1 AND txn_kind = 'expense' AND amount < 0 ORDER BY date"
    ):
        key = _norm(clean_vendor_for_lookup(r["name"] or ""))
        if not key:
            continue
        g = groups.setdefault(key, {"by_date": {}, "envs": {}, "display": r["name"]})
        amt = abs(float(r["amount"]))
        g["by_date"][r["date"]] = max(g["by_date"].get(r["date"], 0.0), amt)  # same-day repeats count once
        env = (r["envelope"] or "").strip().lower()
        g["envs"][env] = g["envs"].get(env, 0) + 1

    out: List[Dict[str, Any]] = []
    for key, g in groups.items():
        dates = sorted(g["by_date"])
        if len(dates) < 3 or key in dismissed or key in existing or any(e and e in key for e in existing):
            continue
        parsed = [date.fromisoformat(d) for d in dates]
        gaps = [(b - a).days for a, b in zip(parsed, parsed[1:])]
        med = statistics.median(gaps)
        spec = next((s for s in KNOWN_INTERVALS if abs(med - s[0]) <= s[3]), None)
        if spec is None:
            continue
        target, freq, interval, wobble = spec
        steady = sum(1 for x in gaps if abs(x - target) <= wobble) / len(gaps)
        amounts = [g["by_date"][d] for d in dates]
        typical = statistics.median(amounts)
        close = sum(1 for a in amounts if abs(a - typical) <= 0.15 * typical) / len(amounts)
        if steady < 0.8 or close < 0.8 or (today - parsed[-1]).days > 2 * target + wobble:
            continue
        env_name = max(g["envs"], key=lambda e: g["envs"][e])
        if env_name not in env_ids:
            continue
        env_id, env_display = env_ids[env_name]
        nxt = recurrence.next_occurrence(freq, interval, parsed[-1], today - timedelta(days=1))
        out.append({
            "key": key, "name": g["display"], "envelope_id": env_id, "envelope": env_display, "payments": len(dates),
            "amount": round(typical, 2), "freq": freq, "interval": interval, "last_date": dates[-1],
            "next_due": nxt.isoformat() if nxt else None,
            "confidence": "high" if steady == 1 and close == 1 and len(dates) >= 4 else "medium",
        })
    out.sort(key=lambda c: (c["confidence"] != "high", -c["payments"], c["name"]))
    return out[:30]


# ---------------------------------------------------------------------------
# Envelopes
# ---------------------------------------------------------------------------


def list_envelopes(conn: Any, today: Optional[date] = None) -> List[Dict[str, Any]]:
    today = today or date.today()
    bills_by_env: Dict[int, List[Dict[str, Any]]] = {}
    for b in list_bills(conn, today=today):
        bills_by_env.setdefault(b["envelope_id"], []).append(b)
    out = []
    for r in conn.execute("SELECT * FROM envelopes WHERE kind != 'system' ORDER BY sort_order, id"):
        e = dict(r)
        bills = bills_by_env.get(e["id"], [])
        active_bills = [b for b in bills if b["active"]]
        derived = e["kind"] == "bill" and bool(active_bills)
        bills_monthly = sum(b["monthly_amount"] for b in active_bills)
        e.update(
            bills=bills, budget_derived=derived,
            monthly_budget=to_dollars(e["monthly_budget_cents"]),
            bills_monthly=round(bills_monthly, 2),
            effective_budget=round(bills_monthly, 2) if derived else to_dollars(e["monthly_budget_cents"]),
            goal=to_dollars(e["goal_cents"]) if e["goal_cents"] is not None else None,
        )
        out.append(e)
    return out


def save_envelope(conn: Any, data: Dict[str, Any]) -> None:
    env = conn.execute("SELECT * FROM envelopes WHERE id = ?", (data.get("id"),)).fetchone()
    if env is None or env["kind"] == "system":
        raise ValidationError("Envelope not found")
    kind = data.get("kind", env["kind"])
    mode = data.get("rollover_mode", env["rollover_mode"])
    if kind not in KINDS:
        raise ValidationError("Unknown envelope kind")
    if mode not in ROLLOVER_MODES:
        raise ValidationError("Unknown rollover setting")
    budget = to_cents(data["monthly_budget"]) if "monthly_budget" in data else env["monthly_budget_cents"]
    if budget < 0:
        raise ValidationError("A budget can't be negative")
    goal = env["goal_cents"]
    if "goal" in data:
        goal = to_cents(data["goal"]) if data["goal"] not in (None, "") else None
    goal_date = env["goal_date"]
    if "goal_date" in data:
        goal_date = str(data["goal_date"] or "").strip() or None
        if goal_date and recurrence.parse(goal_date) is None:
            raise ValidationError("The goal date must be a real date (YYYY-MM-DD)")
    active = env["active"] if "active" not in data else (0 if data["active"] in (0, False) else 1)
    conn.execute(
        "UPDATE envelopes SET kind=?, rollover_mode=?, monthly_budget_cents=?, goal_cents=?, goal_date=?, active=? WHERE id=?",
        (kind, mode, budget, goal, goal_date, active, env["id"]),
    )


# ---------------------------------------------------------------------------
# Go-live and opening balances
# ---------------------------------------------------------------------------


def system_envelope_id(conn: Any) -> int:
    return int(conn.execute("SELECT id FROM envelopes WHERE kind = 'system'").fetchone()[0])


def setup_status(conn: Any) -> Dict[str, Any]:
    go_live = store.get_setting(conn, "go_live_date") or None
    opening: Dict[str, float] = {}
    for r in conn.execute(
        "SELECT from_envelope_id AS f, to_envelope_id AS t, amount_cents AS a FROM envelope_moves "
        "WHERE move_type = 'opening' AND voided_at IS NULL"):
        key = r["t"] if r["t"] is not None else r["f"]
        sign = 1 if r["t"] is not None else -1
        opening[str(key)] = round(opening.get(str(key), 0.0) + sign * to_dollars(r["a"]), 2)
    return {
        "go_live_date": go_live, "opening": opening, "unallocated_id": system_envelope_id(conn),
        "opening_total": round(sum(opening.values()), 2),
    }


def go_live(conn: Any, date_iso: str, opening: Dict[str, Any], reset: bool = False) -> Dict[str, Any]:
    """Set the go-live date and record each envelope's opening balance."""
    if recurrence.parse(date_iso) is None:
        raise ValidationError("Choose a real go-live date")
    if store.get_setting(conn, "go_live_date") and not reset:
        raise ValidationError("Go-live is already set. Use Re-run setup to change it.")
    valid_ids = {int(r[0]) for r in conn.execute("SELECT id FROM envelopes")}
    parsed: Dict[int, int] = {}
    for key, amount in (opening or {}).items():
        env_id = int(key)
        if env_id not in valid_ids:
            raise ValidationError(f"Unknown envelope {key}")
        cents = to_cents(amount or 0)
        if cents:
            parsed[env_id] = cents
    now = datetime.now().isoformat()
    try:
        if reset:
            conn.execute("UPDATE envelope_moves SET voided_at = ? WHERE move_type IN ('opening', 'sweep') AND voided_at IS NULL", (now,))
            # Fills and transfers from before the new start date belong to the old history. Leaving them
            # would count that money on top of the fresh opening balances.
            conn.execute(
                "UPDATE envelope_moves SET voided_at = ? WHERE move_type IN ('fill', 'transfer', 'adjustment') AND date < ? AND voided_at IS NULL",
                (now, date_iso))
            conn.execute(
                """UPDATE allocations SET status = 'voided', voided_at = ?, voided_income_txn_id = income_txn_id, income_txn_id = NULL
                   WHERE status = 'confirmed' AND NOT EXISTS
                     (SELECT 1 FROM envelope_moves m WHERE m.allocation_id = allocations.id AND m.voided_at IS NULL)""", (now,))
            store.set_setting(conn, "sweeps_through", "")
        for env_id, cents in parsed.items():
            conn.execute(
                """INSERT INTO envelope_moves (date, from_envelope_id, to_envelope_id, amount_cents, move_type, note, created_at)
                   VALUES (?, ?, ?, ?, 'opening', 'Opening balance', ?)""",
                (date_iso, None if cents > 0 else env_id, env_id if cents > 0 else None, abs(cents), now),
            )
        store.set_setting(conn, "go_live_date", date_iso)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {"go_live_date": date_iso, "opening_total": round(sum(parsed.values()) / 100.0, 2), "envelopes": len(parsed)}
