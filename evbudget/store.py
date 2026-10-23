"""SQLite storage: schema, settings, backups, and the v1 -> v2 migration."""

import csv
import glob
import json
import os
import shutil
import sqlite3
from datetime import datetime
from typing import Any, Dict, Optional

from . import core_rules, paths

SCHEMA_VERSION = 2
MAX_BACKUPS = 10

DEFAULT_SETTINGS: Dict[str, str] = {
    "bonus_threshold_pct": "50",
    "suggest_window_spending_months": "6",
    "suggest_window_bill_months": "12",
    "bill_match_window_days": "10",
    "bill_amount_tolerance_pct": "10",
    "refund_lookback_days": "120",
    "import_column_presets": "{}",
}

# Columns the transactions table gained in schema v2: (name, DDL type/default).
NEW_TRANSACTION_COLUMNS = [
    ("txn_kind", "TEXT NOT NULL DEFAULT 'expense'"),
    ("income_source_id", "INTEGER"),
    ("person_id", "INTEGER"),
    ("income_subtype", "TEXT"),
    ("income_label", "TEXT"),
    ("bill_id", "INTEGER"),
    ("needs_review", "INTEGER NOT NULL DEFAULT 0"),
    ("user_modified", "INTEGER NOT NULL DEFAULT 0"),
    ("reimburses_txn_id", "TEXT"),
    ("first_imported_at", "TEXT"),
    ("last_seen_at", "TEXT"),
]

TRANSACTION_EXPORT_COLUMNS = [
    "txn_id", "date", "name", "raw_name", "amount", "envelope", "check_num", "account_type",
    "source_file", "is_included", "exclusion_reason", "match_source", "updated_at", "txn_kind",
    "income_source_id", "person_id", "income_subtype", "income_label", "bill_id", "needs_review",
    "user_modified", "reimburses_txn_id", "first_imported_at", "last_seen_at",
]

# ---------------------------------------------------------------------------
# Connections and settings
# ---------------------------------------------------------------------------


