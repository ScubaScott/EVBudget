import os
import unittest
from datetime import date, timedelta

from helpers import TempAppCase
from evbudget import cardbills, cards, import_engine
from evbudget.http_util import HttpError

REF = date(2026, 10, 20)


def series(start, gaps, amount=10.0):
    """{iso date: amount} starting at `start` with the given day gaps between charges."""
    d = start
    out = {d.isoformat(): amount}
    for g in gaps:
        d += timedelta(days=g)
        out[d.isoformat()] = amount
    return out


class EvaluateTests(unittest.TestCase):
    def test_three_monthly_charges_are_likely(self):
        r = cardbills.evaluate(series(date(2026, 7, 1), [31, 31]), REF)
        self.assertEqual((r["confidence"], r["cadence"], r["charges"]), ("likely", "Monthly", 3))
        self.assertFalse(r["stopped"])
        self.assertEqual(r["next_expected"], "2026-11-01")

    def test_a_utility_that_varies_in_amount_is_still_found(self):
        by_date = {"2026-06-15": 380.0, "2026-07-15": 455.0, "2026-08-14": 510.0, "2026-09-15": 402.0, "2026-10-15": 330.0}
        r = cardbills.evaluate(by_date, REF)
        self.assertEqual(r["confidence"], "likely")
        self.assertFalse(r["steady_amount"])

    def test_variable_amounts_on_changing_days_are_not_a_bill(self):
        # Every 34 days: regular enough, but the day of the month wanders and the amounts differ.
        amounts = [50.0, 130.0, 20.0, 90.0, 40.0, 75.0]
        by_date = {d: a for d, a in zip(series(date(2026, 4, 2), [34] * 5), amounts)}
        self.assertIsNone(cardbills.evaluate(by_date, REF))

    def test_two_identical_monthly_charges_are_only_a_maybe(self):
        r = cardbills.evaluate(series(date(2026, 8, 20), [31]), REF)
        self.assertEqual(r["confidence"], "maybe")

    def test_two_charges_at_other_gaps_are_ignored(self):
        self.assertIsNone(cardbills.evaluate(series(date(2026, 4, 1), [91]), REF))     # quarterly
        self.assertIsNone(cardbills.evaluate(series(date(2026, 4, 1), [14]), REF))     # two weeks
        self.assertIsNone(cardbills.evaluate(series(date(2026, 4, 1), [45]), REF))     # no schedule at all

    def test_two_monthly_charges_with_different_amounts_are_ignored(self):
        self.assertIsNone(cardbills.evaluate({"2026-09-01": 12.0, "2026-10-01": 80.0}, REF))

    def test_weekly_needs_four_steady_charges_to_be_likely(self):
        self.assertEqual(cardbills.evaluate(series(date(2026, 9, 20), [7, 7]), REF)["confidence"], "maybe")
        self.assertEqual(cardbills.evaluate(series(date(2026, 9, 6), [7, 7, 7]), REF)["confidence"], "likely")

    def test_weekly_style_charges_with_varying_amounts_are_ignored(self):
        by_date = {"2026-09-05": 20.0, "2026-09-12": 65.0, "2026-09-19": 33.0, "2026-09-26": 90.0, "2026-10-03": 41.0}
        self.assertIsNone(cardbills.evaluate(by_date, REF))

    def test_every_few_weeks_schedules_are_understood(self):
        self.assertEqual(cardbills.evaluate(series(date(2026, 7, 1), [28, 28, 28]), REF)["cadence"], "Every 4 weeks")
        self.assertEqual(cardbills.evaluate(series(date(2026, 8, 1), [21, 21, 21]), REF)["cadence"], "Every 3 weeks")

    def test_yearly_needs_just_two_charges(self):
        r = cardbills.evaluate({"2024-10-05": 120.0, "2025-10-06": 120.0}, REF)
        self.assertEqual((r["confidence"], r["cadence"]), ("likely", "Yearly"))

    def test_a_long_overdue_bill_is_marked_stopped(self):
        r = cardbills.evaluate(series(date(2026, 1, 1), [31, 28, 31]), REF)
        self.assertTrue(r["stopped"])
        self.assertIsNone(r["next_expected"])


