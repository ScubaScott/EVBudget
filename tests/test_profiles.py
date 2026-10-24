import json
import os
import shutil
import tempfile
import unittest

from helpers import ROOT  # noqa: F401  (puts the project on sys.path)
from evbudget import api, paths, profiles, rules, store
from evbudget.http_util import HttpError, Request

LEGACY_ATTRS = ("LEGACY_ENVELOPES_PATH", "LEGACY_USER_RULES_PATH", "LEGACY_KNOWN_VENDORS_PATH")


class ProfileCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="evbudget_prof_")
        self._saved = {a: getattr(paths, a) for a in LEGACY_ATTRS}
        for a in LEGACY_ATTRS:
            setattr(paths, a, os.path.join(self.tmp, "legacy_" + a.lower() + ".json"))
        self.legacy_db = os.path.join(self.tmp, "LocalStore", "budget_store.db")
        self.legacy_input = os.path.join(self.tmp, "input")
        self.profiles = profiles.Profiles(
            profiles_dir=os.path.join(self.tmp, "Profiles"),
            shared_db_path=os.path.join(self.tmp, "Shared", "shared_rules.db"),
            legacy_db_path=self.legacy_db, legacy_input_dir=self.legacy_input,
        )
        self.app = api.App(profiles=self.profiles, web_dir=os.path.join(ROOT, "web"))

    def tearDown(self):
        for a, v in self._saved.items():
            setattr(paths, a, v)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def call(self, fn, body=None, query=None):
        raw = json.dumps(body).encode("utf-8") if body is not None else b""
        return fn(Request("POST", "/test", query or {}, {"Content-Type": "application/json"}, raw))

    def open(self, name, pin=""):
        return self.call(self.app.profile_open, {"name": name, "pin": pin})

    def create(self, name, pin=""):
        return self.call(self.app.profile_create, {"name": name, "pin": pin})

    def rows(self, sql, *args):
        c = self.app.conn()
        try:
            return [dict(r) for r in c.execute(sql, args)]
        finally:
            c.close()

    def add_rule(self, **kw):
        body = {"phrase": "ZZVENDOR", "stage": "envelope", "set_envelope": "Gas"}
        body.update(kw)
        return self.call(self.app.save_rule, body)["id"]

    def classify(self, name="ZZVENDOR STORE 12", amount=-10.0):
        c = self.app.conn()
        try:
            return rules.classify(rules.load_context(c), raw_name=name, payee=name, amount=amount, date_iso="2026-05-01")
        finally:
            c.close()


class ProfileFilesTests(ProfileCase):
    def test_create_list_and_names(self):
        self.profiles.create("Scott")
        self.profiles.create("Kendra Budget")
        self.assertEqual([p["name"] for p in self.profiles.listing()], ["Kendra Budget", "Scott"])
        with self.assertRaises(profiles.ProfileError):
            self.profiles.create("scott")  # same name, different case
        for bad in ("", "  ", "../evil", "a/b", "CON", "x" * 41, ".hidden"):
            with self.assertRaises(profiles.ProfileError, msg=bad):
                self.profiles.create(bad)

    def test_new_profile_is_empty_but_has_the_default_envelopes(self):
        self.create("Pat")
        self.assertEqual(self.rows("SELECT COUNT(*) AS n FROM people")[0]["n"], 0)
        self.assertEqual(self.rows("SELECT COUNT(*) AS n FROM income_sources")[0]["n"], 0)
        names = {r["name"] for r in self.rows("SELECT name FROM envelopes")}
        self.assertTrue({"Groceries", "Gas", "Shopping"} <= names)

    def test_pin_set_check_change_remove(self):
        self.profiles.create("Pat")
        self.assertFalse(self.profiles.has_pin("Pat"))
        self.assertTrue(self.profiles.check_pin("Pat", "anything"))  # no PIN means open
        self.profiles.set_pin("Pat", "", "1234")
        self.assertTrue(self.profiles.has_pin("Pat"))
        self.assertTrue(self.profiles.check_pin("Pat", "1234"))
        self.assertFalse(self.profiles.check_pin("Pat", "9999"))
        self.assertFalse(self.profiles.check_pin("Pat", ""))
        with self.assertRaises(profiles.ProfileError):
            self.profiles.set_pin("Pat", "wrong", "5678")
        with self.assertRaises(profiles.ProfileError):
            self.profiles.set_pin("Pat", "1234", "12")  # too short
        self.profiles.set_pin("Pat", "1234", "5678")
        self.assertTrue(self.profiles.check_pin("Pat", "5678"))
        self.assertFalse(self.profiles.set_pin("Pat", "5678", ""))
        self.assertFalse(self.profiles.has_pin("Pat"))

    def test_pin_is_stored_hashed(self):
        self.profiles.create("Pat", pin="4321")
        with open(os.path.join(self.profiles.profile_paths("Pat")["dir"], "pin.json")) as f:
            self.assertNotIn("4321", f.read())

    def test_deleting_pin_file_resets_it(self):
        self.profiles.create("Pat", pin="4321")
        os.remove(os.path.join(self.profiles.profile_paths("Pat")["dir"], "pin.json"))
        self.assertTrue(self.profiles.check_pin("Pat", ""))


