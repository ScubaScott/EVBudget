import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import HTTPServer

from helpers import TempAppCase
from evbudget import http_util, import_engine, rules, store
from evbudget.http_util import HttpError
from evbudget.textutil import legacy_txn_id


class TransactionEditTests(TempAppCase):
    def test_marking_as_transfer_excludes_the_row(self):
        tid = self.add_txn(amount=-500.0, name="X")
        self.call(self.app.update_transaction, {"txn_id": tid, "txn_kind": "transfer"})
        row = self.get_txn(tid)
        self.assertEqual((row["txn_kind"], row["is_included"], row["user_modified"]), ("transfer", 0, 1))
        self.assertTrue(row["exclusion_reason"])

    def test_including_clears_the_reason(self):
        tid = self.add_txn(is_included=0, exclusion_reason="User Excluded")
        self.call(self.app.update_transaction, {"txn_id": tid, "is_included": 1})
        self.assertEqual(self.get_txn(tid)["exclusion_reason"], "")

    def test_leaving_paycheck_clears_income_fields(self):
        tid = self.add_txn(amount=100.0, envelope="", txn_kind="paycheck", income_subtype="regular",
                           income_source_id=1, person_id=1)
        self.call(self.app.update_transaction, {"txn_id": tid, "txn_kind": "other_income"})
        row = self.get_txn(tid)
        self.assertEqual((row["income_subtype"], row["income_source_id"], row["person_id"]), (None, None, 1))
        self.call(self.app.update_transaction, {"txn_id": tid, "txn_kind": "refund"})
        self.assertIsNone(self.get_txn(tid)["person_id"])

    def test_unknown_kind_and_bad_date_are_rejected(self):
        tid = self.add_txn()
        with self.assertRaises(HttpError):
            self.call(self.app.update_transaction, {"txn_id": tid, "txn_kind": "bogus"})
        with self.assertRaises(HttpError):
            self.call(self.app.update_transaction, {"txn_id": tid, "date": "2026-13-45"})
        with self.assertRaises(HttpError):
            self.call(self.app.update_transaction, {"txn_id": "nope"})

    def test_old_style_dates_are_accepted_and_stored_as_iso(self):
        tid = self.add_txn()
        self.call(self.app.update_transaction, {"txn_id": tid, "date": "03/04/2026"})
        self.assertEqual(self.get_txn(tid)["date"], "2026-03-04")

    def test_reimbursement_copies_the_envelope_of_the_expense(self):
        expense = self.add_txn(amount=-100.0, envelope="Restaurant", date="2026-05-01")
        venmo = self.add_txn(amount=50.0, envelope="", txn_kind="refund", needs_review=1, date="2026-05-05")
        self.call(self.app.update_transaction, {"txn_id": venmo, "reimburses_txn_id": expense, "needs_review": 0})
        row = self.get_txn(venmo)
        self.assertEqual((row["envelope"], row["reimburses_txn_id"], row["needs_review"]), ("Restaurant", expense, 0))

    def test_reimbursement_must_point_at_a_different_expense(self):
        venmo = self.add_txn(amount=50.0, envelope="", txn_kind="refund")
        with self.assertRaises(HttpError):
            self.call(self.app.update_transaction, {"txn_id": venmo, "reimburses_txn_id": venmo})
        with self.assertRaises(HttpError):
            self.call(self.app.update_transaction, {"txn_id": venmo, "reimburses_txn_id": "missing"})

    def test_deleting_the_expense_unlinks_reimbursements(self):
        expense = self.add_txn(amount=-100.0)
        venmo = self.add_txn(amount=50.0, txn_kind="refund", reimburses_txn_id=expense)
        self.call(self.app.delete_transaction, {"txn_id": expense})
        self.assertIsNone(self.get_txn(venmo)["reimburses_txn_id"])

    def test_bulk_update_sets_kind_and_flags_rows(self):
        a, b = self.add_txn(amount=-5.0), self.add_txn(amount=-6.0)
        res = self.call(self.app.bulk_update, {"txn_ids": [a, b], "updates": {"txn_kind": "cc_payment"}})
        self.assertEqual(res["updated_count"], 2)
        row = self.get_txn(a)
        self.assertEqual((row["txn_kind"], row["is_included"], row["user_modified"]), ("cc_payment", 0, 1))
        self.call(self.app.bulk_update, {"txn_ids": [a], "updates": {"txn_kind": "expense"}})
        self.assertEqual(self.get_txn(a)["is_included"], 1)  # leaving an excluded kind re-includes it

    def test_bulk_update_can_learn_rules(self):
        a = self.add_txn(amount=-5.0, name="ZED SHOP")
        self.call(self.app.bulk_update, {
            "txn_ids": [a], "updates": {"envelope": "Restaurant", "name": "Zed"},
            "save_rule": True, "rule_phrase": "ZED SHOP",
        })
        c = self.conn()
        rule = c.execute("SELECT * FROM rules WHERE phrase = 'ZED SHOP'").fetchone()
        c.close()
        self.assertEqual((rule["stage"], rule["set_envelope"], rule["set_payee"], rule["origin"]),
                         ("envelope", "Restaurant", "Zed", "user"))

    def test_learning_a_refund_rule_keeps_spending_separate(self):
        a = self.add_txn(amount=10.0, name="ZELLE IN", envelope="", txn_kind="refund")
        self.call(self.app.bulk_update, {
            "txn_ids": [a], "updates": {"txn_kind": "refund", "envelope": "Groceries"},
            "save_rule": True, "rule_phrase": "ZELLE IN",
        })
        c = self.conn()
        stages = [r["stage"] for r in c.execute("SELECT stage FROM rules WHERE phrase = 'ZELLE IN'")]
        c.close()
        self.assertEqual(stages, ["refund"])

    def test_newer_user_rule_runs_before_older_one(self):
        self.call(self.app.save_rule, {"phrase": "WALMART", "set_envelope": "Groceries"})
        self.call(self.app.save_rule, {"phrase": "WALMART FUEL", "set_envelope": "Gas"})
        c = self.conn()
        ctx = rules.load_context(c)
        c.close()
        rule = ctx.rules.first(("envelope",), "WALMART FUEL 123", "WALMART FUEL 123")
        self.assertEqual(rule["set_envelope"], "Gas")


