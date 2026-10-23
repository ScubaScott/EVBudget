import csv
import io
import unittest
from datetime import date

from helpers import TempAppCase
from evbudget import envelopes as env_mod, ledger
from evbudget.http_util import Response

TODAY = date(2026, 10, 22)


class P5Case(TempAppCase):
    def eid(self, name):
        c = self.conn()
        try:
            return c.execute("SELECT id FROM envelopes WHERE name = ?", (name,)).fetchone()[0]
        finally:
            c.close()

    def pay_series(self, name, dates, amounts, envelope="Bills: Subscriptions"):
        for i, d in enumerate(dates):
            amt = amounts[i] if isinstance(amounts, list) else amounts
            self.add_txn(date=d, amount=-amt, name=name, raw_name=name, envelope=envelope)

    def discover(self):
        c = self.conn()
        try:
            return env_mod.discover_bills(c, TODAY)
        finally:
            c.close()


class DiscoveryTests(P5Case):
    def test_monthly_payee_is_found_with_schedule_amount_and_envelope(self):
        self.pay_series("NETFLIX 4412", ["2026-06-03", "2026-07-03", "2026-08-03", "2026-09-03", "2026-10-03"], [15.49, 15.49, 15.49, 15.49, 15.49])
        found = self.discover()
        self.assertEqual(len(found), 1)
        c = found[0]
        self.assertEqual((c["freq"], c["interval"], c["amount"], c["payments"], c["envelope"], c["confidence"]),
                         ("monthly", 1, 15.49, 5, "Bills: Subscriptions", "high"))
        self.assertEqual(c["next_due"], "2026-11-03")

    def test_other_schedules_weekly_biweekly_semiannual_yearly(self):
        self.pay_series("WEEKLY CO", ["2026-09-18", "2026-09-25", "2026-10-02", "2026-10-09", "2026-10-16"], 20, "Gas")
        self.pay_series("BIWEEKLY CO", ["2026-08-07", "2026-08-21", "2026-09-04", "2026-09-18", "2026-10-02"], 55, "Gas")
        self.pay_series("SEMI INS", ["2025-03-15", "2025-09-14", "2026-03-16", "2026-09-15"], 1851, "Insurance: Auto")
        self.pay_series("YEARLY FEE", ["2023-10-10", "2024-10-09", "2025-10-11"], 99, "Bills: One Time")
        got = {c["name"]: (c["freq"], c["interval"]) for c in self.discover()}
        self.assertEqual(got, {"WEEKLY CO": ("weekly", 1), "BIWEEKLY CO": ("weekly", 2), "SEMI INS": ("monthly", 6), "YEARLY FEE": ("yearly", 1)})

    def test_irregular_or_varying_payees_are_not_offered(self):
        self.pay_series("RANDOM SHOP", ["2026-06-01", "2026-06-09", "2026-07-30", "2026-08-02", "2026-10-15"], 20)
        self.pay_series("PRICEY", ["2026-07-03", "2026-08-03", "2026-09-03"], [20, 200, 25])
        self.pay_series("TWICE ONLY", ["2026-09-03", "2026-10-03"], 30)
        self.assertEqual(self.discover(), [])

    def test_stopped_payees_are_left_out(self):
        self.pay_series("OLD GYM", ["2025-01-05", "2025-02-05", "2025-03-05", "2025-04-05"], 30)
        self.assertEqual(self.discover(), [])

    def test_payees_that_already_have_a_bill_or_were_dismissed_are_left_out(self):
        self.pay_series("SPOTIFY", ["2026-07-05", "2026-08-05", "2026-09-05", "2026-10-05"], 11.99)
        self.pay_series("HULU", ["2026-07-07", "2026-08-07", "2026-09-07", "2026-10-07"], 9.99)
        self.assertEqual(sorted(c["name"] for c in self.discover()), ["HULU", "SPOTIFY"])
        self.call(self.app.save_bill, {"envelope_id": self.eid("Bills: Subscriptions"), "name": "Music", "match_phrase": "SPOTIFY",
                                       "amount": 11.99, "freq": "monthly", "interval": 1, "anchor_date": "2026-10-05"})
        self.assertEqual([c["name"] for c in self.discover()], ["HULU"])
        self.call(self.app.dismiss_bill_candidate, {"key": "HULU"})
        self.assertEqual(self.discover(), [])

    def test_same_day_repeats_count_once_and_store_numbers_are_ignored(self):
        self.pay_series("KWIK GYM #12", ["2026-08-03", "2026-08-03", "2026-09-03", "2026-10-03"], 25)
        self.assertEqual(len(self.discover()), 1)

    def test_endpoint(self):
        self.pay_series("NETFLIX", ["2026-07-03", "2026-08-03", "2026-09-03", "2026-10-03"], 15.49)
        res = self.call(self.app.discover_bills, method="GET")
        self.assertEqual([c["name"] for c in res["candidates"]], ["NETFLIX"])


