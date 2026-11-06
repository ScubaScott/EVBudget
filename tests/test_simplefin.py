"""Bank sync through SimpleFIN: connecting, downloading, account choices, and not saving anything twice."""

import base64
import io
import json
import unittest
import urllib.error
import urllib.parse
from datetime import date, datetime, timedelta, timezone

from helpers import TempAppCase

from evbudget import import_engine, simplefin, store
from evbudget.http_util import HttpError

ACCESS = "https://user1:secret@bridge.example.org/simplefin"
CLAIM = "https://bridge.example.org/simplefin/claim/TOKEN123"
TOKEN = base64.b64encode(CLAIM.encode()).decode()


def ts(day: str, hour: int = 12) -> int:
    d = date.fromisoformat(day)
    return int(datetime(d.year, d.month, d.day, hour, tzinfo=timezone.utc).timestamp())


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


class FakeBridge:
    """Stands in for urllib.request.urlopen."""

    def __init__(self, accounts=None, errlist=None, connections=None):
        self.accounts = accounts or []
        self.errlist = errlist or []
        self.connections = connections or [{"conn_id": "C1", "name": "Chase Bank", "org_id": "chase", "sfin_url": "x"}]
        self.requests = []
        self.fail_with = None
        self.claimed = False

    def __call__(self, req, timeout=None, context=None):
        self.requests.append(req)
        if self.fail_with:
            raise urllib.error.HTTPError(req.full_url, self.fail_with, "nope", {}, io.BytesIO(b""))
        if req.get_method() == "POST":
            if self.claimed:
                raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {}, io.BytesIO(b""))
            self.claimed = True
            return _Resp(ACCESS.encode())
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(req.full_url).query)
        start, end = int(q["start-date"][0]), int(q["end-date"][0])
        accts = []
        for a in self.accounts:
            txns = [t for t in a["transactions"] if start <= (t.get("transacted_at") or t["posted"] or t.get("_when", 0)) < end]
            accts.append(dict(a, transactions=txns))
        return _Resp(json.dumps({"errlist": self.errlist, "connections": self.connections, "accounts": accts}).encode())


def account(acct_id="A1", name="Sapphire Card (3855)", txns=(), balance="-120.50", conn_id="C1"):
    return {"id": acct_id, "name": name, "conn_id": conn_id, "currency": "USD", "balance": balance,
            "available-balance": "0", "balance-date": ts(date.today().isoformat()), "transactions": list(txns)}


def txn(tid, day, amount, desc, pending=False):
    t = {"id": tid, "posted": 0 if pending else ts(day), "amount": amount, "description": desc}
    if pending:
        t["pending"] = True
        t["_when"] = ts(day)
    return t


