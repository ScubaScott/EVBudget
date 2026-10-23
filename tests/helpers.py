"""Shared test setup: a throwaway database and app in a temp folder."""

import json
import os
import shutil
import sys
import tempfile
import unittest
from typing import Any, Dict, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from evbudget import api, paths, store  # noqa: E402
from evbudget.http_util import Request  # noqa: E402

LEGACY_ATTRS = ("LEGACY_ENVELOPES_PATH", "LEGACY_USER_RULES_PATH", "LEGACY_KNOWN_VENDORS_PATH")


class TempAppCase(unittest.TestCase):
    """Each test gets a fresh v2 database with the default seeds."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="evbudget_test_")
        self._saved = {a: getattr(paths, a) for a in LEGACY_ATTRS}
        for a in LEGACY_ATTRS:
            setattr(paths, a, os.path.join(self.tmp, "legacy_" + a.lower() + ".json"))
        self.db = os.path.join(self.tmp, "LocalStore", "budget_store.db")
        self.input_dir = os.path.join(self.tmp, "input")
        os.makedirs(self.input_dir)
        store.ensure_schema(self.db)
        self.app = api.App(
            db_path=self.db, input_dir=self.input_dir, web_dir=os.path.join(ROOT, "web"),
            master_csv_path=os.path.join(self.tmp, "LocalStore", "master.csv"),
        )
        # Types are chosen by the owner, never guessed from names, so say which seeded envelopes are bills.
        c = store.connect(self.db)
        c.execute("UPDATE envelopes SET kind='bill', rollover_mode='rollover' WHERE LOWER(name) LIKE 'insurance:%' OR LOWER(name) IN "
                  "('property taxes', 'bills: mortgage', 'bills: every month', 'bills: subscriptions')")
        c.commit()
        c.close()

    def tearDown(self) -> None:
        for a, v in self._saved.items():
            setattr(paths, a, v)
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- helpers -----------------------------------------------------------

    def conn(self):
        return store.connect(self.db)

    def call(self, fn, body: Optional[Dict[str, Any]] = None, query: Optional[Dict[str, str]] = None, method: str = "POST"):
        raw = json.dumps(body).encode("utf-8") if body is not None else b""
        req = Request(method, "/test", query or {}, {"Content-Type": "application/json"}, raw)
        result = fn(req)
        return result

    def add_txn(self, **kw) -> str:
        row = {
            "txn_id": f"TXN-T{len(self._all_ids()) + 1:05d}", "date": "2026-05-01", "name": "TEST", "raw_name": "TEST",
            "amount": -10.0, "envelope": "Shopping", "check_num": "", "account_type": "Credit / Debit",
            "source_file": "t.csv", "is_included": 1, "exclusion_reason": "", "match_source": "",
            "updated_at": "2026-05-01T00:00:00", "txn_kind": "expense", "needs_review": 0, "user_modified": 0,
        }
        row.update(kw)
        cols = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        c = self.conn()
        c.execute(f"INSERT INTO transactions ({cols}) VALUES ({marks})", list(row.values()))
        c.commit()
        c.close()
        return row["txn_id"]

    def _all_ids(self):
        c = self.conn()
        ids = [r[0] for r in c.execute("SELECT txn_id FROM transactions")]
        c.close()
        return ids

    def get_txn(self, txn_id: str) -> Dict[str, Any]:
        c = self.conn()
        row = c.execute("SELECT * FROM transactions WHERE txn_id = ?", (txn_id,)).fetchone()
        c.close()
        return dict(row) if row else {}

    def write_csv(self, name: str, text: str) -> str:
        path = os.path.join(self.input_dir, name)
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        return path
