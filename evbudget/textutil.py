"""Text, date, and ID helpers shared by the importer and the rules engine."""

import hashlib
import html
import re
from datetime import datetime
from typing import Tuple


def sanitize_text(text: str) -> str:
    """Unescape HTML, turn commas into spaces, and collapse whitespace.

    This exact behavior feeds transaction ID hashing. Changing it would give
    every existing transaction a new ID, so do not change it.
    """
    if not text:
        return ""
    cleaned = html.unescape(text)
    cleaned = cleaned.replace(",", " ")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def collapse_ws(text: str) -> str:
    """Collapse runs of whitespace to one space."""
    return re.sub(r"\s+", " ", text or "").strip()


def display_name(raw_name: str, check_num: str = "") -> str:
    """Friendly payee name for display. No length cap, commas are kept."""
    if check_num and check_num.strip():
        return f"CHECK #{check_num.strip()}"
    return collapse_ws(html.unescape(raw_name or ""))


def clean_vendor_for_lookup(raw_name: str) -> str:
    """Strip POS codes, store numbers, phone numbers, and states from a payee."""
    name = raw_name.upper()
    name = re.sub(r"^(SQ\s*\*|TST\s*\*|SP\s*\*|PY\s*\*|PAYPAL\s*\*|CHECKCARD\s*)", "", name)
    name = re.sub(r"#\s*\d+", "", name)
    name = re.sub(r"\bSTORE\s*\d+\b", "", name)
    name = re.sub(r"\b\d{3}[-.\s]\d{3}[-.\s]\d{4}\b", "", name)
    name = re.sub(r"\b[A-Z\s]{2,15}\s+(MN|WI|IA|IL|CA|NY|TX|FL|CO|WA|AZ)\b", "", name)
    name = re.sub(r"[^A-Z0-9\s&'-]", " ", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name if name else raw_name.strip()


_STOP_TOKENS = {"THE", "AND", "INC", "LLC", "CO", "COM", "PMTS", "PAYMENT"}


def vendor_key_tokens(name: str) -> Tuple[str, ...]:
    """First two meaningful alphabetic words of a payee, used to match refunds to purchases."""
    cleaned = clean_vendor_for_lookup(name or "")
    tokens = []
    for tok in re.findall(r"[A-Z]+", cleaned.upper()):
        if len(tok) >= 2 and tok not in _STOP_TOKENS:
            tokens.append(tok)
        if len(tokens) == 2:
            break
    return tuple(tokens)


DATE_FORMATS = [
    "%m/%d/%Y",
    "%Y-%m-%d",
    "%m/%d/%y",
    "%m-%d-%Y",
    "%Y/%m/%d",
    "%d-%b-%Y",
    "%d/%m/%Y",
]


def parse_date_string(date_str: str) -> Tuple[str, datetime]:
    """Parse common bank date formats. Returns (ISO YYYY-MM-DD, datetime).

    Unparseable input comes back unchanged with datetime.min.
    """
    date_clean = (date_str or "").strip()
    for fmt in DATE_FORMATS:
        try:
            dt = datetime.strptime(date_clean, fmt)
            return dt.strftime("%Y-%m-%d"), dt
        except ValueError:
            continue
    return date_clean, datetime.min


def is_iso_date(value: str) -> bool:
    """True for a real calendar date in YYYY-MM-DD form."""
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return False
    try:
        datetime.strptime(value, "%Y-%m-%d")
        return True
    except ValueError:
        return False


def to_iso_date(value: str) -> str:
    """Return an ISO date for ISO or MM/DD/YYYY input, or '' if it can't be read."""
    if is_iso_date(value):
        return value
    iso, dt = parse_date_string(value)
    return iso if dt != datetime.min else ""


def parse_currency_amount(val_str: str) -> float:
    """Strip currency symbols and commas, returning a float."""
    if not val_str:
        return 0.0
    clean = re.sub(r"[^\d.-]", "", val_str.strip())
    try:
        return float(clean) if clean else 0.0
    except ValueError:
        return 0.0


def generate_deterministic_txn_id(
    date_str: str,
    raw_payee: str,
    amount_val: float,
    account_type: str,
    raw_ref: str = "",
    occurrence_index: int = 1,
) -> str:
    """Stable ID for de-duplication across re-imports.

    Uses the bank's own reference number when it has one. Otherwise hashes the
    ISO date, sanitized raw description, amount, account class, and a counter
    for identical same-day charges. This must stay identical to the pre-3.0
    importer or existing transactions would be imported twice.
    """
    if raw_ref and raw_ref.strip():
        cleaned_ref = re.sub(r"[^A-Za-z0-9]", "", raw_ref.strip())
        if len(cleaned_ref) >= 6:
            return f"TXN-{cleaned_ref}"

    date_formatted, dt_obj = parse_date_string(date_str)
    date_key = dt_obj.strftime("%Y-%m-%d") if dt_obj != datetime.min else date_formatted.strip()
    clean_raw_name = sanitize_text(raw_payee).upper()
    amount_key = f"{round(amount_val, 2):.2f}"
    acc_key = "CREDIT" if "CREDIT" in account_type.upper() else "BANK"

    payload_parts = [date_key, clean_raw_name, amount_key, acc_key]
    if occurrence_index > 1:
        payload_parts.append(f"OCC_{occurrence_index}")

    payload = "|".join(payload_parts)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12].upper()
    return f"TXN-{digest}"


def legacy_txn_id(
    date_str: str, raw_payee: str, amount_val: float, account_type: str, occurrence_index: int = 1
) -> str:
    """ID formula used by an early build of the importer (October 2026).

    Some stored transactions carry these IDs. Matching against them keeps a
    re-import from adding those rows a second time.
    """
    _, dt_obj = parse_date_string(date_str)
    date_key = dt_obj.strftime("%m/%d/%Y") if dt_obj != datetime.min else (date_str or "").strip()
    parts = [date_key, sanitize_text(raw_payee).upper(), f"{round(amount_val, 2):.2f}", account_type.upper()]
    if occurrence_index > 1:
        parts.append(f"OCC_{occurrence_index}")
    return "TXN-" + hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:12].upper()