class DeletedAndLegacyIdTests(TempAppCase):
    CSV = "Transaction Date,Post Date,Description,Category,Type,Amount,Memo\n05/02/2026,05/03/2026,KROGER #123,Groceries,Sale,-52.10,\n"

    def preview(self, name):
        c = self.conn()
        try:
            info = import_engine.inspect_file_header(import_engine.resolve_input_file(name, self.input_dir), {})
            return import_engine.build_preview(c, [info])
        finally:
            c.close()

    def test_deleted_rows_do_not_come_back_unless_restored(self):
        self.write_csv("a.csv", self.CSV)
        pv = self.preview("a.csv")
        c = self.conn()
        import_engine.commit_import(c, pv["records"], ["a.csv"])
        c.close()
        tid = pv["records"][0]["txn_id"]
        self.call(self.app.delete_transaction, {"txn_id": tid})
        self.assertEqual(self.get_txn(tid), {})

        again = self.preview("a.csv")
        self.assertEqual((again["summary"]["new"], again["summary"]["deleted"]), (0, 1))
        c = self.conn()
        result = import_engine.commit_import(c, again["records"], ["a.csv"])
        c.close()
        self.assertEqual((result["inserted"], result["skipped_deleted"]), (0, 1))

        again["records"][0]["restore"] = True
        c = self.conn()
        result = import_engine.commit_import(c, again["records"], ["a.csv"])
        c.close()
        self.assertEqual(result["inserted"], 1)
        self.assertEqual(self.preview("a.csv")["summary"]["existing"], 1)

    def test_rows_saved_under_the_early_id_formula_count_as_existing(self):
        self.write_csv("a.csv", self.CSV)
        old_id = legacy_txn_id("05/02/2026", "KROGER #123", -52.10, "Credit / Debit")
        self.add_txn(txn_id=old_id, date="2026-05-02", raw_name="KROGER #123", name="KROGER #123", amount=-52.10)
        pv = self.preview("a.csv")
        self.assertEqual((pv["summary"]["new"], pv["summary"]["existing"]), (0, 1))
        self.assertEqual(pv["records"][0]["txn_id"], old_id)
        c = self.conn()
        result = import_engine.commit_import(c, pv["records"], ["a.csv"])
        c.close()
        self.assertEqual((result["inserted"], result["existing"]), (0, 1))
        c = self.conn()
        self.assertEqual(c.execute("SELECT COUNT(*) FROM transactions").fetchone()[0], 1)
        c.close()


