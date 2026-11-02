"""Credit card statement PDFs: pull out just the transactions.

Text is extracted with pypdf (pure Python). GenericParser reads every statement by looking for what they all have:
a statement period, an account number, dated lines ending in an amount, and the totals the statement
prints (purchases, payments and credits, previous and new balance). It works out which sign convention
the statement uses by checking the rows against those printed totals, so no bank-specific code is needed.
PARSERS can still hold a bank-specific parser, tried first, for a statement the generic one cannot read.

Every parser checks its rows against the totals the statement prints. A statement that doesn't add up
is still returned, but with a warning, so a missed or misread row is never silent.

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

def money(text: str) -> float:
    """'-$1,459.44' -> -1459.44, '.10' -> 0.1."""
    return round(float(text.replace("$", "").replace(",", "")), 2)


def squash(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


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


# ---------------------------------------------------------------------------
# Generic: any statement that shows what we expect to find
# ---------------------------------------------------------------------------

_AMOUNT = r"[+-]?\$?(?:[\d,]*\d)?\.\d\d"
_DATE_FULL = r"\d\d/\d\d/(?:\d{4}|\d\d)"
# A transaction line: a date, optionally a second (posting) date, an optional marker, the description, then the amount.
_GEN_ROW = re.compile(rf"^(\d\d/\d\d)(?:/(\d{{4}}|\d\d))?(?:\s+\d\d/\d\d)?\s+(?:[&*#]\s+)?(.+?)\s+({_AMOUNT})$")
_GEN_TOTAL = re.compile(rf"^(.*?[A-Za-z].*?)[\s:]+({_AMOUNT})\s*$")
_GEN_REF = re.compile(r"^([A-Z0-9]{12,})\s+(\S.*)$")   # a reference number some banks print before the description
_GEN_DETAIL = re.compile(r"^(Order Number|Reference|Ref|Confirmation|Invoice|Item)\b.*$", re.I)
_PERIOD = [
    re.compile(rf"({_DATE_FULL})\s*(?:-|\u2013|to|through|thru)\s*({_DATE_FULL})", re.I),
]
_END_DATE = re.compile(rf"(?:Statement|Closing) Date:?\s*({_DATE_FULL})", re.I)
_LAST4 = [
    re.compile(r"(?:account|acct\.?|card)(?: number| no\.?| #)?[^\n\d]{0,12}ending(?: in)?\s*(\d{4})\b", re.I),
    re.compile(r"(?:account|acct\.?|card)\s*(?:number|no\.?|#)?:?\s*(?:[Xx*\u2022]+[\s-]*){1,4}(\d{4})\b", re.I),
    re.compile(r"\bending in\s+(\d{4})\b", re.I),
]
# Banks recognised by name, only to label the account ("Chase 3855"). An unknown bank is still read.
_BANK_NAMES = {
    "Chase": r"\bchase\b", "Citi": r"\bciti(?:bank|cards?)?\b", "Capital One": r"capital\s*one", "Discover": r"\bdiscover\b",
    "American Express": r"american express|\bamex\b", "Bank of America": r"bank of america|bankofamerica",
    "Wells Fargo": r"wells\s*fargo", "Barclays": r"\bbarclays\b", "U.S. Bank": r"\bu\.?s\.? bank\b|usbank",
    "Synchrony": r"synchrony|syncb", "PNC": r"\bpnc\b", "Credit One": r"credit one", "Apple Card": r"apple card",
}
_DEBIT_WORDS = re.compile(r"purchase|debit|charge|spend|new transactions", re.I)
_CREDIT_WORDS = re.compile(r"payment|credit", re.I)
_OTHER_DEBIT_WORDS = re.compile(r"fee|interest|cash advance|balance transfer", re.I)


def _any_date(text: str) -> date:
    m, d, y = text.split("/")
    year = int(y)
    return date(year + 2000 if year < 100 else year, int(m), int(d))


def _dated(mmdd: str, year: Optional[str], end: date) -> str:
    """The date of a line. With no year printed, it is the latest such date not after the statement's end."""
    month, day = (int(x) for x in mmdd.split("/"))
    if year:
        y = int(year)
        y = y + 2000 if y < 100 else y
    else:
        y = end.year if (month, day) <= (end.month, end.day) else end.year - 1
    try:
        return date(y, month, day).isoformat()
    except ValueError:
        raise StatementError(f"Invalid date {mmdd} on statement.")


