"""Bank file import: inspect, parse, preview, and commit.

Preview classifies every row but writes nothing. Commit inserts only rows whose
transaction ID is new. Rows already in the database are never changed, apart
from noting which file they were seen in again.
"""

import csv
import json
import os
import re
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from . import paths, rules, store
from .textutil import (
    clean_vendor_for_lookup,
    display_name,
    generate_deterministic_txn_id,
    parse_currency_amount,
    parse_date_string,
    is_iso_date,
    legacy_txn_id,
    sanitize_text,
)

# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------


def list_input_files(input_dir: Optional[str] = None) -> List[Dict[str, Any]]:
    input_dir = input_dir or paths.INPUT_DIR
    out = []
    if os.path.isdir(input_dir):
        for name in sorted(os.listdir(input_dir)):
            full = os.path.join(input_dir, name)
            if os.path.isfile(full) and name.lower().endswith(".csv"):
                st = os.stat(full)
                out.append({
                    "filename": name, "filepath": full, "size": st.st_size,
                    "modified": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d"),
                })
    return out


def save_upload(filename: str, data: bytes, input_dir: Optional[str] = None) -> str:
    """Save an uploaded file into the input folder without overwriting a different file."""
    input_dir = input_dir or paths.INPUT_DIR
    os.makedirs(input_dir, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9 ._()\-]", "_", os.path.basename(filename)) or "upload.csv"
    if not safe.lower().endswith(".csv"):
        safe += ".csv"
    target = os.path.join(input_dir, safe)
    stem, ext = os.path.splitext(safe)
    n = 1
    while os.path.exists(target):
        with open(target, "rb") as f:
            if f.read() == data:
                return target  # same file again; reuse it
        n += 1
        target = os.path.join(input_dir, f"{stem} ({n}){ext}")
    with open(target, "wb") as f:
        f.write(data)
    return target


def resolve_input_file(filename: str, input_dir: Optional[str] = None) -> str:
    """Path of a file inside the input folder, refusing anything outside it."""
    input_dir = os.path.realpath(input_dir or paths.INPUT_DIR)
    full = os.path.realpath(os.path.join(input_dir, os.path.basename(filename)))
    if os.path.dirname(full) != input_dir or not os.path.isfile(full):
        raise FileNotFoundError(filename)
    return full


# ---------------------------------------------------------------------------
# Header inspection
# ---------------------------------------------------------------------------


def header_signature(headers: List[str]) -> str:
    return "|".join(sorted(h.strip().lower() for h in headers))


