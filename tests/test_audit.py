import unittest
from datetime import date

from helpers import TempAppCase
from evbudget import audit, import_engine
from evbudget.http_util import HttpError

TODAY = date(2026, 10, 22)


class AuditCase(TempAppCase):
    def setUp(self):
        super().setUp()
        # Data starts well before anything the tests look at, so "new payee" isn't just "start of the data"
        self.add_txn(date="2026-01-02", amount=-5, name="OLD SHOP", raw_name="OLD SHOP", envelope="Shopping")

    def spend(self, d, amt, name, raw=None, envelope="Shopping", **kw):
        return self.add_txn(date=d, amount=-amt, name=name, raw_name=raw or name, envelope=envelope, **kw)

    def run_checks(self, *ids, scope=None, **kw):
        c = self.conn()
        try:
            return audit.run(c, ids, scope_ids=scope, today=TODAY, **kw)
        finally:
            c.close()

    def keys(self, res, check):
        return [f["key"] for f in res["findings"] if f["check"] == check]


class DuplicateTests(AuditCase):
    def test_same_payee_amount_and_day(self):
        a = self.spend("2026-10-05", 42.10, "CORNER CAFE")
        b = self.spend("2026-10-05", 42.10, "CORNER CAFE")
        self.spend("2026-10-05", 42.10, "OTHER PLACE")             # same amount, different payee: not this check
        self.spend("2026-10-06", 42.10, "CORNER CAFE")             # next day: the other check
        res = self.run_checks("dup_same_day")
        self.assertEqual(self.keys(res, "dup_same_day"), ["|".join(sorted([a, b]))])
        self.assertIn("2 charges", res["findings"][0]["reason"])

    def test_store_numbers_do_not_hide_a_duplicate(self):
        self.spend("2026-10-05", 30, "HY-VEE #1562")
        self.spend("2026-10-05", 30, "HY-VEE #1999")
        self.assertEqual(len(self.run_checks("dup_same_day")["findings"]), 1)

    def test_within_three_days_but_not_four(self):
        self.spend("2026-10-01", 80, "GYM")
        self.spend("2026-10-03", 80, "GYM")
        self.spend("2026-10-12", 80, "GYM")
        self.spend("2026-10-16", 80, "GYM")
        pairs = self.run_checks("dup_near_days")["findings"]
        self.assertEqual(len(pairs), 1)
        self.assertEqual([t["date"] for t in pairs[0]["txns"]], ["2026-10-01", "2026-10-03"])

    def test_tiny_amounts_are_ignored_for_duplicates(self):
        self.spend("2026-10-05", 0.50, "APP")
        self.spend("2026-10-05", 0.50, "APP")
        self.assertEqual(self.run_checks("dup_same_day", "dup_near_days")["findings"], [])

    def test_rows_saved_twice_from_different_files_are_found_once_and_not_as_same_day(self):
        a = self.add_txn(date="2026-10-05", amount=-1000, name="TRANSFER", raw_name="TRANSFER X", source_file="one.csv", is_included=0, txn_kind="transfer")
        b = self.add_txn(date="2026-10-05", amount=-1000, name="TRANSFER", raw_name="TRANSFER X", source_file="two.csv", is_included=0, txn_kind="transfer")
        res = self.run_checks("dup_import_twins", "dup_same_day")
        self.assertEqual(len(res["findings"]), 1)
        self.assertEqual((res["findings"][0]["check"], res["findings"][0]["severity"]), ("dup_import_twins", "low"))
        c = self.add_txn(date="2026-10-06", amount=-20, name="SHOP", raw_name="SHOP", source_file="one.csv")
        d = self.add_txn(date="2026-10-06", amount=-20, name="SHOP", raw_name="SHOP", source_file="two.csv")
        res = self.run_checks("dup_import_twins", "dup_same_day")
        counted = [f for f in res["findings"] if f["severity"] == "high"]
        self.assertEqual([f["check"] for f in counted], ["dup_import_twins"])   # reported once, not also as a same-day repeat
        self.assertTrue(a and b and c and d)

    def test_two_real_repeats_from_one_file_are_not_called_twins(self):
        self.spend("2026-10-05", 10, "CLINIC", source_file="one.csv")
        self.spend("2026-10-05", 10, "CLINIC", source_file="one.csv")
        res = self.run_checks("dup_import_twins", "dup_same_day")
        self.assertEqual([f["check"] for f in res["findings"]], ["dup_same_day"])

    def test_same_date_and_amount_across_payees_is_its_own_optional_check(self):
        self.spend("2026-10-05", 19.99, "PLACE ONE")
        self.spend("2026-10-05", 19.99, "PLACE TWO")
        self.assertEqual(len(self.run_checks("dup_amount_date")["findings"]), 1)
        self.assertEqual(self.run_checks("dup_same_day")["findings"], [])


