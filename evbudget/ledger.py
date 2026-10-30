"""Envelope balances, markers, paycheck fills, and transfers.

Balances are never stored. They are worked out from three things:
  - money moves (opening balances, fills, transfers; older databases may also hold retired month-end sweeps)
  - spending and refunds tagged to the envelope
  - income, which lands in Unallocated until a fill moves it out
All of it counts only from the go-live date on.
"""

import calendar
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from . import envelopes as env_mod
from . import recurrence, store
from .envelopes import ValidationError, to_cents, to_dollars

INCOME_KINDS = ("paycheck", "other_income")
SPEND_KINDS = ("expense", "refund")


class Conflict(Exception):
    """The request clashes with existing data (the API turns this into a 409)."""

    def __init__(self, message: str, **extra: Any):
        super().__init__(message)
        self.extra = extra


def go_live_date(conn: Any) -> Optional[date]:
    return recurrence.parse(store.get_setting(conn, "go_live_date") or "")


def _first_of_month(d: date) -> date:
    return d.replace(day=1)


# ---------------------------------------------------------------------------
# Balances
# ---------------------------------------------------------------------------


def _envelope_index(conn: Any) -> Dict[str, Any]:
    rows = conn.execute("SELECT id, name, kind FROM envelopes").fetchall()
    unalloc = next(int(r["id"]) for r in rows if r["kind"] == "system")
    return {"by_name": {r["name"].strip().lower(): int(r["id"]) for r in rows if r["kind"] != "system"}, "unalloc": unalloc}


def balances(
    conn: Any, go_live: date, move_cutoff: date, txn_cutoff: date, carry_month_start: Optional[date] = None
) -> Dict[int, int]:
    """Balance in cents for every envelope (Unallocated included).

    With `carry_month_start`, only what an envelope held at the start of that month counts:
    earlier moves, sweeps made on the 1st, and opening balances. Fills and transfers made
    during the month itself are left out, so they can't inflate the month's target.
    """
    idx = _envelope_index(conn)
    bal: Dict[int, int] = defaultdict(int)
    for m in conn.execute(
        "SELECT date, from_envelope_id AS f, to_envelope_id AS t, amount_cents AS a, move_type FROM envelope_moves WHERE voided_at IS NULL"
    ):
        d = recurrence.parse(m["date"])
        if d is None or d > move_cutoff:
            continue
        if carry_month_start is not None:
            counts = (
                m["move_type"] == "opening" or d < carry_month_start
                or (d == carry_month_start and m["move_type"] == "sweep")
            )
            if not counts:
                continue
        if m["t"] is not None:
            bal[int(m["t"])] += m["a"]
        if m["f"] is not None:
            bal[int(m["f"])] -= m["a"]
    for t in conn.execute(
        """SELECT LOWER(TRIM(COALESCE(envelope, ''))) AS env, txn_kind AS kind,
                  SUM(CAST(ROUND(amount * 100) AS INTEGER)) AS cents
           FROM transactions WHERE is_included = 1 AND date >= ? AND date <= ? AND txn_kind IN
                ('expense', 'refund', 'paycheck', 'other_income') GROUP BY env, kind""",
        (go_live.isoformat(), txn_cutoff.isoformat())):
        if t["kind"] in INCOME_KINDS:
            bal[idx["unalloc"]] += t["cents"]
        else:
            bal[idx["by_name"].get(t["env"], idx["unalloc"])] += t["cents"]
    return dict(bal)


# ---------------------------------------------------------------------------
# Targets: capacity, "should have" marker, status
# ---------------------------------------------------------------------------


