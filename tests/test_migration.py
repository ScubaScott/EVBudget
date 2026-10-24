import json
import os
import shutil
import sqlite3
import tempfile
import unittest

from helpers import ROOT  # noqa: F401  (sets sys.path)
from evbudget import paths, store

V1_SCHEMA = """
CREATE TABLE transactions (
    txn_id TEXT PRIMARY KEY, date TEXT NOT NULL, name TEXT NOT NULL, raw_name TEXT, amount REAL NOT NULL,
    envelope TEXT, check_num TEXT, account_type TEXT, source_file TEXT, is_included INTEGER DEFAULT 1,
    exclusion_reason TEXT, match_source TEXT, updated_at TEXT
)
"""

V1_ROWS = [
    # id, date, name, raw, amount, envelope, included, reason, match
    ("A1", "01/02/2026", "CARGILL INCORPOR PAYROLL", "CARGILL INCORPOR PAYROLL  1", 3500.00, "", 1, "", "Income / Unallocated"),
    ("A2", "01/16/2026", "CARGILL INCORPOR PAYROLL", "CARGILL INCORPOR PAYROLL  2", 3500.00, "", 1, "", "Income / Unallocated"),
    ("A3", "01/30/2026", "CARGILL INCORPOR PAYROLL", "CARGILL INCORPOR PAYROLL  3", 12000.00, "", 1, "", "Income / Unallocated"),
    ("B1", "02/03/2026", "KROGER", "KROGER #123", -50.00, "Groceries", 1, "", "Default Fallback"),
    ("B2", "02/04/2026", "COSTCO", "COSTCO WHSE", -80.00, "Groceries", 1, "", "Core Rule"),
    ("B3", "02/05/2026", "WEIRD SHOP", "WEIRD SHOP", -15.00, "Restaurant", 1, "", "Default Fallback"),  # hand-edited
    ("C1", "02/06/2026", "AUTOMATIC PAYMENT - THANK", "AUTOMATIC PAYMENT - THANK", 900.00, "", 0, "Credit Card Payment / Autopay", "Core Rule"),
    ("C2", "02/07/2026", "ONLINE TRANSFER FROM REILING S", "ONLINE TRANSFER FROM REILING S", 20.00, "", 0, "Internal Account Transfer", "Core Rule"),
    ("D1", "02/08/2026", "COSTCO", "COSTCO RETURN", 12.00, "", 1, "", "Income / Unallocated"),
    ("D2", "02/09/2026", "VENMO PAYMENT", "VENMO PAYMENT", 281.00, "", 1, "", "Income / Unallocated"),
    ("D3", "02/10/2026", "BARBER", "BARBER", 50.60, "Bills: One Time", 1, "", "User Learned Rule"),
]


