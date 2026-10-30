import unittest
from datetime import date

from helpers import TempAppCase
from evbudget import billmatch, import_engine, ledger, store
from evbudget.http_util import HttpError

D = date


class LedgerCase(TempAppCase):
    GO_LIVE = "2026-10-01"

    def eid(self, name):
        c = self.conn()
        try:
            return c.execute("SELECT id FROM envelopes WHERE name = ?", (name,)).fetchone()[0]
        finally:
            c.close()

    def start(self, opening=None, go_live=None):
        c = self.conn()
        unalloc = c.execute("SELECT id FROM envelopes WHERE kind = 'system'").fetchone()[0]
        c.close()
        body = {str(self.eid(k) if k != "Unallocated" else unalloc): v for k, v in (opening or {}).items()}
        self.call(self.app.setup_go_live, {"date": go_live or self.GO_LIVE, "opening": body})

    def budget(self, name, amount, **kw):
        self.call(self.app.save_envelope_settings, dict({"id": self.eid(name), "monthly_budget": amount}, **kw))

    def states(self, as_of):
        c = self.conn()
        try:
            data = ledger.envelope_states(c, as_of)
        finally:
            c.close()
        return data, {e["name"]: e for e in data["envelopes"]}

    def bal(self, as_of, name):
        data, by = self.states(as_of)
        return data["unallocated"] if name == "Unallocated" else by[name]["balance"]

    def pay(self, d, amt, **kw):
        return self.add_txn(date=d, amount=amt, envelope="", name="PAYROLL", raw_name="PAYROLL", txn_kind="paycheck",
                            income_source_id=1, person_id=1, income_subtype="regular", **kw)

    def invariant(self, as_of):
        c = self.conn()
        try:
            r = ledger.check_invariant(c, as_of)
        finally:
            c.close()
        self.assertTrue(r["ok"], r)

    def confirm(self, lines, txn=None, as_of=D(2026, 10, 20), **kw):
        return self.call(self.app.allocation_confirm, dict({"income_txn_id": txn, "lines": lines}, **kw), query={"as_of": as_of.isoformat()})


class BalanceTests(LedgerCase):
    def test_nothing_counts_before_go_live(self):
        self.add_txn(date="2026-09-15", amount=-99, envelope="Groceries")
        self.start({"Groceries": 200})
        self.assertEqual(self.bal(D(2026, 10, 5), "Groceries"), 200.0)

    def test_balances_follow_spending_refunds_and_income(self):
        self.start({"Groceries": 200, "Unallocated": 50})
        self.add_txn(date="2026-10-03", amount=-80, envelope="Groceries")
        self.add_txn(date="2026-10-04", amount=30, envelope="Groceries", txn_kind="refund")
        self.add_txn(date="2026-10-05", amount=-10, envelope="")  # no envelope: comes out of Unallocated
        self.pay("2026-10-06", 3000)
        self.add_txn(date="2026-10-07", amount=-500, is_included=0, txn_kind="transfer")  # excluded rows never count
        self.assertEqual(self.bal(D(2026, 10, 10), "Groceries"), 150.0)
        self.assertEqual(self.bal(D(2026, 10, 10), "Unallocated"), 3040.0)
        self.invariant(D(2026, 10, 10))

    def test_unknown_envelope_name_falls_back_to_unallocated(self):
        self.start({})
        self.add_txn(date="2026-10-03", amount=-40, envelope="Deleted Thing")
        self.assertEqual(self.bal(D(2026, 10, 5), "Unallocated"), -40.0)
        self.invariant(D(2026, 10, 5))

    def test_disabled_until_go_live(self):
        data, _ = self.states(D(2026, 10, 5))
        self.assertFalse(data["enabled"])


