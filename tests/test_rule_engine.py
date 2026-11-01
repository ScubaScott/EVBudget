import glob
import os
import sqlite3
import tempfile
import unittest

from helpers import TempAppCase
from evbudget import import_engine, rule_engine, rule_migration, rules, store
from evbudget.http_util import HttpError
from evbudget.textutil import display_name


class RuleCase(TempAppCase):
    def save(self, **kw):
        return self.call(self.app.save_rule, kw)["id"]

    def classify(self, raw, amount=-10.0, check="", trans_type=""):
        c = self.conn()
        ctx = rules.load_context(c)
        c.close()
        return rules.classify(ctx, raw_name=raw, payee=display_name(raw, check), amount=amount, date_iso="2026-06-01",
                              check_num=check, trans_type=trans_type)


class OneRuleManyActionsTests(RuleCase):
    def test_one_rule_renames_and_sends_to_an_envelope(self):
        self.save(name="Sam's Club", phrase="sams club", set_payee="Sam's Club", set_envelope="Groceries")
        res = self.classify("SAMS CLUB #6436 SHAKOPEE MN")
        self.assertEqual((res["name"], res["envelope"]), ("Sam's Club", "Groceries"))
        self.assertEqual([(r["name"], r["did"]) for r in res["rules"]], [("Sam's Club", ["rename", "→ Groceries"])])

    def test_a_clean_up_rule_and_a_specific_rule_both_apply(self):
        self.save(name="Remove store numbers", phrase=r"(.*?)\s*#\d+.*$", match_mode="regex", set_payee=r"\1")
        self.save(name="Sam's Club", phrase="SAMS CLUB", set_envelope="Groceries")
        res = self.classify("SAMS CLUB #6436")
        self.assertEqual((res["name"], res["envelope"]), ("SAMS CLUB", "Groceries"))
        self.assertEqual({r["name"] for r in res["rules"]}, {"Remove store numbers", "Sam's Club"})

    def test_for_each_action_the_first_rule_in_the_list_wins(self):
        self.save(name="General", phrase="SHOP", set_envelope="Restaurant")
        self.save(name="Specific", phrase="ZZ SHOP", set_envelope="Gas", set_needs_review=1)   # newer, so it runs first
        res = self.classify("ZZ SHOP 12")
        self.assertEqual((res["envelope"], res["needs_review"]), ("Gas", 1))

    def test_a_rule_can_match_the_name_another_rule_produced(self):
        self.save(name="Amazon", phrase="AMZN MKTP", set_payee="Amazon")
        self.save(name="Amazon orders", phrase="AMAZON", set_envelope="Gas")
        res = self.classify("AMZN MKTP US*2K4")
        self.assertEqual((res["name"], res["envelope"]), ("Amazon", "Gas"))

    def test_direction_keeps_a_rule_to_money_in_or_out(self):
        self.save(name="Venmo in", phrase="VENMO", direction="in", set_kind="refund", set_envelope="Groceries")
        self.assertEqual(self.classify("VENMO PAYMENT 1", 25.0)["envelope"], "Groceries")
        self.assertEqual(self.classify("VENMO PAYMENT 1", -25.0)["envelope"], "Shopping")     # the last-resort rule

    def test_a_kind_action_excludes_transfers(self):
        self.save(name="Own transfer", phrase="ZZ MOVE", set_kind="transfer", exclusion_reason="Internal Account Transfer")
        res = self.classify("ZZ MOVE TO SAVINGS", -100.0)
        self.assertEqual((res["txn_kind"], res["is_included"], res["envelope"]), ("transfer", False, ""))

    def test_the_last_resort_rules_are_ordinary_rules(self):
        names = {r["name"]: r for r in self.call(self.app.get_rules, method="GET")["rules"]}
        for name in ("Checks", "Everything else spent", "Card payments (the bank marks them PAYMENT)"):
            self.assertIn(name, names)
        self.assertEqual(self.classify("Some Card Payment", 100.0, trans_type="Payment")["txn_kind"], "cc_payment")
        # Switch the last resort off and unmatched spending is simply left without an envelope
        r = names["Everything else spent"]
        self.call(self.app.save_rule, dict(r, active=0))
        self.assertEqual(self.classify("SOME RANDOM SHOP", -12.0)["envelope"], "")

    def test_unknown_money_in_is_still_parked_for_review(self):
        res = self.classify("MYSTERY DEPOSIT 99", 40.0)
        self.assertEqual((res["txn_kind"], res["needs_review"]), ("other_income", 1))