def inspect_file_header(file_path: str, presets: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Read the header and a few rows, and guess the column mapping and account type."""
    try:
        with open(file_path, mode="r", encoding="utf-8-sig", errors="replace", newline="") as f:
            reader = csv.reader(f)
            header = next(reader, None)
            if not header:
                return {"valid": False, "error": "Empty file", "filepath": file_path, "filename": os.path.basename(file_path)}
            sample_rows = []
            for _ in range(5):
                row = next(reader, None)
                if row:
                    sample_rows.append(row)

        cols_lower = [c.strip().lower() for c in header]
        fname = os.path.basename(file_path).lower()

        if "reference number" in cols_lower and "posting date" in cols_lower:
            account_type = "Credit / Debit"
        elif "type" in cols_lower and "category" in cols_lower:
            account_type = "Credit / Debit"
        elif "check #" in cols_lower:
            account_type = "Bank / Checking"
        elif "checking" in fname or "check" in fname or "bank" in fname:
            account_type = "Bank / Checking"
        elif "chase" in fname or "credit" in fname or "card" in fname or "activity" in fname:
            account_type = "Credit / Debit"
        else:
            account_type = "Bank / Checking"

        date_col = name_col = amount_col = debit_col = credit_col = check_col = ref_col = type_col = ""

        date_priority = [
            "transaction date", "trans date", "trans_date", "transaction_date",
            "post date", "posting date", "date",
        ]
        for term in date_priority:
            if not date_col:
                for idx, col in enumerate(cols_lower):
                    if term == col or (term in col and "update" not in col):
                        date_col = header[idx]
                        break

        for idx, col in enumerate(cols_lower):
            orig = header[idx]
            if not name_col and any(t in col for t in ["description", "payee", "merchant", "name"]):
                name_col = orig
            if any(t == col or "debit" in col for t in ["debit", "withdrawal"]):
                debit_col = orig
            if any(t == col or "credit" in col for t in ["credit", "deposit"]):
                credit_col = orig
            if not amount_col and any(t in col for t in ["amount", "total"]):
                amount_col = orig
            if not check_col and "check" in col:
                check_col = orig
            if not ref_col and any(t in col for t in ["reference", "ref #", "ref number", "txn id", "fitid"]):
                ref_col = orig
            if not type_col and col == "type":
                type_col = orig

        is_dual = bool(debit_col and credit_col)
        is_mapped = bool(date_col and name_col and (amount_col or is_dual))

        sign_mode = "standard"
        if not is_dual and amount_col and amount_col in header and account_type == "Credit / Debit":
            amt_idx = header.index(amount_col)
            non_pmt = []
            for s_row in sample_rows:
                if len(s_row) > amt_idx:
                    text = " ".join(s_row).upper()
                    if "PAYMENT" not in text and "AUTOPAY" not in text:
                        val = parse_currency_amount(s_row[amt_idx])
                        if val != 0.0:
                            non_pmt.append(val)
            if non_pmt and all(a > 0 for a in non_pmt):
                sign_mode = "inverted"

        info = {
            "valid": True,
            "filename": os.path.basename(file_path),
            "filepath": file_path,
            "headers": header,
            "sample_rows": sample_rows,
            "account_type": account_type,
            "is_mapped": is_mapped,
            "from_preset": False,
            "mapping": {
                "date_col": date_col, "name_col": name_col, "amount_col": amount_col,
                "debit_col": debit_col, "credit_col": credit_col, "check_col": check_col,
                "ref_col": ref_col, "type_col": type_col, "is_dual": is_dual, "sign_mode": sign_mode,
            },
        }
        preset = (presets or {}).get(header_signature(header))
        if preset and isinstance(preset.get("mapping"), dict):
            info["mapping"].update(preset["mapping"])
            info["account_type"] = preset.get("account_type", info["account_type"])
            m = info["mapping"]
            info["is_mapped"] = bool(m.get("date_col") and m.get("name_col") and (m.get("amount_col") or m.get("is_dual")))
            info["from_preset"] = True
        return info
    except Exception as exc:
        return {"valid": False, "error": str(exc), "filepath": file_path, "filename": os.path.basename(file_path)}


def load_presets(conn: Any) -> Dict[str, Any]:
    try:
        data = json.loads(store.get_setting(conn, "import_column_presets") or "{}")
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


def save_preset(conn: Any, headers: List[str], mapping: Dict[str, Any], account_type: str) -> None:
    presets = load_presets(conn)
    presets[header_signature(headers)] = {"mapping": mapping, "account_type": account_type}
    store.set_setting(conn, "import_column_presets", json.dumps(presets))


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def parse_file(file_info: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], int]:
    """Read one bank file into raw rows with IDs. Returns (rows, skipped_count)."""
    file_path = file_info["filepath"]
    mapping = file_info["mapping"]
    account_type = file_info.get("account_type", "Bank / Checking")

    rows: List[Dict[str, Any]] = []
    skipped = 0
    occurrence_counts: Dict[str, int] = {}

    with open(file_path, mode="r", encoding="utf-8-sig", errors="replace", newline="") as f:
        for row in csv.DictReader(f):
            raw_date = row.get(mapping.get("date_col", ""), "") or ""
            raw_name = row.get(mapping.get("name_col", ""), "") or ""
            raw_check = row.get(mapping.get("check_col", ""), "") or ""
            raw_ref = row.get(mapping.get("ref_col", ""), "") or ""
            raw_type = row.get(mapping.get("type_col", ""), "") or ""
            if not raw_date.strip() or not raw_name.strip():
                continue

            if mapping.get("is_dual"):
                debit = parse_currency_amount(row.get(mapping.get("debit_col", ""), ""))
                credit = parse_currency_amount(row.get(mapping.get("credit_col", ""), ""))
                amount = -abs(debit) if debit != 0.0 else abs(credit) if credit != 0.0 else 0.0
            else:
                amount = parse_currency_amount(row.get(mapping.get("amount_col", ""), ""))
                if mapping.get("sign_mode") == "inverted":
                    amount = -amount

            date_iso, dt_obj = parse_date_string(raw_date)
            if dt_obj == datetime.min:
                skipped += 1
                continue

            check_num = raw_check.strip()
            if not check_num:
                m = re.search(r"\bCHECK\s*#?\s*(\d+)\b", raw_name.upper())
                if m:
                    check_num = m.group(1)

            occ_key = f"{date_iso}|{sanitize_text(raw_name).upper()}|{amount:.2f}|{account_type.upper()}"
            occurrence = occurrence_counts.get(occ_key, 0) + 1
            occurrence_counts[occ_key] = occurrence

            txn_id = generate_deterministic_txn_id(raw_date, raw_name, amount, account_type, raw_ref, occurrence)
            has_bank_ref = len(re.sub(r"[^A-Za-z0-9]", "", raw_ref.strip())) >= 6
            legacy_id = None if has_bank_ref else legacy_txn_id(raw_date, raw_name, amount, account_type, occurrence)
            rows.append({
                "txn_id": txn_id, "legacy_id": legacy_id, "date": date_iso, "raw_name": raw_name, "amount": round(amount, 2),
                "check_num": check_num, "account_type": account_type, "trans_type": raw_type,
                "source_file": os.path.basename(file_path),
            })
    return rows, skipped


# ---------------------------------------------------------------------------
# Preview and commit
# ---------------------------------------------------------------------------

RECORD_FIELDS = [
    "txn_id", "date", "name", "raw_name", "amount", "envelope", "check_num", "account_type", "source_file",
    "is_included", "exclusion_reason", "match_source", "txn_kind", "income_source_id", "person_id",
    "income_subtype", "income_label", "needs_review", "reimburses_txn_id",
]


def _existing_rows(conn: Any, ids: List[str]) -> Dict[str, Dict[str, Any]]:
    found: Dict[str, Dict[str, Any]] = {}
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        marks = ",".join("?" for _ in chunk)
        for r in conn.execute(f"SELECT * FROM transactions WHERE txn_id IN ({marks})", chunk):
            found[r["txn_id"]] = dict(r)
    return found


def _deleted_ids(conn: Any, ids: List[str]) -> set:
    found = set()
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        marks = ",".join("?" for _ in chunk)
        found.update(r[0] for r in conn.execute(f"SELECT txn_id FROM deleted_transactions WHERE txn_id IN ({marks})", chunk))
    return found


def build_preview(conn: Any, file_infos: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Parse and classify the chosen files. Nothing is written.

    Each row is one of: new, existing (already in the database, including rows saved
    under an older ID formula), or deleted (removed earlier, so not brought back).
    """
    parsed: List[Dict[str, Any]] = []
    seen = set()
    skipped = 0
    duplicates_in_files = 0
    for fi in file_infos:
        if not fi.get("is_mapped") or not os.path.isfile(fi.get("filepath", "")):
            continue
        file_rows, bad = parse_file(fi)
        skipped += bad
        for row in file_rows:
            if row["txn_id"] in seen:
                duplicates_in_files += 1
                continue
            seen.add(row["txn_id"])
            parsed.append(row)

    all_ids = [r["txn_id"] for r in parsed] + [r["legacy_id"] for r in parsed if r.get("legacy_id")]
    existing = _existing_rows(conn, all_ids)
    tombstones = _deleted_ids(conn, all_ids)

    matched: Dict[str, str] = {}  # parsed txn_id -> id of the stored row
    for r in parsed:
        if r["txn_id"] in existing:
            matched[r["txn_id"]] = r["txn_id"]
        elif r.get("legacy_id") and r["legacy_id"] in existing:
            matched[r["txn_id"]] = r["legacy_id"]

    ctx = rules.load_context(conn)
    to_classify = sorted((r for r in parsed if r["txn_id"] not in matched), key=lambda r: (r["date"], r["amount"], r["txn_id"]))
    records: List[Dict[str, Any]] = []
    for r in to_classify:
        payee = display_name(r["raw_name"], r["check_num"])
        res = rules.classify(
            ctx, raw_name=r["raw_name"], payee=payee, amount=r["amount"], date_iso=r["date"],
            check_num=r["check_num"], trans_type=r["trans_type"],
        )
        if res["txn_kind"] == "expense" and res["is_included"] and res["envelope"]:
            ctx.expenses.add(r["date"], res["name"], res["envelope"])
        was_deleted = r["txn_id"] in tombstones or (r.get("legacy_id") in tombstones)
        rec = {k: res.get(k) for k in RECORD_FIELDS if k in res}
        rec.update(
            txn_id=r["txn_id"], date=r["date"], raw_name=r["raw_name"], amount=r["amount"],
            check_num=r["check_num"], account_type=r["account_type"], source_file=r["source_file"],
            is_included=bool(res["is_included"]), status="deleted" if was_deleted else "new", edited=False,
        )
        records.append(rec)

    for r in parsed:
        stored_id = matched.get(r["txn_id"])
        if stored_id:
            e = existing[stored_id]
            rec = {k: e.get(k) for k in RECORD_FIELDS}
            rec.update(
                is_included=bool(e.get("is_included")), status="existing", edited=False,
                seen_in_file=r["source_file"],
            )
            records.append(rec)

    records.sort(key=lambda r: (r["date"], r["txn_id"]), reverse=True)
    counts = {s: sum(1 for r in records if r["status"] == s) for s in ("new", "existing", "deleted")}
    return {
        "records": records,
        "summary": {
            "total": len(records),
            "new": counts["new"],
            "existing": counts["existing"],
            "deleted": counts["deleted"],
            "excluded_new": sum(1 for r in records if r["status"] == "new" and not r["is_included"]),
            "needs_review_new": sum(1 for r in records if r["status"] == "new" and r.get("needs_review")),
            "skipped_bad_date": skipped,
            "duplicates_across_files": duplicates_in_files,
        },
    }


def _clean_record(r: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Validate a record sent back from the review grid. Returns None if it can't be saved."""
    txn_id = str(r.get("txn_id") or "").strip()
    date = str(r.get("date") or "").strip()
    if not txn_id or not is_iso_date(date):
        return None
    try:
        amount = round(float(r.get("amount")), 2)
    except (TypeError, ValueError):
        return None
    if amount != amount or abs(amount) == float("inf"):
        return None

    kind = r.get("txn_kind")
    if kind not in rules.KINDS:
        kind = "expense" if amount < 0 else "other_income"
    included = bool(r.get("is_included"))
    reason = str(r.get("exclusion_reason") or "").strip()
    if kind in rules.EXCLUDED_KINDS:
        included = False
    if not included and not reason:
        reason = "Internal Account Transfer" if kind == "transfer" else "Credit Card Payment / Autopay" if kind == "cc_payment" else "User Excluded"
    if included:
        reason = ""

    out = {
        "txn_id": txn_id, "date": date, "amount": amount, "txn_kind": kind,
        "name": str(r.get("name") or r.get("raw_name") or "").strip() or "(no description)",
        "raw_name": str(r.get("raw_name") or ""),
        "envelope": str(r.get("envelope") or "").strip(),
        "check_num": str(r.get("check_num") or "").strip(),
        "account_type": str(r.get("account_type") or "").strip(),
        "source_file": str(r.get("source_file") or "").strip(),
        "seen_in_file": str(r.get("seen_in_file") or "").strip(),
        "restore": bool(r.get("restore")),
        "is_included": 1 if included else 0,
        "exclusion_reason": reason,
        "match_source": str(r.get("match_source") or "").strip(),
        "needs_review": 1 if r.get("needs_review") else 0,
        "user_modified": 1 if r.get("edited") else 0,
        "reimburses_txn_id": (str(r.get("reimburses_txn_id")).strip() or None) if r.get("reimburses_txn_id") else None,
        "income_label": (str(r.get("income_label") or "").strip() or None),
        "income_source_id": None, "person_id": None, "income_subtype": None,
    }
    if kind == "paycheck":
        out["income_source_id"] = int(r["income_source_id"]) if r.get("income_source_id") else None
        out["person_id"] = int(r["person_id"]) if r.get("person_id") else None
        out["income_subtype"] = r.get("income_subtype") if r.get("income_subtype") in ("regular", "bonus") else "regular"
    elif kind == "other_income" and r.get("person_id"):
        out["person_id"] = int(r["person_id"])
    return out


def commit_import(conn: Any, records: List[Dict[str, Any]], file_names: List[str]) -> Dict[str, Any]:
    """Insert new rows. Existing rows only get last_seen_at and the file name noted."""
    now = datetime.now().isoformat()
    inserted = existing = rejected = excluded = review = skipped_deleted = 0
    try:
        for raw in records:
            rec = _clean_record(raw)
            if rec is None:
                rejected += 1
                continue
            row = conn.execute("SELECT source_file FROM transactions WHERE txn_id = ?", (rec["txn_id"],)).fetchone()
            if row is not None:
                files = [f.strip() for f in (row[0] or "").split(",") if f.strip()]
                seen_in = rec["seen_in_file"] or rec["source_file"]
                if seen_in and seen_in not in files:
                    files.append(seen_in)
                conn.execute(
                    "UPDATE transactions SET last_seen_at = ?, source_file = ? WHERE txn_id = ?",
                    (now, ", ".join(files), rec["txn_id"]),
                )
                existing += 1
                continue
            if conn.execute("SELECT 1 FROM deleted_transactions WHERE txn_id = ?", (rec["txn_id"],)).fetchone():
                if not rec["restore"]:
                    skipped_deleted += 1
                    continue
                conn.execute("DELETE FROM deleted_transactions WHERE txn_id = ?", (rec["txn_id"],))
            conn.execute(
                """INSERT INTO transactions (
                       txn_id, date, name, raw_name, amount, envelope, check_num, account_type, source_file,
                       is_included, exclusion_reason, match_source, updated_at, txn_kind, income_source_id,
                       person_id, income_subtype, income_label, needs_review, user_modified,
                       reimburses_txn_id, first_imported_at, last_seen_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    rec["txn_id"], rec["date"], rec["name"], rec["raw_name"], rec["amount"], rec["envelope"],
                    rec["check_num"], rec["account_type"], rec["source_file"], rec["is_included"],
                    rec["exclusion_reason"], rec["match_source"], now, rec["txn_kind"], rec["income_source_id"],
                    rec["person_id"], rec["income_subtype"], rec["income_label"], rec["needs_review"],
                    rec["user_modified"], rec["reimburses_txn_id"], now, now,
                ),
            )
            inserted += 1
            excluded += 0 if rec["is_included"] else 1
            review += rec["needs_review"]
        cur = conn.execute(
            """INSERT INTO import_batches (created_at, files, rows_total, rows_new, rows_existing, rows_excluded, rows_needs_review)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (now, json.dumps(file_names), len(records), inserted, existing, excluded, review),
        )
        batch_id = cur.lastrowid
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    total = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    return {
        "inserted": inserted, "existing": existing, "rejected": rejected, "excluded": excluded,
        "skipped_deleted": skipped_deleted,
        "needs_review": review, "batch_id": batch_id, "total_in_store": total,
    }


# ---------------------------------------------------------------------------
# Online merchant lookup (on demand, from the review grid)
# ---------------------------------------------------------------------------


def lookup_vendor_online(vendor_query: str) -> Dict[str, Any]:
    cleaned = clean_vendor_for_lookup(vendor_query)
    url = (
        "https://api.duckduckgo.com/?q=" + urllib.parse.quote(f"{cleaned} business category")
        + "&format=json&no_html=1&skip_disambig=1"
    )
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "EVBudget/3.0"})
        with urllib.request.urlopen(req, timeout=4) as response:
            data = json.loads(response.read().decode("utf-8"))
        abstract = data.get("AbstractText", "")
        text = f"{data.get('Heading', '')} {abstract}".lower()
        snippet = abstract[:100]
        checks = [
            (["restaurant", "food", "cafe", "grill", "bakery", "bar", "pizza", "dining"], "Restaurant", "High"),
            (["gas station", "fuel", "petroleum", "convenience store"], "Gas", "High"),
            (["supermarket", "grocery", "produce", "meat market"], "Groceries", "High"),
            (["software", "streaming", "subscription", "cloud", "telecom"], "Bills: Subscriptions", "Medium"),
            (["utility", "electric", "power", "water", "waste", "sanitation"], "Bills: Every Month", "High"),
            (["clinic", "dental", "ortho", "medical", "hospital", "doctor"], "Bills: One Time", "Medium"),
        ]
        for words, envelope, confidence in checks:
            if any(w in text for w in words):
                return {"envelope": envelope, "confidence": confidence, "snippet": snippet}
        return {"envelope": "Shopping", "confidence": "Low", "snippet": snippet or "No conclusive category found"}
    except Exception:
        return {"envelope": "Shopping", "confidence": "None", "snippet": "Lookup timed out or unavailable"}