class MarkerTests(LedgerCase):
    def test_spending_marker_paces_down_through_the_month(self):
        self.start({}, go_live="2026-11-01")
        self.budget("Groceries", 600)
        _, by = self.states(D(2026, 11, 16))
        self.assertEqual(by["Groceries"]["marker"], 300.0)   # day 16 of 30, nothing carried in
        self.assertEqual(by["Groceries"]["capacity"], 600.0)
        _, by = self.states(D(2026, 11, 1))
        self.assertEqual(by["Groceries"]["marker"], 600.0)

    def test_carry_in_comes_from_last_month_not_from_this_months_fills(self):
        self.start({"Groceries": 100}, go_live="2026-10-01")
        self.budget("Groceries", 600, rollover_mode="rollover")
        t = self.pay("2026-11-01", 3000)
        self.confirm([{"envelope_id": self.eid("Groceries"), "amount": 600}], txn=t, as_of=D(2026, 11, 2))
        _, by = self.states(D(2026, 11, 16))
        self.assertEqual(by["Groceries"]["carry_in"], 100.0)
        self.assertEqual(by["Groceries"]["marker"], 400.0)  # 100 + 600 x (1 - 15/30)

    def test_status_labels(self):
        self.start({"Gas": 10, "Shopping": -5}, go_live="2026-11-01")
        self.budget("Groceries", 600)
        self.budget("Gas", 600)
        self.add_txn(date="2026-11-03", amount=300, envelope="Groceries", txn_kind="refund")  # halfway funded by day 16
        _, by = self.states(D(2026, 11, 16))
        self.assertEqual(by["Groceries"]["status"], "on_track")
        self.assertEqual(by["Gas"]["status"], "behind")
        self.assertEqual(by["Shopping"]["status"], "overdrawn")
        self.budget("Insurance: Motorcycle", 0)
        _, by = self.states(D(2026, 11, 16))
        self.assertIn(by["Insurance: Motorcycle"]["status"], ("on_track", "funded"))

    def test_semi_annual_bill_marker_climbs_toward_the_due_date(self):
        self.start({}, go_live="2026-09-15")
        self.call(self.app.save_bill, {"envelope_id": self.eid("Insurance: Auto"), "name": "T", "amount": 1800, "freq": "monthly",
                                       "interval": 6, "anchor_date": "2026-09-15", "next_due_date": "2027-03-15"})
        _, by = self.states(D(2026, 9, 15))
        self.assertEqual((by["Insurance: Auto"]["capacity"], by["Insurance: Auto"]["marker"]), (1800.0, 0.0))
        _, by = self.states(D(2026, 12, 14))   # about halfway through the cycle
        self.assertAlmostEqual(by["Insurance: Auto"]["marker"], 900.0, delta=8)
        _, by = self.states(D(2027, 3, 20))
        self.assertEqual(by["Insurance: Auto"]["marker"], 1800.0)

    def test_savings_marker_runs_from_opening_to_goal(self):
        self.start({"Gas": 0}, go_live="2026-01-01")
        self.call(self.app.save_envelope_settings, {"id": self.eid("Gas"), "kind": "savings", "goal": 1000, "goal_date": "2027-01-01"})
        _, by = self.states(D(2026, 7, 2))
        self.assertAlmostEqual(by["Gas"]["marker"], 500.0, delta=5)
        self.assertEqual(by["Gas"]["capacity"], 1000.0)


