"""Match payments to bills and move each bill's next due date forward."""

import re
import statistics
from datetime import date, timedelta
from typing import Any, Dict, Iterable, List, Optional

from . import recurrence, store
from .textutil import vendor_key_tokens


# ---------------------------------------------------------------------------
# Comparing bank text with a bill's match text
# ---------------------------------------------------------------------------

def words(text: str) -> List[str]:
    """Upper-case words with punctuation and spacing dropped: "WCI*Dicks  Sanitation" -> WCI DICKS SANITATION."""
    return re.sub(r"[^A-Z0-9]+", " ", (text or "").upper()).split()


def phrase_in(phrase: str, text: str) -> bool:
    """True when the phrase's words appear in the text, starting at a word. Punctuation, case, spacing and what
    comes after the phrase (payment numbers, store codes) do not matter."""
    want = " ".join(words(phrase))
    return bool(want) and f" {want}" in " " + " ".join(words(text))


def bill_covers(bill: Any, texts: Iterable[str], linked_ids: Iterable[int] = ()) -> bool:
    """Does this bill already stand for a payee? `texts` are every way the payee is written (bank text, display
    name). It does if a payment is linked to it, its match text is in the payee, or the payee and the bill name
    begin with the same two words ("Spotify" and "SPOTIFY USA")."""
    if bill["id"] in set(linked_ids):
        return True
    texts = [t for t in texts if t]
    phrase = (bill["match_phrase"] or "").strip()
    if phrase and any(phrase_in(phrase, t) for t in texts):
        return True
    mine = vendor_key_tokens(bill["name"] or "")
    for t in texts:
        theirs = vendor_key_tokens(t)
        if mine and theirs and (mine[:len(theirs)] == theirs or theirs[:len(mine)] == mine):
            return True
    return False


def suggest_phrase(names: List[str]) -> str:
    """Short, steady match text for a payee: the words every payment starts with, cut before the first word that
    holds a digit (order codes, dates, reference and store numbers change every time)."""
    if not names:
        return ""
    lists = []
    for n in names:
        keep = []
        for w in words(n):
            if any(ch.isdigit() for ch in w):
                break
            keep.append(w)
        lists.append(keep)
    lists = [l for l in lists if l]
    if not lists:
        return " ".join(words(names[0])[:2])
    common = lists[0]
    for l in lists[1:]:
        n = 0
        while n < min(len(common), len(l)) and common[n] == l[n]:
            n += 1
        common = common[:n]
    phrase = " ".join(common)
    return phrase if len(phrase) >= 3 else " ".join(lists[0])


def payee_text(txn: Any) -> str:
    return f"{txn['raw_name'] or ''} {txn['name'] or ''}"


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
    hay = payee_text(txn)
    amount = abs(float(txn["amount"]))
    best, best_rank = None, None
    for bill in conn.execute("SELECT * FROM bills WHERE envelope_id = ? AND active = 1 AND next_due_date IS NOT NULL", (env["id"],)):
        phrase = (bill["match_phrase"] or "").strip()
        if phrase and not phrase_in(phrase, hay):
            continue
        expected = _expected_amount(bill)
        tol = bill["amount_tolerance_pct"] if bill["amount_tolerance_pct"] is not None else default_tol
        amount_ok = not expected or abs(amount - expected) <= expected * tol / 100.0
        # Bank text that names the bill is proof enough: a price rise should still link. Without match text the
        # amount is all there is to go on.
        if not amount_ok and not phrase:
            continue
        due = recurrence.parse(bill["next_due_date"])
        if due is None:
            continue
        reach = 3 if bill["freq"] == "weekly" else window
        gap = abs((paid - due).days)
        rank = (0 if amount_ok else 1, gap)
        if gap <= reach and (best_rank is None or rank < best_rank):
            best, best_rank = bill, rank
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


def matching_payments(conn: Any, phrase: str, envelope_id: Optional[int] = None, limit: int = 60) -> List[Dict[str, Any]]:
    """Saved payments whose bank text fits a match phrase, newest first, with the bill each is already linked to."""
    if not words(phrase):
        return []
    env_name = None
    if envelope_id:
        row = conn.execute("SELECT name FROM envelopes WHERE id = ?", (int(envelope_id),)).fetchone()
        env_name = (row["name"] if row else "").strip().lower()
    out = []
    for t in conn.execute(
        """SELECT txn_id, date, name, raw_name, amount, envelope, bill_id FROM transactions
           WHERE is_included = 1 AND txn_kind = 'expense' AND amount < 0 ORDER BY date DESC"""):
        if not phrase_in(phrase, payee_text(t)):
            continue
        out.append({
            "txn_id": t["txn_id"], "date": t["date"], "name": t["name"], "amount": abs(float(t["amount"])),
            "envelope": t["envelope"] or "", "bill_id": t["bill_id"],
            "other_envelope": bool(env_name is not None and (t["envelope"] or "").strip().lower() != env_name),
        })
        if len(out) >= limit:
            break
    return out


