"""Match payments to bills and move each bill's next due date forward."""

import statistics
from datetime import date, timedelta
from typing import Any, Dict, Optional

from . import recurrence, store


def _expected_amount(bill: Any) -> float:
    return (bill["amount_cents"] or 0) / 100.0


def _advance(conn: Any, bill: Any, txn: Any) -> None:
    """Record the payment and push the due date to the next occurrence."""
    due = recurrence.parse(bill["next_due_date"] or "")
    anchor = recurrence.parse(bill["anchor_date"] or "")
    next_due = bill["next_due_date"]
    active = bill["active"]
    if bill["freq"] == "once":
        active = 0
    elif due and anchor:
        nxt = recurrence.next_occurrence(bill["freq"], bill["interval"], anchor, due)
        next_due = nxt.isoformat() if nxt else next_due
    amount_cents = bill["amount_cents"]
    conn.execute(
        "UPDATE bills SET last_paid_date = ?, last_paid_txn_id = ?, next_due_date = ?, active = ? WHERE id = ?",
        (txn["date"], txn["txn_id"], next_due, active, bill["id"]))
    conn.execute("UPDATE transactions SET bill_id = ? WHERE txn_id = ?", (bill["id"], txn["txn_id"]))
    if bill["amount_mode"] == "estimated":
        recent = [abs(float(r[0])) for r in conn.execute(
            "SELECT amount FROM transactions WHERE bill_id = ? ORDER BY date DESC LIMIT 3", (bill["id"],))]
        if recent:
            amount_cents = int(round(statistics.mean(recent) * 100))
            conn.execute("UPDATE bills SET amount_cents = ? WHERE id = ?", (amount_cents, bill["id"]))


def match_transaction(conn: Any, txn_id: str) -> Optional[int]:
    """Link an expense to the bill it pays, if one fits on text, amount, and date. Returns the bill id."""
    txn = conn.execute(
        "SELECT txn_id, date, name, raw_name, amount, envelope, bill_id, txn_kind, is_included FROM transactions WHERE txn_id = ?",
        (txn_id,)).fetchone()
    if txn is None or txn["bill_id"] or not txn["is_included"] or txn["txn_kind"] != "expense" or txn["amount"] >= 0:
        return None
    env = conn.execute("SELECT id FROM envelopes WHERE LOWER(name) = LOWER(?)", ((txn["envelope"] or "").strip(),)).fetchone()
    if env is None:
        return None
    window = int(store.get_setting(conn, "bill_match_window_days"))
    default_tol = int(store.get_setting(conn, "bill_amount_tolerance_pct"))
    paid = date.fromisoformat(txn["date"])
    hay = " ".join(f"{txn['raw_name'] or ''} {txn['name'] or ''}".upper().split())
    amount = abs(float(txn["amount"]))
    best, best_gap = None, None
    for bill in conn.execute("SELECT * FROM bills WHERE envelope_id = ? AND active = 1 AND next_due_date IS NOT NULL", (env["id"],)):
        phrase = (bill["match_phrase"] or "").strip().upper()
        if phrase and phrase not in hay:
            continue
        expected = _expected_amount(bill)
        tol = bill["amount_tolerance_pct"] if bill["amount_tolerance_pct"] is not None else default_tol
        if expected and abs(amount - expected) > expected * tol / 100.0:
            continue
        due = recurrence.parse(bill["next_due_date"])
        if due is None:
            continue
        reach = 3 if bill["freq"] == "weekly" else window
        gap = abs((paid - due).days)
        if gap <= reach and (best_gap is None or gap < best_gap):
            best, best_gap = bill, gap
    if best is None:
        return None
    _advance(conn, best, txn)
    return int(best["id"])


def link(conn: Any, bill_id: int, txn_id: str) -> None:
    """Tie a payment to a bill by hand, ignoring the date window."""
    bill = conn.execute("SELECT * FROM bills WHERE id = ?", (bill_id,)).fetchone()
    txn = conn.execute("SELECT txn_id, date, bill_id FROM transactions WHERE txn_id = ?", (txn_id,)).fetchone()
    if bill is None or txn is None:
        raise ValueError("Bill or transaction not found")
    if txn["bill_id"]:
        raise ValueError("That payment is already linked to a bill")
    _advance(conn, bill, txn)


def unlink(conn: Any, txn_id: str) -> None:
    """Undo a link: the bill is due again on the date it was paid for."""
    txn = conn.execute("SELECT bill_id FROM transactions WHERE txn_id = ?", (txn_id,)).fetchone()
    if txn is None or not txn["bill_id"]:
        raise ValueError("That payment isn't linked to a bill")
    bill = conn.execute("SELECT * FROM bills WHERE id = ?", (txn["bill_id"],)).fetchone()
    conn.execute("UPDATE transactions SET bill_id = NULL WHERE txn_id = ?", (txn_id,))
    if bill is not None and bill["last_paid_txn_id"] == txn_id:
        next_due, active = bill["next_due_date"], 1
        anchor = recurrence.parse(bill["anchor_date"] or "")
        due = recurrence.parse(bill["next_due_date"] or "")
        if bill["freq"] != "once" and anchor and due:
            prev = recurrence.previous_occurrence(bill["freq"], bill["interval"], anchor, due - timedelta(days=1))
            next_due = prev.isoformat() if prev else next_due
        conn.execute(
            "UPDATE bills SET last_paid_date = NULL, last_paid_txn_id = NULL, next_due_date = ?, active = ? WHERE id = ?",
            (next_due, active, bill["id"]))