class FillTests(LedgerCase):
    def test_fill_moves_money_and_void_restores_it_exactly(self):
        self.start({"Groceries": 50})
        t = self.pay("2026-10-09", 3500)
        before = {n: self.bal(D(2026, 10, 20), n) for n in ("Groceries", "Gas", "Unallocated")}
        res = self.confirm([{"envelope_id": self.eid("Groceries"), "amount": 400.25}, {"envelope_id": self.eid("Gas"), "amount": 100}], txn=t)
        self.assertEqual(self.bal(D(2026, 10, 20), "Groceries"), 450.25)
        self.assertEqual(self.bal(D(2026, 10, 20), "Unallocated"), 3500 - 500.25)
        self.invariant(D(2026, 10, 20))
        self.call(self.app.allocation_void, {"allocation_id": res["allocation_id"]})
        self.assertEqual({n: self.bal(D(2026, 10, 20), n) for n in before}, before)
        self.invariant(D(2026, 10, 20))

    def test_queue_lists_deposits_that_are_not_fully_funded(self):
        self.start({})
        a, b, full = self.pay("2026-10-09", 3500), self.pay("2026-10-23", 3500), self.pay("2026-10-16", 1000)
        self.add_txn(date="2026-09-25", amount=3500, envelope="", txn_kind="paycheck", income_source_id=1)  # before go-live
        self.confirm([{"envelope_id": self.eid("Groceries"), "amount": 100}], txn=a, as_of=D(2026, 10, 25))
        self.confirm([{"envelope_id": self.eid("Groceries"), "amount": 1000}], txn=full, as_of=D(2026, 10, 25))
        c = self.conn()
        queue = ledger.fill_queue(c)
        c.close()
        self.assertEqual([q["txn_id"] for q in queue], [b, a])
        self.assertEqual((queue[1]["partial"], queue[1]["remaining"]), (True, 3400.0))
        self.assertFalse(queue[0]["partial"])

    def test_partial_flag_in_transactions_payload(self):
        self.start({})
        t = self.pay("2026-10-09", 1000)
        self.confirm([{"envelope_id": self.eid("Groceries"), "amount": 100}], txn=t)
        x = [x for x in self.call(self.app.get_transactions, method="GET")["transactions"] if x["txn_id"] == t][0]
        self.assertEqual((x["funded"], x["partially_funded"]), (False, True))

    def test_cannot_allocate_more_than_is_in_unallocated(self):
        self.start({})
        t = self.pay("2026-10-09", 1000)
        with self.assertRaises(HttpError):
            self.confirm([{"envelope_id": self.eid("Groceries"), "amount": 1000.01}], txn=t)
        self.assertEqual(self.bal(D(2026, 10, 20), "Groceries"), 0.0)  # nothing half-saved

    def test_second_allocation_needs_replace_and_replace_swaps_the_lines(self):
        self.start({})
        t = self.pay("2026-10-09", 1000)
        first = self.confirm([{"envelope_id": self.eid("Groceries"), "amount": 300}], txn=t)
        with self.assertRaises(HttpError) as ctx:
            self.confirm([{"envelope_id": self.eid("Gas"), "amount": 50}], txn=t)
        self.assertEqual(ctx.exception.status, 409)
        self.confirm([{"envelope_id": self.eid("Gas"), "amount": 50}], txn=t, replace_allocation_id=first["allocation_id"])
        self.assertEqual((self.bal(D(2026, 10, 20), "Groceries"), self.bal(D(2026, 10, 20), "Gas")), (0.0, 50.0))
        self.invariant(D(2026, 10, 20))

    def test_validation(self):
        self.start({})
        t = self.pay("2026-10-09", 1000)
        for lines in ([], [{"envelope_id": 9999, "amount": 5}], [{"envelope_id": self.eid("Gas"), "amount": -5}], [{"envelope_id": self.eid("Gas"), "amount": 0}]):
            with self.assertRaises(HttpError):
                self.confirm(lines, txn=t)
        expense = self.add_txn(date="2026-10-10", amount=-5)
        with self.assertRaises(HttpError):
            self.confirm([{"envelope_id": self.eid("Gas"), "amount": 5}], txn=expense)

    def test_draft_uses_the_template_for_regular_paychecks_but_not_bonuses(self):
        self.start({}, go_live="2026-10-01")
        self.call(self.app.save_template, {"income_source_id": 1, "lines": [{"envelope_id": self.eid("Groceries"), "amount": 300}]})
        regular = self.pay("2026-10-09", 3500)
        bonus = self.add_txn(date="2026-10-10", amount=9000, envelope="", txn_kind="paycheck", income_source_id=1, income_subtype="bonus")
        d1 = self.call(self.app.allocation_draft, query={"txn_id": regular, "as_of": "2026-10-20"}, method="GET")
        d2 = self.call(self.app.allocation_draft, query={"txn_id": bonus, "as_of": "2026-10-20"}, method="GET")
        self.assertEqual((d1["from_template"], d1["lines"]), (True, [{"envelope_id": self.eid("Groceries"), "amount": 300.0}]))
        self.assertEqual((d2["from_template"], d2["lines"]), (False, []))

    def test_suggest_covers_overdrawn_then_bills_then_spending(self):
        self.start({"Shopping": -40}, go_live="2026-11-01")
        self.budget("Groceries", 600)
        self.call(self.app.save_bill, {"envelope_id": self.eid("Insurance: Auto"), "name": "T", "amount": 1800, "freq": "monthly",
                                       "interval": 6, "anchor_date": "2026-10-15", "next_due_date": "2027-04-15"})
        res = self.call(self.app.allocation_suggest, {"amount": 100000}, query={"as_of": "2026-11-16"})["lines"]
        got = {l["envelope_id"]: l["amount"] for l in res}
        self.assertEqual(got[self.eid("Shopping")], 40.0)          # back to zero first
        self.assertEqual(got[self.eid("Groceries")], 300.0)        # up to its marker (day 16 of 30)
        self.assertGreater(got[self.eid("Insurance: Auto")], 0)
        small = self.call(self.app.allocation_suggest, {"amount": 25}, query={"as_of": "2026-11-16"})["lines"]
        self.assertEqual(small, [{"envelope_id": self.eid("Shopping"), "amount": 25.0}])  # runs out in priority order