def _bill_marker_cents(bill: Dict[str, Any], as_of: date, go_live: date) -> float:
    due = recurrence.parse(bill["next_due_date"] or "")
    if due is None:
        return 0.0
    if due <= as_of:
        return float(bill["amount_cents"])
    anchor = recurrence.parse(bill["anchor_date"] or "")
    start = None
    if anchor and bill["freq"] != "once":
        start = recurrence.previous_occurrence(bill["freq"], bill["interval"], anchor, due - timedelta(days=1))
    start = start or go_live
    span = (due - start).days
    if span <= 0:
        return float(bill["amount_cents"])
    frac = min(1.0, max(0.0, (as_of - start).days / span))
    return bill["amount_cents"] * frac


def envelope_states(conn: Any, as_of: date) -> Dict[str, Any]:
    """Everything the Envelopes tab shows: balance, capacity, marker and status for each envelope."""
    G = go_live_date(conn)
    if G is None:
        return {"enabled": False, "go_live_date": None, "as_of": as_of.isoformat(), "envelopes": []}
    bal = balances(conn, G, as_of, as_of)
    month_start = _first_of_month(as_of)
    carry = balances(conn, G, as_of, month_start - timedelta(days=1), carry_month_start=month_start)
    dim = calendar.monthrange(as_of.year, as_of.month)[1]
    f_month = (as_of.day - 1) / dim

    opening: Dict[int, int] = defaultdict(int)
    for r in conn.execute("SELECT to_envelope_id AS t, from_envelope_id AS f, amount_cents AS a FROM envelope_moves WHERE move_type = 'opening' AND voided_at IS NULL"):
        if r["t"] is not None:
            opening[int(r["t"])] += r["a"]
        if r["f"] is not None:
            opening[int(r["f"])] -= r["a"]

    out: List[Dict[str, Any]] = []
    for e in env_mod.list_envelopes(conn, as_of):
        eid = e["id"]
        balance = bal.get(eid, 0)
        carry_in = carry.get(eid, 0)
        budget = to_cents(e["effective_budget"])
        active_bills = [b for b in e["bills"] if b["active"]]
        marker: Optional[float]
        if e["kind"] == "bill" and active_bills:
            capacity = float(sum(b["amount_cents"] for b in active_bills))
            marker = sum(_bill_marker_cents(b, as_of, G) for b in active_bills)
            mode = "accrue"
        elif e["kind"] == "bill":
            capacity = carry_in + budget
            marker = carry_in + budget * ((as_of.day) / dim)
            mode = "accrue"
        elif e["kind"] == "savings":
            goal = e["goal_cents"]
            capacity = float(goal) if goal else float(balance + budget)
            goal_date = recurrence.parse(e["goal_date"] or "")
            if goal and goal_date and goal_date > G:
                base = opening.get(eid, 0)
                frac = min(1.0, max(0.0, (as_of - G).days / (goal_date - G).days))
                marker = base + (goal - base) * frac
            else:
                marker = None
            mode = "accrue"
        else:
            capacity = float(carry_in + budget)
            marker = carry_in + budget * (1 - f_month)
            mode = "pace_down"

        if balance < 0:
            status = "overdrawn"
        elif balance > 0 and balance >= capacity:  # includes a target of zero or less (a carried-in deficit)
            status = "funded"
        elif marker is not None and balance < marker * 0.9:
            status = "behind"
        else:
            status = "on_track"
        due_dates = sorted(b["next_due_date"] for b in active_bills if b["next_due_date"])
        out.append({
            "id": eid, "name": e["name"], "kind": e["kind"], "rollover_mode": e["rollover_mode"], "active": e["active"],
            "balance": to_dollars(balance), "capacity": to_dollars(int(capacity)),
            "marker": None if marker is None else to_dollars(int(round(marker))),
            "gap": None if marker is None else to_dollars(int(round(marker - balance))),
            "carry_in": to_dollars(carry_in), "budget": e["effective_budget"], "marker_mode": mode, "status": status,
            "next_due": due_dates[0] if due_dates else None,
            "overdue": any(b["overdue"] for b in active_bills),
        })
    unalloc_id = _envelope_index(conn)["unalloc"]
    unalloc = bal.get(unalloc_id, 0)
    uncategorized = uncategorized_summary(conn, G, as_of)
    in_envelopes = sum(bal.get(e["id"], 0) for e in out)
    return {
        "enabled": True, "go_live_date": G.isoformat(), "as_of": as_of.isoformat(), "envelopes": out,
        "unallocated_id": unalloc_id, "unallocated": to_dollars(unalloc), "total_in_envelopes": to_dollars(in_envelopes),
        "over_allocated": unalloc < 0, "uncategorized": uncategorized, "overdrawn_count": sum(1 for e in out if e["status"] == "overdrawn"),
    }


