"""Accounts: which card or account each imported transaction came from.

A name is chosen on each import (suggested from the file name, or from the last four digits on a PDF statement)
and remembered, so the next month's file for the same card gets the same name. Transactions imported before
names existed are grouped by file here, so they can be named in one pass.

Accounts are a managed list (the accounts table): add, rename, change type, merge, delete. A transaction keeps the
account's name as text, so renaming or merging rewrites it. No balances are tracked. Any name that appears on a
transaction (from an import or an older budget) is added to the list the next time it is read.
"""

import json
import os
import re
from datetime import datetime
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
    adopt_names(conn)
    return [r[0] for r in conn.execute("SELECT name FROM accounts ORDER BY name COLLATE NOCASE")]


# ---------------------------------------------------------------------------
# The managed list of accounts
# ---------------------------------------------------------------------------

ACCOUNT_TYPES = ("Bank / Checking", "Credit / Debit", "Savings", "Other")
DEFAULT_TYPE = "Bank / Checking"


def adopt_names(conn: Any) -> None:
    """Add any account name that is on a transaction but not in the list yet. Its type is the one most of its
    transactions carry."""
    best: Dict[str, Any] = {}
    for r in conn.execute(
        """SELECT account_name AS n, account_type AS t, COUNT(*) AS c FROM transactions
           WHERE account_name IS NOT NULL AND account_name != '' GROUP BY account_name, account_type""").fetchall():
        if r["t"] in ACCOUNT_TYPES and (r["n"] not in best or r["c"] > best[r["n"]][1]):
            best[r["n"]] = (r["t"], r["c"])
        else:
            best.setdefault(r["n"], (DEFAULT_TYPE, 0))
    now = datetime.now().isoformat()
    for name, (kind, _) in best.items():
        conn.execute("INSERT OR IGNORE INTO accounts (name, account_type, created_at) VALUES (?, ?, ?)", (name, kind, now))


def list_accounts(conn: Any) -> List[Dict[str, Any]]:
    """Every account with how much is in it and which import files it came from."""
    adopt_names(conn)
    stats = {r["n"].lower(): r for r in conn.execute(
        """SELECT account_name AS n, COUNT(*) AS c, MIN(date) AS a, MAX(date) AS b FROM transactions
           WHERE account_name IS NOT NULL AND account_name != '' GROUP BY account_name""")}
    files: Dict[str, set] = {}
    for r in conn.execute("SELECT DISTINCT account_name AS n, source_file AS f FROM transactions WHERE account_name IS NOT NULL AND account_name != ''"):
        for f in (r["f"] or "").split(","):
            if f.strip():
                files.setdefault(r["n"].lower(), set()).add(f.strip())
    out = []
    for a in conn.execute("SELECT id, name, account_type FROM accounts ORDER BY name COLLATE NOCASE"):
        s = stats.get(a["name"].lower())
        out.append({
            "id": a["id"], "name": a["name"], "account_type": a["account_type"],
            "transactions": s["c"] if s else 0, "first_date": s["a"] if s else None, "last_date": s["b"] if s else None,
            "files": sorted(files.get(a["name"].lower(), ())),
        })
    return out


def _clean_type(value: Any) -> str:
    return value if value in ACCOUNT_TYPES else DEFAULT_TYPE


def _find(conn: Any, name: str) -> Any:
    return conn.execute("SELECT id, name FROM accounts WHERE name = ?", (name,)).fetchone()


def _rewrite_name(conn: Any, old: str, new: str) -> None:
    """Move everything that points at an account's old name to its new one."""
    conn.execute("UPDATE transactions SET account_name = ? WHERE account_name = ? COLLATE NOCASE", (new, old))
    conn.execute("UPDATE OR IGNORE card_bill_checks SET card = ? WHERE card = ? COLLATE NOCASE", (new, old))
    conn.execute("DELETE FROM card_bill_checks WHERE card = ? COLLATE NOCASE", (old,))
    memory = load_memory(conn)
    changed = False
    for k, v in list(memory.items()):
        if str(v).lower() == old.lower():
            memory[k], changed = new, True
    if changed:
        store.set_setting(conn, "import_account_names", json.dumps(memory))


def save_account(conn: Any, account_id: Optional[int], name: Any, account_type: Any) -> int:
    """Create an account, or rename it / change its type. Raises ValueError with a message the user can act on."""
    name = clean_name(name)
    if not name:
        raise ValueError("Give the account a name.")
    kind = _clean_type(account_type)
    clash = _find(conn, name)
    if account_id is None:
        if clash:
            raise ValueError(f"There is already an account called '{clash['name']}'.")
        return conn.execute("INSERT INTO accounts (name, account_type, created_at) VALUES (?, ?, ?)",
                            (name, kind, datetime.now().isoformat())).lastrowid
    row = conn.execute("SELECT id, name FROM accounts WHERE id = ?", (account_id,)).fetchone()
    if row is None:
        raise ValueError("That account no longer exists.")
    if clash and clash["id"] != account_id:
        raise ValueError(f"There is already an account called '{clash['name']}'. Use Merge to combine them.")
    if row["name"] != name:
        _rewrite_name(conn, row["name"], name)
    conn.execute("UPDATE accounts SET name = ?, account_type = ? WHERE id = ?", (name, kind, account_id))
    conn.execute("UPDATE transactions SET account_type = ? WHERE account_name = ? COLLATE NOCASE", (kind, name))
    return account_id


def delete_account(conn: Any, account_id: int, merge_into: Optional[int] = None) -> int:
    """Remove an account. One that has transactions must be merged into another account, so none are left behind.
    Returns how many transactions moved."""
    row = conn.execute("SELECT id, name FROM accounts WHERE id = ?", (account_id,)).fetchone()
    if row is None:
        raise ValueError("That account no longer exists.")
    count = conn.execute("SELECT COUNT(*) FROM transactions WHERE account_name = ? COLLATE NOCASE", (row["name"],)).fetchone()[0]
    moved = 0
    if count:
        target = conn.execute("SELECT id, name, account_type FROM accounts WHERE id = ?", (merge_into,)).fetchone() if merge_into else None
        if target is None or target["id"] == account_id:
            raise ValueError(f"'{row['name']}' has {count} transaction{'' if count == 1 else 's'}. Choose another account to merge them into.")
        _rewrite_name(conn, row["name"], target["name"])
        conn.execute("UPDATE transactions SET account_type = ? WHERE account_name = ? COLLATE NOCASE", (target["account_type"], target["name"]))
        moved = count
    conn.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
    return moved


def assign_account(conn: Any, name: Any, account_type: str = DEFAULT_TYPE) -> str:
    """The account a transaction is being moved to; created if it is new. An empty name means no account."""
    name = clean_name(name)
    if not name:
        return ""
    existing = _find(conn, name)
    if existing:
        return existing["name"]
    conn.execute("INSERT INTO accounts (name, account_type, created_at) VALUES (?, ?, ?)",
                 (name, _clean_type(account_type), datetime.now().isoformat()))
    return name


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
