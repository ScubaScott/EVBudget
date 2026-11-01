"""Finding paychecks in imported deposits, so a new user doesn't have to dig the payroll text out of a bank file.

Works like "Bills we noticed", but for money coming in: deposits from the same payer that arrive on a
steady weekly, every-2-weeks, or monthly rhythm and don't belong to a pay source yet.
"""

import html
import json
import os
import re
import statistics
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

from . import recurrence, store
from .textutil import collapse_ws

# (days between deposits, frequency, interval, allowed wobble in days)
PAY_INTERVALS = [(7, "weekly", 1, 2), (14, "weekly", 2, 3), (30, "monthly", 1, 4)]

# Bank filler words that say nothing about who paid
NOISE_WORDS = {"PPD", "CCD", "WEB", "ACH", "ID", "DES", "INDN", "CO", "ENTRY", "DESCR", "TRACE", "TEL", "IAT", "ORIG"}

MIN_DEPOSITS = 3
MIN_TYPICAL = 50.0
DISMISSED_KEY = "dismissed_income_candidates"


def _deposit_text(raw_name: str, name: str) -> str:
    """The bank description the way pay source phrases are matched at import time."""
    return collapse_ws(html.unescape(raw_name or name or "")).upper()


def _words(text: str) -> List[str]:
    """Words with no digits, punctuation trimmed, filler words left out."""
    out = []
    for tok in text.split():
        tok = tok.strip(".,:;*#/-()")
        if tok and not any(ch.isdigit() for ch in tok) and tok not in NOISE_WORDS:
            out.append(tok)
    return out


def group_key(text: str) -> str:
    """First three meaningful words: the same payer gives the same key even when trace numbers change."""
    return " ".join(_words(text)[:3])


def suggest_phrase(texts: List[str], key: str) -> str:
    """The longest start that every description shares, cut back to whole, meaningful words."""
    prefix = os.path.commonprefix(texts)
    if any(len(t) > len(prefix) and t[len(prefix)] != " " for t in texts):
        prefix = prefix.rsplit(" ", 1)[0] if " " in prefix else ""   # the last word is cut off part-way
    tokens = prefix.split()
    while tokens and (any(ch.isdigit() for ch in tokens[-1]) or tokens[-1].strip(".,:;*#/-()") in NOISE_WORDS
                      or not tokens[-1].strip(".,:;*#/-()")):
        tokens.pop()
    phrase = " ".join(tokens)
    return phrase if len(phrase) >= 5 else key


def suggest_source_name(key: str) -> str:
    first = key.split()[0] if key else ""
    return first.title()


def dismissed(conn: Any) -> List[str]:
    try:
        data = json.loads(store.get_setting(conn, DISMISSED_KEY) or "[]")
        return data if isinstance(data, list) else []
    except ValueError:
        return []


def dismiss(conn: Any, key: str) -> None:
    keys = dismissed(conn)
    if key and key not in keys:
        keys.append(key)
        store.set_setting(conn, DISMISSED_KEY, json.dumps(keys))


def discover_pay_sources(conn: Any) -> List[Dict[str, Any]]:
    """Repeating deposits that look like paychecks and aren't tied to a pay source yet."""
    phrases = [collapse_ws(r[0] or "").upper() for r in conn.execute("SELECT match_phrase FROM income_sources")]
    phrases = [p for p in phrases if p]
    skip = set(dismissed(conn))
    newest = conn.execute("SELECT MAX(date) FROM transactions").fetchone()[0]
    if not newest:
        return []
    newest_d = date.fromisoformat(newest)

    groups: Dict[str, Dict[str, Any]] = {}
    for r in conn.execute(
        """SELECT date, name, raw_name, amount FROM transactions
           WHERE amount > 0 AND is_included = 1 AND income_source_id IS NULL
             AND txn_kind IN ('paycheck', 'other_income') ORDER BY date"""
    ):
        text = _deposit_text(r["raw_name"], r["name"])
        key = group_key(text)
        if not key or key in skip or any(p in text for p in phrases):
            continue
        g = groups.setdefault(key, {"by_date": {}, "texts": [], "name": r["name"]})
        amt = float(r["amount"])
        g["by_date"][r["date"]] = max(g["by_date"].get(r["date"], 0.0), amt)   # a split deposit counts once, at its largest part
        g["texts"].append(text)
        g["name"] = r["name"] or g["name"]

    out: List[Dict[str, Any]] = []
    for key, g in groups.items():
        dates = sorted(g["by_date"])
        if len(dates) < MIN_DEPOSITS:
            continue
        parsed = [date.fromisoformat(d) for d in dates]
        gaps = [(b - a).days for a, b in zip(parsed, parsed[1:])]
        med = statistics.median(gaps)
        spec = next((s for s in PAY_INTERVALS if abs(med - s[0]) <= s[3]), None)
        if spec is None:
            continue
        target, freq, interval, wobble = spec
        steady = sum(1 for x in gaps if abs(x - target) <= wobble) / len(gaps)
        amounts = [g["by_date"][d] for d in dates]
        typical = statistics.median(amounts)
        close = sum(1 for a in amounts if abs(a - typical) <= 0.25 * typical) / len(amounts)   # bonuses are allowed to stand out
        stale = (newest_d - parsed[-1]).days > 2 * target + wobble
        if steady < 0.75 or close < 0.6 or typical < MIN_TYPICAL or stale:
            continue
        nxt = recurrence.next_occurrence(freq, interval, parsed[-1], newest_d)
        out.append({
            "key": key, "name": g["name"], "suggested_name": suggest_source_name(key),
            "match_phrase": suggest_phrase(g["texts"], key),
            "deposits": len(dates), "typical_amount": round(typical, 2),
            "freq": freq, "interval": interval, "last_date": dates[-1],
            "next_date": nxt.isoformat() if nxt else None,
            "confidence": "high" if steady == 1 and close >= 0.8 and len(dates) >= 4 else "medium",
        })
    out.sort(key=lambda c: (c["confidence"] != "high", -c["typical_amount"], c["key"]))
    return out[:10]