class GuardTests(LedgerCase):
    def test_allocated_paycheck_cannot_be_deleted_or_excluded_until_voided(self):
        self.start({})
        t = self.pay("2026-10-09", 1000)
        self.confirm([{"envelope_id": self.eid("Groceries"), "amount": 100}], txn=t)
        with self.assertRaises(HttpError) as ctx:
            self.call(self.app.delete_transaction, {"txn_id": t})
        self.assertEqual((ctx.exception.status, ctx.exception.extra.get("needs_void")), (409, True))
        with self.assertRaises(HttpError):
            self.call(self.app.update_transaction, {"txn_id": t, "is_included": 0})
        with self.assertRaises(HttpError):
            self.call(self.app.update_transaction, {"txn_id": t, "txn_kind": "expense"})
        self.call(self.app.update_transaction, {"txn_id": t, "amount": 1100})  # amount edits are fine
        self.call(self.app.delete_transaction, {"txn_id": t, "void_allocation": True})
        self.assertEqual(self.bal(D(2026, 10, 20), "Groceries"), 0.0)
        self.invariant(D(2026, 10, 20))

    def test_funded_flag_in_transactions_payload(self):
        self.start({})
        t = self.pay("2026-10-09", 1000)
        self.confirm([{"envelope_id": self.eid("Groceries"), "amount": 1000}], txn=t)
        data = self.call(self.app.get_transactions, method="GET")
        self.assertEqual(data["go_live_date"], self.GO_LIVE)
        self.assertTrue([x for x in data["transactions"] if x["txn_id"] == t][0]["funded"])


