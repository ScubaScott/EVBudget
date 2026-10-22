import unittest

from helpers import TempAppCase
from evbudget import import_engine, store
from evbudget.textutil import generate_deterministic_txn_id, parse_date_string, sanitize_text

CHASE = """Transaction Date,Post Date,Description,Category,Type,Amount,Memo
05/02/2026,05/03/2026,KROGER #123,Groceries,Sale,-52.10,
05/03/2026,05/04/2026,HY-VEE FAST & FRESH,Gas,Sale,-40.00,
05/03/2026,05/04/2026,HY-VEE FAST & FRESH,Gas,Sale,-40.00,
05/04/2026,05/05/2026,TARGET 0001,Shopping,Return,15.00,
05/05/2026,05/06/2026,AUTOMATIC PAYMENT - THANK YOU,,Payment,200.00,
"""

CHECKING = """Date,Description,Debit,Credit,Check #
05/08/2026,CARGILL INCORPOR PAYROLL 1,,3500.00,
05/09/2026,CHECK # 700,250.00,,700
05/10/2026,BAD DATE ROW,5.00,,
not-a-date,Broken,5.00,,
"""


class TxnIdTests(unittest.TestCase):
    def test_id_is_stable(self):
        # Computed with the pre-3.0 importer; must never change or imports would duplicate.
        a = generate_deterministic_txn_id("05/02/2026", "KROGER #123", -52.10, "Credit / Debit")
        b = generate_deterministic_txn_id("2026-05-02", "KROGER #123", -52.10, "Credit / Debit")
        self.assertEqual(a, b)
        self.assertEqual(a, "TXN-" + a[4:])
        self.assertEqual(len(a), 16)

    def test_occurrence_and_bank_ref(self):
        base = generate_deterministic_txn_id("2026-05-02", "X", -1.0, "Bank / Checking")
        second = generate_deterministic_txn_id("2026-05-02", "X", -1.0, "Bank / Checking", occurrence_index=2)
        self.assertNotEqual(base, second)
        self.assertEqual(generate_deterministic_txn_id("2026-05-02", "X", -1.0, "Bank", raw_ref="ab-123456"), "TXN-ab123456")

    def test_dates_are_iso(self):
        self.assertEqual(parse_date_string("10/16/26")[0], "2026-10-16")
        self.assertEqual(parse_date_string("2026-10-16")[0], "2026-10-16")
        self.assertEqual(sanitize_text("AT&amp;T, MOBILITY"), "AT&T MOBILITY")


