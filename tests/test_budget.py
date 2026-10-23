import unittest
from datetime import date

from helpers import TempAppCase
from evbudget import budget, envelopes as env_mod, store
from evbudget.http_util import HttpError

TODAY = date(2026, 10, 22)


class BudgetCase(TempAppCase):
    def env_id(self, name):
        c = self.conn()
        try:
            return c.execute("SELECT id FROM envelopes WHERE name = ?", (name,)).fetchone()[0]
        finally:
            c.close()

    def spend(self, month, amount, envelope="Groceries", day="15", name="STORE"):
        return self.add_txn(date=f"{month}-{day}", amount=-amount, envelope=envelope, name=name, raw_name=name)

    def suggestions(self):
        c = self.conn()
        try:
            return budget.suggestions(c, TODAY)
        finally:
            c.close()


class BillTests(BudgetCase):
    def bill(self, **kw):
        body = {"envelope_id": self.env_id("Insurance: Auto"), "name": "Travelers", "amount": 1800, "freq": "monthly",
                "interval": 6, "anchor_date": "2026-03-15"}
        body.update(kw)
        return self.call(self.app.save_bill, body)

    def test_semi_annual_bill_costs_a_sixth_a_month_and_sets_next_due(self):
        self.bill()
        c = self.conn()
        e = [x for x in env_mod.list_envelopes(c, TODAY) if x["name"] == "Insurance: Auto"][0]
        c.close()
        b = e["bills"][0]
        self.assertEqual(b["monthly_amount"], 300.0)
        self.assertEqual(b["next_due_date"], "2027-03-15")  # anchor Mar/Sep; after today (Oct 22) -> Mar 2027
        self.assertEqual(b["upcoming"][:2], ["2027-03-15", "2027-09-15"])
        self.assertTrue(e["budget_derived"])
        self.assertEqual(e["effective_budget"], 300.0)

    def test_several_bills_add_up_and_inactive_ones_do_not(self):
        self.bill()
        self.bill(name="Second", amount=120, interval=1, anchor_date="2026-01-31")
        self.bill(name="Off", amount=999, interval=1, active=0)
        c = self.conn()
        e = [x for x in env_mod.list_envelopes(c, TODAY) if x["name"] == "Insurance: Auto"][0]
        c.close()
        self.assertEqual(e["bills_monthly"], 420.0)

    def test_weekly_and_yearly_monthly_equivalents(self):
        w = {"name": "Weekly", "amount": 52, "freq": "weekly", "interval": 1, "anchor_date": "2026-01-02",
             "envelope_id": self.env_id("Groceries")}
        self.call(self.app.save_bill, w)
        self.call(self.app.save_bill, dict(w, name="Yearly", amount=1200, freq="yearly", anchor_date="2026-06-01"))
        bills = {b["name"]: b for b in self.call(self.app.get_bills, method="GET")["bills"]}
        self.assertAlmostEqual(bills["Weekly"]["monthly_amount"], 225.33, places=2)
        self.assertEqual(bills["Yearly"]["monthly_amount"], 100.0)

    def test_validation(self):
        for bad in ({"name": ""}, {"freq": "daily"}, {"anchor_date": "2026-02-30"}, {"amount": "abc"}, {"envelope_id": 9999}):
            with self.assertRaises(HttpError, msg=str(bad)):
                self.bill(**bad)

    def test_overdue_flag(self):
        self.bill(interval=1, anchor_date="2026-01-31", next_due_date="2026-09-01")
        b = self.call(self.app.get_bills, method="GET")["bills"][0]
        self.assertTrue(b["overdue"])

    def test_preview_dates_and_delete(self):
        res = self.call(self.app.bill_preview, query={"freq": "monthly", "interval": "1", "anchor": "2026-01-31"}, method="GET")
        self.assertEqual(len(res["dates"]), 3)
        bid = self.bill()["id"]
        self.call(self.app.delete_bill, {"id": bid})
        self.assertEqual(self.call(self.app.get_bills, method="GET")["bills"], [])

    def test_history_guesses_schedule(self):
        for m in ("2026-03", "2026-04", "2026-05", "2026-06"):
            self.spend(m, 85, envelope="Bills: Every Month", name="CENTERPOINT")
        self.add_txn(date="2026-02-10", amount=-1000, envelope="Insurance: Home", name="HOME INS", raw_name="HOME INS")
        c = self.conn()
        cp = env_mod.bill_history(c, self.env_id("Bills: Every Month"))[0]
        home = env_mod.bill_history(c, self.env_id("Insurance: Home"))[0]
        c.close()
        self.assertEqual((cp["name"], cp["payments"], cp["freq"], cp["interval"]), ("CENTERPOINT", 4, "monthly", 1))
        self.assertEqual((home["freq"], home["typical_amount"]), ("yearly", 1000.0))

    def test_history_uses_the_typical_amount_not_a_one_off(self):
        for m, amt in (("2026-03", 1851), ("2026-09", 1852), ("2026-10", 0.10)):
            self.add_txn(date=f"{m}-16", amount=-amt, envelope="Insurance: Auto", name="TRAVELERS", raw_name="TRAVELERS")
        c = self.conn()
        t = env_mod.bill_history(c, self.env_id("Insurance: Auto"))[0]
        c.close()
        self.assertEqual((t["typical_amount"], t["last_amount"]), (1851.0, 0.10))