class LeftoverTests(LedgerCase):
    def test_leftovers_stay_in_their_envelope_across_months(self):
        self.start({"Groceries": 120, "Insurance: Auto": 500, "Gas": 30}, go_live="2026-10-01")
        self.add_txn(date="2026-10-10", amount=-50, envelope="Groceries")
        self.add_txn(date="2026-10-11", amount=-60, envelope="Gas")
        data, by = self.states(D(2026, 11, 5))
        self.assertEqual(by["Groceries"]["balance"], 70.0)
        self.assertEqual(by["Gas"]["balance"], -30.0)
        self.assertEqual(by["Insurance: Auto"]["balance"], 500.0)
        self.assertEqual(data["unallocated"], 0.0)
        self.invariant(D(2026, 11, 5))

    def test_no_month_end_moves_are_ever_created(self):
        self.start({"Groceries": 100}, go_live="2026-09-01")
        self.assertEqual(self.bal(D(2026, 12, 20), "Unallocated"), 0.0)
        c = self.conn()
        n = c.execute("SELECT COUNT(*) FROM envelope_moves WHERE move_type = 'sweep'").fetchone()[0]
        c.close()
        self.assertEqual(n, 0)

    def test_old_start_fresh_envelopes_are_switched_to_carry_over(self):
        self.start({"Groceries": 100}, go_live="2026-09-01")
        c = self.conn()
        c.execute("UPDATE envelopes SET rollover_mode = 'reset' WHERE name = 'Groceries'")
        c.commit()
        store.create_schema(c)
        mode = c.execute("SELECT rollover_mode FROM envelopes WHERE name = 'Groceries'").fetchone()[0]
        c.close()
        self.assertEqual(mode, "rollover")

    def test_earlier_sweeps_still_count_and_are_left_alone(self):
        self.start({"Groceries": 100}, go_live="2026-09-01")
        c = self.conn()
        c.execute("""INSERT INTO envelope_moves (date, from_envelope_id, to_envelope_id, amount_cents, move_type, period_key, note, created_at)
                     VALUES ('2026-10-01', ?, ?, 4000, 'sweep', '2026-09', 'Month-end leftover', '2026-10-01T00:00:00')""",
                  (self.eid("Groceries"), self.eid("Unallocated")))
        c.commit()
        c.close()
        self.assertEqual(self.bal(D(2026, 11, 20), "Groceries"), 60.0)
        self.assertEqual(self.bal(D(2026, 11, 20), "Unallocated"), 40.0)
        self.add_txn(date="2026-09-05", amount=-1, envelope="Groceries")   # editing the past does not rewrite it
        self.assertEqual(self.bal(D(2026, 11, 20), "Unallocated"), 40.0)


class BackdatedFillTests(LedgerCase):
    def test_filling_old_paychecks_after_a_backdated_go_live_gives_the_right_leftovers(self):
        self.start({}, go_live="2026-01-01")                       # backdated to the start of the year
        jan, feb = self.pay("2026-01-09", 3000), self.pay("2026-02-06", 3000)
        self.add_txn(date="2026-01-20", amount=-400, envelope="Groceries")
        self.add_txn(date="2026-02-20", amount=-500, envelope="Groceries")
        # Looking at balances before any fills exist must not change what the fills give later.
        before = self.bal(D(2026, 4, 15), "Groceries")
        self.assertEqual(before, -900.0)
        g = [{"envelope_id": self.eid("Groceries"), "amount": 600}]
        self.confirm(g, txn=jan, as_of=D(2026, 4, 15))
        self.confirm(g, txn=feb, as_of=D(2026, 4, 15))
        # Jan: 600 - 400 = 200 left. Feb: 600 - 500 = 100 left. Both stay in Groceries.
        self.assertEqual(self.bal(D(2026, 4, 15), "Groceries"), 300.0)
        self.assertEqual(self.bal(D(2026, 4, 15), "Unallocated"), 6000 - 1200)
        self.invariant(D(2026, 4, 15))
        # Undoing a January fill takes its 600 back out.
        c = self.conn()
        alloc = c.execute("SELECT id FROM allocations WHERE status = 'confirmed' ORDER BY id").fetchone()[0]
        c.close()
        self.call(self.app.allocation_void, {"allocation_id": alloc})
        self.assertEqual(self.bal(D(2026, 4, 15), "Groceries"), -300.0)  # -400 + 600 - 500
        self.invariant(D(2026, 4, 15))


