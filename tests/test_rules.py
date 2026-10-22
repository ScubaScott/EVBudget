import unittest

from helpers import TempAppCase
from evbudget import income, rules, store
from evbudget.textutil import display_name


class ClassifyTests(TempAppCase):
    def classify(self, raw, amount, date="2026-06-01", check="", trans_type="", ctx=None):
        c = self.conn()
        ctx = ctx or rules.load_context(c)
        c.close()
        return rules.classify(
            ctx, raw_name=raw, payee=display_name(raw, check), amount=amount, date_iso=date,
            check_num=check, trans_type=trans_type,
        ), ctx

    def test_paycheck_goes_to_person_and_source(self):
        res, _ = self.classify("CARGILL INCORPOR PAYROLL    260605 9341434863253W6 REILING,SCOTT L", 3500.12)
        self.assertEqual(res["txn_kind"], "paycheck")
        self.assertEqual(res["income_subtype"], "regular")
        c = self.conn()
        scott = c.execute("SELECT id FROM people WHERE name = 'Scott'").fetchone()[0]
        c.close()
        self.assertEqual(res["person_id"], scott)
        self.assertEqual(res["envelope"], "")

    def test_bonus_is_flagged_against_earlier_regular_checks(self):
        raw = "CARGILL INCORPOR PAYROLL    1"
        _, ctx = self.classify(raw, 3500.0, date="2026-05-01")
        self.classify(raw, 3500.0, date="2026-05-15", ctx=ctx)
        self.classify(raw, 3450.0, date="2026-05-29", ctx=ctx)
        bonus, _ = self.classify(raw, 10474.36, date="2026-06-12", ctx=ctx)
        self.assertEqual(bonus["income_subtype"], "bonus")
        nxt, _ = self.classify(raw, 3500.0, date="2026-06-26", ctx=ctx)
        self.assertEqual(nxt["income_subtype"], "regular")

    def test_bonus_does_not_raise_the_typical_amount(self):
        raw = "CARGILL INCORPOR PAYROLL    1"
        _, ctx = self.classify(raw, 3500.0, date="2026-05-01")
        self.classify(raw, 10000.0, date="2026-05-15", ctx=ctx)  # bonus, not added to history
        second, _ = self.classify(raw, 10000.0, date="2026-05-29", ctx=ctx)
        self.assertEqual(second["income_subtype"], "bonus")

    def test_first_ever_paycheck_is_regular(self):
        res, _ = self.classify("HEALTHPARTNERS I PAYROLL 123", 1700.0)
        self.assertEqual(res["income_subtype"], "regular")

    def test_kid_transfer_is_kept_as_shopping(self):
        res, _ = self.classify("ONLINE TRANSFER TO REILING J WAY2SAVE", -20.0)
        self.assertEqual((res["txn_kind"], res["envelope"], res["is_included"]), ("expense", "Shopping", True))

    def test_internal_transfers_and_card_payments_are_excluded(self):
        res, _ = self.classify("ONLINE TRANSFER FROM REILING S WAY2SAVE SAVINGS", 500.0)
        self.assertEqual((res["txn_kind"], res["is_included"]), ("transfer", False))
        res, _ = self.classify("CHASE CREDIT CRD AUTOPAY 260925", -9543.10)
        self.assertEqual((res["txn_kind"], res["is_included"]), ("cc_payment", False))
        res, _ = self.classify("SAMS CLUB        PAYMENT    Sep 23 52133", -1424.41)
        self.assertEqual(res["txn_kind"], "cc_payment")
        res, _ = self.classify("Some Card Payment", 100.0, trans_type="Payment")
        self.assertEqual(res["txn_kind"], "cc_payment")

    def test_venmo_is_a_reimbursement_awaiting_review(self):
        res, _ = self.classify("VENMO            CASHOUT    260718 1051763821123   KENDRA REILING", 200.0)
        self.assertEqual(res["txn_kind"], "refund")
        self.assertEqual(res["envelope"], "")
        self.assertEqual(res["needs_review"], 1)

    def test_irs_refund_is_other_income(self):
        res, _ = self.classify("IRS TREAS 310 TAX REF 030926", 1330.0)
        self.assertEqual((res["txn_kind"], res["income_label"]), ("other_income", "Tax Refund"))

    def test_refund_goes_back_to_envelope_of_recent_purchase(self):
        c = self.conn()
        ctx = rules.load_context(c)
        c.close()
        ctx.expenses.add("2026-05-20", "TARGET 00012724", "Groceries")
        res, _ = self.classify("TARGET T-1272", 22.49, date="2026-06-01", ctx=ctx)
        self.assertEqual((res["txn_kind"], res["envelope"], res["needs_review"]), ("refund", "Groceries", 0))

    def test_refund_ignores_purchases_outside_lookback(self):
        c = self.conn()
        ctx = rules.load_context(c)
        c.close()
        ctx.expenses.add("2025-10-01", "TARGET 00012724", "Groceries")
        res, _ = self.classify("TARGET T-1272", 22.49, date="2026-06-01", ctx=ctx)
        self.assertEqual(res["txn_kind"], "other_income")

    def test_refund_uses_envelope_rule_when_no_purchase_found(self):
        res, _ = self.classify("COSTCO WHSE #123", 45.0)
        self.assertEqual((res["txn_kind"], res["envelope"]), ("refund", "Groceries"))

    def test_unknown_positive_is_other_income_for_review(self):
        res, _ = self.classify("MYSTERY DEPOSIT", 80.0)
        self.assertEqual((res["txn_kind"], res["needs_review"]), ("other_income", 1))

    def test_statement_credit_needs_review_without_a_forced_kind(self):
        res, _ = self.classify("STATEMENT CREDIT", 512.37)
        self.assertEqual(res["needs_review"], 1)
        self.assertEqual(res["txn_kind"], "other_income")

    def test_check_number_goes_to_one_time_bills(self):
        res, _ = self.classify("CHECK # 647", -250.0, check="647")
        self.assertEqual((res["envelope"], res["name"], res["match_source"]), ("Bills: One Time", "CHECK #647", "Check Heuristic"))

    def test_default_is_shopping(self):
        res, _ = self.classify("SOME RANDOM SHOP", -12.0)
        self.assertEqual((res["envelope"], res["match_source"]), ("Shopping", "Default Fallback"))

    def test_gas_brand_needs_a_whole_word(self):
        self.assertEqual(self.classify("BP #1234 SHAKOPEE", -40.0)[0]["envelope"], "Gas")
        self.assertEqual(self.classify("BPAY TRANSFER SERVICES", -40.0)[0]["envelope"], "Shopping")

    def test_walmart_com_is_not_groceries(self):
        self.assertEqual(self.classify("WALMART SUPERCENTER 123", -40.0)[0]["envelope"], "Groceries")
        self.assertEqual(self.classify("WALMART.COM 8009666546", -40.0)[0]["envelope"], "Shopping")

    def test_html_escaped_bank_text_still_matches(self):
        c = self.conn()
        store.insert_rule(c, {"phrase": "AT&T MOBILITY EPAY", "stage": "envelope", "origin": "user",
                              "priority": 100, "set_envelope": "Bills: Every Month"})
        c.commit()
        c.close()
        res, _ = self.classify("AT&amp;T MOBILITY EPAY", -85.0)
        self.assertEqual(res["envelope"], "Bills: Every Month")

    def test_payee_only_rule_does_not_block_envelope_rules(self):
        c = self.conn()
        store.insert_rule(c, {"phrase": "BILL PAY CENTERPOINT ENER", "stage": "payee", "origin": "user",
                              "priority": 100, "set_payee": "CENTERPOINT ENERGY"})
        c.commit()
        c.close()
        res, _ = self.classify("BILL PAY CENTERPOINT ENER   RECURRING", -90.0)
        self.assertEqual((res["name"], res["envelope"]), ("CENTERPOINT ENERGY", "Bills: Every Month"))

    def test_user_rule_beats_core_rule(self):
        c = self.conn()
        store.insert_rule(c, {"phrase": "COSTCO GAS", "stage": "envelope", "origin": "user",
                              "priority": 99, "set_envelope": "Gas"})
        c.commit()
        c.close()
        self.assertEqual(self.classify("COSTCO GAS #12", -50.0)[0]["envelope"], "Gas")