class ImportPreviewTests(RuleCase):
    def preview(self):
        self.write_csv("a.csv", "\n".join(["Transaction Date,Post Date,Description,Category,Type,Amount,Memo", "05/02/2026,05/03/2026,SAMS CLUB #6436 SHAKOPEE MN,Shopping,Sale,-45.00,", "05/03/2026,05/04/2026,PLAIN UNKNOWN SHOP,Shopping,Sale,-9.00,", "05/04/2026,05/05/2026,CARGILL INCORPOR PAYROLL 1,Payroll,Credit,3500.00,"]) + "\n")
        c = self.conn()
        info = import_engine.inspect_file_header(import_engine.resolve_input_file("a.csv", self.input_dir), {})
        pv = import_engine.build_preview(c, [info])
        c.close()
        return {r["raw_name"]: r for r in pv["records"]}

    def test_each_row_lists_the_rules_that_applied_and_keeps_the_bank_text(self):
        rid = self.save(name="Sam's Club", phrase="sams club", set_payee="Sam's Club", set_envelope="Groceries")
        recs = self.preview()
        sams = recs["SAMS CLUB #6436 SHAKOPEE MN"]
        self.assertEqual((sams["name"], sams["envelope"]), ("Sam's Club", "Groceries"))
        self.assertIn("SAMS CLUB #6436", sams["raw_name"])                         # the original is still there to show
        self.assertEqual([(x["id"], x["name"], x["did"]) for x in sams["rules"]], [(rid, "Sam's Club", ["rename", "→ Groceries"])])

    def test_last_resort_rules_and_built_in_steps_are_listed_too(self):
        recs = self.preview()
        self.assertEqual([x["name"] for x in recs["PLAIN UNKNOWN SHOP"]["rules"]], ["Everything else spent"])
        pay = recs["CARGILL INCORPOR PAYROLL 1"]
        self.assertEqual(pay["rules"], [])
        self.assertTrue(any(n.startswith("Paycheck") for n in pay["notes"]))


class ValidationTests(RuleCase):
    def test_a_rule_must_do_something(self):
        with self.assertRaises(HttpError) as cm:
            self.save(name="Nothing", phrase="X")
        self.assertIn("at least one", cm.exception.message)

    def test_only_a_text_filter_can_rename(self):
        with self.assertRaises(HttpError):
            self.save(name="Bad", filter_type="check", set_payee="x")

    def test_unknown_options_are_refused(self):
        for bad in ({"direction": "sideways"}, {"filter_type": "magic"}, {"match_mode": "fuzzy"}, {"set_kind": "gift"}):
            with self.assertRaises(HttpError, msg=bad):
                self.save(phrase="X", set_envelope="Gas", **bad)

    def test_a_missing_name_falls_back_to_the_new_name_or_the_text(self):
        a = self.save(phrase="ZZA", set_envelope="Gas")
        b = self.save(phrase="ZZB", set_payee="The B Place")
        got = {r["id"]: r["name"] for r in self.call(self.app.get_rules, method="GET")["rules"]}
        self.assertEqual((got[a], got[b]), ("ZZA", "The B Place"))

    def test_a_rule_pointing_at_a_missing_envelope_is_marked(self):
        a = self.save(name="Boat", phrase="ZZBOAT", set_envelope="Boat Fuel")
        rule = [r for r in self.call(self.app.get_rules, method="GET")["rules"] if r["id"] == a][0]
        self.assertIn("Boat Fuel", rule["problem"])


class OrderingTests(RuleCase):
    def order(self):
        return [r["name"] for r in self.call(self.app.get_rules, method="GET")["rules"]]

    def test_new_rules_go_first_and_can_be_moved(self):
        a = self.save(name="First added", phrase="ZZ1", set_envelope="Gas")
        b = self.save(name="Second added", phrase="ZZ2", set_envelope="Gas")
        self.assertEqual(self.order()[:2], ["Second added", "First added"])
        self.call(self.app.move_rule, {"id": b, "to": "down"})
        self.assertEqual(self.order()[:2], ["First added", "Second added"])
        self.call(self.app.move_rule, {"id": b, "to": "bottom"})
        self.assertEqual(self.order()[-1], "Second added")
        self.call(self.app.move_rule, {"id": b, "to": "top"})
        self.assertEqual(self.order()[0], "Second added")
        with self.assertRaises(HttpError):
            self.call(self.app.move_rule, {"id": a, "to": "sideways"})

    def test_deleting_an_envelope_takes_it_out_of_rules_but_keeps_their_other_actions(self):
        a = self.save(name="Zed", phrase="ZZED", set_payee="Zed", set_envelope="Restaurant")
        b = self.save(name="Only env", phrase="ZZONLY", set_envelope="Restaurant")
        self.call(self.app.delete_envelope, {"envelope": "Restaurant"})
        got = {r["id"]: r for r in self.call(self.app.get_rules, method="GET")["rules"]}
        self.assertEqual((got[a]["set_envelope"], got[a]["set_payee"], got[a]["active"]), (None, "Zed", 1))
        self.assertEqual(got[b]["active"], 0)

    def test_renaming_an_envelope_updates_the_shared_rules_too(self):
        a = self.save(name="Zed", phrase="ZZED", set_envelope="Restaurant")
        self.call(self.app.rename_envelope, {"old_name": "Restaurant", "new_name": "Eating Out"})
        got = {r["id"]: r for r in self.call(self.app.get_rules, method="GET")["rules"]}
        self.assertEqual(got[a]["set_envelope"], "Eating Out")