class EnvelopeSettingsTests(BudgetCase):
    def test_save_and_validate(self):
        gid = self.env_id("Groceries")
        self.call(self.app.save_envelope_settings, {"id": gid, "monthly_budget": 650.5, "rollover_mode": "rollover", "kind": "spending"})
        c = self.conn()
        row = c.execute("SELECT * FROM envelopes WHERE id = ?", (gid,)).fetchone()
        c.close()
        self.assertEqual((row["monthly_budget_cents"], row["rollover_mode"]), (65050, "rollover"))
        for bad in ({"kind": "bogus"}, {"rollover_mode": "never"}, {"monthly_budget": -5}, {"monthly_budget": "x"}, {"goal_date": "nope"}):
            with self.assertRaises(HttpError):
                self.call(self.app.save_envelope_settings, dict({"id": gid}, **bad))
        with self.assertRaises(HttpError):
            self.call(self.app.save_envelope_settings, {"id": 0, "monthly_budget": 1})

    def test_switching_an_envelope_off_hides_it_from_pickers(self):
        gid = self.env_id("Gas")
        self.call(self.app.save_envelope_settings, {"id": gid, "active": 0})
        self.assertNotIn("Gas", self.call(self.app.get_envelopes, method="GET")["envelopes"])


class SuggestionTests(BudgetCase):
    def setUp(self):
        super().setUp()
        self.add_txn(date="2026-01-02", amount=-5, envelope="Gas", name="EARLY", raw_name="EARLY")  # data starts in January

    def test_spending_average_median_range_and_confidence(self):
        for m, amt in zip(("2026-04", "2026-05", "2026-06", "2026-07", "2026-08", "2026-09"), (400, 500, 600, 500, 450, 550)):
            self.spend(m, amt)
        self.add_txn(date="2026-09-20", amount=50, envelope="Groceries", txn_kind="refund", name="R", raw_name="R")  # refund nets out
        s = self.suggestions()["suggestions"][str(self.env_id("Groceries"))]
        self.assertEqual(s["basis"], "average")
        self.assertEqual((s["min"], s["max"]), (400.0, 600.0))
        self.assertEqual(s["months"], 6)
        self.assertEqual(s["confidence"], "high")
        self.assertAlmostEqual(s["avg"], 491.67, places=2)  # (2950 - 50) / 6
        self.assertEqual(s["suggested"], 495.0)             # rounded up to the next $5
        self.assertEqual(s["median"], 500.0)

    def test_current_month_and_excluded_rows_are_ignored(self):
        self.spend("2026-10", 9999)
        self.add_txn(date="2026-08-10", amount=-9999, envelope="Groceries", is_included=0, txn_kind="transfer")
        self.spend("2026-08", 100)
        s = self.suggestions()["suggestions"][str(self.env_id("Groceries"))]
        self.assertEqual(s["avg"], round(100 / 6, 2))

    def test_annual_bill_is_spread_over_twelve_months(self):
        self.add_txn(date="2026-03-10", amount=-9779.90, envelope="Insurance: Home", name="HOME", raw_name="HOME")
        s = self.suggestions()["suggestions"][str(self.env_id("Insurance: Home"))]
        self.assertEqual(s["basis"], "annualized")
        self.assertEqual(s["suggested"], 815.0)  # 9779.90 / 12 = 814.99, up to the next $5
        self.assertTrue(s["partial_history"])

    def test_bill_envelope_with_bills_suggests_the_bills_total(self):
        self.call(self.app.save_bill, {"envelope_id": self.env_id("Insurance: Auto"), "name": "T", "amount": 1800,
                                       "freq": "monthly", "interval": 6, "anchor_date": "2026-03-15"})
        s = self.suggestions()["suggestions"][str(self.env_id("Insurance: Auto"))]
        self.assertEqual((s["basis"], s["suggested"]), ("bills", 300.0))

    def test_savings_gets_no_suggestion_and_summary_compares_with_income(self):
        self.call(self.app.save_envelope_settings, {"id": self.env_id("Gas"), "kind": "savings", "monthly_budget": 200})
        self.call(self.app.save_envelope_settings, {"id": self.env_id("Groceries"), "monthly_budget": 600})
        out = self.suggestions()
        self.assertIsNone(out["suggestions"][str(self.env_id("Gas"))]["suggested"])
        self.assertEqual(out["summary"]["total_budgeted"], 800.0)
        self.assertEqual(out["summary"]["surplus"], round(out["summary"]["planning_income"] - 800.0, 2))

    def test_accept_skips_envelopes_whose_budget_comes_from_bills(self):
        auto, groc = self.env_id("Insurance: Auto"), self.env_id("Groceries")
        self.call(self.app.save_bill, {"envelope_id": auto, "name": "T", "amount": 100, "freq": "monthly", "interval": 1, "anchor_date": "2026-01-01"})
        res = self.call(self.app.accept_budget, {"items": [{"envelope_id": auto, "amount": 999}, {"envelope_id": groc, "amount": 450}]})
        self.assertEqual(res["updated"], 1)
        c = self.conn()
        got = {r["id"]: r["monthly_budget_cents"] for r in c.execute("SELECT id, monthly_budget_cents FROM envelopes")}
        c.close()
        self.assertEqual((got[groc], got[auto]), (45000, 0))