class NamingTests(unittest.TestCase):
    def test_suggestions_and_memory_keys(self):
        self.assertEqual(cards.suggest_name("Chase1484_Activity_09.csv"), "Chase 1484")
        self.assertEqual(cards.suggest_name("Checking 1_1_-10_21.csv"), "Checking")
        self.assertEqual(cards.suggest_name("Synchrony_8-22.pdf"), "Synchrony")
        self.assertEqual(cards.suggest_name("2026.csv"), "Imported")
        self.assertEqual(cards.memory_key("Chase1484_Activity_09.csv"), cards.memory_key("Chase1484_Activity_20261021.csv"))
        self.assertEqual(cards.first_file("a.csv, b.csv"), "a.csv")


class CardBillsDbTests(TempAppCase):
    def charge(self, card, day, name, amount, **kw):
        return self.add_txn(account_name=card, date=day, name=name, raw_name=name, amount=-amount, envelope="Shopping", **kw)

    def monthly(self, card, name, amount, first=date(2026, 6, 5), count=5):
        d = first
        for _ in range(count):
            self.charge(card, d.isoformat(), name, amount)
            d += timedelta(days=30)

    def find(self, card="Visa 1111"):
        c = self.conn()
        try:
            return cardbills.find_bills(c, card)
        finally:
            c.close()

    def test_only_the_chosen_cards_charges_count(self):
        self.monthly("Visa 1111", "NETFLIX.COM", 15.49)
        self.monthly("Mastercard 2222", "SPOTIFY USA", 11.99)
        found = self.find()
        self.assertEqual([i["merchant"] for i in found["items"]], ["NETFLIX.COM"])

    def test_refunds_excluded_charges_and_other_kinds_are_left_out(self):
        self.monthly("Visa 1111", "NETFLIX.COM", 15.49)
        d = date(2026, 6, 7)
        for _ in range(4):
            self.add_txn(account_name="Visa 1111", date=d.isoformat(), name="HULU", raw_name="HULU", amount=-8.0, is_included=0, txn_kind="cc_payment")
            d += timedelta(days=30)
        self.assertEqual([i["merchant"] for i in self.find()["items"]], ["NETFLIX.COM"])

    def test_two_subscriptions_at_one_merchant_are_listed_separately(self):
        self.monthly("Visa 1111", "APPLE.COM/BILL", 2.99, first=date(2026, 6, 3))
        self.monthly("Visa 1111", "APPLE.COM/BILL", 9.99, first=date(2026, 6, 14))
        amounts = sorted(i["typical_amount"] for i in self.find()["items"])
        self.assertEqual(amounts, [2.99, 9.99])

    def test_existing_bill_is_linked_by_phrase_and_by_name(self):
        self.monthly("Visa 1111", "NETFLIX.COM", 15.49)
        self.monthly("Visa 1111", "SPOTIFY USA", 11.99)
        env = self.conn().execute("SELECT id FROM envelopes WHERE name = 'Bills: Subscriptions'").fetchone()[0]
        c = self.conn()
        c.execute("INSERT INTO bills (envelope_id, name, match_phrase, amount_mode, amount_cents, freq, interval, anchor_date, next_due_date) "
                  "VALUES (?, 'Streaming', 'NETFLIX', 'fixed', 1549, 'monthly', 1, '2026-11-05', '2026-11-05')", (env,))
        c.execute("INSERT INTO bills (envelope_id, name, amount_mode, amount_cents, freq, interval, anchor_date, next_due_date) "
                  "VALUES (?, 'Spotify', 'fixed', 1199, 'monthly', 1, '2026-11-05', '2026-11-05')", (env,))
        c.commit()
        c.close()
        bills = {i["merchant"]: i["bill"]["name"] if i["bill"] else None for i in self.find()["items"]}
        self.assertEqual(bills, {"NETFLIX.COM": "Streaming", "SPOTIFY USA": "Spotify"})

    def test_checklist_state_and_progress_are_saved_per_card(self):
        self.monthly("Visa 1111", "NETFLIX.COM", 15.49)
        self.monthly("Visa 1111", "SPOTIFY USA", 11.99)
        self.monthly("Mastercard 2222", "NETFLIX.COM", 15.49)
        key = self.find()["items"][0]["key"]
        self.call(self.app.mark_card_bill, {"card": "Visa 1111", "key": key, "status": "updated"})
        found = self.find()
        self.assertEqual(found["progress"], {"updated": 1, "total": 2})
        self.assertEqual(self.find("Mastercard 2222")["progress"], {"updated": 0, "total": 1})  # another card's list is separate
        self.call(self.app.mark_card_bill, {"card": "Visa 1111", "key": key, "status": "not_a_bill"})
        self.assertEqual(self.find()["progress"], {"updated": 0, "total": 1})
        self.call(self.app.mark_card_bill, {"card": "Visa 1111", "key": key, "status": "pending"})
        self.assertEqual(self.find()["progress"], {"updated": 0, "total": 2})

    def test_bad_requests_are_rejected(self):
        with self.assertRaises(HttpError):
            self.call(self.app.mark_card_bill, {"card": "Visa 1111", "key": "X", "status": "done"})
        with self.assertRaises(HttpError):
            self.call(self.app.mark_card_bill, {"card": "", "key": "X", "status": "updated"})
        with self.assertRaises(HttpError):
            self.app.card_bills(self.request({}))

    def request(self, query):
        from evbudget.http_util import Request
        return Request("GET", "/x", query, {}, b"")

    def test_card_bills_endpoint(self):
        self.monthly("Visa 1111", "NETFLIX.COM", 15.49)
        out = self.app.card_bills(self.request({"card": "Visa 1111"}))
        self.assertEqual(out["items"][0]["cadence"], "Monthly")
        self.assertEqual(out["through"], max(r["date"] for r in self.app.get_transactions(self.request({}))["transactions"]))

    # ---- naming cards

    def test_earlier_imports_are_grouped_and_named(self):
        for i, f in enumerate(["Chase1484_Activity_01.csv", "Chase1484_Activity_02.csv", "Checking 1_1.csv"]):
            self.add_txn(account_name=None, source_file=f, date=f"2026-0{i + 1}-01")
        c = self.conn()
        groups = cards.unnamed_groups(c, self.input_dir)
        self.assertEqual({g["suggested"]: g["transactions"] for g in groups}, {"Chase 1484": 2, "Checking": 1})
        named = cards.name_groups(c, [{"key": "chase1484", "name": "Chase Freedom"}], self.input_dir)
        c.commit()
        self.assertEqual(named, 2)
        self.assertEqual([x["name"] for x in cards.list_cards(c)], ["Chase Freedom"])
        self.assertEqual(cards.load_memory(c)["chase1484"], "Chase Freedom")
        self.assertEqual([g["suggested"] for g in cards.unnamed_groups(c, self.input_dir)], ["Checking"])
        c.close()

    def test_a_remembered_name_is_suggested_for_the_next_file(self):
        c = self.conn()
        cards.remember(c, "chase1484", "Chase Freedom")
        c.commit()
        memory = cards.load_memory(c)
        c.close()
        path = self.write_csv("Chase1484_Activity_11.csv", "Transaction Date,Post Date,Description,Category,Type,Amount,Memo\n")
        info = import_engine.inspect_file_header(path, None, memory)
        self.assertEqual(info["account_name"], "Chase Freedom")

    def test_import_stores_the_card_name_and_backfills_rows_seen_again(self):
        csv_text = ("Transaction Date,Post Date,Description,Category,Type,Amount,Memo\n"
                    "05/02/2026,05/03/2026,KROGER #123,Groceries,Sale,-52.10,\n"
                    "05/03/2026,05/04/2026,NETFLIX.COM,Bills,Sale,-15.49,\n")
        path = self.write_csv("Chase1484_Activity_05.csv", csv_text)

        def run(name):
            c = self.conn()
            info = import_engine.inspect_file_header(path)
            info["account_name"] = name
            preview = import_engine.build_preview(c, [info])
            import_engine.commit_import(c, preview["records"], ["Chase1484_Activity_05.csv"])
            c.close()

        run("")  # first import without a card name
        self.assertEqual({r["account_name"] for r in self.app.conn().execute("SELECT account_name FROM transactions")}, {None})
        run("Chase 1484")  # importing the same file again with a name fills it in
        names = [r[0] for r in self.conn().execute("SELECT account_name FROM transactions")]
        self.assertEqual(names, ["Chase 1484", "Chase 1484"])

    def test_the_name_chosen_at_import_is_saved_on_new_rows(self):
        path = self.write_csv("Chase1484_Activity_06.csv", "Transaction Date,Post Date,Description,Category,Type,Amount,Memo\n"
                              "06/02/2026,06/03/2026,TARGET,Shopping,Sale,-10.00,\n")
        c = self.conn()
        info = import_engine.inspect_file_header(path)
        self.assertEqual(info["account_name"], "Chase 1484")  # suggested from the file name
        preview = import_engine.build_preview(c, [info])
        import_engine.commit_import(c, preview["records"], ["x"])
        self.assertEqual(c.execute("SELECT account_name FROM transactions").fetchone()[0], "Chase 1484")
        c.close()


if __name__ == "__main__":
    unittest.main()