class GenericParser(StatementParser):
    """Any statement with dated lines, an amount on each, and printed totals to check them against."""

    name = "Statement"

    def detect(self, text: str) -> bool:
        return any(_GEN_ROW.match(squash(l)) for l in text.splitlines())

    def _period(self, text: str) -> tuple:
        for pattern in _PERIOD:
            for m in pattern.finditer(text):
                try:
                    a, b = _any_date(m.group(1)), _any_date(m.group(2))
                except ValueError:
                    continue
                if a < b and (b - a).days <= 100:
                    return a, b
        m = _END_DATE.search(text)
        if m:
            try:
                end = _any_date(m.group(1))
                return date.fromordinal(end.toordinal() - 30), end
            except ValueError:
                pass
        raise StatementError("Found what look like transactions, but not the statement's dates (a billing period or statement date).")

    @staticmethod
    def _bank(text: str) -> str:
        counts = {n: len(re.findall(rx, text, re.I)) for n, rx in _BANK_NAMES.items()}
        best = max(counts, key=lambda n: counts[n])
        return best if counts[best] else GenericParser.name

    @staticmethod
    def _balance_total(totals: List[tuple], label: str) -> Optional[float]:
        for lab, value in totals:
            if re.match(label, lab.strip(), re.I):
                return value
        return None

    def parse(self, text: str) -> Dict[str, Any]:
        start, end = self._period(text)
        last4 = next((m.group(1) for rx in _LAST4 for m in [rx.search(text)] if m), "")

        rows: List[Dict[str, Any]] = []
        totals: List[tuple] = []
        previous: Optional[Dict[str, Any]] = None
        for line in (squash(l) for l in text.splitlines()):
            if not line:
                continue
            m = _GEN_ROW.match(line)
            if m:
                amount = money(m.group(4).replace("+", ""))
                ref, description = "", squash(m.group(3))
                token = _GEN_REF.match(description)
                if token and re.search(r"\d", token.group(1)) and re.search(r"[A-Z]", token.group(1)):
                    ref, description = token.group(1), token.group(2)
                if amount == 0:   # "interest charge $0.00" and the like are not transactions
                    previous = None
                    continue
                previous = {"date": _dated(m.group(1), m.group(2), end), "ref": ref, "description": description, "amount": amount, "detail": ""}
                rows.append(previous)
                continue
            if _GEN_DETAIL.match(line) and previous is not None:
                previous["detail"] = squash(f"{previous['detail']} {line}")
                continue
            t = _GEN_TOTAL.match(line)
            if t and not re.match(r"^\d\d/\d\d", line):
                totals.append((t.group(1), money(t.group(2).replace("+", ""))))
            previous = None
        if not rows:
            raise StatementError("No transaction lines (a date, a description and an amount) were found in this statement.")

        positive = round(sum(r["amount"] for r in rows if r["amount"] > 0), 2)
        negative = round(-sum(r["amount"] for r in rows if r["amount"] < 0), 2)
        charges_positive, debit_total, credit_total = self._convention(rows, totals, positive, negative)   # debit_total: (printed, rows)
        for r in rows:
            r["amount"] = -r["amount"] if charges_positive else r["amount"]
        debits, credits = (positive, negative) if charges_positive else (negative, positive)
        return {
            "account_last4": last4, "period_start": start.isoformat(), "period_end": end.isoformat(),
            "bank": self._bank(text), "rows": rows, "checks": self._checks(totals, debits, credits, debit_total, credit_total),
        }

    def _convention(self, rows: List[Dict[str, Any]], totals: List[tuple], positive: float, negative: float) -> tuple:
        """Which sign means money out, judged by which one makes the rows match the statement's printed totals."""
        best = None
        for charges_positive in (True, False):
            debits, credits = (positive, negative) if charges_positive else (negative, positive)
            found = debits
            d = self._match(totals, debits, _DEBIT_WORDS, other=True)
            if d is None:   # some statements leave fee and interest charges out of their purchases total
                found = round(debits - self._fees(rows, charges_positive), 2)
                d = self._match(totals, found, _DEBIT_WORDS, other=True)
            d = None if d is None else (d, found)
            c = self._match(totals, credits, _CREDIT_WORDS, other=False, several=True)
            score = (d is not None) + (c is not None)
            if best is None or score > best[0]:
                best = (score, charges_positive, d, c)
        if best[0] == 0:
            # No totals to go by: a payment is the one thing that is always money in.
            payments = [r["amount"] for r in rows if re.search(r"payment|thank you", r["description"], re.I)]
            charges_positive = not payments or sum(1 for a in payments if a < 0) >= len(payments) / 2
            return charges_positive, None, None
        return best[1], best[2], best[3]

    @staticmethod
    def _fees(rows: List[Dict[str, Any]], charges_positive: bool) -> float:
        """Charges that are fees or interest, as a positive amount, in the sign convention being tried."""
        return round(sum(abs(r["amount"]) for r in rows if _OTHER_DEBIT_WORDS.search(r["description"])
                         and (r["amount"] > 0) == charges_positive), 2)

    @staticmethod
    def _match(totals: List[tuple], amount: float, words: Any, other: bool = False, several: bool = False) -> Optional[float]:
        """A printed total, with a label of the right kind, that equals this sum. Purchases may be printed apart
        from fees, interest, cash advances and balance transfers, and credits apart from payments, so a few
        printed totals can add up to the sum together."""
        main = list(dict.fromkeys(abs(v) for lab, v in totals if words.search(lab) and abs(v) > 0))[:5]
        extras = list(dict.fromkeys(abs(v) for lab, v in totals if other and _OTHER_DEBIT_WORDS.search(lab) and abs(v) > 0))[:5]
        if several:   # any mix of the printed credit totals
            for mask in range(1, 1 << len(main)):
                total = sum(e for i, e in enumerate(main) if mask >> i & 1)
                if abs(total - amount) < 0.005:
                    return round(total, 2)
            return None
        for base in main or ([0.0] if abs(amount) < 0.005 else []):
            for mask in range(1 << len(extras)):
                total = base + sum(e for i, e in enumerate(extras) if mask >> i & 1)
                if abs(total - amount) < 0.005:
                    return round(total, 2)
        return None

    def _checks(self, totals: List[tuple], debits: float, credits: float,
                debit_total: Optional[tuple], credit_total: Optional[float]) -> List[Dict[str, Any]]:
        expected, found = debit_total if debit_total else (None, debits)
        checks = [_check("Purchases and other debits", expected, found)]
        if credits or credit_total is not None:
            checks.append(_check("Payments and credits", credit_total, credits))
        # The labelled totals didn't all match, but the balance still has to add up: previous - credits + debits = new.
        prev = self._balance_total(totals, r"previous balance")
        new = self._balance_total(totals, r"new balance")
        if not all(c["ok"] for c in checks) and prev is not None and new is not None:
            if abs(prev - credits + debits - new) < 0.005:
                return [_check("Previous balance - credits + purchases = new balance", new, new)]
        return checks


# Bank-specific parsers, tried before the generic one. Empty: the generic parser reads every bank we have tried
# (Synchrony, Chase). Add a class here only if a statement defeats it.
PARSERS: List[StatementParser] = []
FALLBACK = GenericParser()


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


def parse_statement_text(text: str) -> Dict[str, Any]:
    parser = next((p for p in PARSERS if p.detect(text)), None)
    if parser is None:
        if not FALLBACK.detect(text):
            raise StatementError(
                "No transactions were found in this PDF. EVBudget reads credit card statements that list each "
                "transaction on its own line with a date and an amount."
            )
        parser = FALLBACK
    result = parser.parse(text)
    warnings = [
        f"{c['label']}: statement says {c['expected']:.2f} but the rows add up to {c['found']:.2f}"
        if c["expected"] is not None else f"{c['label']}: statement total not found, could not verify"
        for c in result["checks"] if not c["ok"]
    ]
    if not result["rows"]:
        warnings.append("No transactions found in this statement.")
    result.update(
        bank=result.get("bank") or parser.name, account_type=parser.account_type, warnings=warnings,
        reconciled=bool(result["rows"]) and not warnings,
    )
    return result


def parse_statement(path: str) -> Dict[str, Any]:
    return parse_statement_text(extract_text(path))