class TemplateTests(BudgetCase):
    def paychecks(self):
        for d in ("2026-05-01", "2026-05-15", "2026-05-29"):
            self.add_txn(date=d, amount=3500, envelope="", txn_kind="paycheck", income_source_id=1, person_id=1, income_subtype="regular")
        for d in ("2026-05-08", "2026-05-22"):
            self.add_txn(date=d, amount=1700, envelope="", txn_kind="paycheck", income_source_id=2, person_id=2, income_subtype="regular")

    def test_generated_lines_add_back_up_to_the_budget(self):
        self.paychecks()
        budgets = {"Groceries": 800, "Gas": 250, "Bills: Mortgage": 1167}
        for name, amount in budgets.items():
            self.call(self.app.save_envelope_settings, {"id": self.env_id(name), "monthly_budget": amount})
        monthly_total = 0.0
        for sid, per_month in ((1, 26 / 12), (2, 26 / 12)):
            gen = self.call(self.app.generate_template, {"income_source_id": sid})
            monthly_total += gen["total"] * per_month
            self.assertEqual({l["envelope_id"] for l in gen["lines"]}, {self.env_id(n) for n in budgets})
        # Whole-dollar rounding keeps the monthly total within a few dollars of the budget.
        self.assertAlmostEqual(monthly_total, sum(budgets.values()), delta=10)

    def test_save_replaces_lines_and_validates(self):
        a, b = self.env_id("Groceries"), self.env_id("Gas")
        self.call(self.app.save_template, {"income_source_id": 1, "lines": [{"envelope_id": a, "amount": 300}, {"envelope_id": b, "amount": 50}]})
        self.call(self.app.save_template, {"income_source_id": 1, "lines": [{"envelope_id": b, "amount": 75.5}]})
        t = [x for x in self.call(self.app.get_templates, method="GET")["templates"] if x["income_source_id"] == 1][0]
        self.assertEqual(t["lines"], [{"envelope_id": b, "amount": 75.5}])
        for bad in ([{"envelope_id": a, "amount": 1}, {"envelope_id": a, "amount": 2}], [{"envelope_id": 9999, "amount": 1}],
                    [{"envelope_id": a, "amount": -1}]):
            with self.assertRaises(HttpError):
                self.call(self.app.save_template, {"income_source_id": 1, "lines": bad})

    def test_generate_needs_a_schedule(self):
        c = self.conn()
        c.execute("INSERT INTO income_sources (person_id, name, match_phrase, cadence_freq, cadence_interval) VALUES (1, 'X', 'X', 'once', 1)")
        sid = c.execute("SELECT MAX(id) FROM income_sources").fetchone()[0]
        c.commit()
        c.close()
        with self.assertRaises(HttpError):
            self.call(self.app.generate_template, {"income_source_id": sid})


