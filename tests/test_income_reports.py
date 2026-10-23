import unittest
from datetime import date

from helpers import TempAppCase
from evbudget import income, recurrence
from evbudget.http_util import HttpError


class RecurrenceTests(unittest.TestCase):
    def test_biweekly(self):
        a = date(2026, 10, 9)
        self.assertEqual(recurrence.next_occurrence("weekly", 2, a, a), date(2026, 10, 23))
        self.assertEqual(recurrence.previous_occurrence("weekly", 2, a, date(2026, 10, 20)), a)
        self.assertEqual(recurrence.occurrences_between("weekly", 2, a, date(2026, 1, 1), date(2026, 1, 31)),
                         [date(2026, 1, 2), date(2026, 1, 16), date(2026, 1, 30)])

    def test_month_end_is_clamped(self):
        got = recurrence.occurrences_between("monthly", 1, date(2026, 1, 31), date(2026, 1, 1), date(2026, 4, 30))
        self.assertEqual(got, [date(2026, 1, 31), date(2026, 2, 28), date(2026, 3, 31), date(2026, 4, 30)])

    def test_semiannual_and_yearly_and_once(self):
        self.assertEqual(recurrence.occurrences_between("monthly", 6, date(2026, 3, 15), date(2026, 1, 1), date(2027, 12, 31)),
                         [date(2026, 3, 15), date(2026, 9, 15), date(2027, 3, 15), date(2027, 9, 15)])
        self.assertEqual(recurrence.next_occurrence("yearly", 1, date(2025, 6, 1), date(2026, 6, 1)), date(2027, 6, 1))
        self.assertEqual(recurrence.next_occurrence("once", 1, date(2026, 1, 1), date(2026, 2, 1)), None)
        self.assertEqual(recurrence.occurrences_per_year("weekly", 2), 26)

    def test_three_paycheck_months_in_2026(self):
        # Fridays every two weeks from 2026-10-09: 2026 has two 3-paycheck months.
        months = {}
        for d in recurrence.occurrences_between("weekly", 2, date(2026, 10, 9), date(2026, 1, 1), date(2026, 12, 31)):
            months[d.strftime("%m")] = months.get(d.strftime("%m"), 0) + 1
        self.assertEqual([m for m, n in months.items() if n == 3], ["01", "07"])


