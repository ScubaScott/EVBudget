"""Repeating-date math: weekly, monthly, and yearly schedules with an anchor date."""

import calendar
import math
from datetime import date, timedelta
from typing import List, Optional

FREQUENCIES = ("once", "weekly", "monthly", "yearly")


def parse(value: str) -> Optional[date]:
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


def occurrences_per_year(freq: str, interval: int) -> float:
    """How many times a year the schedule fires (0 for a one-time date)."""
    interval = max(1, int(interval or 1))
    if freq == "weekly":
        return 52.0 / interval
    if freq == "monthly":
        return 12.0 / interval
    if freq == "yearly":
        return 1.0 / interval
    return 0.0


def _month_date(anchor: date, month_index: int) -> date:
    """The date in the month `month_index` (year*12+month-1) that keeps the anchor's day, clamped to month end."""
    year, month0 = divmod(month_index, 12)
    last = calendar.monthrange(year, month0 + 1)[1]
    return date(year, month0 + 1, min(anchor.day, last))


def occurrences_between(freq: str, interval: int, anchor: date, start: date, end: date) -> List[date]:
    """Every occurrence from start to end, inclusive."""
    interval = max(1, int(interval or 1))
    out: List[date] = []
    if end < start:
        return out
    if freq == "once":
        return [anchor] if start <= anchor <= end else []
    if freq == "weekly":
        step = 7 * interval
        k = math.ceil((start - anchor).days / step)
        d = anchor + timedelta(days=k * step)
        while d <= end:
            out.append(d)
            d += timedelta(days=step)
        return out
    if freq in ("monthly", "yearly"):
        months = interval * (12 if freq == "yearly" else 1)
        base = anchor.year * 12 + anchor.month - 1
        start_idx = start.year * 12 + start.month - 1
        k = math.floor((start_idx - base) / months) - 1
        while True:
            d = _month_date(anchor, base + k * months)
            if d > end:
                break
            if d >= start:
                out.append(d)
            k += 1
        return out
    return out


def next_occurrence(freq: str, interval: int, anchor: date, after: date) -> Optional[date]:
    """First occurrence strictly after `after`."""
    if freq == "once":
        return anchor if anchor > after else None
    window_end = after + timedelta(days=366 * max(1, int(interval or 1)) * (1 if freq != "yearly" else 1) + 400)
    found = occurrences_between(freq, interval, anchor, after + timedelta(days=1), window_end)
    return found[0] if found else None


def previous_occurrence(freq: str, interval: int, anchor: date, on_or_before: date) -> Optional[date]:
    """Latest occurrence on or before the given date."""
    if freq == "once":
        return anchor if anchor <= on_or_before else None
    span = 366 * max(1, int(interval or 1)) + 400
    found = occurrences_between(freq, interval, anchor, on_or_before - timedelta(days=span), on_or_before)
    return found[-1] if found else None
