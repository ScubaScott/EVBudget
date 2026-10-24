"""Card / account names: which card or account each imported transaction came from.

A name is chosen on each import (suggested from the file name, or from the last four digits on a PDF statement)
and remembered, so the next month's file for the same card gets the same name. Transactions imported before
names existed are grouped by file here, so they can be named in one pass.
"""

import json
import os
import re
from typing import Any, Dict, List, Optional

from . import pdf_statements, store

MAX_NAME_LENGTH = 60


def first_file(source_file: Optional[str]) -> str:
    """Rows seen in more than one file list them all, comma separated; the first is where they came from."""
    return (source_file or "").split(",")[0].strip()


def _stem(filename: str) -> str:
    return os.path.splitext(os.path.basename(filename))[0]


def memory_key(filename: str) -> str:
    """What stays the same from one month's file to the next: Chase1484_Activity_09.csv -> chase1484."""
    m = re.match(r"[A-Za-z0-9]+", _stem(filename))
    return m.group(0).lower() if m else ""


def suggest_name(filename: str) -> str:
    stem = _stem(filename)
    m = re.match(r"([A-Za-z]+)[ _-]?(\d{4})(?!\d)", stem)
    if m:
        return f"{m.group(1).capitalize()} {m.group(2)}"
    m = re.match(r"[A-Za-z]+", stem)
    return m.group(0).capitalize() if m else "Imported"


def pdf_key(bank: str, last4: str) -> str:
    return f"{bank}{last4}".lower().replace(" ", "")


def pdf_name(bank: str, last4: str) -> str:
    return f"{bank} {last4}" if last4 else bank


def clean_name(name: Any) -> str:
    return " ".join(str(name or "").split())[:MAX_NAME_LENGTH]


# ---------------------------------------------------------------------------
# Remembered names
# ---------------------------------------------------------------------------


def load_memory(conn: Any) -> Dict[str, str]:
    try:
        data = json.loads(store.get_setting(conn, "import_account_names") or "{}")
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


def remember(conn: Any, key: str, name: str) -> None:
    if not key or not name:
        return
    memory = load_memory(conn)
    memory[key] = name
    store.set_setting(conn, "import_account_names", json.dumps(memory))


def known_names(conn: Any) -> List[str]:
    return [r[0] for r in conn.execute(
        "SELECT DISTINCT account_name FROM transactions WHERE account_name IS NOT NULL AND account_name != '' ORDER BY account_name COLLATE NOCASE")]


def list_cards(conn: Any) -> List[Dict[str, Any]]:
    return [
        {"name": r["account_name"], "transactions": r["n"], "first_date": r["a"], "last_date": r["b"],
         "account_type": r["t"]}
        for r in conn.execute(
            """SELECT account_name, COUNT(*) n, MIN(date) a, MAX(date) b, MIN(account_type) t FROM transactions
               WHERE account_name IS NOT NULL AND account_name != '' GROUP BY account_name ORDER BY account_name COLLATE NOCASE""")
    ]


# ---------------------------------------------------------------------------
# Naming earlier imports
# ---------------------------------------------------------------------------


def _group_unnamed_files(conn: Any, input_dir: Optional[str]) -> Dict[str, Dict[str, Any]]:
    """Files whose transactions have no card name yet, grouped by what card they probably are.

    A PDF that is still in the input folder is read for its last four digits, so two cards from the same
    bank stay apart. Anything else is grouped by the start of its file name.
    """
    memory = load_memory(conn)
    groups: Dict[str, Dict[str, Any]] = {}
    rows = conn.execute(
        """SELECT source_file, MIN(account_type) AS account_type, COUNT(*) AS n, MIN(date) AS first_date, MAX(date) AS last_date
           FROM transactions WHERE account_name IS NULL OR account_name = '' GROUP BY source_file""").fetchall()
    for r in rows:
        filename = first_file(r["source_file"])
        key, suggested = memory_key(filename), suggest_name(filename)
        path = os.path.join(input_dir, os.path.basename(filename)) if input_dir and filename else ""
        if filename.lower().endswith(".pdf") and path and os.path.isfile(path):
            try:
                stmt = pdf_statements.parse_statement(path)
                key, suggested = pdf_key(stmt["bank"], stmt["account_last4"]), pdf_name(stmt["bank"], stmt["account_last4"])
            except Exception:
                pass
        g = groups.setdefault(key, {
            "key": key, "suggested": memory.get(key) or suggested, "account_type": r["account_type"],
            "source_files": [], "transactions": 0, "first_date": r["first_date"], "last_date": r["last_date"],
        })
        g["source_files"].append(r["source_file"])
        g["transactions"] += r["n"]
        g["first_date"] = min(g["first_date"], r["first_date"])
        g["last_date"] = max(g["last_date"], r["last_date"])
    return groups


def unnamed_groups(conn: Any, input_dir: Optional[str] = None) -> List[Dict[str, Any]]:
    groups = _group_unnamed_files(conn, input_dir)
    out = sorted(groups.values(), key=lambda g: -g["transactions"])
    for g in out:
        g["files"] = len(g["source_files"])
        g["example_file"] = first_file(g["source_files"][0])
        del g["source_files"]
    return out


def name_groups(conn: Any, assignments: List[Dict[str, Any]], input_dir: Optional[str] = None) -> int:
    """Give each group of earlier imports its card name. Returns how many transactions were named."""
    groups = _group_unnamed_files(conn, input_dir)
    named = 0
    for a in assignments:
        name = clean_name(a.get("name"))
        group = groups.get(str(a.get("key") or ""))
        if not name or group is None:
            continue
        for source_file in group["source_files"]:
            named += conn.execute(
                "UPDATE transactions SET account_name = ? WHERE source_file = ? AND (account_name IS NULL OR account_name = '')",
                (name, source_file),
            ).rowcount
        remember(conn, group["key"], name)
    return named