class IncomeHelperTests(unittest.TestCase):
    def test_typical_is_median_of_last_six_before_date(self):
        hist = [("2026-01-0%d" % i, a) for i, a in enumerate([100, 100, 100, 100, 100, 100, 300], start=1)]
        self.assertEqual(income.typical_amount(hist, "2026-02-01"), 100)
        self.assertIsNone(income.typical_amount(hist, "2026-01-01"))

    def test_split_deposits_do_not_drag_the_typical_amount_down(self):
        # Each pay date has a big deposit and a small side deposit.
        hist = []
        for d in ("2026-01-02", "2026-01-16", "2026-01-30"):
            hist += [(d, 2827.62), (d, 100.0)]
        self.assertEqual(income.typical_amount(hist, "2026-02-13"), 2827.62)
        self.assertEqual(income.subtype_for(2850.0, 2827.62, 50), "regular")

    def test_override_wins(self):
        self.assertEqual(income.typical_amount([("2026-01-01", 5)], "2026-02-01", override=1000), 1000)

    def test_threshold(self):
        self.assertEqual(income.subtype_for(5300, 3500, 50), "bonus")
        self.assertEqual(income.subtype_for(5200, 3500, 50), "regular")
        self.assertEqual(income.subtype_for(9999, None, 50), "regular")


if __name__ == "__main__":
    unittest.main()