class IncomeReportTests(TempAppCase):
    def pay(self, d, amt, source=1, person=1, subtype="regular", **kw):
        return self.add_txn(date=d, amount=amt, name="PAYROLL", raw_name="PAYROLL", envelope="", txn_kind="paycheck",
                            income_source_id=source, person_id=person, income_subtype=subtype, **kw)

    def test_planning_income_is_typical_times_paychecks_per_year(self):
        for i, d in enumerate(["2026-05-01", "2026-05-15", "2026-05-29", "2026-06-12"]):
            self.pay(d, 3500.0 + i)           # Scott / source 1
        for d in ["2026-05-08", "2026-05-22", "2026-06-05"]:
            self.pay(d, 1700.0, source=2, person=2)
        self.pay("2026-06-12", 10000.0, subtype="bonus")  # a bonus must not count
        plan = self.call(self.app.get_income_sources, method="GET")["planning"]
        scott = 3501.5 * 26 / 12
        kendra = 1700.0 * 26 / 12
        self.assertAlmostEqual(plan["by_person"]["Scott"], round(scott, 2), places=2)
        self.assertAlmostEqual(plan["by_person"]["Kendra"], round(kendra, 2), places=2)
        self.assertAlmostEqual(plan["monthly_total"], round(scott, 2) + round(kendra, 2), places=2)

    def test_changing_a_paycheck_to_bonus_changes_planning_income(self):
        a = self.pay("2026-05-01", 3500.0)
        b = self.pay("2026-05-15", 5000.0)
        before = income.planning_income(self.conn())["by_person"]["Scott"]
        self.call(self.app.update_transaction, {"txn_id": b, "txn_kind": "paycheck", "income_subtype": "bonus", "person_id": 1})
        after = income.planning_income(self.conn())["by_person"]["Scott"]
        self.assertAlmostEqual(after, round(3500.0 * 26 / 12, 2), places=2)
        self.assertGreater(before, after)
        self.assertTrue(a)

    def test_manual_typical_override_wins(self):
        self.pay("2026-05-01", 3500.0)
        src = self.call(self.app.get_income_sources, method="GET")["sources"][0]
        self.call(self.app.save_income_source, {
            "id": src["id"], "name": src["name"], "match_phrase": src["match_phrase"], "person_id": src["person_id"],
            "cadence_freq": "weekly", "cadence_interval": 2, "cadence_anchor_date": "2026-10-09", "typical_amount": 4000})
        s = self.call(self.app.get_income_sources, method="GET")["sources"][0]
        self.assertEqual((s["typical"], s["typical_source"]), (4000.0, "manual"))

    def test_monthly_series_match_sql_totals(self):
        self.pay("2026-05-01", 3500.0)
        self.pay("2026-05-15", 3500.0)
        self.pay("2026-05-08", 1700.0, source=2, person=2)
        self.pay("2026-05-20", 9000.0, subtype="bonus")
        self.add_txn(date="2026-05-10", amount=300.0, envelope="", txn_kind="other_income", income_label="Tax Refund")
        self.add_txn(date="2026-05-11", amount=-200.0, envelope="Groceries")
        self.add_txn(date="2026-05-12", amount=50.0, envelope="Groceries", txn_kind="refund")
        self.add_txn(date="2026-05-13", amount=-999.0, is_included=0, txn_kind="transfer", exclusion_reason="x")
        res = self.call(self.app.income_summary, query={"from": "2026-05-01", "to": "2026-06-30"}, method="GET")
        may = res["monthly"][0]
        self.assertEqual(may["month"], "2026-05")
        self.assertEqual(may["series"], {"Scott regular": 7000.0, "Kendra regular": 1700.0, "Bonus": 9000.0, "Other income": 300.0})
        self.assertEqual(may["income"], 18000.0)
        self.assertEqual(may["spend"], 150.0)  # 200 expense less 50 refund; excluded transfer ignored
        self.assertEqual(res["monthly"][1]["income"], 0.0)
        cards = res["cards"]
        self.assertEqual(cards["bonus_total"], 9000.0)
        self.assertEqual(cards["income_by_person"], {"Scott": 16000.0, "Kendra": 1700.0})
        self.assertEqual(cards["other_by_label"], {"Tax Refund": 300.0})
        self.assertEqual(cards["total_income"], 18000.0)

    def test_trend_lists_points_per_source_with_bonus_marked(self):
        self.pay("2026-05-01", 3500.0)
        self.pay("2026-05-15", 9000.0, subtype="bonus")
        res = self.call(self.app.income_summary, query={"from": "2026-01-01", "to": "2026-12-31"}, method="GET")
        scott = [t for t in res["trend"] if t["name"] == "Cargill"][0]
        self.assertEqual([(p["date"], p["subtype"]) for p in scott["points"]], [("2026-05-01", "regular"), ("2026-05-15", "bonus")])

    def test_extra_paycheck_months_are_marked(self):
        res = self.call(self.app.income_summary, query={"from": "2026-01-01", "to": "2026-12-31"}, method="GET")
        flagged = {m["month"]: m["extra_paycheck_sources"] for m in res["monthly"] if m["extra_paycheck_sources"]}
        # Cargill (anchor Oct 9): Jan, Jul. HealthPartners (anchor Oct 16): May, Oct.
        self.assertEqual({m: sorted(v) for m, v in flagged.items()},
                         {"2026-01": ["Cargill"], "2026-05": ["HealthPartners"], "2026-07": ["Cargill"], "2026-10": ["HealthPartners"]})

    def test_next_pay_date_follows_the_last_paycheck(self):
        self.pay("2026-10-09", 3500.0)
        res = self.call(self.app.income_summary, query={"from": "2026-01-01", "to": "2026-12-31"}, method="GET")
        self.assertEqual(res["cards"]["next_pay_dates"]["Scott"], "2026-10-23")

    def test_source_crud_and_validation(self):
        res = self.call(self.app.save_income_source, {"name": "Side gig", "match_phrase": "ACME PAY", "person_id": 1,
                                                       "cadence_freq": "monthly", "cadence_interval": 1, "cadence_anchor_date": "2026-01-15"})
        sid = res["id"]
        with self.assertRaises(HttpError):
            self.call(self.app.save_income_source, {"name": "", "match_phrase": "X", "person_id": 1})
        with self.assertRaises(HttpError):
            self.call(self.app.save_income_source, {"name": "A", "match_phrase": "X", "person_id": 1, "cadence_freq": "daily"})
        with self.assertRaises(HttpError):
            self.call(self.app.save_income_source, {"name": "A", "match_phrase": "X", "person_id": 1, "cadence_anchor_date": "2026-02-30"})
        self.add_txn(amount=10.0, txn_kind="paycheck", income_source_id=sid, envelope="")
        out = self.call(self.app.delete_income_source, {"id": sid})
        self.assertTrue(out["deactivated"])  # has history, so it is switched off rather than deleted
        gone = self.call(self.app.save_income_source, {"name": "Temp", "match_phrase": "TMP", "person_id": 1, "cadence_anchor_date": "2026-01-01"})["id"]
        self.assertFalse(self.call(self.app.delete_income_source, {"id": gone})["deactivated"])

    def test_new_source_can_retag_existing_deposits(self):
        t = self.add_txn(amount=500.0, name="ACME PAY 123", raw_name="ACME PAY 123", envelope="", txn_kind="other_income",
                         needs_review=1)
        prev = self.call(self.app.income_source_preview, query={"phrase": "acme pay"}, method="GET")
        self.assertEqual(prev["count"], 1)
        res = self.call(self.app.save_income_source, {"name": "Acme", "match_phrase": "ACME PAY", "person_id": 2,
                                                       "cadence_freq": "monthly", "cadence_anchor_date": "2026-01-15",
                                                       "apply_to_existing": True})
        self.assertEqual(res["retagged"], 1)
        row = self.get_txn(t)
        self.assertEqual((row["txn_kind"], row["person_id"]), ("paycheck", 2))

    def test_people_crud(self):
        pid = self.call(self.app.save_person, {"name": "Alex"})["id"]
        with self.assertRaises(HttpError):
            self.call(self.app.save_person, {"name": "alex"})
        self.call(self.app.save_person, {"id": pid, "name": "Alex", "color": "#ff0000", "active": 0})
        people = {p["name"]: p for p in self.call(self.app.get_people, method="GET")["people"]}
        self.assertEqual((people["Alex"]["color"], people["Alex"]["active"]), ("#ff0000", 0))

    def test_marking_a_deposit_as_paycheck_links_its_income_source(self):
        t = self.add_txn(amount=3500.0, name="x", raw_name="CARGILL INCORPOR PAYROLL 99", envelope="", txn_kind="other_income")
        self.call(self.app.update_transaction, {"txn_id": t, "txn_kind": "paycheck"})
        row = self.get_txn(t)
        self.assertEqual((row["income_source_id"], row["person_id"]), (1, 1))


if __name__ == "__main__":
    unittest.main()