class StartFreshTests(LedgerCase):
    def test_rerunning_setup_with_a_later_date_wipes_old_fills_and_starts_clean(self):
        self.start({"Groceries": 100}, go_live="2026-01-01")
        old = self.pay("2026-01-09", 3000)
        self.confirm([{"envelope_id": self.eid("Groceries"), "amount": 500}], txn=old, as_of=D(2026, 3, 1))
        self.call(self.app.move_transfer, {"from_envelope_id": self.eid("Groceries"), "to_envelope_id": self.eid("Gas"), "amount": 50, "date": "2026-01-20"},
                  query={"as_of": "2026-03-01"})
        keep = self.pay("2026-03-06", 3000)
        self.call(self.app.setup_go_live, {"date": "2026-03-01", "opening": {str(self.eid("Groceries")): 75, str(self.eid("Gas")): 20}, "reset": True})
        # Only the fresh opening balances count; January's fill and transfer are gone.
        self.assertEqual((self.bal(D(2026, 3, 2), "Groceries"), self.bal(D(2026, 3, 2), "Gas")), (75.0, 20.0))
        self.invariant(D(2026, 3, 2))
        c = self.conn()
        queue = [q["txn_id"] for q in ledger.fill_queue(c)]
        backups = c.execute("SELECT COUNT(*) FROM allocations WHERE status = 'voided'").fetchone()[0]
        c.close()
        self.assertEqual(queue, [keep])      # the January paycheck is history; March's still needs filling
        self.assertEqual(backups, 1)
        # And the January paycheck can't be mistaken for fillable income any more.
        with self.assertRaises(HttpError):
            self.confirm([{"envelope_id": self.eid("Gas"), "amount": 10}], txn=old, as_of=D(2026, 3, 2))

    def test_fills_on_or_after_the_new_date_are_kept(self):
        self.start({}, go_live="2026-01-01")
        late = self.pay("2026-03-06", 3000)
        self.confirm([{"envelope_id": self.eid("Groceries"), "amount": 200}], txn=late, as_of=D(2026, 3, 10))
        self.call(self.app.setup_go_live, {"date": "2026-03-01", "opening": {}, "reset": True})
        self.assertEqual(self.bal(D(2026, 3, 10), "Groceries"), 200.0)


class TransferTests(LedgerCase):
    def test_transfer_covers_an_overdrawn_envelope_and_can_be_undone(self):
        self.start({"Shopping": -40, "Unallocated": 100})
        res = self.call(self.app.move_transfer, {"from_envelope_id": self.ledger_unalloc(), "to_envelope_id": self.eid("Shopping"), "amount": 40, "note": "cover"},
                        query={"as_of": "2026-10-10"})
        self.assertEqual((self.bal(D(2026, 10, 10), "Shopping"), self.bal(D(2026, 10, 10), "Unallocated")), (0.0, 60.0))
        self.invariant(D(2026, 10, 10))
        self.call(self.app.move_void, {"id": res["id"]})
        self.assertEqual(self.bal(D(2026, 10, 10), "Shopping"), -40.0)

    def ledger_unalloc(self):
        c = self.conn()
        try:
            return c.execute("SELECT id FROM envelopes WHERE kind = 'system'").fetchone()[0]
        finally:
            c.close()

    def test_transfer_validation_and_opening_moves_cannot_be_voided(self):
        self.start({"Groceries": 10})
        g, gas = self.eid("Groceries"), self.eid("Gas")
        for bad in ({"from_envelope_id": g, "to_envelope_id": g, "amount": 5}, {"from_envelope_id": g, "to_envelope_id": gas, "amount": 0},
                    {"from_envelope_id": g, "to_envelope_id": gas, "amount": 5, "date": "2026-09-01"}, {"from_envelope_id": g, "to_envelope_id": 9999, "amount": 5}):
            with self.assertRaises(HttpError):
                self.call(self.app.move_transfer, bad, query={"as_of": "2026-10-10"})
        c = self.conn()
        opening_id = c.execute("SELECT id FROM envelope_moves WHERE move_type = 'opening'").fetchone()[0]
        c.close()
        with self.assertRaises(HttpError):
            self.call(self.app.move_void, {"id": opening_id})

    def test_ledger_shows_running_balance(self):
        self.start({"Groceries": 100})
        self.add_txn(date="2026-10-03", amount=-30, envelope="Groceries", name="KROGER")
        self.call(self.app.move_transfer, {"from_envelope_id": self.ledger_unalloc(), "to_envelope_id": self.eid("Groceries"), "amount": 20, "date": "2026-10-04"},
                  query={"as_of": "2026-10-10"})
        res = self.call(self.app.envelope_ledger, query={"envelope_id": str(self.eid("Groceries"))}, method="GET")
        self.assertEqual([(e["label"][:8], e["balance"]) for e in res["entries"]], [("Opening ", 100.0), ("KROGER", 70.0), ("Transfer", 90.0)])
        self.assertEqual(res["end_balance"], 90.0)

    def test_ledger_rows_for_fills_point_back_to_the_paycheck(self):
        self.start({})
        t = self.pay("2026-10-09", 1000)
        a = self.confirm([{"envelope_id": self.eid("Groceries"), "amount": 100}], txn=t)["allocation_id"]
        res = self.call(self.app.envelope_ledger, query={"envelope_id": str(self.eid("Groceries"))}, method="GET")
        fill = [e for e in res["entries"] if e["move_type"] == "fill"][0]
        self.assertEqual((fill["income_txn_id"], fill["allocation_id"]), (t, a))