def link_past(conn: Any, bill_id: int, txn_ids: List[str]) -> int:
    """Tie several past payments to a bill at once. The bill's last-paid date becomes the newest of them, and its
    next due date moves past any due date that payment already covers (unlike link(), which moves it one step)."""
    bill = conn.execute("SELECT * FROM bills WHERE id = ?", (bill_id,)).fetchone()
    if bill is None:
        raise ValueError("Bill not found")
    rows = []
    for tid in txn_ids:
        t = conn.execute("SELECT txn_id, date, bill_id FROM transactions WHERE txn_id = ?", (tid,)).fetchone()
        if t is not None and not t["bill_id"]:
            rows.append(t)
    if not rows:
        return 0
    for t in rows:
        conn.execute("UPDATE transactions SET bill_id = ? WHERE txn_id = ?", (bill_id, t["txn_id"]))
    newest = max(rows, key=lambda t: t["date"])
    next_due, active = bill["next_due_date"], bill["active"]
    if not bill["last_paid_date"] or newest["date"] >= bill["last_paid_date"]:
        window = int(store.get_setting(conn, "bill_match_window_days"))
        paid = date.fromisoformat(newest["date"])
        due, anchor = recurrence.parse(next_due or ""), recurrence.parse(bill["anchor_date"] or "")
        if bill["freq"] == "once":
            if due and due - timedelta(days=window) <= paid:
                active = 0
        elif due and anchor:
            for _ in range(60):   # one step per due date the payment already covers
                if due - timedelta(days=window) > paid:
                    break
                nxt = recurrence.next_occurrence(bill["freq"], bill["interval"], anchor, due)
                if not nxt:
                    break
                due = nxt
            next_due = due.isoformat()
        conn.execute("UPDATE bills SET last_paid_date = ?, last_paid_txn_id = ?, next_due_date = ?, active = ? WHERE id = ?",
                     (newest["date"], newest["txn_id"], next_due, active, bill_id))
    return len(rows)


def review_phrases(conn: Any) -> List[Dict[str, Any]]:
    """Bills whose match text holds a number (an order code, a date, a reference) that will not repeat, with
    shorter text that will."""
    out = []
    for b in conn.execute("SELECT id, name, match_phrase, envelope_id FROM bills WHERE match_phrase IS NOT NULL AND TRIM(match_phrase) != ''").fetchall():
        if not any(any(ch.isdigit() for ch in w) for w in words(b["match_phrase"])):
            continue
        suggested = suggest_phrase([b["match_phrase"]])
        if not suggested or " ".join(words(suggested)) == " ".join(words(b["match_phrase"])):
            continue
        out.append({
            "bill_id": b["id"], "name": b["name"], "phrase": b["match_phrase"], "suggested": suggested,
            "now_matches": len(matching_payments(conn, b["match_phrase"], b["envelope_id"], limit=500)),
            "would_match": len(matching_payments(conn, suggested, b["envelope_id"], limit=500)),
        })
    return out


# ---------------------------------------------------------------------------
# A fixed-amount bill that now costs something else
# ---------------------------------------------------------------------------

DISMISSED_PRICE_KEY = "dismissed_price_changes"


def _dismissed_prices(conn: Any) -> Dict[str, str]:
    import json
    try:
        data = json.loads(store.get_setting(conn, DISMISSED_PRICE_KEY) or "{}")
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


def price_changes(conn: Any) -> List[Dict[str, Any]]:
    """Active bills marked "the same every time" whose latest linked payment differs from the bill's amount by more
    than the bill's allowed difference (10% unless set). A bill you chose to keep stays quiet until a newer payment
    changes the picture."""
    from . import envelopes as env_mod   # envelopes imports this module

    default_tol = int(store.get_setting(conn, "bill_amount_tolerance_pct"))
    dismissed = _dismissed_prices(conn)
    budgets = {e["id"]: e for e in env_mod.list_envelopes(conn)}
    out = []
    for b in conn.execute(
        """SELECT b.*, t.amount AS paid_amount, t.date AS paid_date FROM bills b
           JOIN transactions t ON t.txn_id = b.last_paid_txn_id
           WHERE b.active = 1 AND b.amount_mode = 'fixed' AND b.amount_cents > 0 AND b.freq != 'once'"""):
        old, new = b["amount_cents"] / 100.0, abs(float(b["paid_amount"]))
        tol = b["amount_tolerance_pct"] if b["amount_tolerance_pct"] is not None else default_tol
        if abs(new - old) <= old * tol / 100.0 or dismissed.get(str(b["id"])) == b["last_paid_txn_id"]:
            continue
        per_year = recurrence.occurrences_per_year(b["freq"], b["interval"])
        env = budgets.get(b["envelope_id"])
        monthly = round((new - old) * per_year / 12, 2)
        out.append({
            "bill_id": b["id"], "name": b["name"], "envelope_id": b["envelope_id"], "envelope": env["name"] if env else "",
            "old_amount": round(old, 2), "new_amount": round(new, 2), "percent": round((new - old) / old * 100, 1),
            "paid_date": b["paid_date"], "monthly_change": monthly,
            # Bill envelopes take their monthly budget from their bills; any other kind has a budget you set yourself
            "budget_follows_bills": bool(env and env["budget_derived"]),
            "budget_now": env["effective_budget"] if env else 0.0,
        })
    out.sort(key=lambda c: -abs(c["monthly_change"]))
    return out


def resolve_price_change(conn: Any, bill_id: int, action: str) -> None:
    """action "update": the bill's amount becomes what was paid. "keep": leave the bill alone and stop asking about
    that payment."""
    import json
    b = conn.execute(
        """SELECT b.id, b.last_paid_txn_id, t.amount AS paid FROM bills b LEFT JOIN transactions t ON t.txn_id = b.last_paid_txn_id
           WHERE b.id = ?""", (bill_id,)).fetchone()
    if b is None or not b["last_paid_txn_id"]:
        raise ValueError("That bill has no payment to compare with")
    if action == "update":
        conn.execute("UPDATE bills SET amount_cents = ? WHERE id = ?", (int(round(abs(float(b["paid"])) * 100)), bill_id))
    elif action == "keep":
        d = _dismissed_prices(conn)
        d[str(bill_id)] = b["last_paid_txn_id"]
        store.set_setting(conn, DISMISSED_PRICE_KEY, json.dumps(d))
    else:
        raise ValueError("Unknown choice")
