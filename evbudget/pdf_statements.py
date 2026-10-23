"""Credit card statement PDFs: pull out just the transactions.

Text is extracted with pypdf (pure Python), then a parser written for that bank's
layout turns it into rows. Each bank is one small class registered in PARSERS, so
adding a bank means adding a class here and a sample text in the tests.

Every parser also reads the totals the statement prints (payments, purchases) and
checks them against the rows it found. A statement that doesn't add up is still
returned, but with a warning, so a missed or misread row is never silent.

Amounts follow the app's convention: money out is negative, money in is positive.
"""

import re
import subprocess
import sys
from datetime import date
from typing import Any, Dict, List, Optional

PYPDF_REQUIREMENT = "pypdf>=4.0"


class StatementError(Exception):
    """The file can't be read as a supported statement. The message is meant for the user."""


# ---------------------------------------------------------------------------
# Text extraction
# ---------------------------------------------------------------------------


def _load_pypdf() -> Any:
    """Import pypdf, installing it with pip on first use if it is missing."""
    try:
        import pypdf
        return pypdf
    except ImportError:
        pass
    print(f" * Installing {PYPDF_REQUIREMENT} (needed to read PDF statements)...")
    try:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", PYPDF_REQUIREMENT],
            check=True, capture_output=True, timeout=300,
        )
        import importlib
        importlib.invalidate_caches()
        import pypdf
        return pypdf
    except Exception as exc:
        raise StatementError(
            f"Reading PDFs needs the pypdf package and it could not be installed automatically ({exc}). "
            "Run:  pip install pypdf"
        ) from exc


def extract_text(path: str) -> str:
    pypdf = _load_pypdf()
    try:
        reader = pypdf.PdfReader(path)
        if reader.is_encrypted and not reader.decrypt(""):
            raise StatementError("This PDF is password protected. Save an unprotected copy and try again.")
        text = "\n".join((page.extract_text() or "") for page in reader.pages)
    except StatementError:
        raise
    except Exception as exc:
        raise StatementError(f"Could not read this PDF ({exc}).") from exc
    if not text.strip():
        raise StatementError("No text found in this PDF. It may be a scanned image, which isn't supported.")
    return text


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_MONEY = r"-?\$[\d,]+\.\d\d"


def money(text: str) -> float:
    """'-$1,459.44' -> -1459.44."""
    return round(float(text.replace("$", "").replace(",", "")), 2)