def uncategorized_summary(conn: Any, go_live: date, as_of: date) -> Dict[str, Any]:
    """Spending and refunds since go-live with no (or an unknown) envelope. They hit Unallocated."""
    idx = _envelope_index(conn)
    spent = refunded = 0.0
    n = 0
    for r in conn.execute(
        """SELECT amount, txn_kind, LOWER(TRIM(COALESCE(envelope, ''))) AS env FROM transactions
           WHERE is_included = 1 AND date >= ? AND date <= ? AND txn_kind IN ('expense', 'refund')""",
        (go_live.isoformat(), as_of.isoformat())):
        if r["env"] in idx["by_name"]:
            continue
        n += 1
        if r["txn_kind"] == "expense":
            spent += -float(r["amount"])
        else:
            refunded += float(r["amount"])
    return {"count": n, "spent": round(spent, 2), "refunded": round(refunded, 2), "net": round(refunded - spent, 2)}


def ledger_csv(conn: Any, envelope_id: int, start: Optional[str], end: Optional[str]) -> Dict[str, Any]:
    """One envelope's history as CSV text, plus a file name."""
    import csv
    import io
    name = conn.execute("SELECT name FROM envelopes WHERE id = ?", (envelope_id,)).fetchone()
    if name is None:
        raise ValidationError("Envelope not found")
    data = ledger(conn, envelope_id, start, end)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Date", "Type", "Description", "Note", "Amount", "Balance"])
    kinds = {"move": "Money move", "txn": "Transaction"}
    for e in data["entries"]:
        w.writerow([e["date"], kinds.get(e["kind"], e["kind"]), e["label"], e["note"], f"{e['amount']:.2f}", f"{e['balance']:.2f}"])
    safe = "".join(ch if ch.isalnum() else "_" for ch in name["name"]).strip("_") or "envelope"
    return {"text": buf.getvalue(), "filename": f"{safe}_history.csv"}


def check_invariant(conn: Any, as_of: date) -> Dict[str, Any]:
    """Every dollar must be somewhere: the balances must add up to opening money + income - net spending."""
    G = go_live_date(conn)
    bal = balances(conn, G, as_of, as_of)
    opening = 0
    for r in conn.execute("SELECT from_envelope_id AS f, to_envelope_id AS t, amount_cents AS a FROM envelope_moves WHERE move_type = 'opening' AND voided_at IS NULL"):
        opening += r["a"] if r["t"] is not None else -r["a"]
    flow = conn.execute(
        """SELECT COALESCE(SUM(CAST(ROUND(amount * 100) AS INTEGER)), 0) FROM transactions
           WHERE is_included = 1 AND date >= ? AND date <= ? AND txn_kind IN ('expense', 'refund', 'paycheck', 'other_income')""",
        (G.isoformat(), as_of.isoformat())).fetchone()[0]
    return {"sum_of_balances": sum(bal.values()), "expected": opening + flow, "ok": sum(bal.values()) == opening + flow}


# ---------------------------------------------------------------------------
# Fill queue, drafts, suggestions
# ---------------------------------------------------------------------------