class ProtocolTests(unittest.TestCase):
    def test_setup_token_decodes_to_claim_url(self):
        self.assertEqual(simplefin.decode_setup_token(TOKEN), CLAIM)
        self.assertEqual(simplefin.decode_setup_token("  " + TOKEN[:20] + "\n" + TOKEN[20:]), CLAIM)

    def test_bad_tokens_are_explained(self):
        with self.assertRaises(simplefin.SimpleFinError) as ctx:
            simplefin.decode_setup_token("not a token!!")
        self.assertIn("isn't a SimpleFIN setup token", str(ctx.exception))
        with self.assertRaises(simplefin.SimpleFinError) as ctx:
            simplefin.decode_setup_token(base64.b64encode(b"http://insecure.example/claim").decode())
        self.assertIn("https", str(ctx.exception))

    def test_claim_once_then_used_token_warns(self):
        bridge = FakeBridge()
        self.assertEqual(simplefin.claim(TOKEN, bridge), ACCESS)
        with self.assertRaises(simplefin.SimpleFinError) as ctx:
            simplefin.claim(TOKEN, bridge)
        self.assertEqual(ctx.exception.code, "token_used")
        self.assertIn("someone else may have", str(ctx.exception))

    def test_fetch_sends_basic_auth_without_credentials_in_url(self):
        bridge = FakeBridge([account()])
        simplefin.fetch(ACCESS, date(2026, 10, 1), date(2026, 10, 31), bridge)
        req = bridge.requests[0]
        self.assertNotIn("secret", req.full_url)
        self.assertTrue(req.full_url.startswith("https://bridge.example.org/simplefin/accounts?"))
        self.assertIn("version=2", req.full_url)
        self.assertNotIn("pending", req.full_url)
        self.assertEqual(req.get_header("Authorization"), "Basic " + base64.b64encode(b"user1:secret").decode())

    def test_payment_needed_and_access_revoked(self):
        bridge = FakeBridge([account()])
        bridge.fail_with = 402
        with self.assertRaises(simplefin.SimpleFinError) as ctx:
            simplefin.fetch(ACCESS, date(2026, 10, 1), date(2026, 10, 2), bridge)
        self.assertIn("subscription", str(ctx.exception))
        bridge.fail_with = 403
        with self.assertRaises(simplefin.SimpleFinError) as ctx:
            simplefin.fetch(ACCESS, date(2026, 10, 1), date(2026, 10, 2), bridge)
        self.assertIn("turned off", str(ctx.exception))

    def test_errors_are_explained_and_cleaned(self):
        data = {"connections": [{"conn_id": "C1", "name": "Chase"}], "accounts": [{"id": "A1", "name": "Checking"}],
                "errlist": [{"code": "con.auth", "msg": "Login\x00 needed\n\n now", "conn_id": "C1"},
                            {"code": "act.missingdata", "msg": "gap", "account_id": "A1"},
                            {"code": "zzz.new", "msg": "something"}],
                "errors": ["old style message"]}
        out = simplefin.explain_errors(data)
        self.assertEqual(len(out), 4)
        self.assertEqual(out[0]["message"], "Login needed now")
        self.assertIn("Chase needs you to sign in again", out[0]["explain"])
        self.assertIn("Nothing is treated as deleted", out[1]["explain"])
        self.assertEqual(out[2]["explain"], "Note from SimpleFIN.")
        self.assertEqual([e["level"] for e in out], ["problem", "problem", "problem", "note"])

    def test_lost_characters_are_dropped_from_names(self):
        self.assertEqual(simplefin.sanitize("WAY2SAVE�� SAVINGS ...5241"), "WAY2SAVE SAVINGS ...5241")

    def test_ids_are_unique_across_accounts(self):
        a = simplefin.txn_id_for("h", "C1", "A1", "T1")
        self.assertNotEqual(a, simplefin.txn_id_for("h", "C1", "A2", "T1"))
        self.assertNotEqual(a, simplefin.txn_id_for("h", "C2", "A1", "T1"))
        self.assertEqual(a, simplefin.txn_id_for("h", "C1", "A1", "T1"))
        self.assertTrue(a.startswith("SF-"))

    def test_rows_skip_pending_unmapped_and_before_start(self):
        data = {"accounts": [
            account("A1", txns=[txn("T1", "2026-10-02", "-5.25", "STARBUCKS 123"), txn("T2", "2026-10-03", "-9", "PENDING", pending=True),
                                txn("T0", "2026-09-01", "-1", "OLD"), txn("T3", "2026-10-04", "200.00", "AUTOPAY PAYMENT - THANK YOU")]),
            account("A2", txns=[txn("T1", "2026-10-02", "-3", "X")]),
        ]}
        mapping = {"C1|A1": {"name": "Chase 3855", "account_type": "Credit / Debit", "start_date": "2026-09-15"}}
        rows, counts = simplefin.to_rows(data, "h", mapping)
        self.assertEqual([r["raw_name"] for r in rows], ["STARBUCKS 123", "AUTOPAY PAYMENT - THANK YOU"])
        self.assertEqual(rows[0]["amount"], -5.25)
        self.assertEqual(rows[0]["date"], "2026-10-02")
        self.assertEqual(rows[1]["trans_type"], "Payment")
        self.assertEqual(rows[0]["source_file"], "SimpleFIN")
        self.assertEqual(counts, {"pending": 1, "unmapped": 1, "ignored": 0, "before_start": 1, "unreadable": 0})

    def test_bank_wording_from_memo_is_preferred(self):
        t = txn("T1", "2026-10-02", "-25.50", "Fishing bait")
        t.update(payee="John's Fishin Shack", memo="JOHNS FISHIN SHACK BAIT")
        rows, _ = simplefin.to_rows({"accounts": [account("A1", txns=[t])]}, "h", {"C1|A1": {"name": "X"}})
        self.assertEqual(rows[0]["raw_name"], "JOHNS FISHIN SHACK BAIT")
        self.assertEqual(rows[0]["other_texts"], ["Fishing bait", "John's Fishin Shack"])

    def test_long_gaps_are_split_into_90_day_requests(self):
        pieces = simplefin.chunks(date(2026, 1, 1), date(2026, 7, 1))
        self.assertEqual(len(pieces), 5)                      # 181 days in 45-day pieces
        self.assertTrue(all((b - a).days <= simplefin.MAX_DAYS for a, b in pieces))
        self.assertEqual(pieces[0][0], date(2026, 1, 1))
        self.assertEqual(pieces[-1][1], date(2026, 7, 1))


