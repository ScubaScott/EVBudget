"""Managing accounts: the list, adding, renaming, merging, deleting, and moving a transaction to another account."""

import unittest

from helpers import TempAppCase
from evbudget import cards
from evbudget.http_util import HttpError


class AccountTests(TempAppCase):
    def setUp(self):
        super().setUp()
        self.a1 = self.add_txn(account_name="Chase 3653", source_file="chase_jan.csv", date="2026-01-05")
        self.a2 = self.add_txn(account_name="Chase 3653", source_file="chase_feb.csv,other.csv", date="2026-02-05")
        self.b1 = self.add_txn(account_name="Checking", account_type="Bank / Checking", source_file="chk.csv")
        self.none = self.add_txn()

    def accounts(self):
        return {a["name"]: a for a in self.call(self.app.accounts_list, method="GET")["accounts"]}

    def test_names_already_on_transactions_become_accounts_with_their_stats(self):
        got = self.accounts()
        self.assertEqual(set(got), {"Chase 3653", "Checking"})
        chase = got["Chase 3653"]
        self.assertEqual((chase["transactions"], chase["first_date"], chase["last_date"]), (2, "2026-01-05", "2026-02-05"))
        self.assertEqual(chase["files"], ["chase_feb.csv", "chase_jan.csv", "other.csv"])
        self.assertEqual(chase["account_type"], "Credit / Debit")

    def test_add_an_account_before_any_import(self):
        self.call(self.app.accounts_save, {"name": "  Savings   Club ", "account_type": "Savings"})
        got = self.accounts()["Savings Club"]
        self.assertEqual((got["transactions"], got["account_type"]), (0, "Savings"))
        with self.assertRaises(HttpError):
            self.call(self.app.accounts_save, {"name": "savings club"})   # same name, any capitals
        with self.assertRaises(HttpError):
            self.call(self.app.accounts_save, {"name": "  "})

    def test_rename_moves_its_transactions_and_import_memory(self):
        c = self.conn()
        cards.remember(c, "chase3653", "Chase 3653")
        c.commit()
        c.close()
        acct = self.accounts()["Chase 3653"]
        self.call(self.app.accounts_save, {"id": acct["id"], "name": "Sapphire", "account_type": "Credit / Debit"})
        self.assertEqual(self.get_txn(self.a1)["account_name"], "Sapphire")
        self.assertEqual(self.get_txn(self.a2)["account_name"], "Sapphire")
        self.assertEqual(self.get_txn(self.b1)["account_name"], "Checking")
        c = self.conn()
        self.assertEqual(cards.load_memory(c)["chase3653"], "Sapphire")
        c.close()
        self.assertEqual(set(self.accounts()), {"Sapphire", "Checking"})

    def test_rename_to_an_existing_name_is_refused(self):
        acct = self.accounts()["Chase 3653"]
        with self.assertRaises(HttpError):
            self.call(self.app.accounts_save, {"id": acct["id"], "name": "checking"})

    def test_changing_type_updates_its_transactions(self):
        acct = self.accounts()["Checking"]
        self.call(self.app.accounts_save, {"id": acct["id"], "name": "Checking", "account_type": "Savings"})
        self.assertEqual(self.get_txn(self.b1)["account_type"], "Savings")

    def test_an_account_with_transactions_must_be_merged_not_deleted(self):
        got = self.accounts()
        with self.assertRaises(HttpError):
            self.call(self.app.accounts_delete, {"id": got["Chase 3653"]["id"]})
        res = self.call(self.app.accounts_delete, {"id": got["Chase 3653"]["id"], "merge_into": got["Checking"]["id"]})
        self.assertEqual(res["moved"], 2)
        self.assertEqual(self.get_txn(self.a1)["account_name"], "Checking")
        self.assertEqual(self.get_txn(self.a1)["account_type"], "Bank / Checking")
        self.assertEqual(set(self.accounts()), {"Checking"})

    def test_an_empty_account_can_be_deleted(self):
        new = self.call(self.app.accounts_save, {"name": "Old card"})["id"]
        self.assertEqual(self.call(self.app.accounts_delete, {"id": new})["moved"], 0)
        self.assertNotIn("Old card", self.accounts())

    def test_moving_one_transaction_to_another_account(self):
        self.call(self.app.update_transaction, {"txn_id": self.none, "account_name": "Checking"})
        self.assertEqual(self.get_txn(self.none)["account_name"], "Checking")
        self.call(self.app.update_transaction, {"txn_id": self.none, "account_name": "Brand New"})   # a new name is added to the list
        self.assertIn("Brand New", self.accounts())
        self.call(self.app.update_transaction, {"txn_id": self.none, "name": "x"})                     # left alone when not sent
        self.assertEqual(self.get_txn(self.none)["account_name"], "Brand New")
        self.call(self.app.update_transaction, {"txn_id": self.none, "account_name": ""})
        self.assertFalse(self.get_txn(self.none)["account_name"])

    def test_transactions_list_carries_the_account_names(self):
        res = self.call(self.app.get_transactions, method="GET")
        self.assertEqual(res["accounts"], ["Chase 3653", "Checking"])


if __name__ == "__main__":
    unittest.main()