def squash(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _us_date(text: str) -> date:
    m, d, y = (int(x) for x in text.split("/"))
    return date(y, m, d)


def infer_date(mmdd: str, start: date, end: date) -> str:
    """Add the year to an MM/DD found on a statement covering start..end (may span New Year)."""
    month, day = (int(x) for x in mmdd.split("/"))
    year = start.year if start.year == end.year or month >= start.month else end.year
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        raise StatementError(f"Invalid date {mmdd} on statement.")


def _check(label: str, expected: Optional[float], found: float) -> Dict[str, Any]:
    ok = expected is not None and abs(expected - found) < 0.005
    return {"label": label, "expected": expected, "found": round(found, 2), "ok": ok}


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------


class StatementParser:
    name = ""
    account_type = "Credit / Debit"

    def detect(self, text: str) -> bool:
        raise NotImplementedError

    def parse(self, text: str) -> Dict[str, Any]:
        """Return {account_last4, period_start, period_end, rows, checks}.

        Each row: {date (ISO), ref, description, amount, detail}.
        """
        raise NotImplementedError


class SynchronyParser(StatementParser):
    """Synchrony Bank cards (Sam's Club, Amazon, PayPal and others using the same layout)."""

    name = "Synchrony"

    _ROW = re.compile(rf"^(\d\d/\d\d)\s+([A-Z0-9]{{10,}})\s+(.+?)\s+({_MONEY})$")
    # Interest and fee lines have no reference number.
    _FEE = re.compile(rf"^(\d\d/\d\d)\s+((?:INTEREST CHARGE|LATE FEE|RETURNED PAYMENT FEE|ANNUAL FEE|FEE)\b.*?)\s+({_MONEY})$")
    _CONTINUES = re.compile(r"^\(Continued on next page\)$|^Transaction Detail \(Continued\)$", re.I)

    def detect(self, text: str) -> bool:
        return bool(re.search(r"synchrony|syncb", text, re.I)) and "Transaction Detail" in text

    def parse(self, text: str) -> Dict[str, Any]:
        cycle = re.search(r"Billing Cycle from (\d\d/\d\d/\d{4}) to (\d\d/\d\d/\d{4})", text)
        if not cycle:
            raise StatementError("Synchrony statement found, but not its billing cycle dates.")
        start, end = _us_date(cycle.group(1)), _us_date(cycle.group(2))
        last4 = re.search(r"Account Number ending in\s+(\d{4})", text)

        start_at = text.index("Transaction Detail")
        rows: List[Dict[str, Any]] = []
        payments_total = purchases_total = None
        previous: Optional[Dict[str, Any]] = None
        for line in (squash(l) for l in text[start_at:].splitlines()):
            if not line:
                continue
            if line.startswith("Interest Charge Calculation"):
                break
            if self._CONTINUES.match(line):
                previous = None
                continue
            m = re.match(rf"^Payments\s+({_MONEY})$", line)
            if m:
                payments_total = money(m.group(1))
                continue
            m = re.match(rf"^Purchases and Other Debits\s+({_MONEY})$", line)
            if m:
                purchases_total = money(m.group(1))
                continue
            m = self._ROW.match(line)
            if m:
                # The statement prints charges as positive and payments as negative.
                previous = {
                    "date": infer_date(m.group(1), start, end), "ref": m.group(2),
                    "description": squash(m.group(3)), "amount": -money(m.group(4)), "detail": "",
                }
                rows.append(previous)
                continue
            m = self._FEE.match(line)
            if m:
                amount = money(m.group(3))
                previous = None
                if amount:
                    rows.append({
                        "date": infer_date(m.group(1), start, end), "ref": "",
                        "description": squash(m.group(2)), "amount": -amount, "detail": "",
                    })
                continue
            # Itemised lines under Sam's Club / Walmart purchases; kept as a note on the row.
            if previous is not None and not line.startswith(("Total ", "Date ")):
                previous["detail"] = squash(f"{previous['detail']} {line}".lstrip(", "))

        refd = [r for r in rows if r["ref"]]
        money_in = sum(r["amount"] for r in refd if r["amount"] > 0)
        money_out = -sum(r["amount"] for r in refd if r["amount"] < 0)
        checks = [_check("Purchases and other debits", purchases_total, money_out)]
        # A statement with no payments prints no Payments subtotal; that is fine if none were found.
        if payments_total is not None or money_in:
            checks.append(_check("Payments", None if payments_total is None else -payments_total, money_in))
        return {
            "account_last4": last4.group(1) if last4 else "",
            "period_start": start.isoformat(), "period_end": end.isoformat(),
            "rows": rows, "checks": checks,
        }


PARSERS: List[StatementParser] = [SynchronyParser()]


def supported_banks() -> List[str]:
    return [p.name for p in PARSERS]


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


def parse_statement_text(text: str) -> Dict[str, Any]:
    parser = next((p for p in PARSERS if p.detect(text)), None)
    if parser is None:
        raise StatementError(
            "This PDF isn't a statement format EVBudget knows yet. Supported: " + ", ".join(supported_banks()) + "."
        )
    result = parser.parse(text)
    warnings = [
        f"{c['label']}: statement says {c['expected']:.2f} but the rows add up to {c['found']:.2f}"
        if c["expected"] is not None else f"{c['label']}: statement total not found, could not verify"
        for c in result["checks"] if not c["ok"]
    ]
    if not result["rows"]:
        warnings.append("No transactions found in this statement.")
    result.update(
        bank=parser.name, account_type=parser.account_type, warnings=warnings,
        reconciled=bool(result["rows"]) and not warnings,
    )
    return result


def parse_statement(path: str) -> Dict[str, Any]:
    return parse_statement_text(extract_text(path))