class SyncFlowTests(TempAppCase):
    def setUp(self):
        super().setUp()
        today = date.today()
        self.d = lambda n: (today - timedelta(days=n)).isoformat()
        self.bridge = FakeBridge([account("A1", txns=[
            txn("T1", self.d(10), "-42.17", "AMZN Mktp US*2K3LX9"),       # saved from a CSV as "Amazon.com"
            txn("T2", self.d(4), "-12.00", "SQ *JOES COFFEE"),          # new
            txn("T3", self.d(2), "-7.00", "PENDING THING", pending=True),
        ])])
        self.app.simplefin_opener = self.bridge
        self.csv_id = self.add_txn(txn_id="TXN-CSV0001", date=self.d(9), name="Amazon.com", raw_name="Amazon.com",
                                   amount=-42.17, account_name="Chase 3855", source_file="Chase3855_Activity.csv")
        c = self.conn()
        c.execute("INSERT INTO accounts (name, account_type) VALUES ('Chase 3855', 'Credit / Debit')")
        c.commit()
        c.close()

    def connect_and_sync(self):
        self.call(self.app.simplefin_connect, {"token": TOKEN})
        return self.call(self.app.simplefin_sync, {})

    def map_accounts(self, start=None):
        return self.call(self.app.simplefin_accounts, {"accounts": [
            {"key": "C1|A1", "name": "Chase 3855", "account_type": "Credit / Debit", "start_date": start}]})

    def test_connect_stores_access_url_but_never_shows_it(self):
        out = self.call(self.app.simplefin_connect, {"token": TOKEN})
        self.assertTrue(out["connected"])
        self.assertEqual(out["host"], "bridge.example.org")
        self.assertNotIn("secret", json.dumps(out))
        status = self.call(self.app.simplefin_status, method="GET")
        self.assertNotIn("secret", json.dumps(status))
        c = self.conn()
        self.assertEqual(store.get_setting(c, simplefin.ACCESS_URL_KEY), ACCESS)
        c.close()

    def test_first_sync_asks_where_accounts_go_and_suggests_the_matching_one(self):
        out = self.connect_and_sync()
        self.assertTrue(out["needs_mapping"])
        self.assertNotIn("preview", out)
        a = out["accounts"][0]
        self.assertEqual(a["name"], "Chase 3855")              # same last four digits
        self.assertEqual(a["account_type"], "Credit / Debit")
        self.assertEqual(a["start_date"], (date.fromisoformat(self.d(9)) - timedelta(days=7)).isoformat())
        self.assertEqual((a["posted"], a["pending"]), (2, 1))
        self.assertEqual(len(self.bridge.requests), 3)        # claim + 90 days in two downloads

    def test_synced_row_pairs_with_differently_worded_file_row_then_stays_linked(self):
        self.connect_and_sync()
        preview = self.map_accounts()["preview"]
        rows = {r["raw_name"]: r for r in preview["records"]}
        amzn = rows["AMZN Mktp US*2K3LX9"]
        self.assertEqual(amzn["status"], "new")
        self.assertEqual(amzn["likely_duplicate"]["txn_id"], self.csv_id)
        self.assertEqual(amzn["likely_duplicate"]["confidence"], "medium")
        self.assertNotIn("likely_duplicate", rows["SQ *JOES COFFEE"])
        self.assertEqual(preview["summary"]["pending"], 1)
        self.assertEqual(len(self.bridge.requests), 3)        # choosing accounts didn't download again

        coffee = rows["SQ *JOES COFFEE"]
        res = self.call(self.app.import_commit, {
            "records": [coffee], "files": ["SimpleFIN"], "simplefin": True,
            "links": [{"txn_id": amzn["txn_id"], "alias_of": self.csv_id}]})
        self.assertEqual((res["inserted"], res["linked"]), (1, 1))
        c = self.conn()
        self.assertEqual(store.get_setting(c, simplefin.SYNCED_THROUGH_KEY), date.today().isoformat())
        self.assertEqual(c.execute("SELECT COUNT(*) FROM transactions WHERE amount = -42.17").fetchone()[0], 1)
        c.close()

        again = self.call(self.app.simplefin_preview, {})
        status = {r["raw_name"] if r["status"] == "new" else r["txn_id"]: r["status"] for r in again["records"]}
        self.assertEqual(status.get(self.csv_id), "existing")  # the linked row now shows as already saved
        self.assertEqual(again["summary"]["new"], 0)
        self.assertEqual(again["summary"]["likely_duplicates"], 0)

    def test_deleted_row_stays_deleted_under_its_synced_id(self):
        self.connect_and_sync()
        preview = self.map_accounts()["preview"]
        amzn = next(r for r in preview["records"] if r["raw_name"].startswith("AMZN"))
        self.call(self.app.import_commit, {"records": [], "files": ["SimpleFIN"], "links": [{"txn_id": amzn["txn_id"], "alias_of": self.csv_id}]})
        self.call(self.app.delete_transaction, {"txn_id": self.csv_id})
        again = self.call(self.app.simplefin_preview, {})
        amzn = next(r for r in again["records"] if r["raw_name"].startswith("AMZN"))
        self.assertEqual(amzn["status"], "deleted")

    def test_ignored_accounts_and_start_date(self):
        self.connect_and_sync()
        out = self.call(self.app.simplefin_accounts, {"accounts": [{"key": "C1|A1", "ignore": True}]})
        self.assertEqual(out["preview"]["records"], [])
        self.assertEqual(out["preview"]["summary"]["ignored"], 3)
        out = self.map_accounts(start=self.d(5))
        self.assertEqual([r["raw_name"] for r in out["preview"]["records"]], ["SQ *JOES COFFEE"])

    def test_next_sync_overlaps_the_last_one(self):
        c = self.conn()
        store.set_setting(c, simplefin.SYNCED_THROUGH_KEY, date(2026, 10, 20).isoformat())
        c.commit()
        start, end, short = simplefin.sync_range(c, today=date(2026, 10, 25))
        self.assertEqual((start, end, short), (date(2026, 10, 15), date(2026, 10, 26), False))
        start, end, short = simplefin.sync_range(c, today=date(2028, 1, 1))
        self.assertTrue(short)
        self.assertEqual((end - start).days, simplefin.MAX_DAYS * simplefin.MAX_CHUNKS)
        c.close()

    def test_daily_request_limit(self):
        self.call(self.app.simplefin_connect, {"token": TOKEN})
        c = self.conn()
        store.set_setting(c, simplefin.REQUESTS_KEY, json.dumps({"day": date.today().isoformat(), "count": simplefin.DAILY_LIMIT}))
        c.commit()
        c.close()
        with self.assertRaises(HttpError) as ctx:
            self.call(self.app.simplefin_sync, {})
        self.assertEqual(ctx.exception.status, 429)
        self.assertEqual(len(self.bridge.requests), 1)        # only the claim

    def test_sync_errors_reach_the_page(self):
        self.bridge.errlist = [{"code": "con.auth", "msg": "Please re-authenticate", "conn_id": "C1"}]
        out = self.connect_and_sync()
        self.assertEqual(out["errors"][0]["message"], "Please re-authenticate")
        self.assertIn("Chase Bank needs you to sign in again", out["errors"][0]["explain"])

    def test_renaming_an_account_keeps_sync_pointed_at_it(self):
        self.connect_and_sync()
        self.map_accounts()
        c = self.conn()
        acct = c.execute("SELECT id FROM accounts WHERE name = 'Chase 3855'").fetchone()[0]
        c.close()
        self.call(self.app.accounts_save, {"id": acct, "name": "Sapphire", "account_type": "Credit / Debit"})
        c = self.conn()
        self.assertEqual(simplefin.load_mapping(c)["C1|A1"]["name"], "Sapphire")
        c.close()

    def test_account_choices_can_be_changed_without_a_new_download(self):
        self.connect_and_sync()
        self.map_accounts()
        self.app._sf_fetch = None                             # the app was restarted: nothing downloaded this session
        status = self.call(self.app.simplefin_status, method="GET")
        self.assertFalse(status["needs_mapping"])
        a = status["accounts"][0]
        self.assertEqual((a["sf_name"], a["name"], a["connection"]), ("Sapphire Card (3855)", "Chase 3855", "Chase Bank"))
        self.assertIsNone(a["posted"])
        out = self.map_accounts(start=self.d(3))
        self.assertNotIn("preview", out)
        c = self.conn()
        self.assertEqual(simplefin.load_mapping(c)["C1|A1"]["start_date"], self.d(3))
        c.close()
        self.assertEqual(len(self.bridge.requests), 3)        # still only the claim and the first sync

    def test_banks_simplefin_has_not_checked_lately_are_flagged(self):
        self.bridge.accounts[0]["balance-date"] = ts(self.d(3))
        out = self.connect_and_sync()
        self.assertTrue(out["accounts"][0]["stale"])
        self.assertTrue(out["accounts"][0]["updated"].startswith(self.d(3)))
        self.app._sf_fetch = None
        self.assertTrue(self.call(self.app.simplefin_status, method="GET")["accounts"][0]["stale"])   # remembered
        out = self.call(self.app.simplefin_accounts, {"accounts": [{"key": "C1|A1", "ignore": True}]})
        self.assertFalse(out["accounts"][0]["stale"])         # not warned about for an account that is left out

    def test_moving_a_starting_day_earlier_makes_the_next_sync_reach_back(self):
        self.connect_and_sync()
        preview = self.map_accounts(start=self.d(5))["preview"]
        self.call(self.app.import_commit, {"records": [r for r in preview["records"] if r["status"] == "new"],
                                           "files": ["SimpleFIN"], "simplefin": True})
        c = self.conn()
        self.assertIsNone(simplefin.backfill_from(c))         # the first download already reached it
        start, _, _ = simplefin.sync_range(c)
        self.assertEqual(start, date.today() - timedelta(days=simplefin.OVERLAP_DAYS))
        c.close()

        out = self.map_accounts(start=self.d(30))
        self.assertIsNone(out["reach_back"])                  # this session's download began earlier than that
        self.app._sf_fetch = None
        status = self.call(self.app.simplefin_status, method="GET")
        self.assertEqual(status["reach_back"], self.d(30))
        c = self.conn()
        self.assertEqual(simplefin.sync_range(c)[0].isoformat(), self.d(30))
        c.close()
        out = self.call(self.app.simplefin_sync, {})
        self.assertIsNone(out["reach_back"])
        self.call(self.app.import_commit, {"records": [r for r in out["preview"]["records"] if r["status"] == "new"],
                                           "files": ["SimpleFIN"], "simplefin": True})
        c = self.conn()
        self.assertIsNone(simplefin.backfill_from(c))
        c.close()

    def test_bank_accounts_only_go_to_accounts_on_the_list(self):
        self.bridge.accounts.append(account("A2", name="EVERYDAY CHECKING ...9664", balance="200"))
        out = self.connect_and_sync()
        rows = {a["key"]: a for a in out["accounts"]}
        self.assertEqual(rows["C1|A2"]["name"], "")              # nothing on the list fits: not made up
        self.assertEqual([o["name"] for o in out["account_options"]], ["Chase 3855"])
        with self.assertRaises(HttpError) as ctx:
            self.call(self.app.simplefin_accounts, {"accounts": [
                {"key": "C1|A1", "name": "Chase 3855"}, {"key": "C1|A2", "sf_name": "EVERYDAY CHECKING", "name": "Checking"}]})
        self.assertIn("no account called \"Checking\"", str(ctx.exception))
        self.assertIn("Accounts first", str(ctx.exception))
        with self.assertRaises(HttpError):
            self.call(self.app.simplefin_accounts, {"accounts": [{"key": "C1|A2", "sf_name": "EVERYDAY CHECKING", "name": ""}]})
        c = self.conn()
        self.assertEqual(c.execute("SELECT COUNT(*) FROM accounts").fetchone()[0], 1)
        c.close()

        self.call(self.app.accounts_save, {"name": "Checking", "account_type": "Bank / Checking"})
        out = self.call(self.app.simplefin_accounts, {"accounts": [
            {"key": "C1|A1", "name": "chase 3855", "account_type": "Bank / Checking"}, {"key": "C1|A2", "name": "Checking"}]})
        rows = {a["key"]: a for a in out["accounts"]}
        self.assertEqual((rows["C1|A1"]["name"], rows["C1|A1"]["account_type"]), ("Chase 3855", "Credit / Debit"))   # the account's type
        self.assertIn("preview", out)

    def test_a_deleted_account_is_asked_about_again(self):
        self.connect_and_sync()
        self.map_accounts()
        c = self.conn()
        c.execute("DELETE FROM transactions")
        acct = c.execute("SELECT id FROM accounts WHERE name = 'Chase 3855'").fetchone()[0]
        c.commit()
        c.close()
        self.call(self.app.accounts_delete, {"id": acct})
        out = self.call(self.app.simplefin_status, method="GET")
        self.assertTrue(out["needs_mapping"])
        self.assertEqual(out["accounts"][0]["name"], "")
        self.assertNotIn("preview", out)

    def test_disconnect_forgets_access_but_keeps_choices(self):
        self.connect_and_sync()
        self.map_accounts()
        out = self.call(self.app.simplefin_disconnect, {})
        self.assertFalse(out["connected"])
        c = self.conn()
        self.assertEqual(store.get_setting(c, simplefin.ACCESS_URL_KEY), "")
        self.assertIn("C1|A1", simplefin.load_mapping(c))
        c.close()


