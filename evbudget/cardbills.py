"""Find the automatic bills on a card, so each can be moved when the card number changes.

A merchant on the card is a possible bill when its charges repeat on a regular schedule. The same schedules as
"Bills we noticed" are understood (weekly, every 2 weeks, monthly, every 2, 3 or 6 months, yearly) plus every
3 and 4 weeks.

  likely  3 or more charges on different days with most gaps near one schedule; a yearly bill needs just 2.
          Weekly-style schedules also need steady amounts, which keeps frequent fill-ups and groceries out;
          a monthly bill may vary in amount (a utility) as long as it arrives on schedule.
  maybe   2 charges one monthly-or-longer gap apart with similar amounts, or 3 or more that fit loosely.
  stopped the last charge is long overdue compared with the card's newest transaction. Hidden by default.

A merchant that charges several different amounts (Apple, Amazon) is also tried amount by amount, so two
subscriptions at one merchant show up separately.

Which merchants have been moved to the new card is kept in card_bill_checks, per card.
"""

import statistics
from collections import Counter
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

from . import recurrence
from .envelopes import KNOWN_INTERVALS
from .textutil import clean_vendor_for_lookup, vendor_key_tokens

# (days between charges, frequency, interval, allowed wobble in days)
INTERVALS = sorted(KNOWN_INTERVALS + [(21, "weekly", 3, 3), (28, "weekly", 4, 2)])
STATUSES = ("updated", "not_a_bill")
AMOUNT_CLOSE = 0.15


def cadence_label(freq: str, interval: int) -> str:
    if freq == "weekly":
        return "Weekly" if interval == 1 else f"Every {interval} weeks"
    if freq == "monthly":
        return {1: "Monthly", 3: "Quarterly (every 3 months)"}.get(interval, f"Every {interval} months")
    return "Yearly"


def _norm(text: str) -> str:
    return " ".join((text or "").upper().split())


def _best_interval(median_gap: float) -> Optional[Tuple[int, str, int, int]]:
    fits = [(abs(median_gap - s[0]) / s[3], s) for s in INTERVALS if abs(median_gap - s[0]) <= s[3]]
    return min(fits, key=lambda f: f[0])[1] if fits else None


def evaluate(by_date: Dict[str, float], reference: date) -> Optional[Dict[str, Any]]:
    """Judge one merchant's charges (date -> amount). None if they don't look like a repeating bill."""
    dates = sorted(by_date)
    if len(dates) < 2:
        return None
    parsed = [date.fromisoformat(d) for d in dates]
    gaps = [(b - a).days for a, b in zip(parsed, parsed[1:])]
    spec = _best_interval(statistics.median(gaps))
    if spec is None:
        return None
    target, freq, interval, wobble = spec
    steady = sum(1 for g in gaps if abs(g - target) <= wobble) / len(gaps)
    amounts = [by_date[d] for d in dates]
    typical = statistics.median(amounts)
    close = sum(1 for a in amounts if abs(a - typical) <= AMOUNT_CLOSE * typical) / len(amounts)
    frequent = freq == "weekly"  # these need steady amounts to be told apart from ordinary shopping
    median_day = statistics.median(d.day for d in parsed)
    same_day_of_month = all(min(abs(d.day - median_day), 31 - abs(d.day - median_day)) <= 6 for d in parsed)
    identical = all(abs(a - typical) <= max(0.5, 0.02 * typical) for a in amounts)

    if len(dates) >= 3:
        if freq == "yearly":
            confidence = "likely" if steady >= 0.8 else "maybe"
        elif frequent:
            if steady >= 0.8 and close >= 0.8:
                confidence = "likely" if len(dates) >= 4 else "maybe"
            elif steady >= 0.6 and close >= 0.6:
                confidence = "maybe"
            else:
                return None
        else:  # monthly or longer: the amount may vary (a utility) if it arrives on the same day each time
            if steady >= 0.8 and (close >= 0.8 or same_day_of_month):
                confidence = "likely" if close >= 0.8 or len(dates) >= 4 else "maybe"
            elif steady >= 0.6 and (close >= 0.6 or same_day_of_month):
                confidence = "maybe"
            else:
                return None
    elif freq == "yearly":
        confidence = "likely"
    elif freq == "monthly" and interval == 1 and identical:
        confidence = "maybe"  # two identical charges a month apart; any longer gap is too often coincidence
    else:
        return None

    stopped = (reference - parsed[-1]).days > 2 * target + wobble
    next_expected = None if stopped else recurrence.next_occurrence(freq, interval, parsed[-1], reference)
    return {
        "cadence": cadence_label(freq, interval), "freq": freq, "interval": interval, "charges": len(dates),
        "first_date": dates[0], "last_date": dates[-1], "typical_amount": round(typical, 2),
        "min_amount": round(min(amounts), 2), "max_amount": round(max(amounts), 2), "steady_amount": close >= 0.8,
        "confidence": confidence, "stopped": stopped,
        "next_expected": next_expected.isoformat() if next_expected else None,
    }