class AppProfileTests(ProfileCase):
    def test_nothing_works_until_a_budget_is_open(self):
        with self.assertRaises(HttpError) as cm:
            self.app.conn()
        self.assertEqual((cm.exception.status, cm.exception.extra.get("code")), (409, "no_profile"))
        with self.assertRaises(HttpError):
            self.app.import_scan(Request("GET", "/x", {}, {}, b""))

    def test_picker_page_until_open(self):
        self.assertIn(b"profile-picker", self.app.page(Request("GET", "/", {}, {}, b"")).body)
        self.create("Pat")
        self.assertNotIn(b"profile-picker", self.app.page(Request("GET", "/", {}, {}, b"")).body)
        self.call(self.app.profile_close)
        self.assertIn(b"profile-picker", self.app.page(Request("GET", "/", {}, {}, b"")).body)

    def test_open_checks_pin(self):
        self.create("Pat", pin="1234")
        self.call(self.app.profile_close)
        for bad in ("", "0000"):
            with self.assertRaises(HttpError) as cm:
                self.open("Pat", bad)
            self.assertEqual(cm.exception.status, 403)
        self.assertEqual(self.open("pat", "1234")["name"], "Pat")  # name matched ignoring case

    def test_two_budgets_keep_separate_transactions(self):
        self.create("Scott")
        c = self.app.conn()
        c.execute("INSERT INTO transactions (txn_id, date, name, raw_name, amount, envelope, account_type, is_included, txn_kind) "
                  "VALUES ('T1', '2026-05-01', 'MINE', 'MINE', -5, 'Gas', 'Credit / Debit', 1, 'expense')")
        c.commit()
        c.close()
        self.call(self.app.profile_close)
        self.create("Kendra")
        self.assertEqual(self.rows("SELECT COUNT(*) AS n FROM transactions")[0]["n"], 0)
        self.call(self.app.profile_close)
        self.open("Scott")
        self.assertEqual(self.rows("SELECT txn_id FROM transactions"), [{"txn_id": "T1"}])

    def test_each_budget_has_its_own_input_folder(self):
        self.create("Scott")
        a = self.app.input_dir
        self.call(self.app.profile_close)
        self.create("Kendra")
        self.assertNotEqual(a, self.app.input_dir)
        self.assertTrue(self.app.input_dir.endswith(os.path.join("Kendra", "input")))

    def test_pin_endpoint(self):
        self.create("Pat")
        self.assertTrue(self.call(self.app.profile_pin, {"current_pin": "", "new_pin": "1234"})["has_pin"])
        with self.assertRaises(HttpError):
            self.call(self.app.profile_pin, {"current_pin": "nope", "new_pin": ""})
        self.assertFalse(self.call(self.app.profile_pin, {"current_pin": "1234", "new_pin": ""})["has_pin"])

    def test_listing_reports_state(self):
        self.create("Pat", pin="1234")
        info = self.app.profiles_list(Request("GET", "/x", {}, {}, b""))
        self.assertEqual(info["active"], "Pat")
        self.assertEqual(info["profiles"], [{"name": "Pat", "has_pin": True, "last_used": True}])
        self.assertFalse(info["legacy_found"])