class EnvelopeTests(TempAppCase):
    def test_unallocated_is_not_listed(self):
        names = self.call(self.app.get_envelopes, method="GET")["envelopes"]
        self.assertNotIn("Unallocated", names)
        self.assertIn("Groceries", names)

    def test_add_rejects_duplicates_any_case(self):
        self.call(self.app.add_envelope, {"envelope": "Vacation"})
        with self.assertRaises(HttpError):
            self.call(self.app.add_envelope, {"envelope": "vacation"})
        with self.assertRaises(HttpError):
            self.call(self.app.add_envelope, {"envelope": "unallocated"})

    def test_rename_moves_transactions_and_rules(self):
        tid = self.add_txn(envelope="Gas")
        self.call(self.app.rename_envelope, {"old_name": "Gas", "new_name": "Fuel"})
        self.assertEqual(self.get_txn(tid)["envelope"], "Fuel")
        c = self.conn()
        stale = c.execute("SELECT COUNT(*) FROM rules WHERE set_envelope = 'Gas'").fetchone()[0]
        moved = c.execute("SELECT COUNT(*) FROM rules WHERE set_envelope = 'Fuel'").fetchone()[0]
        c.close()
        self.assertEqual(stale, 0)
        self.assertGreater(moved, 0)

    def test_delete_with_reassign(self):
        tid = self.add_txn(envelope="Gas")
        res = self.call(self.app.delete_envelope, {"envelope": "Gas", "reassign_to": "Shopping"})
        self.assertEqual(res["reassigned_count"], 1)
        self.assertEqual(self.get_txn(tid)["envelope"], "Shopping")
        self.assertNotIn("Gas", res["envelopes"])

    def test_delete_to_unallocated_turns_off_its_rules(self):
        self.call(self.app.delete_envelope, {"envelope": "Gas", "reassign_to": ""})
        c = self.conn()
        active = c.execute("SELECT COUNT(*) FROM rules WHERE set_envelope = 'Gas' AND active = 1").fetchone()[0]
        c.close()
        self.assertEqual(active, 0)

    def test_cannot_delete_or_rename_unallocated(self):
        with self.assertRaises(HttpError):
            self.call(self.app.delete_envelope, {"envelope": "Unallocated"})
        with self.assertRaises(HttpError):
            self.call(self.app.rename_envelope, {"old_name": "Unallocated", "new_name": "Pool"})


class ReclassifyTests(TempAppCase):
    def test_preview_changes_nothing_and_edited_rows_are_skipped(self):
        auto = self.add_txn(name="NETFLIX", raw_name="NETFLIX.COM", envelope="Shopping", match_source="Default Fallback")
        edited = self.add_txn(name="NETFLIX", raw_name="NETFLIX.COM", envelope="Restaurant", user_modified=1)

        res = self.call(self.app.reclassify_transactions, {"apply": False})
        self.assertEqual(res["changed_count"], 1)
        self.assertEqual(res["changes"][0]["txn_id"], auto)
        self.assertEqual(res["changes"][0]["after"]["envelope"], "Bills: Subscriptions")
        self.assertEqual(self.get_txn(auto)["envelope"], "Shopping")

        self.call(self.app.reclassify_transactions, {"apply": True})
        self.assertEqual(self.get_txn(auto)["envelope"], "Bills: Subscriptions")
        self.assertEqual(self.get_txn(edited)["envelope"], "Restaurant")

    def test_include_edited_rows_when_asked(self):
        edited = self.add_txn(name="NETFLIX", raw_name="NETFLIX.COM", envelope="Restaurant", user_modified=1)
        res = self.call(self.app.reclassify_transactions, {"apply": True, "include_edited": True})
        self.assertEqual(res["changed_count"], 1)
        self.assertEqual(self.get_txn(edited)["envelope"], "Bills: Subscriptions")

    def test_limited_to_chosen_rows(self):
        a = self.add_txn(name="NETFLIX", raw_name="NETFLIX.COM", envelope="Shopping")
        b = self.add_txn(name="SPOTIFY", raw_name="SPOTIFY", envelope="Shopping")
        self.call(self.app.reclassify_transactions, {"apply": True, "txn_ids": [a]})
        self.assertEqual(self.get_txn(a)["envelope"], "Bills: Subscriptions")
        self.assertEqual(self.get_txn(b)["envelope"], "Shopping")