class UnusualTests(AuditCase):
    def test_first_payment_to_a_new_person(self):
        self.spend("2026-03-01", 40, "ZELLE", raw="ZELLE TO SAM OLD ON 03/01 REF ABC123")                  # known by now
        self.spend("2026-09-10", 25, "ZELLE", raw="ZELLE TO SAM OLD ON 09/10 REF DEF456")
        new = self.spend("2026-10-10", 300, "ZELLE", raw="ZELLE TO LEE FEEDLUND ON 10/10 REF GHI789")
        res = self.run_checks("new_person_paid")
        self.assertEqual(self.keys(res, "new_person_paid"), [new])
        f = res["findings"][0]
        self.assertEqual(f["severity"], "high")
        self.assertIn("Zelle: LEE FEEDLUND", f["reason"])

    def test_venmo_counts_once_then_is_known(self):
        self.spend("2026-04-01", 20, "VENMO PAYMENT", raw="VENMO PAYMENT 123")
        self.spend("2026-10-01", 20, "VENMO PAYMENT", raw="VENMO PAYMENT 456")
        self.assertEqual(self.run_checks("new_person_paid")["findings"], [])

    def test_only_recent_first_payments_in_a_full_scan(self):
        self.spend("2026-03-01", 40, "ZELLE", raw="ZELLE TO OLD FRIEND ON 03/01 REF AAA111")     # first seen long ago
        self.assertEqual(self.run_checks("new_person_paid")["findings"], [])
        self.assertEqual(len(self.run_checks("new_person_paid", lookback_days=365)["findings"]), 1)

    def test_payees_first_seen_at_the_start_of_the_data_are_not_new(self):
        self.spend("2026-01-10", 900, "BIG STORE")
        self.assertEqual(self.run_checks("new_payee_large", lookback_days=1000)["findings"], [])

    def test_new_payee_large_uses_your_threshold_and_skips_known_bills(self):
        big = self.spend("2026-10-02", 650, "ROCK AUTO PARTS")
        self.spend("2026-10-03", 150, "SMALL SHOP")
        self.spend("2026-10-04", 900, "SCOTT COUNTY TRE", envelope="Property Taxes")
        self.call(self.app.save_bill, {"envelope_id": self.eid("Property Taxes"), "name": "Property tax", "match_phrase": "SCOTT COUNTY TRE",
                                       "amount": 900, "freq": "monthly", "interval": 6, "anchor_date": "2026-10-04"})
        res = self.run_checks("new_payee_large")
        self.assertEqual(self.keys(res, "new_payee_large"), [big])
        self.assertEqual(len(self.run_checks("new_payee_large", large_amount=100)["findings"]), 2)   # bill payee still skipped

    def test_new_payee_far_above_the_envelopes_usual(self):
        for i in range(12):
            self.spend(f"2026-09-{i + 1:02d}", 12, f"SNACK {i}", envelope="Restaurant")
        odd = self.spend("2026-10-02", 140, "FANCY PLACE", envelope="Restaurant")
        res = self.run_checks("new_payee_large")
        self.assertEqual(self.keys(res, "new_payee_large"), [odd])
        self.assertIn("times the usual", res["findings"][0]["reason"])

    def test_tiny_charge_from_a_new_payee(self):
        t = self.spend("2026-10-02", 0.99, "SKETCHY SITE")
        self.spend("2026-10-03", 0.99, "SKETCHY SITE")        # second tiny charge is not a "first"
        self.assertEqual(self.keys(self.run_checks("tiny_new_payee"), "tiny_new_payee"), [t])

    def test_price_change_on_a_steady_subscription(self):
        for m in ("2026-05", "2026-06", "2026-07", "2026-08", "2026-09"):
            self.spend(f"{m}-03", 15.49, "STREAMING CO", envelope="Bills: Subscriptions")
        jump = self.spend("2026-10-03", 22.99, "STREAMING CO", envelope="Bills: Subscriptions")
        res = self.run_checks("price_change")
        self.assertEqual(self.keys(res, "price_change"), [jump])
        self.assertIn("up 48%", res["findings"][0]["reason"])

    def test_small_drift_and_irregular_payees_are_not_price_changes(self):
        for m, amt in zip(("2026-05", "2026-06", "2026-07", "2026-08", "2026-09", "2026-10"), (15.49, 15.49, 15.99, 15.49, 15.99, 16.49)):
            self.spend(f"{m}-03", amt, "STREAMING CO")
        for d, amt in (("2026-06-01", 20), ("2026-06-09", 90), ("2026-08-30", 20), ("2026-09-02", 150), ("2026-10-15", 20)):
            self.spend(d, amt, "RANDOM PLACE")
        self.assertEqual(self.run_checks("price_change")["findings"], [])

    def test_a_new_repeating_charge(self):
        self.spend("2026-08-12", 9.99, "NEW APP")
        self.spend("2026-09-12", 9.99, "NEW APP")
        res = self.run_checks("new_recurring")
        self.assertEqual(len(res["findings"]), 1)
        self.assertIn("starting 2026-08-12", res["findings"][0]["reason"])
        self.spend("2026-09-13", 9.99, "ONE OFF CLUB")
        self.assertEqual(len(self.run_checks("new_recurring")["findings"]), 1)

    def test_uncategorized(self):
        u = self.spend("2026-10-05", 12, "NO ENVELOPE", envelope="")
        self.assertEqual(self.keys(self.run_checks("uncategorized"), "uncategorized"), [u])

    def eid(self, name):
        c = self.conn()
        try:
            return c.execute("SELECT id FROM envelopes WHERE name = ?", (name,)).fetchone()[0]
        finally:
            c.close()