def _amount_clusters(by_date: Dict[str, float]) -> List[Dict[str, float]]:
    """Split one merchant's charges into runs of nearly the same amount (a merchant with two subscriptions)."""
    clusters: List[Dict[str, float]] = []
    for d, amt in sorted(by_date.items(), key=lambda kv: kv[1]):
        if clusters and amt - max(clusters[-1].values()) <= max(0.5, 0.03 * amt):
            clusters[-1][d] = amt
        else:
            clusters.append({d: amt})
    return [c for c in clusters if len(c) >= 2]


def _find_bill(bills: List[Any], key_tokens: Tuple[str, ...], hay: str, linked_ids: set) -> Optional[Dict[str, Any]]:
    """The existing bill this merchant already belongs to, if any."""
    for b in bills:
        if b["id"] in linked_ids:
            return {"id": b["id"], "name": b["name"]}
    for b in bills:
        phrase = _norm(b["match_phrase"] or "")
        if phrase and phrase in hay:
            return {"id": b["id"], "name": b["name"]}
    for b in bills:
        bill_tokens = vendor_key_tokens(b["name"] or "")  # "Spotify" and "SPOTIFY USA" are the same merchant
        if bill_tokens and key_tokens and (key_tokens[:len(bill_tokens)] == bill_tokens or bill_tokens[:len(key_tokens)] == key_tokens):
            return {"id": b["id"], "name": b["name"]}
    return None


def find_bills(conn: Any, card: str) -> Dict[str, Any]:
    """Possible automatic bills on a card, with their checklist state."""
    rows = conn.execute(
        """SELECT date, name, raw_name, amount, bill_id FROM transactions
           WHERE account_name = ? AND is_included = 1 AND txn_kind = 'expense' AND amount < 0 ORDER BY date""",
        (card,),
    ).fetchall()
    newest = conn.execute("SELECT MAX(date) FROM transactions WHERE account_name = ?", (card,)).fetchone()[0]
    reference = date.fromisoformat(newest) if newest else date.today()
    bills = conn.execute("SELECT id, name, match_phrase FROM bills").fetchall()
    states = {r["merchant_key"]: r["status"] for r in conn.execute("SELECT merchant_key, status FROM card_bill_checks WHERE card = ?", (card,))}

    merchants: Dict[Tuple[str, ...], Dict[str, Any]] = {}
    for r in rows:
        tokens = vendor_key_tokens(r["name"] or "") or vendor_key_tokens(clean_vendor_for_lookup(r["raw_name"] or ""))
        if not tokens:
            continue
        m = merchants.setdefault(tokens, {"by_date": {}, "names": Counter(), "bill_ids": set(), "hay": set()})
        amount = abs(float(r["amount"]))
        m["by_date"][r["date"]] = max(m["by_date"].get(r["date"], 0.0), amount)  # same-day repeats count once
        m["names"][r["name"] or r["raw_name"] or ""] += 1
        if r["bill_id"]:
            m["bill_ids"].add(r["bill_id"])
        m["hay"].add(_norm(f"{r['raw_name'] or ''} {r['name'] or ''}"))

    items: List[Dict[str, Any]] = []
    for tokens, m in merchants.items():
        found = evaluate(m["by_date"], reference)
        series = [("", found)] if found else []
        if found is None or not found["steady_amount"]:
            by_amount = [(f"|{statistics.median(c.values()):.2f}", evaluate(c, reference)) for c in _amount_clusters(m["by_date"])]
            by_amount = [(suffix, sub) for suffix, sub in by_amount if sub]
            # Amount by amount replaces the whole when it explains most of it (two subscriptions, or a price change);
            # otherwise the whole stands (a bill that simply varies).
            if found is None or any(sub["charges"] >= 0.6 * found["charges"] for _, sub in by_amount):
                series = by_amount
        for suffix, result in series:
            key = " ".join(tokens) + suffix
            items.append(dict(
                result, key=key, merchant=m["names"].most_common(1)[0][0],
                bill=_find_bill(bills, tokens, " ".join(sorted(m["hay"])), m["bill_ids"]),
                status=states.get(key, "pending"),
            ))
    items.sort(key=lambda i: (i["confidence"] != "likely", i["stopped"], i["merchant"].upper()))
    active = [i for i in items if not i["stopped"] and i["status"] != "not_a_bill"]
    return {
        "card": card, "through": newest, "items": items,
        "progress": {"updated": sum(1 for i in active if i["status"] == "updated"), "total": len(active)},
    }


def set_status(conn: Any, card: str, key: str, status: str) -> None:
    """Record that a merchant was moved to the new card, or isn't a bill. "pending" clears it."""
    if status == "pending":
        conn.execute("DELETE FROM card_bill_checks WHERE card = ? AND merchant_key = ?", (card, key))
        return
    if status not in STATUSES:
        raise ValueError(f"Unknown status '{status}'")
    conn.execute(
        """INSERT INTO card_bill_checks (card, merchant_key, status, updated_at) VALUES (?, ?, ?, ?)
           ON CONFLICT(card, merchant_key) DO UPDATE SET status = excluded.status, updated_at = excluded.updated_at""",
        (card, key, status, datetime.now().isoformat()),
    )