def fill_queue(conn: Any) -> List[Dict[str, Any]]:
    G = go_live_date(conn)
    if G is None:
        return []
    rows = conn.execute(
        """SELECT t.txn_id, t.date, t.name, t.amount, t.txn_kind, t.income_subtype, t.income_source_id, t.person_id,
                  t.income_label, p.name AS person_name, s.name AS source_name,
                  (SELECT COALESCE(SUM(a.total_cents), 0) FROM allocations a
                   WHERE a.income_txn_id = t.txn_id AND a.status = 'confirmed') AS allocated_cents
           FROM transactions t
           LEFT JOIN people p ON p.id = t.person_id LEFT JOIN income_sources s ON s.id = t.income_source_id
           WHERE t.is_included = 1 AND t.txn_kind IN ('paycheck', 'other_income') AND t.date >= ?
           ORDER BY t.date DESC, t.txn_id""", (G.isoformat(),)).fetchall()
    out = []
    for r in rows:
        alloc = int(r["allocated_cents"])
        if alloc >= to_cents(r["amount"]):
            continue
        d = {k: r[k] for k in r.keys() if k != "allocated_cents"}
        d["amount"] = float(r["amount"])
        d["allocated"] = to_dollars(alloc)
        d["remaining"] = round(d["amount"] - d["allocated"], 2)
        d["partial"] = alloc > 0
        out.append(d)
    return out


def get_allocation(conn: Any, txn_id: str) -> Optional[Dict[str, Any]]:
    a = conn.execute("SELECT * FROM allocations WHERE income_txn_id = ? AND status = 'confirmed'", (txn_id,)).fetchone()
    if a is None:
        return None
    lines = [
        {"envelope_id": r["to_envelope_id"], "amount": to_dollars(r["amount_cents"])}
        for r in conn.execute("SELECT to_envelope_id, amount_cents FROM envelope_moves WHERE allocation_id = ? AND voided_at IS NULL ORDER BY id", (a["id"],))
    ]
    return {"id": a["id"], "total": to_dollars(a["total_cents"]), "lines": lines}


def draft_allocation(conn: Any, txn_id: str, as_of: date) -> Dict[str, Any]:
    """The allocation screen for one deposit, pre-filled from the pay source's template when it is a regular paycheck."""
    t = conn.execute(
        "SELECT txn_id, date, name, amount, txn_kind, income_subtype, income_source_id FROM transactions WHERE txn_id = ? AND is_included = 1",
        (txn_id,)).fetchone()
    if t is None or t["txn_kind"] not in INCOME_KINDS:
        raise ValidationError("That isn't an income deposit")
    states = envelope_states(conn, as_of)
    if not states["enabled"]:
        raise ValidationError("Set up go-live on the Budget tab first")
    lines: List[Dict[str, Any]] = []
    from_template = False
    if t["txn_kind"] == "paycheck" and t["income_subtype"] != "bonus" and t["income_source_id"]:
        tmpl = conn.execute("SELECT id FROM fill_templates WHERE income_source_id = ?", (t["income_source_id"],)).fetchone()
        if tmpl:
            from_template = True
            lines = [
                {"envelope_id": r["envelope_id"], "amount": to_dollars(r["amount_cents"])}
                for r in conn.execute(
                    "SELECT envelope_id, amount_cents FROM fill_template_lines WHERE template_id = ? ORDER BY sort_order", (tmpl["id"],))
            ]
    return {
        "txn": {"txn_id": t["txn_id"], "date": t["date"], "name": t["name"], "amount": float(t["amount"]),
                "kind": t["txn_kind"], "subtype": t["income_subtype"]},
        "lines": lines, "from_template": from_template, "existing": get_allocation(conn, txn_id),
        "envelopes": states["envelopes"], "unallocated": states["unallocated"],
    }