class ScopeAndReviewTests(AuditCase):
    def test_a_scoped_run_only_reports_what_involves_the_new_rows(self):
        a = self.spend("2026-10-05", 42, "CAFE")
        self.spend("2026-10-05", 42, "CAFE")
        old = self.spend("2026-05-05", 77, "DINER")
        self.spend("2026-05-05", 77, "DINER")
        self.assertEqual(len(self.run_checks("dup_same_day")["findings"]), 1)           # old one is outside the 120 days
        self.assertEqual(len(self.run_checks("dup_same_day", lookback_days=500)["findings"]), 2)
        scoped = self.run_checks("dup_same_day", scope=[old])
        self.assertEqual([f["txns"][0]["name"] for f in scoped["findings"]], ["DINER"])
        self.assertTrue(a)

    def test_marking_ok_hides_a_finding_until_asked_to_show_it(self):
        self.spend("2026-10-05", 42, "CAFE")
        self.spend("2026-10-05", 42, "CAFE")
        f = self.run_checks("dup_same_day")["findings"][0]
        self.call(self.app.audit_dismiss, {"items": [{"check": f["check"], "key": f["key"]}]})
        again = self.run_checks("dup_same_day")
        self.assertEqual((again["findings"], again["hidden_reviewed"]), ([], 1))
        self.assertEqual(len(self.run_checks("dup_same_day", include_reviewed=True)["findings"]), 1)

    def test_findings_are_ordered_worst_first(self):
        self.spend("2026-10-05", 5, "SMALL CAFE")
        self.spend("2026-10-05", 5, "SMALL CAFE")
        self.spend("2026-10-06", 400, "BIG SHOP")
        self.spend("2026-10-06", 400, "BIG SHOP")
        sev = [f["severity"] for f in self.run_checks("dup_same_day")["findings"]]
        self.assertEqual(sev, ["high", "low"])