class ImportFlowTests(TempAppCase):
    def configs(self, *names):
        c = self.conn()
        presets = import_engine.load_presets(c)
        c.close()
        out = []
        for n in names:
            info = import_engine.inspect_file_header(import_engine.resolve_input_file(n, self.input_dir), presets)
            out.append(info)
        return out

    def preview(self, *names):
        c = self.conn()
        try:
            return import_engine.build_preview(c, self.configs(*names))
        finally:
            c.close()

    def commit(self, preview, **edits):
        c = self.conn()
        try:
            return import_engine.commit_import(c, preview["records"], ["x.csv"])
        finally:
            c.close()

    def test_inspect_detects_columns_and_account_type(self):
        self.write_csv("Chase_Activity.csv", CHASE)
        self.write_csv("Checking.csv", CHECKING)
        chase, checking = self.configs("Chase_Activity.csv", "Checking.csv")
        self.assertTrue(chase["is_mapped"])
        self.assertEqual(chase["account_type"], "Credit / Debit")
        self.assertEqual(chase["mapping"]["date_col"], "Transaction Date")
        self.assertTrue(checking["mapping"]["is_dual"])
        self.assertEqual(checking["account_type"], "Bank / Checking")

    def test_preview_classifies_and_writes_nothing(self):
        self.write_csv("Chase_Activity.csv", CHASE)
        self.write_csv("Checking.csv", CHECKING)
        pv = self.preview("Chase_Activity.csv", "Checking.csv")
        by_name = {}
        for r in pv["records"]:
            by_name.setdefault(r["name"], []).append(r)

        self.assertEqual(pv["summary"]["new"], 8)  # two rows with bad dates are skipped, not counted
        self.assertEqual(pv["summary"]["skipped_bad_date"], 1)
        self.assertEqual(len(by_name["HY-VEE FAST & FRESH"]), 2)  # identical same-day charges both kept
        self.assertEqual(by_name["HY-VEE FAST & FRESH"][0]["envelope"], "Gas")
        self.assertEqual(by_name["KROGER #123"][0]["txn_kind"], "expense")
        self.assertEqual(by_name["AUTOMATIC PAYMENT - THANK YOU"][0]["txn_kind"], "cc_payment")
        self.assertFalse(by_name["AUTOMATIC PAYMENT - THANK YOU"][0]["is_included"])
        self.assertEqual(by_name["CARGILL INCORPOR PAYROLL 1"][0]["txn_kind"], "paycheck")
        self.assertEqual(by_name["CHECK #700"][0]["envelope"], "Bills: One Time")
        c = self.conn()
        self.assertEqual(c.execute("SELECT COUNT(*) FROM transactions").fetchone()[0], 0)
        c.close()

    def test_commit_then_reimport_adds_nothing_and_keeps_edits(self):
        self.write_csv("Chase_Activity.csv", CHASE)
        first = self.commit(self.preview("Chase_Activity.csv"))
        self.assertEqual(first["inserted"], 5)

        kroger = [r for r in self.preview("Chase_Activity.csv")["records"] if r["name"] == "KROGER #123"][0]
        # User edits the envelope in the dashboard.
        self.call(self.app.update_transaction, {"txn_id": kroger["txn_id"], "envelope": "Restaurant", "name": "Kroger"})

        again = self.preview("Chase_Activity.csv")
        self.assertEqual(again["summary"]["new"], 0)
        self.assertEqual(again["summary"]["existing"], 5)
        second = self.commit(again)
        self.assertEqual((second["inserted"], second["existing"]), (0, 5))

        row = self.get_txn(kroger["txn_id"])
        self.assertEqual((row["envelope"], row["name"], row["user_modified"]), ("Restaurant", "Kroger", 1))

    def test_overlapping_file_only_adds_new_rows(self):
        self.write_csv("a.csv", CHASE)
        self.commit(self.preview("a.csv"))
        self.write_csv("b.csv", CHASE + "05/20/2026,05/21/2026,NEW SHOP,Shopping,Sale,-9.99,\n")
        pv = self.preview("b.csv")
        self.assertEqual((pv["summary"]["new"], pv["summary"]["existing"]), (1, 5))
        self.commit(pv)
        files = self.get_txn([r for r in pv["records"] if r["status"] == "existing"][0]["txn_id"])["source_file"]
        self.assertIn("a.csv", files)
        self.assertIn("b.csv", files)

    def test_client_edits_are_saved_and_validated(self):
        self.write_csv("Chase_Activity.csv", CHASE)
        pv = self.preview("Chase_Activity.csv")
        target = [r for r in pv["records"] if r["name"] == "TARGET 0001"][0]
        target.update(envelope="Shopping", txn_kind="refund", edited=True, needs_review=0)
        bad = {"txn_id": "TXN-BAD", "date": "13/45/2026", "amount": 5}
        pv["records"].append(bad)
        result = self.commit(pv)
        self.assertEqual(result["rejected"], 1)
        row = self.get_txn(target["txn_id"])
        self.assertEqual((row["txn_kind"], row["envelope"], row["user_modified"]), ("refund", "Shopping", 1))

    def test_excluded_kind_forces_exclusion(self):
        self.write_csv("Chase_Activity.csv", CHASE)
        pv = self.preview("Chase_Activity.csv")
        rec = [r for r in pv["records"] if r["name"] == "KROGER #123"][0]
        rec.update(txn_kind="transfer", is_included=True)
        self.commit(pv)
        row = self.get_txn(rec["txn_id"])
        self.assertEqual(row["is_included"], 0)
        self.assertTrue(row["exclusion_reason"])

    def test_column_mapping_is_remembered(self):
        self.write_csv("Odd.csv", "When,Who,HowMuch\n05/01/2026,SHOP,-5.00\n")
        info = self.configs("Odd.csv")[0]
        self.assertFalse(info["is_mapped"])
        c = self.conn()
        import_engine.save_preset(c, info["headers"], {
            "date_col": "When", "name_col": "Who", "amount_col": "HowMuch", "debit_col": "", "credit_col": "",
            "check_col": "", "ref_col": "", "type_col": "", "is_dual": False, "sign_mode": "standard",
        }, "Bank / Checking")
        c.commit()
        c.close()
        self.write_csv("Odd2.csv", "When,Who,HowMuch\n05/02/2026,SHOP2,-6.00\n")
        again = self.configs("Odd2.csv")[0]
        self.assertTrue(again["is_mapped"])
        self.assertTrue(again["from_preset"])

    def test_upload_never_overwrites_a_different_file(self):
        first = import_engine.save_upload("Activity.csv", b"one", self.input_dir)
        same = import_engine.save_upload("Activity.csv", b"one", self.input_dir)
        other = import_engine.save_upload("Activity.csv", b"two", self.input_dir)
        self.assertEqual(first, same)
        self.assertNotEqual(first, other)
        with open(first, "rb") as fh:
            self.assertEqual(fh.read(), b"one")

    def test_file_names_cannot_escape_the_input_folder(self):
        with self.assertRaises(FileNotFoundError):
            import_engine.resolve_input_file("../../etc/passwd", self.input_dir)


if __name__ == "__main__":
    unittest.main()
