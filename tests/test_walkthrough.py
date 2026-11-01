"""Setup walkthrough state and status, and finding paychecks in imported deposits."""

import unittest
from datetime import date, timedelta

from helpers import TempAppCase
from evbudget import income_discovery, store
from evbudget.http_util import HttpError


class WalkthroughStateTests(TempAppCase):
    def get(self):
        return self.call(self.app.walkthrough_get, method="GET")

    def test_new_empty_budget_auto_starts(self):
        r = self.get()
        self.assertEqual(r["state"]["status"], "not_started")
        self.assertTrue(r["auto_start"])
        self.assertEqual(r["status"]["transactions"], 0)

    def test_no_auto_start_once_there_are_transactions(self):
        self.add_txn()
        self.assertFalse(self.get()["auto_start"])

    def test_no_auto_start_after_go_live(self):
        c = self.conn()
        store.set_setting(c, "go_live_date", "2026-05-01")
        c.commit()
        c.close()
        self.assertFalse(self.get()["auto_start"])

    def test_no_auto_start_after_dismiss(self):
        self.call(self.app.walkthrough_save, {"status": "dismissed", "step": None})
        r = self.get()
        self.assertEqual(r["state"]["status"], "dismissed")
        self.assertFalse(r["auto_start"])

    def test_state_round_trip(self):
        self.call(self.app.walkthrough_save, {"status": "active", "step": "pay_sources", "skipped": ["import", "import"]})
        s = self.get()["state"]
        self.assertEqual((s["status"], s["step"], s["skipped"]), ("active", "pay_sources", ["import"]))
        self.assertTrue(s["updated_at"])

    def test_bad_state_is_rejected(self):
        for body in ({"status": "bogus"}, {"status": "active", "step": "Drop Table"}, {"status": "active", "skipped": "import"}):
            with self.assertRaises(HttpError):
                self.call(self.app.walkthrough_save, body)

    def test_status_counts(self):
        self.add_txn(date="2026-01-05")
        self.add_txn(date="2026-03-09")
        c = self.conn()
        c.execute("UPDATE envelopes SET monthly_budget_cents = 10000 WHERE name = 'Groceries'")
        c.commit()
        spending = c.execute("SELECT COUNT(*) FROM envelopes WHERE active = 1 AND kind = 'spending'").fetchone()[0]
        bills = c.execute("SELECT COUNT(*) FROM envelopes WHERE active = 1 AND kind = 'bill'").fetchone()[0]
        c.close()
        s = self.get()["status"]
        self.assertEqual((s["transactions"], s["first_date"], s["last_date"]), (2, "2026-01-05", "2026-03-09"))
        self.assertEqual(s["envelopes"]["spending"], spending)
        self.assertEqual(s["spending_without_budget"], spending - 1)
        self.assertEqual(s["bill_envelopes_without_bills"], bills)
        self.assertEqual(s["income_sources"], 2)            # the seeded household
        self.assertEqual(s["sources_without_template"], 2)


class PayDiscoveryTests(TempAppCase):
    def add_paychecks(self, raw, amount, start, every, count, kind="other_income"):
        d = start
        for i in range(count):
            self.add_txn(date=d.isoformat(), name=raw.title(), raw_name=f"{raw} PPD ID: {4400 + i}", amount=amount + i,
                         envelope="", txn_kind=kind)
            d += timedelta(days=every)

    def found(self):
        return self.call(self.app.discover_pay_sources, method="GET")["candidates"]

    def test_biweekly_paycheck_is_found_with_a_clean_phrase(self):
        self.add_paychecks("ACME CORP PAYROLL", 2100.0, date(2026, 6, 5), 14, 6)
        [c] = self.found()
        self.assertEqual((c["match_phrase"], c["freq"], c["interval"]), ("ACME CORP PAYROLL", "weekly", 2))
        self.assertEqual((c["suggested_name"], c["deposits"], c["confidence"]), ("Acme", 6, "high"))
        self.assertEqual(c["next_date"], (date(2026, 6, 5) + timedelta(days=14 * 6)).isoformat())

    def test_monthly_deposit_is_found(self):
        d = date(2026, 1, 15)
        for m in range(1, 7):
            self.add_txn(date=date(2026, m, 15).isoformat(), name="Pension", raw_name="STATE PENSION FUND", amount=900.0,
                         envelope="", txn_kind="other_income")
        [c] = self.found()
        self.assertEqual((c["freq"], c["interval"], c["match_phrase"]), ("monthly", 1, "STATE PENSION FUND"))

    def test_deposits_already_tied_to_a_source_are_left_out(self):
        self.add_paychecks("CARGILL INCORPOR PAYROLL", 3500.0, date(2026, 6, 5), 14, 5)
        self.assertEqual(self.found(), [])

    def test_irregular_small_and_old_deposits_are_left_out(self):
        for i, gap in enumerate([3, 40, 9, 61]):
            self.add_txn(date=(date(2026, 1, 1) + timedelta(days=sum([3, 40, 9, 61][:i + 1]))).isoformat(),
                         raw_name="VENMO CASHOUT", amount=300.0, envelope="", txn_kind="other_income")
        self.add_paychecks("TINY INTEREST", 2.0, date(2026, 6, 1), 30, 5)
        self.add_paychecks("OLD JOB PAYROLL", 1500.0, date(2025, 1, 3), 14, 5)
        self.add_txn(date="2026-10-01", raw_name="SOMETHING RECENT", amount=-5.0)   # pushes "now" forward
        self.assertEqual(self.found(), [])

    def test_dismissed_candidates_stay_hidden(self):
        self.add_paychecks("ACME CORP PAYROLL", 2100.0, date(2026, 6, 5), 14, 6)
        key = self.found()[0]["key"]
        self.call(self.app.dismiss_pay_source_candidate, {"key": key})
        self.assertEqual(self.found(), [])

    def test_phrase_trims_part_words_and_numbers(self):
        texts = ["BIGCO PAYROLL DIR DEP 0611 X12", "BIGCO PAYROLL DIR DEP 0625 X99"]
        self.assertEqual(income_discovery.suggest_phrase(texts, "BIGCO PAYROLL DIR"), "BIGCO PAYROLL DIR DEP")
        self.assertEqual(income_discovery.suggest_phrase(["ABCDEF ONE", "ABCDXY TWO"], "ABC"), "ABC")


if __name__ == "__main__":
    unittest.main()
