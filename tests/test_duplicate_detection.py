"""Rows that look like ones already saved: a reissued card, or a statement covering days an earlier file had."""

import unittest

from helpers import TempAppCase
from evbudget import import_engine

NEW_CARD = """Transaction Date,Post Date,Description,Category,Type,Amount,Memo
10/05/2026,10/06/2026,COSTCO WHSE #0783 EDEN PRAIRIE MN,Shopping,Sale,-27.47,
10/05/2026,10/06/2026,PLUTOSLIDES.COM 844-4752870 TX,Shopping,Sale,-34.99,
10/05/2026,10/06/2026,PLUTOSLIDES.COM 844-4752870 TX,Shopping,Sale,-34.99,
10/07/2026,10/08/2026,SHELL OIL 123,Gas,Sale,-40.00,
10/20/2026,10/21/2026,BRAND NEW PLACE,Shopping,Sale,-12.00,
"""


class NameSimilarityTests(unittest.TestCase):
    def test_a_longer_description_of_the_same_purchase_matches(self):
        sim = import_engine.name_similarity
        self.assertGreaterEqual(sim("TACO BELL 7005058", "TACO BELL 7005058 952-942-5766 MN"), 0.9)
        self.assertGreaterEqual(sim("CIRCLEK2746402", "CIRCLEK#2746402 EDEN PRAIRIE MN"), 0.5)
        self.assertGreaterEqual(sim("Amazon", "AMAZON MKTPL*595SZ3QR1 Amzn.com/bill WA"), 0.9)

    def test_different_merchants_do_not(self):
        sim = import_engine.name_similarity
        self.assertLess(sim("SHELL OIL 123", "COSTCO WHSE #0783"), 0.5)
        self.assertEqual(sim("", "COSTCO"), 0.0)


class LikelyDuplicateTests(TempAppCase):
    def setUp(self):
        super().setUp()
        # Saved earlier from the old card's file
        self.old_costco = self.add_txn(date="2026-10-05", raw_name="COSTCO WHSE 0783", name="COSTCO", amount=-27.47,
                                       account_name="Chase 1484", source_file="Chase1484_Activity.csv")
        self.old_pluto = self.add_txn(date="2026-10-05", raw_name="PLUTOSLIDES.COM", name="PLUTOSLIDES", amount=-34.99,
                                      account_name="Chase 1484", source_file="Chase1484_Activity.csv")
        self.old_shell = self.add_txn(date="2026-10-04", raw_name="SHELL OIL 123", name="SHELL", amount=-40.00,   # posted 3 days apart
                                      account_name="Chase 1484", source_file="Chase1484_Activity.csv")

    def preview(self):
        self.write_csv("Chase3855_Activity.csv", NEW_CARD)
        c = self.conn()
        try:
            info = import_engine.inspect_file_header(import_engine.resolve_input_file("Chase3855_Activity.csv", self.input_dir))
            return import_engine.build_preview(c, [info])
        finally:
            c.close()

    def test_rows_matching_saved_ones_from_another_file_are_flagged_with_what_they_match(self):
        p = self.preview()
        flagged = {r["raw_name"]: r for r in p["records"] if r.get("likely_duplicate")}
        self.assertIn("COSTCO WHSE #0783 EDEN PRAIRIE MN", flagged)
        d = flagged["COSTCO WHSE #0783 EDEN PRAIRIE MN"]["likely_duplicate"]
        self.assertEqual((d["txn_id"], d["account_name"], d["date"]), (self.old_costco, "Chase 1484", "2026-10-05"))
        self.assertIn("SHELL OIL 123", flagged)                       # within a few days counts
        self.assertNotIn("BRAND NEW PLACE", flagged)
        self.assertEqual(p["summary"]["likely_duplicates"], 3)

    def test_each_saved_row_explains_only_one_new_row(self):
        # Two identical purchases arrive but only one is saved: the second is genuinely new
        p = self.preview()
        pluto = [r for r in p["records"] if r["raw_name"].startswith("PLUTOSLIDES")]
        self.assertEqual(sorted(bool(r.get("likely_duplicate")) for r in pluto), [False, True])

    def test_rows_from_the_same_file_are_left_to_the_normal_duplicate_check(self):
        self.write_csv("Chase3855_Activity.csv", NEW_CARD)
        c = self.conn()
        info = import_engine.inspect_file_header(import_engine.resolve_input_file("Chase3855_Activity.csv", self.input_dir))
        first = import_engine.build_preview(c, [info])
        import_engine.commit_import(c, [dict(r, skip=False) for r in first["records"] if r["status"] == "new"], ["Chase3855_Activity.csv"])
        again = import_engine.build_preview(c, [info])
        c.close()
        self.assertEqual(again["summary"]["new"], 0)
        self.assertEqual(again["summary"]["likely_duplicates"], 0)

    def test_nothing_is_flagged_when_nothing_matches(self):
        c = self.conn()
        c.execute("DELETE FROM transactions")
        c.commit()
        c.close()
        self.assertEqual(self.preview()["summary"]["likely_duplicates"], 0)


if __name__ == "__main__":
    unittest.main()