def suggest_allocation(conn: Any, amount: float, as_of: date) -> List[Dict[str, Any]]:
    """Spread money where it's needed most: overdrawn first, then bills, then spending, then savings."""
    states = envelope_states(conn, as_of)
    if not states["enabled"]:
        raise ValidationError("Set up go-live on the Budget tab first")
    remaining = to_cents(amount)
    envs = [e for e in states["envelopes"] if e["active"]]
    lines: List[Dict[str, Any]] = []

    def give(env: Dict[str, Any], cents: int) -> None:
        nonlocal remaining
        cents = min(cents, remaining)
        if cents <= 0:
            return
        for ln in lines:
            if ln["envelope_id"] == env["id"]:
                ln["cents"] += cents
                break
        else:
            lines.append({"envelope_id": env["id"], "cents": cents})
        remaining -= cents

    for e in sorted((e for e in envs if e["balance"] < 0), key=lambda e: e["balance"]):
        give(e, to_cents(-e["balance"]))

    def room(e: Dict[str, Any]) -> int:
        have = next((ln["cents"] for ln in lines if ln["envelope_id"] == e["id"]), 0)
        return max(0, to_cents(e["marker"]) - to_cents(e["balance"]) - have) if e["marker"] is not None else 0

    for e in sorted((e for e in envs if e["kind"] == "bill"), key=lambda e: e["next_due"] or "9999"):
        give(e, room(e))
    for e in sorted((e for e in envs if e["kind"] == "spending"), key=lambda e: -room(e)):
        give(e, room(e))
    for e in (e for e in envs if e["kind"] == "savings"):
        give(e, room(e))
    return [{"envelope_id": ln["envelope_id"], "amount": to_dollars(ln["cents"])} for ln in lines]


# ---------------------------------------------------------------------------
# Allocations, transfers, voiding
# ---------------------------------------------------------------------------


def void_allocation(conn: Any, allocation_id: int) -> None:
    a = conn.execute("SELECT * FROM allocations WHERE id = ?", (allocation_id,)).fetchone()
    if a is None or a["status"] != "confirmed":
        raise ValidationError("That allocation isn't active")
    now = datetime.now().isoformat()
    conn.execute("UPDATE envelope_moves SET voided_at = ? WHERE allocation_id = ? AND voided_at IS NULL", (now, allocation_id))
    # Free the deposit to be allocated again, keeping a record of where it came from.
    conn.execute(
        "UPDATE allocations SET status = 'voided', voided_at = ?, voided_income_txn_id = income_txn_id, income_txn_id = NULL WHERE id = ?",
        (now, allocation_id))


def confirm_allocation(
    conn: Any, income_txn_id: Optional[str], lines: List[Dict[str, Any]], as_of: date,
    move_date: Optional[str] = None, replace_allocation_id: Optional[int] = None, template_id: Optional[int] = None,
) -> int:
    """Move money out of Unallocated into envelopes. One database transaction: all lines or none."""
    G = go_live_date(conn)
    if G is None:
        raise ValidationError("Set up go-live on the Budget tab first")
    idx = _envelope_index(conn)
    if income_txn_id:
        t = conn.execute("SELECT date, txn_kind FROM transactions WHERE txn_id = ? AND is_included = 1", (income_txn_id,)).fetchone()
        if t is None or t["txn_kind"] not in INCOME_KINDS:
            raise ValidationError("That isn't an income deposit")
        when = recurrence.parse(t["date"])
        if conn.execute("SELECT 1 FROM allocations WHERE income_txn_id = ? AND status = 'confirmed'", (income_txn_id,)).fetchone() and not replace_allocation_id:
            raise Conflict("This deposit already has an allocation. Replace or void it first.")
    else:
        when = recurrence.parse(move_date or as_of.isoformat())
    if when is None or when < G:
        raise ValidationError("The date must be on or after go-live")

    clean: Dict[int, int] = {}
    for ln in lines:
        env = conn.execute("SELECT id, kind FROM envelopes WHERE id = ?", (ln.get("envelope_id"),)).fetchone()
        if env is None or env["kind"] == "system":
            raise ValidationError("A line points at an envelope that doesn't exist")
        cents = to_cents(ln.get("amount", 0))
        if cents < 0:
            raise ValidationError("Fill amounts can't be negative")
        if cents:
            clean[int(env["id"])] = clean.get(int(env["id"]), 0) + cents
    if not clean:
        raise ValidationError("Add at least one amount to fill")

    try:
        if replace_allocation_id:
            void_allocation(conn, int(replace_allocation_id))
        total = sum(clean.values())
        unalloc = balances(conn, G, as_of if as_of > when else when, as_of if as_of > when else when).get(idx["unalloc"], 0)
        if total > unalloc:
            raise ValidationError(f"That is {to_dollars(total - unalloc):,.2f} more than the {to_dollars(unalloc):,.2f} in Unallocated.")
        now = datetime.now().isoformat()
        alloc_id = conn.execute(
            "INSERT INTO allocations (income_txn_id, template_id, total_cents, status, created_at) VALUES (?, ?, ?, 'confirmed', ?)",
            (income_txn_id, template_id, total, now)).lastrowid
        for env_id, cents in clean.items():
            conn.execute(
                """INSERT INTO envelope_moves (date, from_envelope_id, to_envelope_id, amount_cents, move_type, allocation_id, created_at)
                   VALUES (?, ?, ?, ?, 'fill', ?, ?)""", (when.isoformat(), idx["unalloc"], env_id, cents, alloc_id, now))
        conn.commit()
        return int(alloc_id)
    except Exception:
        conn.rollback()
        raise