def build_v1(path):
    c = sqlite3.connect(path)
    c.execute(V1_SCHEMA)
    for tid, d, n, raw, amt, env, inc, reason, ms in V1_ROWS:
        c.execute(
            "INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (tid, d, n, raw, amt, env, "", "Credit / Debit", "f.csv", inc, reason, ms, "2026-03-01T10:00:00"),
        )
    c.commit()
    c.close()


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="evbudget_mig_")
        self._saved = {a: getattr(paths, a) for a in ("LEGACY_ENVELOPES_PATH", "LEGACY_USER_RULES_PATH", "LEGACY_KNOWN_VENDORS_PATH")}
        paths.LEGACY_ENVELOPES_PATH = os.path.join(self.tmp, "envelopes.json")
        paths.LEGACY_USER_RULES_PATH = os.path.join(self.tmp, "user_rules.json")
        paths.LEGACY_KNOWN_VENDORS_PATH = os.path.join(self.tmp, "known_vendors.json")
        with open(paths.LEGACY_ENVELOPES_PATH, "w") as f:
            json.dump({"envelopes": ["Groceries", "Restaurant", "Shopping", "Bills: One Time"]}, f)
        with open(paths.LEGACY_USER_RULES_PATH, "w") as f:
            json.dump({"phrase_rules": [
                {"phrase": "BARBER", "envelope": "Bills: One Time"},
                {"phrase": "BILL PAY X", "payee": "X CORP"},
            ], "column_presets": {}}, f)
        with open(paths.LEGACY_KNOWN_VENDORS_PATH, "w") as f:
            json.dump({"vendors": {"KROGER": {"envelope": "Groceries"}}}, f)
        self.db = os.path.join(self.tmp, "LocalStore", "budget_store.db")
        os.makedirs(os.path.dirname(self.db))
        build_v1(self.db)

    def tearDown(self):
        for a, v in self._saved.items():
            setattr(paths, a, v)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def rows(self, sql):
        c = store.connect(self.db)
        out = [dict(r) for r in c.execute(sql)]
        c.close()
        return out

    def test_migrates_dates_kinds_and_people(self):
        report = store.ensure_schema(self.db)
        self.assertEqual(report["action"], "migrated")
        self.assertTrue(os.path.exists(report["backup"]))

        dates = self.rows("SELECT MIN(date) lo, MAX(date) hi FROM transactions")[0]
        self.assertEqual((dates["lo"], dates["hi"]), ("2026-01-02", "2026-02-10"))

        kinds = {r["txn_id"]: r for r in self.rows("SELECT * FROM transactions")}
        self.assertEqual(kinds["A1"]["txn_kind"], "paycheck")
        self.assertEqual(kinds["A1"]["income_subtype"], "regular")
        self.assertEqual(kinds["A3"]["income_subtype"], "bonus")
        self.assertEqual(kinds["C1"]["txn_kind"], "cc_payment")
        self.assertEqual(kinds["C2"]["txn_kind"], "transfer")
        self.assertEqual(kinds["B1"]["txn_kind"], "expense")
        # D1: positive, matched an earlier Costco purchase -> refund to that envelope
        self.assertEqual((kinds["D1"]["txn_kind"], kinds["D1"]["envelope"]), ("refund", "Groceries"))
        # D2: Venmo -> reimbursement awaiting review
        self.assertEqual((kinds["D2"]["txn_kind"], kinds["D2"]["envelope"], kinds["D2"]["needs_review"]), ("refund", "", 1))
        # D3: positive with an envelope chosen by hand
        self.assertEqual((kinds["D3"]["txn_kind"], kinds["D3"]["user_modified"]), ("refund", 1))
        # B3: stored envelope differs from what the rules say -> protected from re-runs
        self.assertEqual(kinds["B3"]["user_modified"], 1)
        self.assertEqual(kinds["B1"]["user_modified"], 0)

        people = [r["name"] for r in self.rows("SELECT name FROM people ORDER BY id")]
        self.assertEqual(people, ["Scott", "Kendra"])
        self.assertEqual(kinds["A1"]["person_id"], 1)

    def test_legacy_rules_and_envelopes_come_across(self):
        store.ensure_schema(self.db)
        user = self.rows("SELECT * FROM rules WHERE origin = 'user' ORDER BY priority")
        self.assertEqual([(r["phrase"], r["stage"]) for r in user], [("BARBER", "envelope"), ("BILL PAY X", "payee")])
        vendors = self.rows("SELECT phrase FROM rules WHERE origin = 'vendor'")
        self.assertEqual([r["phrase"] for r in vendors], ["KROGER"])
        env = [r["name"] for r in self.rows("SELECT name FROM envelopes WHERE kind != 'system' ORDER BY sort_order")]
        self.assertEqual(env, ["Groceries", "Restaurant", "Shopping", "Bills: One Time"])
        self.assertFalse(os.path.exists(paths.LEGACY_ENVELOPES_PATH))
        self.assertTrue(os.path.exists(os.path.splitext(paths.LEGACY_ENVELOPES_PATH)[0] + ".migrated.json"))

    def test_re_running_rules_right_after_migration_changes_nothing(self):
        from evbudget import reclassify
        store.ensure_schema(self.db)
        c = store.connect(self.db)
        result = reclassify.reclassify(c)
        c.close()
        self.assertEqual(result["changed_count"], 0, result["changes"])

    def test_rows_excluded_by_built_in_rules_lose_the_meaningless_envelope(self):
        c = sqlite3.connect(self.db)
        c.execute("UPDATE transactions SET envelope = 'Shopping' WHERE txn_id IN ('C1', 'C2')")
        c.commit()
        c.close()
        store.ensure_schema(self.db)
        envs = {r["txn_id"]: r["envelope"] for r in self.rows("SELECT txn_id, envelope FROM transactions WHERE txn_id IN ('C1','C2')")}
        self.assertEqual(envs, {"C1": "", "C2": ""})

    def test_running_twice_changes_nothing(self):
        store.ensure_schema(self.db)
        before = self.rows("SELECT * FROM transactions ORDER BY txn_id")
        rules_before = self.rows("SELECT COUNT(*) n FROM rules")[0]["n"]
        again = store.ensure_schema(self.db)
        self.assertEqual(again["action"], "none")
        self.assertEqual(self.rows("SELECT * FROM transactions ORDER BY txn_id"), before)
        self.assertEqual(self.rows("SELECT COUNT(*) n FROM rules")[0]["n"], rules_before)

    def test_failure_restores_the_original_database(self):
        before = self.rows("SELECT * FROM transactions ORDER BY txn_id")
        real = store._migrate

        def boom(conn, **options):
            conn.execute("DELETE FROM transactions")
            conn.commit()
            raise RuntimeError("simulated failure")

        store._migrate = boom
        try:
            with self.assertRaises(RuntimeError):
                store.ensure_schema(self.db)
        finally:
            store._migrate = real
        # Same rows, still the old column set and old date format, no new tables.
        self.assertEqual(self.rows("SELECT * FROM transactions ORDER BY txn_id"), before)
        tables = [r["name"] for r in self.rows("SELECT name FROM sqlite_master WHERE type = 'table'")]
        self.assertEqual(tables, ["transactions"])

    def test_fresh_database_gets_defaults(self):
        fresh = os.path.join(self.tmp, "fresh", "budget_store.db")
        report = store.ensure_schema(fresh)
        self.assertEqual(report["action"], "created")
        c = store.connect(fresh)
        self.assertGreater(c.execute("SELECT COUNT(*) FROM rules").fetchone()[0], 100)
        self.assertEqual(c.execute("SELECT COUNT(*) FROM transactions").fetchone()[0], 0)
        c.close()


if __name__ == "__main__":
    unittest.main()