class BillMatchTests(LedgerCase):
    def setUp(self):
        super().setUp()
        self.start({}, go_live="2026-10-01")
        self.bill_id = self.call(self.app.save_bill, {
            "envelope_id": self.eid("Bills: Every Month"), "name": "Internet", "match_phrase": "QUANTUM FIBER", "amount": 80,
            "freq": "monthly", "interval": 1, "anchor_date": "2026-10-12", "next_due_date": "2026-10-12"})["id"]

    def bill(self):
        c = self.conn()
        try:
            return dict(c.execute("SELECT * FROM bills WHERE id = ?", (self.bill_id,)).fetchone())
        finally:
            c.close()

    def pay_bill(self, d, amt=-80, name="QUANTUM FIBER 123"):
        tid = self.add_txn(date=d, amount=amt, envelope="Bills: Every Month", name=name, raw_name=name)
        c = self.conn()
        try:
            got = billmatch.match_transaction(c, tid)
            c.commit()
        finally:
            c.close()
        return tid, got

    def test_payment_advances_the_due_date_one_month_and_resets_the_marker(self):
        _, by = self.states(D(2026, 10, 12))
        self.assertEqual(by["Bills: Every Month"]["marker"], 80.0)  # due today: fully expected
        tid, got = self.pay_bill("2026-10-13")
        self.assertEqual(got, self.bill_id)
        b = self.bill()
        self.assertEqual((b["next_due_date"], b["last_paid_date"], b["last_paid_txn_id"]), ("2026-11-12", "2026-10-13", tid))
        _, by = self.states(D(2026, 10, 14))
        self.assertLess(by["Bills: Every Month"]["marker"], 10)  # next cycle has barely begun (2 of 31 days)

    def test_no_match_when_text_amount_or_date_is_off(self):
        self.assertIsNone(self.pay_bill("2026-10-13", name="OTHER PAYEE")[1])
        self.assertIsNone(self.pay_bill("2026-10-13", amt=-200)[1])
        self.assertIsNone(self.pay_bill("2026-09-01")[1])  # 41 days from the due date
        self.assertEqual(self.bill()["next_due_date"], "2026-10-12")

    def test_unlink_puts_the_due_date_back(self):
        tid, _ = self.pay_bill("2026-10-13")
        self.call(self.app.unlink_bill, {"txn_id": tid})
        b = self.bill()
        self.assertEqual((b["next_due_date"], b["last_paid_txn_id"]), ("2026-10-12", None))

    def test_manual_link_ignores_the_date_window(self):
        tid = self.add_txn(date="2026-08-01", amount=-80, envelope="Bills: Every Month", name="WHATEVER")
        self.call(self.app.link_bill, {"bill_id": self.bill_id, "txn_id": tid})
        self.assertEqual(self.bill()["next_due_date"], "2026-11-12")
        with self.assertRaises(HttpError):
            self.call(self.app.link_bill, {"bill_id": self.bill_id, "txn_id": tid})

    def test_one_time_bill_switches_off_after_payment(self):
        bid = self.call(self.app.save_bill, {"envelope_id": self.eid("Bills: One Time"), "name": "Fee", "amount": 50, "freq": "once",
                                             "anchor_date": "2026-10-20", "next_due_date": "2026-10-20"})["id"]
        tid = self.add_txn(date="2026-10-20", amount=-50, envelope="Bills: One Time", name="FEE")
        c = self.conn()
        billmatch.match_transaction(c, tid)
        c.commit()
        active = c.execute("SELECT active FROM bills WHERE id = ?", (bid,)).fetchone()[0]
        c.close()
        self.assertEqual(active, 0)

    def test_estimated_bill_follows_recent_payments(self):
        self.call(self.app.save_bill, {"id": self.bill_id, "envelope_id": self.eid("Bills: Every Month"), "name": "Internet",
                                       "match_phrase": "QUANTUM FIBER", "amount": 80, "amount_mode": "estimated", "freq": "monthly",
                                       "interval": 1, "anchor_date": "2026-10-12", "next_due_date": "2026-10-12", "amount_tolerance_pct": 50})
        self.pay_bill("2026-10-12", amt=-100)
        self.assertEqual(self.bill()["amount_cents"], 10000)

    def test_upcoming_and_overdue(self):
        self.call(self.app.save_bill, {"envelope_id": self.eid("Bills: Subscriptions"), "name": "Old", "amount": 10, "freq": "monthly",
                                       "interval": 1, "anchor_date": "2026-09-01", "next_due_date": "2026-09-01"})
        res = self.call(self.app.upcoming_bills, query={"as_of": "2026-10-10", "days": "30"}, method="GET")["bills"]
        self.assertEqual([(b["name"], b["overdue"]) for b in res], [("Old", True), ("Internet", False)])
        self.assertEqual(res[1]["covered"], False)

    def test_import_commit_matches_new_expenses_to_bills(self):
        self.write_csv("a.csv", "Transaction Date,Post Date,Description,Category,Type,Amount,Memo\n10/12/2026,10/13/2026,QUANTUM FIBER 9,Bills,Sale,-80.00,\n")
        c = self.conn()
        info = import_engine.inspect_file_header(import_engine.resolve_input_file("a.csv", self.input_dir), {})
        pv = import_engine.build_preview(c, [info])
        res = import_engine.commit_import(c, pv["records"], ["a.csv"])
        c.close()
        self.assertEqual(res["bills_matched"], 1)
        self.assertEqual(self.bill()["next_due_date"], "2026-11-12")