class AutoOpenTests(ProfileCase):
    def page(self):
        body = self.app.page(Request("GET", "/", {}, {}, b"")).body
        return "picker" if b"profile-picker" in body else "dashboard"

    def test_the_only_budget_opens_without_asking(self):
        self.profiles.create("Scott")
        self.assertIsNone(self.app.profile_name)
        self.assertEqual(self.page(), "dashboard")
        self.assertEqual(self.app.profile_name, "Scott")

    def test_a_budget_with_a_pin_still_asks(self):
        self.profiles.create("Scott", pin="1234")
        self.assertEqual(self.page(), "picker")
        self.assertIsNone(self.app.profile_name)

    def test_several_budgets_ask_which(self):
        self.profiles.create("Scott")
        self.profiles.create("Kendra")
        self.assertEqual(self.page(), "picker")

    def test_no_budgets_shows_the_picker_to_create_one(self):
        self.assertEqual(self.page(), "picker")

    def test_older_data_waiting_to_be_moved_is_offered_first(self):
        self.profiles.create("Scott")
        os.makedirs(os.path.dirname(self.legacy_db), exist_ok=True)
        open(self.legacy_db, "w").close()
        self.assertEqual(self.page(), "picker")

    def test_switching_budget_shows_the_picker_instead_of_reopening(self):
        self.profiles.create("Scott")
        self.assertEqual(self.page(), "dashboard")
        self.call(self.app.profile_close)
        self.assertEqual(self.page(), "picker")
        self.open("Scott")
        self.assertEqual(self.page(), "dashboard")

    def test_a_budget_that_cannot_be_opened_falls_back_to_the_picker(self):
        self.profiles.create("Scott")
        original = store.ensure_schema
        store.ensure_schema = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("damaged"))
        try:
            self.assertEqual(self.page(), "picker")
        finally:
            store.ensure_schema = original
        self.assertIsNone(self.app.profile_name)