def transfer(conn: Any, from_id: int, to_id: int, amount: Any, move_date: Optional[str], note: str, as_of: date) -> int:
    G = go_live_date(conn)
    if G is None:
        raise ValidationError("Set up go-live on the Budget tab first")
    ids = {int(r[0]) for r in conn.execute("SELECT id FROM envelopes")}
    if from_id not in ids or to_id not in ids:
        raise ValidationError("Choose two envelopes")
    if from_id == to_id:
        raise ValidationError("Pick two different envelopes")
    cents = to_cents(amount)
    if cents <= 0:
        raise ValidationError("The amount must be more than zero")
    when = recurrence.parse(move_date or as_of.isoformat())
    if when is None or when < G:
        raise ValidationError("The date must be on or after go-live")
    mid = conn.execute(
        """INSERT INTO envelope_moves (date, from_envelope_id, to_envelope_id, amount_cents, move_type, note, created_at)
           VALUES (?, ?, ?, ?, 'transfer', ?, ?)""", (when.isoformat(), from_id, to_id, cents, (note or "").strip() or None, datetime.now().isoformat())
    ).lastrowid
    conn.commit()
    return int(mid)


def void_move(conn: Any, move_id: int) -> None:
    m = conn.execute("SELECT move_type, voided_at, date FROM envelope_moves WHERE id = ?", (move_id,)).fetchone()
    if m is None or m["voided_at"]:
        raise ValidationError("That move isn't active")
    if m["move_type"] not in ("transfer", "adjustment"):
        raise ValidationError("Only transfers can be undone here. Void a fill from its allocation.")
    conn.execute("UPDATE envelope_moves SET voided_at = ? WHERE id = ?", (datetime.now().isoformat(), move_id))
    conn.commit()