class ReimbursementTests(TempAppCase):
    def test_half_amount_match_ranks_first(self):
        self.add_txn(txn_id="E-OLD", amount=-300.0, name="BIG DINNER", date="2026-05-01", envelope="Restaurant")
        self.add_txn(txn_id="E-HALF", amount=-562.0, name="CONCERT", date="2026-04-20", envelope="Shopping")
        self.add_txn(txn_id="E-SMALL", amount=-100.0, name="TOO SMALL", date="2026-05-02", envelope="Gas")
        venmo = self.add_txn(txn_id="V1", amount=281.0, envelope="", txn_kind="refund", date="2026-05-10")
        res = self.call(self.app.reimbursement_candidates, query={"txn_id": venmo}, method="GET")["candidates"]
        ids = [c["txn_id"] for c in res]
        self.assertEqual(ids[0], "E-HALF")
        self.assertEqual(res[0]["split"], "half")
        self.assertIn("E-OLD", ids)
        self.assertNotIn("E-SMALL", ids)  # smaller than the reimbursement
        self.assertNotIn("V1", ids)

    def test_old_expenses_are_out_of_the_window(self):
        self.add_txn(txn_id="E1", amount=-100.0, date="2026-01-01")
        res = self.call(self.app.reimbursement_candidates, query={"date": "2026-05-10", "amount": "50"}, method="GET")
        self.assertEqual(res["candidates"], [])

    def test_needs_an_id_or_date_and_amount(self):
        with self.assertRaises(HttpError):
            self.call(self.app.reimbursement_candidates, query={}, method="GET")


class ServerTests(TempAppCase):
    def setUp(self):
        super().setUp()
        router = self.app.build_router()
        self.httpd = HTTPServer(("127.0.0.1", 0), lambda *a, **k: None)  # placeholder to get a free port
        port = self.httpd.server_address[1]
        self.httpd.server_close()
        handler = http_util.make_handler(router, {f"127.0.0.1:{port}", f"localhost:{port}"})
        self.httpd = HTTPServer(("127.0.0.1", port), handler)
        self.port = port
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        super().tearDown()

    def get(self, path, headers=None):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", headers=headers or {})
        return urllib.request.urlopen(req, timeout=5)

    def test_health_has_no_cors_header(self):
        with self.get("/api/health") as r:
            self.assertEqual(json.loads(r.read())["status"], "ok")
            self.assertIsNone(r.headers.get("Access-Control-Allow-Origin"))

    def test_foreign_host_header_is_refused(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get("/api/health", headers={"Host": "evil.example.com"})
        self.assertEqual(ctx.exception.code, 403)

    def test_static_path_cannot_escape_web_folder(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get("/web/../evbudget/store.py")
        self.assertEqual(ctx.exception.code, 404)
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get("/web/%2e%2e/evbudget/store.py")
        self.assertEqual(ctx.exception.code, 404)

    def test_page_and_scripts_are_served(self):
        with self.get("/") as r:
            html = r.read().decode()
        self.assertIn("v3.0", html)
        self.assertNotIn("{{VERSION}}", html)
        for path in ("/web/dashboard.css", "/web/js/core.js", "/web/js/analytics.js", "/web/js/import.js"):
            with self.get(path) as r:
                self.assertEqual(r.status, 200, path)
                self.assertGreater(len(r.read()), 1000, path)

    def test_every_script_the_page_loads_exists(self):
        import re
        with self.get("/") as r:
            html = r.read().decode()
        for src in re.findall(r'src="(/web/[^"]+)"', html) + re.findall(r'href="(/web/[^"]+)"', html):
            with self.get(src) as r:
                self.assertEqual(r.status, 200, src)

    def test_unknown_route_is_json_404(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get("/api/nope")
        self.assertEqual(ctx.exception.code, 404)

    def test_bad_json_is_a_400(self):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/transactions/update", data=b"{not json",
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(ctx.exception.code, 400)

    def test_upload_and_scan_round_trip(self):
        boundary = "XBOUNDARYX"
        body = (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"files\"; filename=\"My Bank.csv\"\r\n"
            f"Content-Type: text/csv\r\n\r\nDate,Description,Amount\r\n05/01/2026,SHOP,-5.00\r\n--{boundary}--\r\n"
        ).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/import/upload", data=body, method="POST",
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            self.assertEqual(json.loads(r.read())["saved"], ["My Bank.csv"])
        with self.get("/api/import/scan") as r:
            files = json.loads(r.read())["files"]
        self.assertEqual([f["filename"] for f in files], ["My Bank.csv"])
        self.assertNotIn("filepath", files[0])


if __name__ == "__main__":
    unittest.main()