class ConversionTests(unittest.TestCase):
    OLD = [
        dict(id=1, phrase="NETFLIX", stage="envelope", set_envelope="Bills: Subscriptions", priority=1040, match_mode="contains"),
        dict(id=2, phrase="Netflix", stage="payee", set_payee="Netflix", priority=98, match_mode="contains"),
        dict(id=3, phrase="VENMO PAYMENT", stage="refund", set_kind="refund", set_needs_review=1, priority=200, match_mode="contains"),
        dict(id=4, phrase="ZELLE TO RAY", stage="envelope", set_envelope="Bills: Mortgage", priority=107, match_mode="contains"),
        dict(id=5, phrase="(.*)\\s?#", stage="payee", set_payee="\\1", priority=90, match_mode="regex", description="remove store number"),
        dict(id=6, phrase="KROGER", stage="envelope", set_envelope="Groceries", priority=500, match_mode="vendor"),
        dict(id=7, phrase="ONLINE TRANSFER", stage="exclude", set_kind="transfer", priority=100, match_mode="contains"),
    ]

    def test_duplicates_merge_stale_envelopes_are_renamed_and_everything_gets_a_name(self):
        report = {}
        out = rule_migration.convert_rules(self.OLD, report=report)
        by = {r["phrase"].upper(): r for r in out if r["phrase"]}
        netflix = by["NETFLIX"]
        self.assertEqual((netflix["set_payee"], netflix["set_envelope"], netflix["name"]), ("Netflix", "Subscriptions", "Netflix"))
        self.assertEqual(report["merged"], 1)
        self.assertEqual(by["ZELLE TO RAY"]["set_envelope"], "Mortgage")
        self.assertEqual((by["VENMO PAYMENT"]["direction"], by["VENMO PAYMENT"]["set_kind"]), ("in", "refund"))
        pattern = [r for r in out if r["match_mode"] == "regex"][0]
        self.assertEqual(pattern["name"], "Remove store numbers")
        self.assertEqual(by["KROGER"]["match_mode"], "contains")
        self.assertTrue(all(r["name"] for r in out))

    def test_the_old_running_order_is_kept_and_the_last_resorts_are_added(self):
        out = rule_migration.convert_rules(self.OLD)
        names = [r["name"] for r in out]
        self.assertLess(names.index("Remove store numbers"), names.index("Netflix"))
        self.assertLess(names.index("Checks"), names.index("Netflix"))      # checks have always beaten the name rules
        self.assertEqual(names[-1], "Everything else spent")
        for r in out:
            if r["phrase"] or r["filter_type"] != "text":
                rule_engine.validate_rule(r)

    def test_an_old_database_is_converted_once_with_a_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "shared.db")
            conn = sqlite3.connect(path)
            conn.row_factory = sqlite3.Row
            conn.execute(store.RULES_DDL)
            for r in self.OLD:
                conn.execute("INSERT INTO rules (id, phrase, match_mode, stage, set_kind, set_envelope, set_payee, set_needs_review, "
                             "priority, origin, description) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'user', ?)",
                             (r["id"], r["phrase"], r["match_mode"], r["stage"], r.get("set_kind"), r.get("set_envelope"),
                              r.get("set_payee"), r.get("set_needs_review", 0), r["priority"], r.get("description")))
            conn.commit()
            self.assertGreater(store.migrate_rules(conn, "rules", shared=True), 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM rules WHERE name IS NULL").fetchone()[0], 0)
            self.assertGreaterEqual(conn.execute("SELECT MIN(id) FROM rules").fetchone()[0], store.SHARED_ID_BASE)
            self.assertEqual(len(glob.glob(path + ".before_rule_update_*.bak")), 1)
            self.assertEqual(store.migrate_rules(conn, "rules", shared=True), 0)       # nothing left to do
            self.assertEqual(len(glob.glob(path + ".before_rule_update_*.bak")), 1)
            conn.close()


if __name__ == "__main__":
    unittest.main()