class BalancesEndpointTests(LedgerCase):
    def test_overview_and_invariant_through_a_scripted_month(self):
        self.start({"Groceries": 200, "Unallocated": 0})
        self.budget("Groceries", 600)
        p1, p2 = self.pay("2026-10-02", 3500), self.pay("2026-10-16", 3500)
        self.confirm([{"envelope_id": self.eid("Groceries"), "amount": 300}], txn=p1, as_of=D(2026, 10, 3))
        for i in range(20):
            self.add_txn(date=f"2026-10-{(i % 27) + 1:02d}", amount=-(10 + i), envelope="Groceries" if i % 2 else "Shopping")
        self.add_txn(date="2026-10-18", amount=25, envelope="Shopping", txn_kind="refund")
        self.confirm([{"envelope_id": self.eid("Gas"), "amount": 120}], txn=p2, as_of=D(2026, 10, 17))
        self.call(self.app.move_transfer, {"from_envelope_id": self.eid("Groceries"), "to_envelope_id": self.eid("Gas"), "amount": 15, "date": "2026-10-20"}, query={"as_of": "2026-10-21"})
        for day in (D(2026, 10, 10), D(2026, 10, 31), D(2026, 11, 20)):
            self.invariant(day)
        data = self.call(self.app.envelope_balances, query={"as_of": "2026-10-25"}, method="GET")
        self.assertTrue(data["enabled"])
        self.assertTrue(data["invariant"]["ok"])
        self.assertEqual(data["queue_count"], 2)  # both deposits are only partly split
        self.assertEqual(data["total_in_envelopes"], round(sum(e["balance"] for e in data["envelopes"]), 2))


if __name__ == "__main__":
    unittest.main()
