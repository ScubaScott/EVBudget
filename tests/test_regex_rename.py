import unittest

from helpers import TempAppCase
from evbudget import import_engine, rules, store
from evbudget.http_util import HttpError
from evbudget.textutil import display_name

SQ = {"phrase": r"SQ\s?\*\s?(.*)", "match_mode": "regex", "set_payee": r"\1"}
STORE_NO = {"phrase": r"(.*?)\s*#\d+$", "match_mode": "regex", "set_payee": r"\1"}


class RenameForTests(unittest.TestCase):
    def test_square_prefix_is_stripped(self):
        for raw, want in [("SQ *BABES PLACE", "BABES PLACE"), ("SQ*Vendor", "Vendor"), ("sq * Vendor", "Vendor"), ("SQ *  Two Words", "Two Words")]:
            self.assertEqual(rules.rename_for(SQ, raw.upper(), raw), want, raw)

    def test_store_number_suffix_is_stripped(self):
        self.assertEqual(rules.rename_for(STORE_NO, "STORE #3456", "Store #3456"), "Store")
        self.assertEqual(rules.rename_for(STORE_NO, "TARGET #12", "TARGET #12"), "TARGET")
        self.assertIsNone(rules.rename_for(STORE_NO, "STORE", "Store"))          # nothing to strip
        self.assertIsNone(rules.rename_for(SQ, "TARGET", "TARGET"))

    def test_only_the_matched_part_is_replaced(self):
        rule = {"phrase": r"#\d+", "match_mode": "regex", "set_payee": ""}
        rule["set_payee"] = "NUM"
        self.assertEqual(rules.rename_for(rule, "X #12 Y", "X #12 Y"), "X NUM Y")

    def test_dollar_and_named_group_forms(self):
        self.assertEqual(rules.rename_for({"phrase": r"SQ\s?\*\s?(.*)", "match_mode": "regex", "set_payee": "$1"}, "", "SQ *Cafe"), "Cafe")
        self.assertEqual(rules.rename_for({"phrase": r"SQ\s?\*\s?(.*)", "match_mode": "regex", "set_payee": "${1}!"}, "", "SQ *Cafe"), "Cafe!")
        self.assertEqual(rules.rename_for({"phrase": r"SQ\s?\*\s?(?P<v>.*)", "match_mode": "regex", "set_payee": r"\g<v>"}, "", "SQ *Cafe"), "Cafe")

    def test_a_blank_result_never_becomes_the_name(self):
        self.assertIsNone(rules.rename_for({"phrase": "^.*$", "match_mode": "regex", "set_payee": ""}, "", "Anything"))

    def test_plain_rename_still_replaces_the_whole_name(self):
        rule = {"phrase": "AMAZON MKTPL", "match_mode": "contains", "set_payee": "AMAZON"}
        self.assertEqual(rules.rename_for(rule, "AMAZON MKTPL*302JO0E53", "AMAZON MKTPL*302JO0E53"), "AMAZON")

    def test_validation_catches_bad_patterns_and_missing_groups(self):
        rules.check_rename_rule(r"SQ\s?\*\s?(.*)", r"\1")
        rules.check_rename_rule(r"(a)(b)", "$2$1")
        for pattern, repl in [("(unclosed", "x"), (r"SQ\*(.*)", r"\2"), (r"SQ\*.*", r"\1"), (r"SQ\*(.*)", r"\g<nope>"), (r"SQ\*(.*)", "${3}")]:
            with self.assertRaises(ValueError, msg=(pattern, repl)):
                rules.check_rename_rule(pattern, repl)


class RuleSetChainTests(unittest.TestCase):
    def make(self, *rs):
        return rules.RuleSet([dict(r, stage="payee", priority=i + 1, active=1, origin="user") for i, r in enumerate(rs)])

    def test_pattern_rules_chain(self):
        rs = self.make(SQ, STORE_NO)
        self.assertEqual(rs.rewrite_name("SQ *STORE #3456", "SQ *STORE #3456"), "STORE")

    def test_a_plain_rename_ends_the_chain(self):
        rs = self.make({"phrase": "BIGCO", "match_mode": "contains", "set_payee": "Big Co"}, STORE_NO)
        self.assertEqual(rs.rewrite_name("BIGCO #44", "BIGCO #44"), "Big Co")

    def test_untouched_name_comes_back_unchanged(self):
        self.assertEqual(self.make(SQ).rewrite_name("TARGET", "Target"), "Target")