class SettingsAndApiTests(AuditCase):
    def test_defaults_then_saving_choices(self):
        cfg = self.call(self.app.audit_config, method="GET")
        self.assertIn("dup_same_day", cfg["manual"])
        self.assertNotIn("dup_amount_date", cfg["manual"])
        self.assertIn("tiny_new_payee", cfg["import"])
        self.assertEqual((cfg["large_amount"], cfg["lookback_days"], cfg["auto_open"]), (500.0, 120, True))
        self.call(self.app.audit_save_settings, {"import": ["dup_same_day", "bogus"], "large_amount": 250, "lookback_days": 60, "auto_open": False})
        cfg = self.call(self.app.audit_config, method="GET")
        self.assertEqual((cfg["import"], cfg["large_amount"], cfg["lookback_days"], cfg["auto_open"]), (["dup_same_day"], 250.0, 60, False))
        for bad in ({"large_amount": 0}, {"lookback_days": 0}, {"large_amount": "x"}):
            with self.assertRaises(HttpError):
                self.call(self.app.audit_save_settings, bad)

    def test_run_endpoint_uses_the_dialogs_numbers(self):
        self.spend("2026-10-02", 150, "NEW PLACE")
        self.assertEqual(self.call(self.app.audit_run, {"checks": ["new_payee_large"]})["total"], 0)
        res = self.call(self.app.audit_run, {"checks": ["new_payee_large"], "large_amount": 100, "lookback_days": 4000})
        self.assertEqual(res["total"], 1)


class ImportHookTests(AuditCase):
    def import_file(self, name, text):
        self.write_csv(name, text)
        c = self.conn()
        info = import_engine.inspect_file_header(import_engine.resolve_input_file(name, self.input_dir), {})
        pv = import_engine.build_preview(c, [info])
        c.close()
        return self.call(self.app.import_commit, {"records": pv["records"], "files": [name]})

    HEADER = "Transaction Date,Post Date,Description,Category,Type,Amount,Memo\n"

    def test_import_runs_the_chosen_checks_on_the_new_rows_only(self):
        self.spend("2026-09-01", 9.99, "STREAMING CO")      # already saved; would be a dup of nothing
        res = self.import_file("a.csv", self.HEADER +
                               "10/05/2026,10/06/2026,CORNER CAFE,Food,Sale,-42.10,\n"
                               "10/05/2026,10/06/2026,CORNER CAFE,Food,Sale,-42.10,\n"
                               "10/06/2026,10/07/2026,ZELLE TO NEW PERSON ON 10/06 REF ABC123,Transfer,Sale,-75.00,\n"
                               "10/07/2026,10/08/2026,SKETCHY SITE,Shopping,Sale,-0.99,\n")
        audit_res = res["audit"]
        self.assertEqual(sorted(audit_res["counts"]), ["dup_same_day", "new_person_paid", "tiny_new_payee"])
        self.assertTrue(res["audit_auto_open"])

    def test_nothing_is_reported_when_import_checks_are_all_off(self):
        self.call(self.app.audit_save_settings, {"import": []})
        res = self.import_file("a.csv", self.HEADER + "10/05/2026,10/06/2026,CAFE,Food,Sale,-9.00,\n10/05/2026,10/06/2026,CAFE,Food,Sale,-9.00,\n")
        self.assertEqual(res["audit"]["total"], 0)

    def test_pending_tracks_the_last_import_and_clears_when_marked_ok(self):
        self.import_file("a.csv", self.HEADER + "10/05/2026,10/06/2026,CAFE,Food,Sale,-9.00,\n10/05/2026,10/06/2026,CAFE,Food,Sale,-9.00,\n")
        pend = self.call(self.app.audit_pending, method="GET")
        self.assertEqual(pend["total"], 1)
        f = pend["findings"][0]
        self.call(self.app.audit_dismiss, {"items": [{"check": f["check"], "key": f["key"]}]})
        self.assertEqual(self.call(self.app.audit_pending, method="GET")["total"], 0)
        self.assertEqual(self.call(self.app.audit_config, method="GET")["pending"], 0)


if __name__ == "__main__":
    unittest.main()