class CsvTests(P5Case):
    def test_ledger_csv_has_running_balance_and_a_download_name(self):
        gid = str(self.eid("Groceries"))
        self.call(self.app.setup_go_live, {"date": "2026-10-01", "opening": {gid: 100}})
        self.add_txn(date="2026-10-03", amount=-30.5, envelope="Groceries", name="KROGER, INC")
        resp = self.call(self.app.ledger_csv, query={"envelope_id": gid}, method="GET")
        self.assertIsInstance(resp, Response)
        self.assertIn("attachment", resp.headers["Content-Disposition"])
        self.assertIn("Groceries_history.csv", resp.headers["Content-Disposition"])
        rows = list(csv.reader(io.StringIO(resp.body.decode("utf-8-sig"))))
        self.assertEqual(rows[0], ["Date", "Type", "Description", "Note", "Amount", "Balance"])
        self.assertEqual([r[2:] for r in rows[1:]][1], ["KROGER, INC", "expense", "-30.50", "69.50"])  # comma survives quoting
        self.assertEqual(rows[1][4:], ["100.00", "100.00"])

    def test_csv_download_over_http_sets_headers(self):
        resp = self.call(self.app.ledger_csv, query={"envelope_id": str(self.eid("Groceries"))}, method="GET")
        self.assertTrue(resp.content_type.startswith("text/csv"))

    def test_unknown_envelope_is_a_404_style_error(self):
        from evbudget.http_util import HttpError
        with self.assertRaises(HttpError):
            self.call(self.app.ledger_csv, query={"envelope_id": "9999"}, method="GET")


class WarningTests(P5Case):
    def test_uncategorized_spending_since_go_live_is_counted(self):
        self.call(self.app.setup_go_live, {"date": "2026-10-01", "opening": {}})
        self.add_txn(date="2026-09-20", amount=-99, envelope="")              # before go-live: ignored
        self.add_txn(date="2026-10-05", amount=-40, envelope="")
        self.add_txn(date="2026-10-06", amount=-10, envelope="Old Deleted Envelope")
        self.add_txn(date="2026-10-07", amount=25, envelope="", txn_kind="refund")   # a Venmo-style reimbursement
        self.add_txn(date="2026-10-08", amount=-5, envelope="Groceries")              # fine
        data = self.call(self.app.envelope_balances, query={"as_of": "2026-10-20"}, method="GET")
        self.assertEqual(data["uncategorized"], {"count": 3, "spent": 50.0, "refunded": 25.0, "net": -25.0})
        self.assertTrue(data["over_allocated"])  # 50 spent with no envelope, 25 refunded: Unallocated is down 25

    def test_over_allocation_shows_when_unallocated_goes_negative(self):
        self.call(self.app.setup_go_live, {"date": "2026-10-01", "opening": {}})
        self.add_txn(date="2026-10-05", amount=-40, envelope="")
        data = self.call(self.app.envelope_balances, query={"as_of": "2026-10-20"}, method="GET")
        self.assertTrue(data["over_allocated"])
        self.assertEqual(data["unallocated"], -40.0)


if __name__ == "__main__":
    unittest.main()