class RenameFlowTests(TempAppCase):
    def add_rule(self, rule):
        return self.call(self.app.save_rule, dict(rule, stage="payee"))

    def test_saving_validates_the_replacement(self):
        with self.assertRaises(HttpError):
            self.add_rule({"phrase": r"SQ\*(.*)", "match_mode": "regex", "set_payee": r"\2"})
        with self.assertRaises(HttpError):
            self.add_rule({"phrase": "(oops", "match_mode": "regex", "set_payee": "x"})
        self.add_rule(SQ)

    def test_import_applies_the_rules_but_still_categorizes_from_the_original_text(self):
        self.add_rule(SQ)
        self.add_rule(STORE_NO)
        self.call(self.app.save_rule, {"phrase": "BABES PLACE", "stage": "envelope", "set_envelope": "Restaurant"})
        self.write_csv("a.csv", "Transaction Date,Post Date,Description,Category,Type,Amount,Memo\n"
                                "05/02/2026,05/03/2026,SQ *BABES PLACE,Food,Sale,-20.00,\n"
                                "05/03/2026,05/04/2026,SQ *CORNER STORE #3456,Food,Sale,-9.00,\n"
                                "05/04/2026,05/05/2026,HOME DEPOT #0123,Home,Sale,-50.00,\n")
        c = self.conn()
        info = import_engine.inspect_file_header(import_engine.resolve_input_file("a.csv", self.input_dir), {})
        pv = import_engine.build_preview(c, [info])
        c.close()
        by_raw = {r["raw_name"]: r for r in pv["records"]}
        self.assertEqual(by_raw["SQ *BABES PLACE"]["name"], "BABES PLACE")
        self.assertEqual(by_raw["SQ *BABES PLACE"]["envelope"], "Restaurant")      # rule matched the text with its SQ prefix
        self.assertEqual(by_raw["SQ *CORNER STORE #3456"]["name"], "CORNER STORE")  # both rules chained
        self.assertEqual(by_raw["HOME DEPOT #0123"]["name"], "HOME DEPOT")
        self.assertEqual(by_raw["HOME DEPOT #0123"]["envelope"], "Shopping")

    def test_preview_counts_saved_transactions_and_skips_ones_edited_by_hand(self):
        self.add_txn(name="SQ *CAFE ONE", raw_name="SQ *CAFE ONE")
        self.add_txn(name="SQ *CAFE TWO", raw_name="SQ *CAFE TWO")
        self.add_txn(name="SQ *HAND EDITED", raw_name="SQ *HAND EDITED", user_modified=1)
        self.add_txn(name="TARGET", raw_name="TARGET")
        res = self.call(self.app.rule_preview, SQ)
        self.assertEqual((res["count"], res["locked"]), (2, 1))
        self.assertEqual({(e["before"], e["after"]) for e in res["examples"]}, {("SQ *CAFE ONE", "CAFE ONE"), ("SQ *CAFE TWO", "CAFE TWO")})
        out = self.call(self.app.rule_apply_rename, SQ)
        self.assertEqual(out["renamed"], 2)
        c = self.conn()
        names = sorted(r[0] for r in c.execute("SELECT name FROM transactions"))
        c.close()
        self.assertEqual(names, ["CAFE ONE", "CAFE TWO", "SQ *HAND EDITED", "TARGET"])
        self.assertEqual(self.call(self.app.rule_preview, SQ)["count"], 0)   # idempotent

    def test_preview_needs_both_fields_and_a_valid_pattern(self):
        with self.assertRaises(HttpError):
            self.call(self.app.rule_preview, {"phrase": "", "set_payee": "x"})
        with self.assertRaises(HttpError):
            self.call(self.app.rule_preview, {"phrase": "(bad", "set_payee": "x", "match_mode": "regex"})


if __name__ == "__main__":
    unittest.main()


class EditRuleTests(TempAppCase):
    def rule(self, rid):
        return [r for r in self.call(self.app.get_rules, method="GET")["rules"] if r["id"] == rid][0]

    def test_editing_changes_name_pattern_and_action_in_place(self):
        rid = self.call(self.app.save_rule, {"phrase": "COFFEE", "stage": "envelope", "set_envelope": "Restaurant",
                                             "description": "Coffee shops"})["id"]
        before_priority = self.rule(rid)["priority"]
        # Change what it matches, how, what it does, and its description
        self.call(self.app.save_rule, {"id": rid, "phrase": r"COFFEE\s+#\d+", "match_mode": "regex", "stage": "payee",
                                       "set_payee": "Coffee", "set_kind": None, "set_envelope": None, "description": "Strip coffee store numbers",
                                       "active": 1})
        r = self.rule(rid)
        self.assertEqual((r["phrase"], r["match_mode"], r["stage"], r["set_payee"], r["set_envelope"], r["description"]),
                         (r"COFFEE\s+#\d+", "regex", "payee", "Coffee", None, "Strip coffee store numbers"))
        self.assertEqual(r["priority"], before_priority)               # its place in the order is unchanged
        self.assertEqual(len([x for x in self.call(self.app.get_rules, method="GET")["rules"] if x["phrase"].startswith("COFFEE")]), 1)

    def test_an_edit_that_breaks_the_pattern_is_refused_and_leaves_the_rule_alone(self):
        rid = self.call(self.app.save_rule, {"phrase": r"SQ\s?\*\s?(.*)", "match_mode": "regex", "stage": "payee", "set_payee": r"\1"})["id"]
        with self.assertRaises(HttpError):
            self.call(self.app.save_rule, {"id": rid, "phrase": r"SQ\s?\*\s?(.*)", "match_mode": "regex", "stage": "payee", "set_payee": r"\2"})
        self.assertEqual(self.rule(rid)["set_payee"], r"\1")

    def test_switching_an_action_clears_what_no_longer_applies(self):
        rid = self.call(self.app.save_rule, {"phrase": "VENMO", "stage": "refund", "set_kind": "refund", "set_envelope": "Groceries"})["id"]
        self.call(self.app.save_rule, {"id": rid, "phrase": "VENMO", "stage": "exclude", "set_kind": "transfer", "set_envelope": None,
                                       "exclusion_reason": "Internal Account Transfer"})
        r = self.rule(rid)
        self.assertEqual((r["stage"], r["set_kind"], r["set_envelope"]), ("exclude", "transfer", None))

    def test_editing_an_unknown_rule_is_a_404(self):
        with self.assertRaises(HttpError):
            self.call(self.app.save_rule, {"id": 999999, "phrase": "X", "stage": "envelope", "set_envelope": "Gas"})