class GoLiveTests(BudgetCase):
    def test_opening_balances_are_recorded_and_total_matches(self):
        c = self.conn()
        unalloc = env_mod.system_envelope_id(c)
        c.close()
        opening = {str(self.env_id("Groceries")): 420.55, str(self.env_id("Gas")): 100, str(unalloc): 1000, str(self.env_id("Shopping")): -50}
        res = self.call(self.app.setup_go_live, {"date": "2026-11-01", "opening": opening})
        self.assertEqual(res["opening_total"], 1470.55)
        status = self.call(self.app.setup_status, method="GET")
        self.assertEqual(status["go_live_date"], "2026-11-01")
        self.assertEqual(status["opening"][str(self.env_id("Shopping"))], -50.0)
        self.assertEqual(status["opening_total"], 1470.55)

    def test_cannot_set_twice_without_reset_and_reset_replaces(self):
        gid = str(self.env_id("Groceries"))
        self.call(self.app.setup_go_live, {"date": "2026-11-01", "opening": {gid: 100}})
        with self.assertRaises(HttpError):
            self.call(self.app.setup_go_live, {"date": "2026-12-01", "opening": {gid: 5}})
        self.call(self.app.setup_go_live, {"date": "2026-12-01", "opening": {gid: 250}, "reset": True})
        status = self.call(self.app.setup_status, method="GET")
        self.assertEqual((status["go_live_date"], status["opening"][gid], status["opening_total"]), ("2026-12-01", 250.0, 250.0))

    def test_validation(self):
        with self.assertRaises(HttpError):
            self.call(self.app.setup_go_live, {"date": "soon", "opening": {}})
        with self.assertRaises(HttpError):
            self.call(self.app.setup_go_live, {"date": "2026-11-01", "opening": {"9999": 5}})
        with self.assertRaises(HttpError):
            self.call(self.app.setup_go_live, {"date": "2026-11-01", "opening": {str(self.env_id("Gas")): "lots"}})
        self.assertIsNone(self.call(self.app.setup_status, method="GET")["go_live_date"])  # nothing half-saved

    def test_envelope_holding_money_cannot_be_deleted(self):
        gid = str(self.env_id("Groceries"))
        self.call(self.app.setup_go_live, {"date": "2026-11-01", "opening": {gid: 100}})
        with self.assertRaises(HttpError):
            self.call(self.app.delete_envelope, {"envelope": "Groceries", "reassign_to": "Gas"})
        self.call(self.app.delete_envelope, {"envelope": "Shopping", "reassign_to": "Gas"})  # no money: fine

    def test_deleting_an_envelope_removes_its_bills(self):
        self.call(self.app.save_bill, {"envelope_id": self.env_id("Gas"), "name": "X", "amount": 5, "freq": "monthly", "interval": 1, "anchor_date": "2026-01-01"})
        self.call(self.app.delete_envelope, {"envelope": "Gas", "reassign_to": "Shopping"})
        self.assertEqual(self.call(self.app.get_bills, method="GET")["bills"], [])


class SettingsTests(BudgetCase):
    def test_round_trip_and_validation(self):
        self.call(self.app.save_settings, {"bonus_threshold_pct": 40, "refund_lookback_days": "90"})
        got = self.call(self.app.get_settings, method="GET")
        self.assertEqual((got["bonus_threshold_pct"], got["refund_lookback_days"]), ("40", "90"))
        for bad in ({"bonus_threshold_pct": 0}, {"bonus_threshold_pct": "x"}):
            with self.assertRaises(HttpError):
                self.call(self.app.save_settings, bad)
        self.assertEqual(self.call(self.app.get_settings, method="GET")["bonus_threshold_pct"], "40")

    def test_unknown_keys_are_ignored(self):
        self.call(self.app.save_settings, {"go_live_date": "2020-01-01", "import_column_presets": "{}"})
        self.assertIsNone(self.call(self.app.get_settings, method="GET")["go_live_date"])


if __name__ == "__main__":
    unittest.main()
