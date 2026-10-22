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