def connect(db_path: Optional[str] = None, read_only: bool = False) -> sqlite3.Connection:
    db_path = db_path or paths.DB_PATH
    if read_only:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
    else:
        conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def get_setting(conn: sqlite3.Connection, key: str) -> str:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    if row is not None and row[0] is not None:
        return row[0]
    return DEFAULT_SETTINGS.get(key, "")


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SCHEMA_STATEMENTS = [
    "CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT)",
    "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)",
    """CREATE TABLE IF NOT EXISTS transactions (
        txn_id TEXT PRIMARY KEY,
        date TEXT NOT NULL,
        name TEXT NOT NULL,
        raw_name TEXT,
        amount REAL NOT NULL,
        envelope TEXT,
        check_num TEXT,
        account_type TEXT,
        source_file TEXT,
        is_included INTEGER DEFAULT 1,
        exclusion_reason TEXT,
        match_source TEXT,
        updated_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS people (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL UNIQUE,
        color TEXT,
        active INTEGER NOT NULL DEFAULT 1
    )""",
    """CREATE TABLE IF NOT EXISTS income_sources (
        id INTEGER PRIMARY KEY,
        person_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        match_phrase TEXT NOT NULL,
        cadence_freq TEXT NOT NULL DEFAULT 'weekly',
        cadence_interval INTEGER NOT NULL DEFAULT 1,
        cadence_anchor_date TEXT,
        typical_amount_cents INTEGER,
        bonus_threshold_pct INTEGER,
        active INTEGER NOT NULL DEFAULT 1
    )""",
    """CREATE TABLE IF NOT EXISTS envelopes (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL UNIQUE COLLATE NOCASE,
        kind TEXT NOT NULL DEFAULT 'spending',
        rollover_mode TEXT NOT NULL DEFAULT 'reset',
        monthly_budget_cents INTEGER NOT NULL DEFAULT 0,
        goal_cents INTEGER,
        goal_date TEXT,
        sort_order INTEGER NOT NULL DEFAULT 0,
        active INTEGER NOT NULL DEFAULT 1
    )""",
    """CREATE TABLE IF NOT EXISTS bills (
        id INTEGER PRIMARY KEY,
        envelope_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        match_phrase TEXT,
        amount_mode TEXT NOT NULL DEFAULT 'fixed',
        amount_cents INTEGER NOT NULL DEFAULT 0,
        freq TEXT NOT NULL DEFAULT 'monthly',
        interval INTEGER NOT NULL DEFAULT 1,
        anchor_date TEXT,
        next_due_date TEXT,
        last_paid_date TEXT,
        last_paid_txn_id TEXT,
        amount_tolerance_pct INTEGER,
        active INTEGER NOT NULL DEFAULT 1
    )""",
    """CREATE TABLE IF NOT EXISTS allocations (
        id INTEGER PRIMARY KEY,
        income_txn_id TEXT UNIQUE,
        template_id INTEGER,
        total_cents INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'confirmed',
        created_at TEXT,
        voided_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS envelope_moves (
        id INTEGER PRIMARY KEY,
        date TEXT NOT NULL,
        from_envelope_id INTEGER,
        to_envelope_id INTEGER,
        amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
        move_type TEXT NOT NULL,
        allocation_id INTEGER,
        period_key TEXT,
        note TEXT,
        created_at TEXT,
        voided_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS fill_templates (
        id INTEGER PRIMARY KEY,
        income_source_id INTEGER NOT NULL UNIQUE,
        name TEXT,
        updated_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS fill_template_lines (
        template_id INTEGER NOT NULL,
        envelope_id INTEGER NOT NULL,
        amount_cents INTEGER NOT NULL DEFAULT 0,
        sort_order INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (template_id, envelope_id)
    )""",
    """CREATE TABLE IF NOT EXISTS rules (
        id INTEGER PRIMARY KEY,
        phrase TEXT NOT NULL,
        match_mode TEXT NOT NULL DEFAULT 'contains',
        stage TEXT NOT NULL,
        set_kind TEXT,
        set_envelope TEXT,
        set_payee TEXT,
        set_income_label TEXT,
        set_needs_review INTEGER NOT NULL DEFAULT 0,
        exclusion_reason TEXT,
        label TEXT,
        description TEXT,
        priority INTEGER NOT NULL DEFAULT 1000,
        origin TEXT NOT NULL DEFAULT 'user',
        active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS deleted_transactions (
        txn_id TEXT PRIMARY KEY,
        deleted_at TEXT,
        date TEXT,
        raw_name TEXT,
        amount REAL,
        source_file TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS audit_reviews (
        check_id TEXT NOT NULL,
        item_key TEXT NOT NULL,
        reviewed_at TEXT,
        PRIMARY KEY (check_id, item_key)
    )""",
    """CREATE TABLE IF NOT EXISTS import_batches (
        id INTEGER PRIMARY KEY,
        created_at TEXT,
        files TEXT,
        rows_total INTEGER DEFAULT 0,
        rows_new INTEGER DEFAULT 0,
        rows_existing INTEGER DEFAULT 0,
        rows_excluded INTEGER DEFAULT 0,
        rows_needs_review INTEGER DEFAULT 0
    )""",
]

INDEX_STATEMENTS = [
    "CREATE INDEX IF NOT EXISTS idx_txn_date ON transactions(date)",
    "CREATE INDEX IF NOT EXISTS idx_txn_envelope ON transactions(envelope)",
    "CREATE INDEX IF NOT EXISTS idx_txn_name ON transactions(name)",
    "CREATE INDEX IF NOT EXISTS idx_txn_kind_date ON transactions(txn_kind, date)",
    "CREATE INDEX IF NOT EXISTS idx_txn_income_source ON transactions(income_source_id, date)",
    "CREATE INDEX IF NOT EXISTS idx_txn_bill ON transactions(bill_id)",
    "CREATE INDEX IF NOT EXISTS idx_rules_stage ON rules(stage, priority)",
]