def ledger(conn: Any, envelope_id: int, start: Optional[str], end: Optional[str]) -> Dict[str, Any]:
    """Every move and transaction touching one envelope, with a running balance."""
    G = go_live_date(conn)
    env = conn.execute("SELECT * FROM envelopes WHERE id = ?", (envelope_id,)).fetchone()
    if env is None:
        raise ValidationError("Envelope not found")
    if G is None:
        return {"entries": [], "start_balance": 0.0, "end_balance": 0.0}
    idx = _envelope_index(conn)
    names = {int(r["id"]): r["name"] for r in conn.execute("SELECT id, name FROM envelopes")}
    entries: List[Dict[str, Any]] = []
    for m in conn.execute(
        """SELECT m.*, a.income_txn_id FROM envelope_moves m LEFT JOIN allocations a ON a.id = m.allocation_id
           WHERE m.voided_at IS NULL AND (m.from_envelope_id = ? OR m.to_envelope_id = ?)""", (envelope_id, envelope_id)):
        incoming = m["to_envelope_id"] == envelope_id
        other = m["from_envelope_id"] if incoming else m["to_envelope_id"]
        label = {"opening": "Opening balance", "fill": "Paycheck fill", "sweep": "Month-end leftover (retired)", "transfer": "Transfer", "adjustment": "Adjustment"}[m["move_type"]]
        if other is not None and m["move_type"] in ("transfer", "sweep", "fill"):
            label += (" from " if incoming else " to ") + names.get(int(other), "?")
        entries.append({"date": m["date"], "kind": "move", "id": m["id"], "label": label, "note": m["note"] or "",
                        "amount": to_dollars(m["amount_cents"] if incoming else -m["amount_cents"]), "move_type": m["move_type"],
                        "allocation_id": m["allocation_id"], "income_txn_id": m["income_txn_id"]})
    if env["kind"] == "system":
        for t in conn.execute(
            """SELECT txn_id, date, name, amount, txn_kind, envelope FROM transactions WHERE is_included = 1 AND date >= ? AND
               (txn_kind IN ('paycheck', 'other_income') OR (txn_kind IN ('expense', 'refund')))""", (G.isoformat(),)):
            if t["txn_kind"] in INCOME_KINDS or (t["envelope"] or "").strip().lower() not in _envelope_index(conn)["by_name"]:
                entries.append({"date": t["date"], "kind": "txn", "id": t["txn_id"], "label": t["name"], "note": t["txn_kind"], "amount": float(t["amount"])})
    else:
        for t in conn.execute(
            """SELECT txn_id, date, name, amount, txn_kind FROM transactions WHERE is_included = 1 AND date >= ?
               AND txn_kind IN ('expense', 'refund') AND LOWER(TRIM(envelope)) = LOWER(?)""", (G.isoformat(), env["name"])):
            entries.append({"date": t["date"], "kind": "txn", "id": t["txn_id"], "label": t["name"], "note": t["txn_kind"], "amount": float(t["amount"])})
    entries.sort(key=lambda e: (e["date"], 0 if e["kind"] == "move" else 1))
    running = 0.0
    for e in entries:
        running = round(running + e["amount"], 2)
        e["balance"] = running
    shown = [e for e in entries if (not start or e["date"] >= start) and (not end or e["date"] <= end)]
    start_balance = 0.0
    if shown:
        first = entries.index(shown[0])
        start_balance = entries[first - 1]["balance"] if first > 0 else 0.0
    return {"entries": shown, "start_balance": start_balance, "end_balance": entries[-1]["balance"] if entries else 0.0}


def upcoming_bills(conn: Any, today: date, days: int = 30) -> List[Dict[str, Any]]:
    """Bills due within `days` (and anything overdue), overdue first, with whether the envelope can cover each."""
    states = {e["id"]: e for e in envelope_states(conn, today)["envelopes"]} if go_live_date(conn) else {}
    horizon = (today + timedelta(days=days)).isoformat()
    out = []
    for e in env_mod.list_envelopes(conn, today):
        for b in e["bills"]:
            if not b["active"] or not b["next_due_date"] or b["next_due_date"] > horizon:
                continue
            st = states.get(e["id"])
            out.append({
                "bill_id": b["id"], "name": b["name"], "envelope_id": e["id"], "envelope": e["name"], "due": b["next_due_date"],
                "amount": b["amount"], "overdue": b["overdue"], "covered": (st["balance"] >= b["amount"]) if st else None,
                "balance": st["balance"] if st else None,
            })
    out.sort(key=lambda x: (not x["overdue"], x["due"]))
    return out