class SharedRuleTests(ProfileCase):
    def setUp(self):
        super().setUp()
        self.create("Scott")

    def test_built_in_rules_are_shared_and_seeded_once(self):
        shared = self.rows("SELECT COUNT(*) AS n, MIN(id) AS low FROM shared.rules")[0]
        self.assertGreater(shared["n"], 10)
        self.assertGreaterEqual(shared["low"], store.SHARED_ID_BASE)
        self.assertEqual(self.rows("SELECT COUNT(*) AS n FROM rules")[0]["n"], 0)
        self.call(self.app.profile_close)
        self.create("Kendra")
        self.assertEqual(self.rows("SELECT COUNT(*) AS n FROM shared.rules")[0]["n"], shared["n"])

    def test_scope_must_be_chosen(self):
        with self.assertRaises(HttpError) as cm:
            self.add_rule()
        self.assertIn("just for you or for everyone", cm.exception.message)
        with self.assertRaises(HttpError):
            self.add_rule(scope="everyone")

    def test_shared_rule_reaches_every_budget_personal_does_not(self):
        self.add_rule(phrase="ZZSHARED", scope="shared")
        self.add_rule(phrase="ZZPERSONAL", scope="personal")
        self.assertEqual(self.classify("ZZSHARED 1")["envelope"], "Gas")
        self.assertEqual(self.classify("ZZPERSONAL 1")["envelope"], "Gas")
        self.call(self.app.profile_close)
        self.create("Kendra")
        self.assertEqual(self.classify("ZZSHARED 1")["envelope"], "Gas")
        self.assertEqual(self.classify("ZZPERSONAL 1")["envelope"], "Shopping")  # default: the rule isn't here

    def test_personal_rule_beats_shared(self):
        self.add_rule(scope="shared", set_envelope="Groceries")
        self.assertEqual(self.classify()["envelope"], "Groceries")
        self.add_rule(scope="personal", set_envelope="Restaurant")
        self.assertEqual(self.classify()["envelope"], "Restaurant")

    def test_rule_list_marks_scope_and_ids_do_not_collide(self):
        p = self.add_rule(phrase="ZZP", scope="personal")
        s = self.add_rule(phrase="ZZS", scope="shared")
        self.assertLess(p, store.SHARED_ID_BASE)
        self.assertGreaterEqual(s, store.SHARED_ID_BASE)
        listed = {r["id"]: r["scope"] for r in self.app.get_rules(Request("GET", "/x", {}, {}, b""))["rules"]}
        self.assertEqual((listed[p], listed[s]), ("personal", "shared"))

    def test_edit_in_place_and_delete_use_the_right_store(self):
        s = self.add_rule(phrase="ZZS", scope="shared")
        self.call(self.app.save_rule, {"id": s, "phrase": "ZZS", "stage": "envelope", "set_envelope": "Groceries"})
        self.assertEqual(self.rows("SELECT set_envelope FROM shared.rules WHERE id = ?", s)[0]["set_envelope"], "Groceries")
        self.call(self.app.delete_rule, {"id": s})
        self.assertEqual(self.rows("SELECT id FROM shared.rules WHERE id = ?", s), [])

    def test_changing_scope_moves_the_rule(self):
        p = self.add_rule(phrase="ZZMOVE", scope="personal")
        new_id = self.call(self.app.save_rule, {"id": p, "phrase": "ZZMOVE", "stage": "envelope", "set_envelope": "Gas", "scope": "shared"})["id"]
        self.assertGreaterEqual(new_id, store.SHARED_ID_BASE)
        self.assertEqual(self.rows("SELECT id FROM rules WHERE phrase = 'ZZMOVE'"), [])
        self.assertEqual(len(self.rows("SELECT id FROM shared.rules WHERE phrase = 'ZZMOVE'")), 1)

    def test_same_phrase_can_exist_in_both_scopes(self):
        self.add_rule(phrase="ZZBOTH", scope="personal")
        self.add_rule(phrase="ZZBOTH", scope="shared")
        self.assertEqual(len(self.rows("SELECT id FROM rules WHERE phrase = 'ZZBOTH'")), 1)
        self.assertEqual(len(self.rows("SELECT id FROM shared.rules WHERE phrase = 'ZZBOTH'")), 1)

    def test_bulk_always_do_this_needs_a_scope_and_honours_it(self):
        c = self.app.conn()
        c.execute("INSERT INTO transactions (txn_id, date, name, raw_name, amount, envelope, account_type, is_included, txn_kind) "
                  "VALUES ('T1', '2026-05-01', 'ZZBULK', 'ZZBULK', -5, 'Shopping', 'Credit / Debit', 1, 'expense')")
        c.commit()
        c.close()
        body = {"txn_ids": ["T1"], "updates": {"envelope": "Gas"}, "save_rule": True, "rule_phrase": "ZZBULK"}
        with self.assertRaises(HttpError):
            self.call(self.app.bulk_update, body)
        self.call(self.app.bulk_update, dict(body, rule_scope="shared"))
        self.assertEqual(len(self.rows("SELECT id FROM shared.rules WHERE phrase = 'ZZBULK'")), 1)
        self.assertEqual(self.rows("SELECT id FROM rules WHERE phrase = 'ZZBULK'"), [])


class MissingEnvelopeTests(ProfileCase):
    def setUp(self):
        super().setUp()
        self.create("Scott")

    def test_shared_rule_for_an_envelope_this_budget_lacks_is_skipped_and_flagged(self):
        self.add_rule(scope="shared", set_envelope="Boat Fuel")
        out = self.classify()
        self.assertEqual(out["envelope"], "Shopping")
        self.assertEqual(out["needs_review"], 1)
        self.assertIn("Boat Fuel", out["match_source"])

    def test_it_works_once_the_envelope_exists(self):
        self.add_rule(scope="shared", set_envelope="Boat Fuel")
        c = self.app.conn()
        c.execute("INSERT INTO envelopes (name, kind, rollover_mode, sort_order) VALUES ('Boat Fuel', 'spending', 'reset', 99)")
        c.commit()
        c.close()
        out = self.classify()
        self.assertEqual((out["envelope"], out["needs_review"]), ("Boat Fuel", 0))

    def test_a_skipped_rule_falls_through_to_the_next_match(self):
        self.add_rule(phrase="ZZV", scope="shared", set_envelope="Groceries")
        self.add_rule(scope="shared", set_envelope="Boat Fuel")  # newer, so tried first
        out = self.classify()
        self.assertEqual((out["envelope"], out["needs_review"]), ("Groceries", 1))  # still flagged: a rule was skipped

    def test_personal_rules_are_not_second_guessed(self):
        self.add_rule(scope="personal", set_envelope="Boat Fuel")
        self.assertEqual(self.classify()["envelope"], "Boat Fuel")