def create_schema(conn: sqlite3.Connection) -> None:
    """Create any missing tables, columns, and indexes. Safe to run repeatedly."""
    for stmt in SCHEMA_STATEMENTS:
        conn.execute(stmt)
    existing = {r[1] for r in conn.execute("PRAGMA table_info(transactions)")}
    for col, ddl in NEW_TRANSACTION_COLUMNS:
        if col not in existing:
            conn.execute(f"ALTER TABLE transactions ADD COLUMN {col} {ddl}")
    # Columns added after the first v2 release
    alloc_cols = {r[1] for r in conn.execute("PRAGMA table_info(allocations)")}
    if "voided_income_txn_id" not in alloc_cols:
        conn.execute("ALTER TABLE allocations ADD COLUMN voided_income_txn_id TEXT")
    rule_cols = {r[1] for r in conn.execute("PRAGMA table_info(rules)")}
    if "description" not in rule_cols:
        conn.execute("ALTER TABLE rules ADD COLUMN description TEXT")
    for stmt in INDEX_STATEMENTS:
        conn.execute(stmt)
    conn.commit()


def get_schema_version(conn: sqlite3.Connection) -> int:
    try:
        row = conn.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()
    except sqlite3.OperationalError:
        return 0
    return int(row[0]) if row else 0


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)).fetchone() is not None


# ---------------------------------------------------------------------------
# Backups
# ---------------------------------------------------------------------------