class DuplicateRuleTests(TempAppCase):
    def rec(self, txn_id, day, amount, raw, account="Chase 3855", source="b.csv"):
        return {"txn_id": txn_id, "date": day, "amount": amount, "raw_name": raw, "name": raw, "status": "new",
                "account_name": account, "source_file": source}

    def test_file_rows_still_need_similar_text(self):
        self.add_txn(txn_id="TXN-A", date="2026-10-01", raw_name="NETFLIX.COM", name="Netflix", amount=-15.49, account_name="Chase 3855")
        out = import_engine.find_likely_duplicates(self.conn(), [self.rec("TXN-B", "2026-10-02", -15.49, "HULU 877")])
        self.assertEqual(out, {})

    def test_row_matched_exactly_in_this_batch_cannot_explain_another(self):
        self.add_txn(txn_id="TXN-A", date="2026-10-01", raw_name="STARBUCKS 1", amount=-5.0, account_name="Chase 3855")
        recs = [self.rec("TXN-B", "2026-10-01", -5.0, "STARBUCKS 1")]
        self.assertIn("TXN-B", import_engine.find_likely_duplicates(self.conn(), recs))
        self.assertEqual(import_engine.find_likely_duplicates(self.conn(), recs, taken={"TXN-A"}), {})

    def test_same_account_is_preferred(self):
        self.add_txn(txn_id="TXN-OTHER", date="2026-10-01", raw_name="TARGET 1", amount=-20.0, account_name="Checking")
        self.add_txn(txn_id="TXN-SAME", date="2026-10-03", raw_name="TARGET 1", amount=-20.0, account_name="Chase 3855")
        out = import_engine.find_likely_duplicates(self.conn(), [self.rec("TXN-B", "2026-10-01", -20.0, "TARGET 1")])
        self.assertEqual(out["TXN-B"]["txn_id"], "TXN-SAME")

    def test_synced_row_needs_same_account_to_pair_on_amount_alone(self):
        self.add_txn(txn_id="TXN-A", date="2026-10-01", raw_name="Amazon.com", name="Amazon.com", amount=-42.17, account_name="Checking")
        out = import_engine.find_likely_duplicates(self.conn(), [self.rec("SF-1", "2026-10-01", -42.17, "PAYPAL *XYZ", source="SimpleFIN")])
        self.assertEqual(out, {})

    def test_a_file_row_already_paired_with_a_synced_row_is_not_paired_again_on_amount_alone(self):
        self.add_txn(txn_id="TXN-A", date="2026-10-01", raw_name="Amazon.com", name="Amazon.com", amount=-42.17, account_name="Chase 3855")
        c = self.conn()
        c.execute("INSERT INTO txn_aliases (alias_id, txn_id, source) VALUES ('SF-OLD', 'TXN-A', 'sync')")
        c.commit()
        out = import_engine.find_likely_duplicates(c, [self.rec("SF-NEW", "2026-10-02", -42.17, "PAYPAL *XYZ", source="SimpleFIN")])
        self.assertEqual(out, {})

    def test_saved_synced_row_matches_only_inside_the_download(self):
        self.add_txn(txn_id="SF-OLD", date="2026-10-01", raw_name="SQ *JOES", amount=-12.0, account_name="Chase 3855", source_file="SimpleFIN")
        recs = [self.rec("SF-NEW", "2026-10-02", -12.0, "SQ *JOES", source="SimpleFIN")]
        self.assertEqual(import_engine.find_likely_duplicates(self.conn(), recs, sync_window=("2026-10-02", "2026-11-01")), {})
        out = import_engine.find_likely_duplicates(self.conn(), recs, sync_window=("2026-09-25", "2026-11-01"))
        self.assertEqual(out["SF-NEW"]["txn_id"], "SF-OLD")

    def test_file_import_remembers_rows_left_out_as_duplicates(self):
        self.add_txn(txn_id="TXN-A", date="2026-10-01", raw_name="TACO BELL 7005058", amount=-8.5, account_name="Chase 3855")
        path = f"{self.input_dir}/stmt.csv"
        with open(path, "w", encoding="utf-8") as f:
            f.write("Transaction Date,Description,Amount\n10/02/2026,TACO BELL 7005058 952-942-5766 MN,-8.50\n")
        info = import_engine.inspect_file_header(path)
        info["account_name"] = "Chase 3855"
        c = self.conn()
        preview = import_engine.build_preview(c, [info])
        row = preview["records"][0]
        self.assertEqual(row["likely_duplicate"]["txn_id"], "TXN-A")
        import_engine.commit_import(c, [], ["stmt.csv"], [{"txn_id": row["txn_id"], "alias_of": "TXN-A"}])
        again = import_engine.build_preview(c, [info])
        self.assertEqual([r["status"] for r in again["records"]], ["existing"])
        self.assertEqual(again["records"][0]["txn_id"], "TXN-A")
        c.close()


if __name__ == "__main__":
    unittest.main()