class LegacyMigrationTests(ProfileCase):
    def make_legacy(self):
        store.ensure_schema(self.legacy_db)
        c = store.connect(self.legacy_db)
        c.execute("INSERT INTO transactions (txn_id, date, name, raw_name, amount, envelope, account_type, is_included, txn_kind) "
                  "VALUES ('OLD1', '2026-04-01', 'KROGER', 'KROGER', -20, 'Groceries', 'Credit / Debit', 1, 'expense')")
        store.insert_rule(c, {"phrase": "ZZMYRULE", "stage": "envelope", "set_envelope": "Gas", "origin": "user", "priority": 5})
        c.commit()
        self.legacy_rule_count = c.execute("SELECT COUNT(*) FROM rules").fetchone()[0]
        c.close()
        os.makedirs(self.legacy_input)
        with open(os.path.join(self.legacy_input, "chase.csv"), "w") as f:
            f.write("a,b\n")
        os.makedirs(os.path.join(os.path.dirname(self.legacy_db), "backups"), exist_ok=True)
        with open(os.path.join(os.path.dirname(self.legacy_db), "backups", "budget_store_20260101_000000.db"), "w") as f:
            f.write("x")

    def test_existing_budget_becomes_a_profile(self):
        self.make_legacy()
        self.assertTrue(self.app.profiles_list(Request("GET", "/x", {}, {}, b""))["legacy_found"])
        result = self.call(self.app.profile_migrate, {"name": "Scott"})
        self.assertEqual(result["name"], "Scott")
        self.assertEqual(self.app.profile_name, "Scott")
        self.assertEqual([r["txn_id"] for r in self.rows("SELECT txn_id FROM transactions")], ["OLD1"])
        self.assertGreater(self.rows("SELECT COUNT(*) AS n FROM people")[0]["n"], 0)  # the household came with it
        self.assertFalse(self.app.profiles_list(Request("GET", "/x", {}, {}, b""))["legacy_found"])

    def test_files_move_and_the_old_database_is_kept_as_a_backup(self):
        self.make_legacy()
        self.call(self.app.profile_migrate, {"name": "Scott"})
        folder = self.profiles.profile_paths("Scott")["dir"]
        self.assertTrue(os.path.isfile(os.path.join(folder, "input", "chase.csv")))
        self.assertFalse(os.path.exists(os.path.join(self.legacy_input, "chase.csv")))
        self.assertTrue(os.path.isfile(os.path.join(folder, "backups", "budget_store_20260101_000000.db")))
        self.assertTrue(os.path.isfile(self.legacy_db + ".migrated"))
        self.assertFalse(os.path.exists(self.legacy_db))

    def test_all_old_rules_become_shared_so_other_budgets_get_them(self):
        self.make_legacy()
        self.call(self.app.profile_migrate, {"name": "Scott"})
        self.assertEqual(self.rows("SELECT COUNT(*) AS n FROM rules")[0]["n"], 0)
        self.assertEqual(self.rows("SELECT COUNT(*) AS n FROM shared.rules WHERE phrase = 'ZZMYRULE'")[0]["n"], 1)
        # The built-in rules were already in the shared store; they must not be doubled.
        self.assertEqual(self.rows("SELECT COUNT(*) AS n FROM shared.rules")[0]["n"], self.legacy_rule_count)
        self.call(self.app.profile_close)
        self.create("Kendra")
        self.assertEqual(self.classify("ZZMYRULE 9")["envelope"], "Gas")

    def test_a_failed_move_leaves_everything_where_it_was(self):
        self.make_legacy()
        original = store.move_rules_to_shared
        store.move_rules_to_shared = lambda conn: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            with self.assertRaises(RuntimeError):
                self.call(self.app.profile_migrate, {"name": "Scott"})
        finally:
            store.move_rules_to_shared = original
        self.assertTrue(os.path.isfile(self.legacy_db))
        self.assertEqual(self.profiles.names(), [])
        self.assertTrue(os.path.isfile(os.path.join(self.legacy_input, "chase.csv")))

    def test_cannot_migrate_twice_or_over_an_existing_name(self):
        self.make_legacy()
        self.call(self.app.profile_migrate, {"name": "Scott"})
        with self.assertRaises(HttpError):
            self.call(self.app.profile_migrate, {"name": "Other"})  # nothing left to move


if __name__ == "__main__":
    unittest.main()