def backup_db(db_path: str, backup_dir: Optional[str] = None) -> str:
    """Copy the database to the backups folder and keep the newest MAX_BACKUPS."""
    backup_dir = backup_dir or os.path.join(os.path.dirname(db_path), "backups")
    os.makedirs(backup_dir, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    target = os.path.join(backup_dir, f"budget_store_{stamp}.db")
    src = sqlite3.connect(db_path)
    try:
        dst = sqlite3.connect(target)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    existing = sorted(glob.glob(os.path.join(backup_dir, "budget_store_[0-9]*_[0-9]*.db")))
    for old in existing[:-MAX_BACKUPS]:
        try:
            os.remove(old)
        except OSError:
            pass
    return target


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------


def guess_envelope_kind(name: str) -> Dict[str, str]:
    """Starting kind and rollover mode for an envelope. Names say nothing about the type: everything starts as
    spending and the owner changes it in the envelope manager."""
    return {"kind": "spending", "rollover_mode": "reset"}


def insert_rule(conn: sqlite3.Connection, rule: Dict[str, Any]) -> int:
    cur = conn.execute(
        """INSERT INTO rules (phrase, match_mode, stage, set_kind, set_envelope, set_payee, set_income_label,
                              set_needs_review, exclusion_reason, label, priority, origin, active, created_at, description)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
        (
            rule["phrase"], rule.get("match_mode", "contains"), rule["stage"], rule.get("set_kind"),
            rule.get("set_envelope"), rule.get("set_payee"), rule.get("set_income_label"),
            int(rule.get("set_needs_review") or 0), rule.get("exclusion_reason"), rule.get("label"),
            int(rule.get("priority", 1000)), rule.get("origin", "user"), datetime.now().isoformat(),
            (rule.get("description") or "").strip()[:120] or None,
        ),
    )
    return int(cur.lastrowid)


def seed_core_rules(conn: sqlite3.Connection) -> int:
    for rule in core_rules.core_rules():
        insert_rule(conn, rule)
    return len(core_rules.core_rules())


def _read_json(path: str) -> Any:
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def seed_legacy_rules(conn: sqlite3.Connection) -> Dict[str, int]:
    """Bring over learned rules and known vendors from the pre-3.0 JSON files."""
    counts = {"user": 0, "vendor": 0}
    seen = {(r[0].upper(), r[1]) for r in conn.execute("SELECT phrase, stage FROM rules")}
    user_phrases = set()

    data = _read_json(paths.LEGACY_USER_RULES_PATH) or {}
    for idx, r in enumerate(data.get("phrase_rules", [])):
        phrase = (r.get("phrase") or "").strip()
        if not phrase:
            continue
        action = r.get("action")
        has_envelope = bool((r.get("envelope") or "").strip())
        stage = (
            "include_override" if action == "include"
            else "exclude" if action == "exclude"
            else "envelope" if has_envelope
            else "payee"
        )
        user_phrases.add(phrase.upper())
        if (phrase.upper(), stage) in seen:
            continue
        seen.add((phrase.upper(), stage))
        insert_rule(conn, {
            "phrase": phrase, "stage": stage, "origin": "user", "priority": 100 + min(idx, 399),
            "set_envelope": (r.get("envelope") or "").strip() or None,
            "set_payee": (r.get("payee") or "").strip() or None,
            "set_kind": "expense" if stage == "include_override" else None,
            "exclusion_reason": "User Learned Rule" if stage == "exclude" else None,
            "label": "User Learned Rule",
        })
        counts["user"] += 1

    vendors = (_read_json(paths.LEGACY_KNOWN_VENDORS_PATH) or {}).get("vendors", {})
    for idx, (phrase, info) in enumerate(vendors.items()):
        phrase = phrase.strip()
        env = (info.get("envelope") or "").strip()
        if not phrase or not env or phrase.upper() in user_phrases:
            continue
        insert_rule(conn, {
            "phrase": phrase, "stage": "envelope", "origin": "vendor", "match_mode": "vendor",
            "priority": 500 + min(idx, 499), "set_envelope": env, "label": "Known Vendor DB",
        })
        counts["vendor"] += 1
    return counts


def seed_household(conn: sqlite3.Connection) -> None:
    """Starting people and income sources for this household. Editable afterwards."""
    if conn.execute("SELECT COUNT(*) FROM people").fetchone()[0]:
        return
    scott = conn.execute("INSERT INTO people (name) VALUES ('Scott')").lastrowid
    kendra = conn.execute("INSERT INTO people (name) VALUES ('Kendra')").lastrowid
    conn.execute(
        """INSERT INTO income_sources (person_id, name, match_phrase, cadence_freq, cadence_interval, cadence_anchor_date)
           VALUES (?, 'Cargill', 'CARGILL INCORPOR PAYROLL', 'weekly', 2, '2026-10-09')""",
        (scott,),
    )
    conn.execute(
        """INSERT INTO income_sources (person_id, name, match_phrase, cadence_freq, cadence_interval, cadence_anchor_date)
           VALUES (?, 'HealthPartners', 'HEALTHPARTNERS I PAYROLL', 'weekly', 2, '2026-10-16')""",
        (kendra,),
    )


def seed_envelopes(conn: sqlite3.Connection) -> None:
    """Create the Unallocated row and one row per envelope the household already uses."""
    conn.execute(
        "INSERT OR IGNORE INTO envelopes (name, kind, rollover_mode, sort_order) VALUES ('Unallocated', 'system', 'rollover', -1)"
    )
    data = _read_json(paths.LEGACY_ENVELOPES_PATH)
    names = list(data.get("envelopes", [])) if isinstance(data, dict) else []
    if not names:
        names = list(core_rules.DEFAULT_ENVELOPES)
    # Anything already tagged on a transaction is a real envelope too.
    if _table_exists(conn, "transactions"):
        have = {n.lower() for n in names}
        for row in conn.execute("SELECT DISTINCT TRIM(envelope) FROM transactions WHERE envelope IS NOT NULL AND TRIM(envelope) != ''"):
            if row[0].lower() not in have:
                names.append(row[0])
                have.add(row[0].lower())
    for order, name in enumerate(names):
        name = name.strip()
        if not name:
            continue
        guess = guess_envelope_kind(name)
        conn.execute(
            "INSERT OR IGNORE INTO envelopes (name, kind, rollover_mode, sort_order) VALUES (?, ?, ?, ?)",
            (name, guess["kind"], guess["rollover_mode"], order),
        )


# ---------------------------------------------------------------------------
# Migration v1 -> v2
# ---------------------------------------------------------------------------

CORE_EXCLUSION_REASONS = {
    "Credit Card Payment / Autopay", "Chase Credit Card Autopay", "Amex ACH Payment",
    "Sam's Club Card Payment", "Internal Account Transfer",
}


def _excluded_kind(reason: str, amount: float) -> str:
    low = (reason or "").lower()
    if "transfer" in low:
        return "transfer"
    if any(w in low for w in ("payment", "autopay", "ach", "card", "amex")):
        return "cc_payment"
    return "expense" if amount < 0 else "transfer"


def convert_dates_to_iso(conn: sqlite3.Connection) -> int:
    """Rewrite MM/DD/YYYY dates as YYYY-MM-DD. Rows already in ISO form are untouched."""
    cur = conn.execute(
        """UPDATE transactions
           SET date = substr(date, 7, 4) || '-' || substr(date, 1, 2) || '-' || substr(date, 4, 2)
           WHERE date GLOB '[0-9][0-9]/[0-9][0-9]/[0-9][0-9][0-9][0-9]'"""
    )
    return cur.rowcount


def backfill_transaction_kinds(conn: sqlite3.Connection) -> Dict[str, int]:
    """Give every existing transaction a kind, person, and review flag."""
    from . import rules  # imported here: rules depends on this module

    ctx = rules.load_context(conn)
    stats = {"rows": 0, "user_modified": 0, "needs_review": 0}
    rows = conn.execute("SELECT * FROM transactions ORDER BY date ASC, txn_id ASC").fetchall()
    for row in rows:
        amount = float(row["amount"])
        stored_env = (row["envelope"] or "").strip()
        stamp = row["updated_at"] or datetime.now().isoformat()
        values: Dict[str, Any] = {
            "txn_kind": "expense", "income_source_id": None, "person_id": None, "income_subtype": None,
            "income_label": None, "needs_review": 0, "user_modified": 0, "reimburses_txn_id": None,
            "envelope": stored_env, "match_source": row["match_source"],
        }
        if not row["is_included"]:
            values["txn_kind"] = _excluded_kind(row["exclusion_reason"], amount)
            if (row["exclusion_reason"] or "") not in CORE_EXCLUSION_REASONS:
                values["user_modified"] = 1
            else:
                values["envelope"] = ""  # the old importer filled in a meaningless default here
            res = None
        else:
            res = rules.classify(
                ctx, raw_name=row["raw_name"] or row["name"], payee=row["name"], amount=amount,
                date_iso=row["date"], check_num=row["check_num"] or "",
            )
        if res is None:
            pass
        elif amount < 0:
            values["txn_kind"] = "expense"
            # A stored envelope that differs from what the rules say was set by hand.
            if res["envelope"].strip().lower() != stored_env.lower() or not res["is_included"]:
                values["user_modified"] = 1
        else:
            if res["txn_kind"] == "paycheck":
                values.update(
                    txn_kind="paycheck", income_source_id=res["income_source_id"], person_id=res["person_id"],
                    income_subtype=res["income_subtype"], match_source=res["match_source"],
                )
            elif stored_env:
                # Old importer always left positives blank, so an envelope here was chosen by the user.
                values.update(txn_kind="refund", user_modified=1)
            else:
                values.update(
                    txn_kind=res["txn_kind"], envelope=res["envelope"], income_label=res["income_label"],
                    needs_review=res["needs_review"], match_source=res["match_source"],
                )
        conn.execute(
            """UPDATE transactions SET txn_kind=?, income_source_id=?, person_id=?, income_subtype=?, income_label=?,
                      needs_review=?, user_modified=?, reimburses_txn_id=?, envelope=?, match_source=?,
                      first_imported_at=COALESCE(first_imported_at, ?), last_seen_at=COALESCE(last_seen_at, ?)
               WHERE txn_id=?""",
            (
                values["txn_kind"], values["income_source_id"], values["person_id"], values["income_subtype"],
                values["income_label"], values["needs_review"], values["user_modified"], values["reimburses_txn_id"],
                values["envelope"], values["match_source"], stamp, stamp, row["txn_id"],
            ),
        )
        stats["rows"] += 1
        stats["user_modified"] += values["user_modified"]
        stats["needs_review"] += values["needs_review"]
    return stats


def _retire_legacy_files() -> None:
    for path in (paths.LEGACY_ENVELOPES_PATH, paths.LEGACY_USER_RULES_PATH, paths.LEGACY_KNOWN_VENDORS_PATH):
        if os.path.exists(path):
            target = os.path.splitext(path)[0] + ".migrated.json"
            try:
                os.replace(path, target)
            except OSError:
                pass


def _migrate(conn: sqlite3.Connection) -> Dict[str, Any]:
    report: Dict[str, Any] = {}
    create_schema(conn)
    report["dates_converted"] = convert_dates_to_iso(conn)
    if conn.execute("SELECT COUNT(*) FROM rules").fetchone()[0] == 0:
        report["core_rules"] = seed_core_rules(conn)
        report["legacy_rules"] = seed_legacy_rules(conn)
    seed_household(conn)
    seed_envelopes(conn)
    for key, value in DEFAULT_SETTINGS.items():
        conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (key, value))
    report["backfill"] = backfill_transaction_kinds(conn)
    conn.execute(
        "INSERT INTO schema_meta (key, value) VALUES ('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()
    return report


def ensure_schema(db_path: Optional[str] = None) -> Dict[str, Any]:
    """Open the database, creating or migrating it as needed.

    An existing v1 database is backed up first, and restored if the migration fails.
    """
    db_path = db_path or paths.DB_PATH
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    existed = os.path.exists(db_path)
    conn = connect(db_path)
    try:
        version = get_schema_version(conn)
        if version >= SCHEMA_VERSION:
            create_schema(conn)  # picks up any missing pieces
            return {"action": "none", "version": version}
        has_data = existed and _table_exists(conn, "transactions") and conn.execute(
            "SELECT COUNT(*) FROM transactions"
        ).fetchone()[0] > 0
    finally:
        conn.close()

    backup_path = backup_db(db_path) if has_data else ""
    conn = connect(db_path)
    try:
        report = _migrate(conn)
    except Exception:
        conn.rollback()
        conn.close()
        if backup_path:
            shutil.copy2(backup_path, db_path)
        raise
    conn.close()
    _retire_legacy_files()
    report.update(action="migrated" if has_data else "created", backup=backup_path, version=SCHEMA_VERSION)
    return report


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------


def sync_master_csv(db_path: Optional[str] = None, csv_path: Optional[str] = None) -> bool:
    """Write a plain CSV copy of the transactions table. Nothing reads it back."""
    db_path = db_path or paths.DB_PATH
    csv_path = csv_path or paths.MASTER_CSV_PATH
    if not os.path.exists(db_path):
        return False
    try:
        conn = connect(db_path, read_only=True)
        try:
            cols = ", ".join(TRANSACTION_EXPORT_COLUMNS)
            rows = conn.execute(f"SELECT {cols} FROM transactions ORDER BY date ASC, txn_id ASC").fetchall()
        finally:
            conn.close()
        os.makedirs(os.path.dirname(csv_path), exist_ok=True)
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(TRANSACTION_EXPORT_COLUMNS)
            for r in rows:
                writer.writerow(list(r))
        return True
    except Exception:
        return False
