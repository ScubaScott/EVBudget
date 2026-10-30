"""HTTP API: page, static files, transactions, envelopes, rules, and import."""

import mimetypes
import os
import threading
import time
import traceback
from datetime import datetime, timedelta
from email.parser import BytesParser
from email.policy import HTTP as HTTP_POLICY
from typing import Any, Dict, List, Optional, Tuple

from . import VERSION, audit, billmatch, budget, cardbills, cards, import_engine, ledger, income, paths, recurrence, reclassify, rules, store
from . import profiles as profiles_mod
from . import envelopes as env_mod
from .http_util import HttpError, Request, Response, Router
from .textutil import is_iso_date, to_iso_date

# Open pages say they are alive every few seconds. A page that goes quiet this long is treated as gone
# (browsers slow background tabs to about one beat a minute, so this is well above that).
PAGE_TIMEOUT_SECONDS = 150.0
# Once no page is left, wait this long before stopping, so a reload or moving between pages doesn't stop the app.
CLOSE_GRACE_SECONDS = 15.0

STATIC_TYPES = {
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}

TRANSACTION_FIELDS = [
    "txn_id", "date", "name", "raw_name", "amount", "envelope", "check_num", "account_type", "source_file",
    "is_included", "exclusion_reason", "match_source", "updated_at", "txn_kind", "income_source_id",
    "person_id", "income_subtype", "income_label", "bill_id", "needs_review", "user_modified",
    "reimburses_txn_id", "account_name",
]

EDITABLE_SETTINGS = (
    "bonus_threshold_pct", "suggest_window_spending_months", "suggest_window_bill_months",
    "bill_match_window_days", "bill_amount_tolerance_pct", "refund_lookback_days",
)


def parse_multipart(content_type: str, body: bytes) -> List[Tuple[str, bytes]]:
    """Return (filename, bytes) for each uploaded file in a multipart body."""
    header = b"Content-Type: " + content_type.encode("latin-1") + b"\r\nMIME-Version: 1.0\r\n\r\n"
    msg = BytesParser(policy=HTTP_POLICY).parsebytes(header + body)
    files = []
    if msg.is_multipart():
        for part in msg.iter_parts():
            name = part.get_filename()
            if name:
                files.append((name, part.get_payload(decode=True) or b""))
    return files


def _envelope_names(conn: Any) -> List[str]:
    return [
        r[0] for r in conn.execute(
            "SELECT name FROM envelopes WHERE kind != 'system' AND active = 1 ORDER BY sort_order, id"
        )
    ]


def _envelope_kinds(conn: Any) -> Dict[str, Dict[str, Any]]:
    return {
        r["name"]: {"id": r["id"], "kind": r["kind"]} for r in conn.execute(
            "SELECT id, name, kind FROM envelopes WHERE kind != 'system' AND active = 1")
    }


def _find_envelope(conn: Any, name: str) -> Optional[Any]:
    return conn.execute("SELECT * FROM envelopes WHERE LOWER(name) = LOWER(?)", (name.strip(),)).fetchone()


class App:
    """Holds file locations and builds the route table.

    With `profiles`, no budget is open until one is chosen (open_profile) and the file locations belong to
    that profile. Without it the app works on one fixed budget, from db_path and the other arguments.
    Either way, rules the budgets share live in shared_db_path when that is set.
    """

    def __init__(
        self,
        db_path: Optional[str] = None,
        input_dir: Optional[str] = None,
        web_dir: Optional[str] = None,
        master_csv_path: Optional[str] = None,
        shared_db_path: Optional[str] = None,
        profiles: Optional[profiles_mod.Profiles] = None,
    ):
        self.profiles = profiles
        self.profile_name: Optional[str] = None
        self._auto_open_allowed = True  # off after someone chooses "switch budget", so the picker can be reached
        self.shared_db_path = shared_db_path or (profiles.shared_db_path if profiles else None)
        fixed = profiles is None
        self._db_path = db_path or (paths.DB_PATH if fixed else None)
        self._input_dir = input_dir or (paths.INPUT_DIR if fixed else None)
        self._master_csv_path = master_csv_path or (paths.MASTER_CSV_PATH if fixed else None)
        self.web_dir = os.path.realpath(web_dir or paths.WEB_DIR)
        # Which browser pages are open, so the app can stop itself when the last one closes
        self.clock = time.monotonic
        self.request_shutdown = None  # set by the server: a function that stops it
        self._pages: Dict[str, float] = {}
        self._page_ever_seen = False
        self._empty_since: Optional[float] = None
        self._pages_lock = threading.Lock()

    @staticmethod
    def _open_only(value: Optional[str]) -> str:
        if value is None:
            raise HttpError(409, "No budget is open. Choose one first.", code="no_profile")
        return value

    @property
    def db_path(self) -> str:
        return self._open_only(self._db_path)

    @property
    def input_dir(self) -> str:
        return self._open_only(self._input_dir)

    @property
    def master_csv_path(self) -> str:
        return self._open_only(self._master_csv_path)

    def conn(self) -> Any:
        return store.connect(self.db_path, shared_path=self.shared_db_path)

    # ---------------------------------------------------------------- profiles

    def _need_profiles(self) -> profiles_mod.Profiles:
        if self.profiles is None:
            raise HttpError(400, "This copy of the app is running a single fixed budget.")
        return self.profiles

    def _activate(self, name: str) -> Dict[str, Any]:
        paths_ = self._need_profiles().profile_paths(name)
        report = store.ensure_schema(paths_["db"], self.shared_db_path)
        os.makedirs(paths_["input"], exist_ok=True)
        self._db_path, self._input_dir, self._master_csv_path = paths_["db"], paths_["input"], paths_["csv"]
        self.profile_name = paths_["name"]
        self.profiles.set_last_used(paths_["name"])
        try:  # the shared rules are small and shared by everyone, so keep a copy each time a budget opens
            store.backup_db(self.shared_db_path, backup_dir=os.path.join(os.path.dirname(self.shared_db_path), "backups"),
                            prefix="shared_rules")
        except Exception:
            pass
        self.sync_csv()
        return report

    def _auto_open_only_budget(self) -> None:
        """With exactly one budget, no PIN, and nothing older waiting to be moved into a budget, there is nothing to choose: open it."""
        pr = self.profiles
        if pr is None or self.profile_name is not None or not self._auto_open_allowed or pr.legacy_found():
            return
        names = pr.names()
        if len(names) == 1 and not pr.has_pin(names[0]):
            try:
                self._activate(names[0])
            except Exception:  # fall back to the picker, which shows the problem when the budget is opened
                traceback.print_exc()

    def close_profile(self) -> None:
        self._auto_open_allowed = False
        self.profile_name = None
        if self.profiles is not None:
            self._db_path = self._input_dir = self._master_csv_path = None

    def profiles_list(self, req: Request) -> Any:
        pr = self._need_profiles()
        return {
            "profiles": pr.listing(), "active": self.profile_name, "legacy_found": pr.legacy_found(),
            "min_pin_length": profiles_mod.MIN_PIN_LENGTH,
        }

    def profile_open(self, req: Request) -> Any:
        pr = self._need_profiles()
        data = req.json()
        try:
            name = pr.profile_paths(str(data.get("name") or ""))["name"]
            if not pr.check_pin(name, str(data.get("pin") or "")):
                raise HttpError(403, "That PIN is not right.", code="bad_pin")
            self._activate(name)
        except profiles_mod.ProfileError as exc:
            raise HttpError(400, str(exc))
        return {"success": True, "name": self.profile_name}

    def profile_create(self, req: Request) -> Any:
        pr = self._need_profiles()
        data = req.json()
        try:
            created = pr.create(str(data.get("name") or ""), str(data.get("pin") or ""))
            self._activate(created["name"])
        except profiles_mod.ProfileError as exc:
            raise HttpError(400, str(exc))
        return {"success": True, "name": self.profile_name}

    def profile_migrate(self, req: Request) -> Any:
        pr = self._need_profiles()
        try:
            result = pr.migrate_legacy(str(req.json().get("name") or ""))
            self._activate(result["name"])
        except profiles_mod.ProfileError as exc:
            raise HttpError(400, str(exc))
        return {"success": True, **result}

    def profile_close(self, req: Request) -> Any:
        self._need_profiles()
        self.close_profile()
        return {"success": True}

    def profile_pin(self, req: Request) -> Any:
        """Set, change or remove the PIN of the open budget. An empty new_pin removes it."""
        pr = self._need_profiles()
        if self.profile_name is None:
            self._open_only(None)
        data = req.json()
        try:
            has_pin = pr.set_pin(self.profile_name, str(data.get("current_pin") or ""), str(data.get("new_pin") or ""))
        except profiles_mod.ProfileError as exc:
            raise HttpError(400, str(exc))
        return {"success": True, "has_pin": has_pin}

    def sync_csv(self) -> None:
        store.sync_master_csv(self.db_path, self.master_csv_path)

    # ------------------------------------------------------------------ pages

    def page(self, req: Request) -> Response:
        self._auto_open_only_budget()
        page_name = "profiles.html" if self.profiles is not None and self.profile_name is None else "dashboard.html"
        path = os.path.join(self.web_dir, page_name)
        if not os.path.exists(path):
            return Response(f"<h1>{page_name} not found at {path}</h1>".encode(), "text/html; charset=utf-8", 500)
        with open(path, "r", encoding="utf-8") as f:
            html_text = f.read().replace("{{VERSION}}", f"{VERSION:.1f}")
        return Response(html_text.encode("utf-8"), "text/html; charset=utf-8")

    def static(self, req: Request) -> Response:
        rel = req.path[len("/web/"):]
        full = os.path.realpath(os.path.join(self.web_dir, rel))
        if not full.startswith(self.web_dir + os.sep) or not os.path.isfile(full):
            raise HttpError(404, "File not found")
        ext = os.path.splitext(full)[1].lower()
        ctype = STATIC_TYPES.get(ext) or mimetypes.guess_type(full)[0] or "application/octet-stream"
        with open(full, "rb") as f:
            return Response(f.read(), ctype)

    # ---------------------------------------------------------------- lifecycle

    def heartbeat(self, req: Request) -> Any:
        page = str(req.json().get("page") or "")[:64]
        if page:
            with self._pages_lock:
                self._pages[page] = self.clock()
                self._page_ever_seen = True
        return {"success": True}

    def page_closed(self, req: Request) -> Any:
        with self._pages_lock:
            self._pages.pop(str(req.json().get("page") or "")[:64], None)
        return {"success": True}

    def should_exit(self) -> bool:
        """True once pages have been open and every one of them has been gone for CLOSE_GRACE_SECONDS."""
        now = self.clock()
        with self._pages_lock:
            for page, seen in list(self._pages.items()):
                if now - seen > PAGE_TIMEOUT_SECONDS:
                    del self._pages[page]
            if self._pages:
                self._empty_since = None
                return False
            if not self._page_ever_seen:
                return False
            if self._empty_since is None:
                self._empty_since = now
            return now - self._empty_since >= CLOSE_GRACE_SECONDS

    def shutdown(self, req: Request) -> Any:
        # Needs a JSON body, which another website cannot send to this server without being refused.
        if "application/json" not in req.content_type:
            raise HttpError(400, "Expected JSON")
        if self.request_shutdown is None:
            raise HttpError(400, "This server can't be stopped from the page.")
        self.request_shutdown()
        return {"success": True}

    def health(self, req: Request) -> Dict[str, Any]:
        return {"status": "ok", "version": f"{VERSION:.1f}", "budget": self.profile_name}

    # ----------------------------------------------------------- transactions

    def get_transactions(self, req: Request) -> Dict[str, Any]:
        conn = self.conn()
        try:
            cols = ", ".join(TRANSACTION_FIELDS)
            rows = conn.execute(f"SELECT {cols} FROM transactions ORDER BY date DESC, txn_id ASC").fetchall()
            txns = []
            for r in rows:
                d = {k: r[k] for k in TRANSACTION_FIELDS}
                d["amount"] = float(d["amount"])
                for k in ("name", "raw_name", "envelope", "check_num", "account_type", "source_file",
                          "exclusion_reason", "match_source", "income_label", "income_subtype", "account_name"):
                    d[k] = d[k] or ""
                d["is_included"] = int(d["is_included"]) if d["is_included"] is not None else 1
                d["needs_review"] = int(d["needs_review"] or 0)
                d["user_modified"] = int(d["user_modified"] or 0)
                txns.append(d)
            allocated = {r[0]: int(r[1]) for r in conn.execute(
                "SELECT income_txn_id, SUM(total_cents) FROM allocations WHERE status = 'confirmed' AND income_txn_id IS NOT NULL GROUP BY income_txn_id")}
            for d in txns:
                cents = allocated.get(d["txn_id"])
                # funded = fully split; partial = some of it split, some still in Unallocated
                d["funded"] = cents is not None and cents >= round(d["amount"] * 100)
                d["partially_funded"] = cents is not None and not d["funded"]
            return {
                "version": f"{VERSION:.1f}",
                "go_live_date": store.get_setting(conn, "go_live_date") or None,
                "count": len(txns),
                "envelopes": _envelope_names(conn),
                "people": [dict(r) for r in conn.execute("SELECT id, name FROM people WHERE active = 1 ORDER BY id")],
                "income_sources": [
                    dict(r) for r in conn.execute("SELECT id, name, person_id FROM income_sources WHERE active = 1 ORDER BY id")
                ],
                "kinds": list(rules.KINDS),
                "transactions": txns,
            }
        finally:
            conn.close()

    def update_transaction(self, req: Request) -> Any:
        data = req.json()
        txn_id = data.get("txn_id")
        if not txn_id:
            raise HttpError(400, "Missing transaction ID")
        conn = self.conn()
        try:
            row = conn.execute("SELECT * FROM transactions WHERE txn_id = ?", (txn_id,)).fetchone()
            if row is None:
                raise HttpError(400, "Transaction ID not found")

            date = to_iso_date(str(data.get("date", row["date"])).strip())
            if not date:
                raise HttpError(400, "Invalid date")
            try:
                amount = float(data.get("amount", row["amount"]))
            except (TypeError, ValueError):
                raise HttpError(400, "Invalid amount")

            kind = data.get("txn_kind", row["txn_kind"])
            if kind not in rules.KINDS:
                raise HttpError(400, f"Unknown kind '{kind}'")
            included = int(data.get("is_included", row["is_included"]))
            if kind in rules.EXCLUDED_KINDS:
                included = 0
            if not included or kind not in ("paycheck", "other_income"):
                self._guard_allocation(conn, txn_id, bool(data.get("void_allocation")))
            reason = str(data.get("exclusion_reason", row["exclusion_reason"] or "")).strip()
            if kind in rules.EXCLUDED_KINDS:
                included = 0
            if not included and not reason:
                reason = "Internal Account Transfer" if kind == "transfer" else "Credit Card Payment / Autopay" if kind == "cc_payment" else "User Excluded"
            if included:
                reason = ""

            envelope = str(data.get("envelope", row["envelope"] or "")).strip()
            reimburses = data.get("reimburses_txn_id", row["reimburses_txn_id"]) or None
            if reimburses:
                target = conn.execute(
                    "SELECT envelope FROM transactions WHERE txn_id = ? AND txn_kind = 'expense'", (reimburses,)
                ).fetchone()
                if target is None or reimburses == txn_id:
                    raise HttpError(400, "Reimbursement must point to a different expense")
                if not envelope:
                    envelope = (target["envelope"] or "").strip()

            person_id = data.get("person_id", row["person_id"]) or None
            subtype = data.get("income_subtype", row["income_subtype"]) or None
            income_source_id = row["income_source_id"]
            if kind == "paycheck":
                subtype = subtype if subtype in ("regular", "bonus") else "regular"
                if not income_source_id:
                    hay = " ".join((row["raw_name"] or "").upper().split())
                    for src in conn.execute("SELECT id, person_id, match_phrase FROM income_sources WHERE active = 1"):
                        if src["match_phrase"].upper() in hay and (not person_id or src["person_id"] == int(person_id)):
                            income_source_id = src["id"]
                            person_id = person_id or src["person_id"]
                            break
            else:
                subtype, income_source_id = None, None
                if kind != "other_income":
                    person_id = None
            label = str(data.get("income_label", row["income_label"] or "")).strip() or None
            needs_review = int(data.get("needs_review", row["needs_review"]) or 0)

            now = datetime.now().isoformat()
            conn.execute(
                """UPDATE transactions SET date=?, name=?, amount=?, envelope=?, check_num=?, account_type=?,
                          is_included=?, exclusion_reason=?, txn_kind=?, person_id=?, income_subtype=?,
                          income_source_id=?, income_label=?, reimburses_txn_id=?, needs_review=?,
                          user_modified=1, updated_at=? WHERE txn_id=?""",
                (
                    date, str(data.get("name", row["name"])).strip(), amount, envelope,
                    str(data.get("check_num", row["check_num"] or "")).strip(),
                    str(data.get("account_type", row["account_type"] or "")).strip(),
                    included, reason, kind, person_id, subtype, income_source_id, label, reimburses,
                    needs_review, now, txn_id,
                ),
            )
            conn.commit()
        finally:
            conn.close()
        self.sync_csv()
        return {"success": True, "updated_at": now, "txn_id": txn_id}

    def _guard_allocation(self, conn: Any, txn_id: str, void_ok: bool) -> None:
        """A paycheck that was split across envelopes can't vanish silently: void its fill first."""
        a = conn.execute("SELECT id FROM allocations WHERE income_txn_id = ? AND status = 'confirmed'", (txn_id,)).fetchone()
        if a is None:
            return
        if not void_ok:
            raise HttpError(409, "This deposit has been split across envelopes. Undo that fill first.",
                            needs_void=True, allocation_id=a["id"])
        ledger.void_allocation(conn, a["id"])

    def delete_transaction(self, req: Request) -> Any:
        body = req.json()
        txn_id = body.get("txn_id", "")
        if not txn_id:
            raise HttpError(400, "Missing transaction ID")
        conn = self.conn()
        try:
            self._guard_allocation(conn, txn_id, bool(body.get("void_allocation")))
            row = conn.execute(
                "SELECT date, raw_name, amount, source_file FROM transactions WHERE txn_id = ?", (txn_id,)
            ).fetchone()
            if row is None:
                raise HttpError(400, "Transaction ID not found")
            # Remember the deletion so loading an overlapping bank file doesn't bring the row back.
            conn.execute(
                "INSERT OR REPLACE INTO deleted_transactions (txn_id, deleted_at, date, raw_name, amount, source_file) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (txn_id, datetime.now().isoformat(), row["date"], row["raw_name"], row["amount"], row["source_file"]),
            )
            conn.execute("DELETE FROM transactions WHERE txn_id = ?", (txn_id,))
            conn.execute("UPDATE transactions SET reimburses_txn_id = NULL WHERE reimburses_txn_id = ?", (txn_id,))
            conn.commit()
        finally:
            conn.close()
        self.sync_csv()
        return {"success": True, "txn_id": txn_id}

    def bulk_update(self, req: Request) -> Any:
        data = req.json()
        txn_ids = data.get("txn_ids") or []
        updates = data.get("updates") or {}
        if not txn_ids:
            raise HttpError(400, "No transaction IDs provided")

        sets, params = ["updated_at = ?", "user_modified = 1"], []  # type: List[str], List[Any]
        now = datetime.now().isoformat()
        params.append(now)

        if updates.get("name") is not None:
            sets.append("name = ?")
            params.append(str(updates["name"]).strip())
        if updates.get("envelope") is not None:
            sets.append("envelope = ?")
            params.append(str(updates["envelope"]).strip())
        kind = updates.get("txn_kind")
        included = updates.get("is_included")
        reason = str(updates.get("exclusion_reason", "") or "").strip()
        if kind is not None:
            if kind not in rules.KINDS:
                raise HttpError(400, f"Unknown kind '{kind}'")
            sets.append("txn_kind = ?")
            params.append(kind)
            if kind in rules.EXCLUDED_KINDS:
                included = 0
            elif included is None:
                included = 1
            if kind != "paycheck":
                sets += ["income_subtype = NULL", "income_source_id = NULL"]
            if kind not in ("paycheck", "other_income"):
                sets.append("person_id = NULL")
        if included is not None:
            included = int(included)
            sets.append("is_included = ?")
            params.append(included)
            sets.append("exclusion_reason = ?")
            if included:
                params.append("")
            else:
                params.append(reason or ("Internal Account Transfer" if kind == "transfer" else "Credit Card Payment / Autopay" if kind == "cc_payment" else "User Excluded"))
        if updates.get("person_id") is not None:
            sets.append("person_id = ?")
            params.append(int(updates["person_id"]) or None)
        if updates.get("income_label") is not None:
            sets.append("income_label = ?")
            params.append(str(updates["income_label"]).strip() or None)
        if updates.get("income_subtype") in ("regular", "bonus"):
            sets.append("income_subtype = ?")
            params.append(updates["income_subtype"])
        if updates.get("needs_review") is not None:
            sets.append("needs_review = ?")
            params.append(1 if updates["needs_review"] else 0)

        marks = ",".join("?" for _ in txn_ids)
        conn = self.conn()
        try:
            cur = conn.execute(f"UPDATE transactions SET {', '.join(sets)} WHERE txn_id IN ({marks})", params + list(txn_ids))
            affected = cur.rowcount
            rule_saved = False
            phrase = str(data.get("rule_phrase") or "").strip()
            if data.get("save_rule") and phrase:
                save_user_rules_from_bulk(conn, phrase, updates, kind, included, self._choose_scope(conn, data.get("rule_scope")))
                rule_saved = True
            conn.commit()
        finally:
            conn.close()
        self.sync_csv()
        return {"success": True, "updated_count": affected, "rule_saved": rule_saved, "updated_at": now}

    def reimbursement_candidates(self, req: Request) -> Any:
        conn = self.conn()
        try:
            txn_id = req.query.get("txn_id", "")
            if txn_id:
                row = conn.execute("SELECT date, amount FROM transactions WHERE txn_id = ?", (txn_id,)).fetchone()
                if row is None:
                    raise HttpError(404, "Transaction not found")
                date, amount = row["date"], float(row["amount"])
            else:
                date = req.query.get("date", "")
                try:
                    amount = float(req.query.get("amount", ""))
                except ValueError:
                    raise HttpError(400, "Provide txn_id, or date and amount")
                if not is_iso_date(date):
                    raise HttpError(400, "Provide txn_id, or date and amount")
            return {"candidates": rules.reimbursement_candidates(conn, date, amount, exclude_txn_id=txn_id)}
        finally:
            conn.close()

    def reclassify_transactions(self, req: Request) -> Any:
        data = req.json()
        conn = self.conn()
        try:
            result = reclassify.reclassify(
                conn, txn_ids=data.get("txn_ids") or None,
                include_edited=bool(data.get("include_edited")), apply=bool(data.get("apply")),
            )
        finally:
            conn.close()
        if result["applied"]:
            self.sync_csv()
        return result

    # -------------------------------------------------------------- envelopes

    def get_envelopes(self, req: Request) -> Dict[str, Any]:
        conn = self.conn()
        try:
            counts: Dict[str, int] = {}
            for r in conn.execute("SELECT envelope, COUNT(*) FROM transactions GROUP BY envelope"):
                counts[(r[0] or "").strip()] = int(r[1])
            return {"envelopes": _envelope_names(conn), "counts": counts, "kinds": _envelope_kinds(conn)}
        finally:
            conn.close()

    def add_envelope(self, req: Request) -> Any:
        name = str(req.json().get("envelope", "")).strip()
        if not name:
            raise HttpError(400, "Envelope name cannot be empty")
        conn = self.conn()
        try:
            if _find_envelope(conn, name):
                raise HttpError(400, f"Envelope '{name}' already exists")
            guess = store.guess_envelope_kind(name)
            kind = str(req.json().get("kind") or "spending")
            if kind not in env_mod.KINDS:
                raise HttpError(400, "Unknown envelope type")
            if kind != "spending":
                guess["rollover_mode"] = "rollover"
            guess["kind"] = kind
            order = conn.execute("SELECT COALESCE(MAX(sort_order), 0) + 1 FROM envelopes").fetchone()[0]
            conn.execute(
                "INSERT INTO envelopes (name, kind, rollover_mode, sort_order) VALUES (?, ?, ?, ?)",
                (name, guess["kind"], guess["rollover_mode"], order),
            )
            conn.commit()
            return {"success": True, "envelope": name, "envelopes": _envelope_names(conn), "kinds": _envelope_kinds(conn)}
        finally:
            conn.close()

    def rename_envelope(self, req: Request) -> Any:
        data = req.json()
        old, new = str(data.get("old_name", "")).strip(), str(data.get("new_name", "")).strip()
        if not old or not new:
            raise HttpError(400, "Both current and new envelope names are required")
        conn = self.conn()
        try:
            row = _find_envelope(conn, old)
            if row is None or row["kind"] == "system":
                raise HttpError(400, f"Envelope '{old}' not found")
            clash = _find_envelope(conn, new)
            if clash is not None and clash["id"] != row["id"]:
                raise HttpError(400, f"Envelope '{new}' already exists")
            conn.execute("UPDATE envelopes SET name = ? WHERE id = ?", (new, row["id"]))
            cur = conn.execute(
                "UPDATE transactions SET envelope = ?, updated_at = ? WHERE LOWER(TRIM(envelope)) = LOWER(?)",
                (new, datetime.now().isoformat(), old),
            )
            conn.execute("UPDATE rules SET set_envelope = ? WHERE LOWER(set_envelope) = LOWER(?)", (new, old))
            conn.commit()
            result = {"success": True, "old_name": old, "new_name": new, "renamed_count": cur.rowcount,
                      "envelopes": _envelope_names(conn), "kinds": _envelope_kinds(conn)}
        finally:
            conn.close()
        self.sync_csv()
        return result

    def delete_envelope(self, req: Request) -> Any:
        data = req.json()
        name = str(data.get("envelope", "")).strip()
        reassign_to = str(data.get("reassign_to") or "").strip()
        if not name:
            raise HttpError(400, "Missing envelope name")
        conn = self.conn()
        try:
            row = _find_envelope(conn, name)
            if row is None or row["kind"] == "system":
                raise HttpError(400, f"Envelope '{name}' not found")
            if reassign_to and _find_envelope(conn, reassign_to) is None:
                raise HttpError(400, f"Envelope '{reassign_to}' not found")
            if conn.execute(
                "SELECT 1 FROM envelope_moves WHERE voided_at IS NULL AND (from_envelope_id = ? OR to_envelope_id = ?)",
                (row["id"], row["id"])).fetchone():
                raise HttpError(400, f"'{name}' holds money. Move it out or switch the envelope off on the Budget tab instead.")
            conn.execute("DELETE FROM bills WHERE envelope_id = ?", (row["id"],))
            conn.execute("DELETE FROM fill_template_lines WHERE envelope_id = ?", (row["id"],))
            conn.execute("DELETE FROM envelopes WHERE id = ?", (row["id"],))
            cur = conn.execute(
                "UPDATE transactions SET envelope = ?, updated_at = ? WHERE LOWER(TRIM(envelope)) = LOWER(?)",
                (reassign_to, datetime.now().isoformat(), name),
            )
            if reassign_to:
                conn.execute("UPDATE rules SET set_envelope = ? WHERE LOWER(set_envelope) = LOWER(?)", (reassign_to, name))
            else:
                conn.execute("UPDATE rules SET active = 0 WHERE LOWER(set_envelope) = LOWER(?)", (name,))
            conn.commit()
            result = {"success": True, "envelope": name, "reassigned_count": cur.rowcount,
                      "reassigned_to": reassign_to, "envelopes": _envelope_names(conn), "kinds": _envelope_kinds(conn)}
        finally:
            conn.close()
        self.sync_csv()
        return result

    # ------------------------------------------------------------------ rules

    def get_rules(self, req: Request) -> Any:
        conn = self.conn()
        try:
            rows = [dict(r, scope="personal") for r in conn.execute("SELECT * FROM rules")]
            shared = store.has_shared(conn)
            if shared:
                rows += [dict(r, scope="shared") for r in conn.execute("SELECT * FROM shared.rules")]
            rows.sort(key=lambda r: (r["stage"], r["scope"] == "shared", r["priority"], r["id"]))
            return {"rules": rows, "stages": list(rules.STAGES), "kinds": list(rules.KINDS), "has_shared": shared}
        finally:
            conn.close()

    @staticmethod
    def _rule_table(rule_id: int) -> str:
        """Which store a rule is in follows from its ID (see store.SHARED_ID_BASE)."""
        return "shared.rules" if int(rule_id) >= store.SHARED_ID_BASE else "rules"

    @staticmethod
    def _choose_scope(conn: Any, scope: Any) -> str:
        """The scope for a new rule. Where rules can be shared the person must say which; there is no default."""
        if not store.has_shared(conn):
            return "personal"
        if scope not in ("personal", "shared"):
            raise HttpError(400, "Choose whether this rule is just for you or for everyone.")
        return scope

    def save_rule(self, req: Request) -> Any:
        data = req.json()
        phrase = str(data.get("phrase", "")).strip()
        stage = data.get("stage") or "envelope"
        if not phrase:
            raise HttpError(400, "Rule phrase is required")
        if stage not in rules.STAGES:
            raise HttpError(400, f"Unknown stage '{stage}'")
        mode = data.get("match_mode") or "contains"
        if mode not in rules.MATCH_MODES:
            raise HttpError(400, f"Unknown match mode '{mode}'")
        kind = data.get("set_kind") or None
        if kind and kind not in rules.KINDS:
            raise HttpError(400, f"Unknown kind '{kind}'")
        if mode == "regex":
            try:
                rules.check_rename_rule(phrase, (data.get("set_payee") or "").strip())
            except ValueError as exc:
                raise HttpError(400, str(exc))
        fields = {
            "phrase": phrase, "match_mode": mode, "stage": stage, "set_kind": kind,
            "set_envelope": (data.get("set_envelope") or "").strip() or None,
            "set_payee": (data.get("set_payee") or "").strip() or None,
            "set_income_label": (data.get("set_income_label") or "").strip() or None,
            "set_needs_review": 1 if data.get("set_needs_review") else 0,
            "exclusion_reason": (data.get("exclusion_reason") or "").strip() or None,
            "description": (data.get("description") or "").strip()[:120] or None,
        }
        conn = self.conn()
        try:
            rule_id = data.get("id")
            if not rule_id:
                rule_id = save_user_rule(conn, dict(fields, scope=self._choose_scope(conn, data.get("scope"))))
            else:
                table = self._rule_table(rule_id)
                current = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (int(rule_id),)).fetchone()
                if current is None:
                    raise HttpError(404, "Rule not found")
                active = 0 if data.get("active") in (0, False) else 1
                scope = data.get("scope")
                if scope in ("personal", "shared") and store.has_shared(conn) and (scope == "shared") != (table == "shared.rules"):
                    # Changing who a rule applies to moves it to the other store, keeping its place in the order.
                    moved = dict(current, **fields, active=active)
                    conn.execute(f"DELETE FROM {table} WHERE id = ?", (int(rule_id),))
                    shared = scope == "shared"
                    rule_id = store.insert_rule(conn, moved, shared=shared, table="shared.rules" if shared else "rules")
                else:
                    conn.execute(
                        f"""UPDATE {table} SET phrase=:phrase, match_mode=:match_mode, stage=:stage, set_kind=:set_kind,
                                  set_envelope=:set_envelope, set_payee=:set_payee, set_income_label=:set_income_label,
                                  set_needs_review=:set_needs_review, exclusion_reason=:exclusion_reason,
                                  description=:description, active=:active WHERE id=:id""",
                        dict(fields, id=int(rule_id), active=active),
                    )
            conn.commit()
            return {"success": True, "id": rule_id}
        finally:
            conn.close()

    def _rename_rule_from(self, data: Dict[str, Any]) -> Dict[str, Any]:
        rule = {"phrase": str(data.get("phrase") or "").strip(), "match_mode": data.get("match_mode") or "contains",
                "set_payee": str(data.get("set_payee") or "").strip()}
        if not rule["phrase"] or not rule["set_payee"]:
            raise HttpError(400, "Enter the text to look for and the new name")
        if rule["match_mode"] == "regex":
            try:
                rules.check_rename_rule(rule["phrase"], rule["set_payee"])
            except ValueError as exc:
                raise HttpError(400, str(exc))
        return rule

    def _renames(self, conn: Any, rule: Dict[str, Any]) -> List[Dict[str, Any]]:
        out = []
        for r in conn.execute("SELECT txn_id, name, raw_name, user_modified FROM transactions"):
            hay = " ".join((r["raw_name"] or "").upper().split())
            new = rules.rename_for(rule, hay, r["name"] or "")
            if new is not None and new != r["name"]:
                out.append({"txn_id": r["txn_id"], "before": r["name"], "after": new, "locked": bool(r["user_modified"])})
        return out

    def rule_preview(self, req: Request) -> Any:
        """What a rename rule would do to the transactions already saved."""
        rule = self._rename_rule_from(req.json())

        def work(c):
            changes = self._renames(c, rule)
            open_ = [x for x in changes if not x["locked"]]
            seen, examples = set(), []
            for x in open_:
                if (x["before"], x["after"]) not in seen and len(examples) < 8:
                    seen.add((x["before"], x["after"]))
                    examples.append({"before": x["before"], "after": x["after"]})
            return {"count": len(open_), "locked": len(changes) - len(open_), "examples": examples}
        return self._run(work)

    def rule_apply_rename(self, req: Request) -> Any:
        """Rename saved transactions with this rule, skipping ones you edited by hand."""
        rule = self._rename_rule_from(req.json())

        def work(c):
            now = datetime.now().isoformat()
            n = 0
            for x in self._renames(c, rule):
                if not x["locked"]:
                    c.execute("UPDATE transactions SET name = ?, updated_at = ? WHERE txn_id = ?", (x["after"], now, x["txn_id"]))
                    n += 1
            return {"success": True, "renamed": n}
        out = self._run(work)
        self.sync_csv()
        return out

    def delete_rule(self, req: Request) -> Any:
        rule_id = req.json().get("id")
        if not rule_id:
            raise HttpError(400, "Missing rule id")
        conn = self.conn()
        try:
            conn.execute(f"DELETE FROM {self._rule_table(rule_id)} WHERE id = ?", (int(rule_id),))
            conn.commit()
            return {"success": True}
        finally:
            conn.close()

    # ----------------------------------------------------------------- import

    def _imported_counts(self, conn: Any) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for r in conn.execute("SELECT source_file, COUNT(*) FROM transactions GROUP BY source_file"):
            for name in (r[0] or "").split(","):
                name = name.strip()
                if name:
                    counts[name] = counts.get(name, 0) + int(r[1])
        return counts

    def import_scan(self, req: Request) -> Any:
        conn = self.conn()
        try:
            counts = self._imported_counts(conn)
        finally:
            conn.close()
        files = import_engine.list_input_files(self.input_dir)
        for f in files:
            f["imported_rows"] = counts.get(f["filename"], 0)
            del f["filepath"]
        return {"files": files}

    def import_upload(self, req: Request) -> Any:
        if "multipart/form-data" not in req.content_type:
            raise HttpError(400, "Expected a file upload")
        saved = []
        for name, data in parse_multipart(req.content_type, req.raw):
            target = import_engine.save_upload(name, data, self.input_dir)
            saved.append(os.path.basename(target))
        if not saved:
            raise HttpError(400, "No files received")
        return {"success": True, "saved": saved}

    def import_inspect(self, req: Request) -> Any:
        names = req.json().get("filenames") or []
        conn = self.conn()
        try:
            presets = import_engine.load_presets(conn)
            counts = self._imported_counts(conn)
            account_names = cards.load_memory(conn)
            known_accounts = cards.known_names(conn)
        finally:
            conn.close()
        out = []
        for name in names:
            try:
                path = import_engine.resolve_input_file(name, self.input_dir)
            except FileNotFoundError:
                out.append({"valid": False, "filename": name, "error": "File not found"})
                continue
            info = import_engine.inspect_file_header(path, presets, account_names)
            info.pop("filepath", None)
            info["imported_rows"] = counts.get(info.get("filename", name), 0)
            out.append(info)
        return {"files": out, "accounts": known_accounts}

    def _resolve_file_configs(self, configs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        resolved = []
        for fc in configs:
            try:
                path = import_engine.resolve_input_file(str(fc.get("filename", "")), self.input_dir)
            except FileNotFoundError:
                continue
            mapping = fc.get("mapping") or {}
            resolved.append({
                "filepath": path, "filename": os.path.basename(path), "mapping": mapping,
                "account_type": fc.get("account_type") or "Bank / Checking",
                "is_mapped": import_engine.is_pdf(path) or bool(
                    mapping.get("date_col") and mapping.get("name_col")
                    and (mapping.get("amount_col") or mapping.get("is_dual"))),
                "headers": fc.get("headers") or [],
                "account_name": cards.clean_name(fc.get("account_name")),
                "memory_key": str(fc.get("memory_key") or "") or cards.memory_key(path),
            })
        return resolved

    def import_preview(self, req: Request) -> Any:
        configs = self._resolve_file_configs(req.json().get("files") or [])
        if not configs:
            raise HttpError(400, "No readable files selected")
        conn = self.conn()
        try:
            for fc in configs:
                if fc["is_mapped"] and fc["headers"]:
                    import_engine.save_preset(conn, fc["headers"], fc["mapping"], fc["account_type"])
                cards.remember(conn, fc["memory_key"], fc["account_name"])
            conn.commit()
            preview = import_engine.build_preview(conn, configs)
            preview["people"] = [dict(r) for r in conn.execute("SELECT id, name FROM people WHERE active = 1 ORDER BY id")]
            preview["income_sources"] = [
                dict(r) for r in conn.execute("SELECT id, name, person_id FROM income_sources WHERE active = 1 ORDER BY id")
            ]
            preview["envelopes"] = _envelope_names(conn)
            return preview
        finally:
            conn.close()

    def import_commit(self, req: Request) -> Any:
        data = req.json()
        records = data.get("records") or []
        if not records:
            raise HttpError(400, "Nothing to save")
        conn = self.conn()
        try:
            result = import_engine.commit_import(conn, records, [str(f) for f in data.get("files") or []])
        finally:
            conn.close()
        self.sync_csv()
        result["success"] = True
        # Run the checks you chose for every import on just the rows that were saved
        conn = self.conn()
        try:
            result["audit"] = audit.run_for_import(conn, result.get("inserted_ids") or [])
            result["audit_auto_open"] = audit.get_config(conn)["auto_open"]
        finally:
            conn.close()
        return result

    def import_history(self, req: Request) -> Any:
        conn = self.conn()
        try:
            rows = [dict(r) for r in conn.execute("SELECT * FROM import_batches ORDER BY id DESC LIMIT 20")]
            return {"batches": rows}
        finally:
            conn.close()

    def vendor_lookup(self, req: Request) -> Any:
        return import_engine.lookup_vendor_online(str(req.json().get("query", "")))

    # ----------------------------------------------------------- people / income

    def get_people(self, req: Request) -> Any:
        conn = self.conn()
        try:
            return {"people": [dict(r) for r in conn.execute("SELECT * FROM people ORDER BY id")]}
        finally:
            conn.close()

    def save_person(self, req: Request) -> Any:
        data = req.json()
        name = str(data.get("name", "")).strip()
        if not name:
            raise HttpError(400, "Name is required")
        color = (str(data.get("color") or "").strip() or None)
        active = 0 if data.get("active") in (0, False) else 1
        conn = self.conn()
        try:
            clash = conn.execute("SELECT id FROM people WHERE LOWER(name) = LOWER(?)", (name,)).fetchone()
            pid = data.get("id")
            if clash and (not pid or clash["id"] != int(pid)):
                raise HttpError(400, f"'{name}' already exists")
            if pid:
                if conn.execute("UPDATE people SET name=?, color=?, active=? WHERE id=?", (name, color, active, int(pid))).rowcount == 0:
                    raise HttpError(404, "Person not found")
            else:
                pid = conn.execute("INSERT INTO people (name, color, active) VALUES (?, ?, ?)", (name, color, active)).lastrowid
            conn.commit()
            return {"success": True, "id": int(pid)}
        finally:
            conn.close()

    def get_income_sources(self, req: Request) -> Any:
        conn = self.conn()
        try:
            return {"sources": income.source_stats(conn), "planning": {
                k: v for k, v in income.planning_income(conn).items() if k != "sources"}}
        finally:
            conn.close()

    def income_source_preview(self, req: Request) -> Any:
        """How many saved transactions a phrase would match, with a few examples."""
        phrase = req.query.get("phrase", "").strip().upper()
        if not phrase:
            return {"count": 0, "total": 0.0, "examples": []}
        conn = self.conn()
        try:
            rows = conn.execute(
                "SELECT date, name, raw_name, amount FROM transactions WHERE amount > 0 AND is_included = 1 "
                "ORDER BY date DESC").fetchall()
            hits = [r for r in rows if phrase in " ".join((r["raw_name"] or "").upper().split()) or phrase in (r["name"] or "").upper()]
            return {
                "count": len(hits), "total": round(sum(float(r["amount"]) for r in hits), 2),
                "examples": [{"date": r["date"], "name": r["name"], "amount": float(r["amount"])} for r in hits[:5]],
            }
        finally:
            conn.close()

    def save_income_source(self, req: Request) -> Any:
        data = req.json()
        name = str(data.get("name", "")).strip()
        phrase = str(data.get("match_phrase", "")).strip()
        if not name or not phrase:
            raise HttpError(400, "Name and match text are required")
        freq = data.get("cadence_freq") or "weekly"
        if freq not in recurrence.FREQUENCIES:
            raise HttpError(400, "Unknown pay frequency")
        try:
            interval = max(1, int(data.get("cadence_interval") or 1))
            person_id = int(data["person_id"]) if data.get("person_id") else None
            typical = round(float(data["typical_amount"]) * 100) if data.get("typical_amount") not in (None, "") else None
            threshold = int(data["bonus_threshold_pct"]) if data.get("bonus_threshold_pct") not in (None, "") else None
        except (TypeError, ValueError):
            raise HttpError(400, "A number field is not a valid number")
        anchor = str(data.get("cadence_anchor_date") or "").strip() or None
        if anchor and recurrence.parse(anchor) is None:
            raise HttpError(400, "Anchor date must be a real date (YYYY-MM-DD)")
        active = 0 if data.get("active") in (0, False) else 1
        conn = self.conn()
        try:
            if not person_id or conn.execute("SELECT 1 FROM people WHERE id = ?", (person_id,)).fetchone() is None:
                raise HttpError(400, "Choose who this income belongs to")
            args = (person_id, name, phrase, freq, interval, anchor, typical, threshold, active)
            sid = data.get("id")
            if sid:
                if conn.execute(
                    """UPDATE income_sources SET person_id=?, name=?, match_phrase=?, cadence_freq=?, cadence_interval=?,
                              cadence_anchor_date=?, typical_amount_cents=?, bonus_threshold_pct=?, active=? WHERE id=?""",
                    args + (int(sid),)).rowcount == 0:
                    raise HttpError(404, "Income source not found")
            else:
                sid = conn.execute(
                    """INSERT INTO income_sources (person_id, name, match_phrase, cadence_freq, cadence_interval,
                              cadence_anchor_date, typical_amount_cents, bonus_threshold_pct, active)
                       VALUES (?,?,?,?,?,?,?,?,?)""", args).lastrowid
            conn.commit()
            result: Dict[str, Any] = {"success": True, "id": int(sid), "retagged": 0}
            if data.get("apply_to_existing"):
                ids = [
                    r["txn_id"] for r in conn.execute(
                        "SELECT txn_id, raw_name, name FROM transactions WHERE amount > 0 AND user_modified = 0")
                    if phrase.upper() in " ".join((r["raw_name"] or "").upper().split()) or phrase.upper() in (r["name"] or "").upper()
                ]
                if ids:
                    res = reclassify.reclassify(conn, txn_ids=ids, apply=True)
                    result["retagged"] = res["changed_count"]
        finally:
            conn.close()
        if result["retagged"]:
            self.sync_csv()
        return result

    def delete_income_source(self, req: Request) -> Any:
        sid = req.json().get("id")
        if not sid:
            raise HttpError(400, "Missing id")
        conn = self.conn()
        try:
            used = conn.execute("SELECT COUNT(*) FROM transactions WHERE income_source_id = ?", (int(sid),)).fetchone()[0]
            if used:
                conn.execute("UPDATE income_sources SET active = 0 WHERE id = ?", (int(sid),))
            else:
                conn.execute("DELETE FROM income_sources WHERE id = ?", (int(sid),))
            conn.commit()
            return {"success": True, "deactivated": bool(used)}
        finally:
            conn.close()

    def income_summary(self, req: Request) -> Any:
        today = datetime.now().date()
        start = to_iso_date(req.query.get("from", "")) or f"{today.year}-01-01"
        end = to_iso_date(req.query.get("to", "")) or today.isoformat()
        if start > end:
            raise HttpError(400, "Start date is after end date")
        conn = self.conn()
        try:
            result = income.income_summary(conn, start, end)
            result["budget_monthly"] = round(sum(e["effective_budget"] for e in env_mod.list_envelopes(conn) if e["active"]), 2)
            result["go_live_date"] = store.get_setting(conn, "go_live_date") or None
            return result
        finally:
            conn.close()

    # --------------------------------------------------------- budget / bills

    def _run(self, work):
        """Run work(conn) and commit; validation problems become 400s."""
        conn = self.conn()
        try:
            result = work(conn)
            conn.commit()
            return result
        except env_mod.ValidationError as exc:
            conn.rollback()
            raise HttpError(400, str(exc))
        finally:
            conn.close()

    def budget_envelopes(self, req: Request) -> Any:
        def work(c):
            data = budget.suggestions(c)
            data["envelopes"] = env_mod.list_envelopes(c)
            data["unallocated_id"] = env_mod.system_envelope_id(c)
            return data
        return self._run(work)

    def save_envelope_settings(self, req: Request) -> Any:
        return self._run(lambda c: (env_mod.save_envelope(c, req.json()), {"success": True})[1])

    def get_bills(self, req: Request) -> Any:
        env_id = int(req.query["envelope_id"]) if req.query.get("envelope_id") else None
        return self._run(lambda c: {"bills": env_mod.list_bills(c, env_id)})

    def save_bill(self, req: Request) -> Any:
        return self._run(lambda c: {"success": True, "id": env_mod.save_bill(c, req.json())})

    def delete_bill(self, req: Request) -> Any:
        bill_id = req.json().get("id")
        if not bill_id:
            raise HttpError(400, "Missing id")
        return self._run(lambda c: (env_mod.delete_bill(c, bill_id), {"success": True})[1])

    def bill_preview(self, req: Request) -> Any:
        try:
            interval = max(1, int(req.query.get("interval") or 1))
        except ValueError:
            raise HttpError(400, "Interval must be a number")
        freq = req.query.get("freq") or "monthly"
        if freq not in recurrence.FREQUENCIES:
            raise HttpError(400, "Unknown repeat schedule")
        return {"dates": env_mod.next_due_dates(freq, interval, req.query.get("anchor"), datetime.now().date() - timedelta(days=1))}

    def bill_history(self, req: Request) -> Any:
        try:
            env_id = int(req.query.get("envelope_id", ""))
        except ValueError:
            raise HttpError(400, "Provide envelope_id")
        return self._run(lambda c: {"payees": env_mod.bill_history(c, env_id)})

    def accept_budget(self, req: Request) -> Any:
        items = req.json().get("items") or []
        return self._run(lambda c: {"success": True, "updated": budget.accept_suggestions(c, items)})

    def get_templates(self, req: Request) -> Any:
        return self._run(lambda c: {"templates": budget.get_templates(c)})

    def save_template(self, req: Request) -> Any:
        data = req.json()
        return self._run(lambda c: (budget.save_template(c, int(data.get("income_source_id") or 0), data.get("lines") or []), {"success": True})[1])

    def generate_template(self, req: Request) -> Any:
        sid = int(req.json().get("income_source_id") or 0)
        return self._run(lambda c: budget.generate_template(c, sid))

    def get_settings(self, req: Request) -> Any:
        def work(c):
            out = {k: store.get_setting(c, k) for k in EDITABLE_SETTINGS}
            out["go_live_date"] = store.get_setting(c, "go_live_date") or None
            return out
        return self._run(work)

    def save_settings(self, req: Request) -> Any:
        data = req.json()

        def work(c):
            for k in EDITABLE_SETTINGS:
                if k in data:
                    try:
                        v = int(data[k])
                    except (TypeError, ValueError):
                        raise env_mod.ValidationError(f"{k} must be a whole number")
                    if v < 1:
                        raise env_mod.ValidationError(f"{k} must be at least 1")
                    store.set_setting(c, k, str(v))
            return {"success": True}
        return self._run(work)

    def setup_status(self, req: Request) -> Any:
        return self._run(env_mod.setup_status)

    def setup_go_live(self, req: Request) -> Any:
        data = req.json()
        if data.get("reset"):
            store.backup_db(self.db_path)  # re-running setup rewrites envelope history, so keep a copy first
        return self._run(lambda c: dict(
            env_mod.go_live(c, str(data.get("date") or ""), data.get("opening") or {}, bool(data.get("reset"))), success=True))

    # ------------------------------------------------- balances and fills

    def _today(self, req: Request) -> Any:
        d = recurrence.parse(req.query.get("as_of", "")) if req.query.get("as_of") else None
        return d or datetime.now().date()

    def _ledger_run(self, work):
        """Like _run, but ledger conflicts become 409s."""
        try:
            return self._run(work)
        except ledger.Conflict as exc:
            raise HttpError(409, str(exc), **exc.extra)

    def envelope_balances(self, req: Request) -> Any:
        today = self._today(req)

        def work(c):
            data = ledger.envelope_states(c, today)
            if data["enabled"]:
                data["queue_count"] = len(ledger.fill_queue(c))
                data["bills_due_soon"] = len([b for b in ledger.upcoming_bills(c, today, 7)])
                data["invariant"] = ledger.check_invariant(c, today)
            return data
        return self._ledger_run(work)

    def envelope_ledger(self, req: Request) -> Any:
        try:
            env_id = int(req.query.get("envelope_id", ""))
        except ValueError:
            raise HttpError(400, "Provide envelope_id")
        return self._ledger_run(lambda c: ledger.ledger(c, env_id, req.query.get("from"), req.query.get("to")))

    def ledger_csv(self, req: Request) -> Any:
        try:
            env_id = int(req.query.get("envelope_id", ""))
        except ValueError:
            raise HttpError(400, "Provide envelope_id")
        out = self._ledger_run(lambda c: ledger.ledger_csv(c, env_id, req.query.get("from"), req.query.get("to")))
        return Response(("\ufeff" + out["text"]).encode("utf-8"), "text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{out["filename"]}"'})

    def get_cards(self, req: Request) -> Any:
        """Card names in use, and earlier imports that still have none."""
        return self._run(lambda c: {"cards": cards.list_cards(c), "unnamed": cards.unnamed_groups(c, self.input_dir)})

    def name_cards(self, req: Request) -> Any:
        assignments = req.json().get("assignments") or []
        return self._run(lambda c: {"success": True, "named": cards.name_groups(c, assignments, self.input_dir)})

    def card_bills(self, req: Request) -> Any:
        card = req.query.get("card", "").strip()
        if not card:
            raise HttpError(400, "Choose a card")
        return self._run(lambda c: cardbills.find_bills(c, card))

    def mark_card_bill(self, req: Request) -> Any:
        data = req.json()
        card, key, status = str(data.get("card") or "").strip(), str(data.get("key") or "").strip(), str(data.get("status") or "")
        if not card or not key:
            raise HttpError(400, "Missing card or bill")

        def work(c):
            try:
                cardbills.set_status(c, card, key, status)
            except ValueError as exc:
                raise HttpError(400, str(exc))
            return {"success": True}
        return self._run(work)

    def discover_bills(self, req: Request) -> Any:
        return self._run(lambda c: {"candidates": env_mod.discover_bills(c)})

    def dismiss_bill_candidate(self, req: Request) -> Any:
        key = str(req.json().get("key") or "")
        return self._run(lambda c: (env_mod.dismiss_candidate(c, key), {"success": True})[1])

    def audit_config(self, req: Request) -> Any:
        def work(c):
            cfg = audit.get_config(c)
            cfg["pending"] = audit.pending_from_last_import(c)["total"]
            return cfg
        return self._run(work)

    def audit_save_settings(self, req: Request) -> Any:
        data = req.json()

        def work(c):
            try:
                audit.save_config(c, data)
            except (TypeError, ValueError) as exc:
                raise env_mod.ValidationError(str(exc))
            return {"success": True}
        return self._run(work)

    def audit_run(self, req: Request) -> Any:
        d = req.json()

        def work(c):
            try:
                return audit.run(c, d.get("checks") or [], lookback_days=int(d["lookback_days"]) if d.get("lookback_days") else None,
                                 large_amount=float(d["large_amount"]) if d.get("large_amount") else None,
                                 include_reviewed=bool(d.get("include_reviewed")))
            except (TypeError, ValueError) as exc:
                raise env_mod.ValidationError(str(exc))
        return self._run(work)

    def audit_dismiss(self, req: Request) -> Any:
        items = req.json().get("items") or []
        return self._run(lambda c: {"success": True, "dismissed": audit.dismiss(c, items)})

    def audit_pending(self, req: Request) -> Any:
        return self._run(audit.pending_from_last_import)

    def upcoming_bills(self, req: Request) -> Any:
        days = int(req.query.get("days") or 30)
        return self._ledger_run(lambda c: {"bills": ledger.upcoming_bills(c, self._today(req), days)})

    def link_bill(self, req: Request) -> Any:
        data = req.json()

        def work(c):
            try:
                billmatch.link(c, int(data.get("bill_id") or 0), str(data.get("txn_id") or ""))
            except ValueError as exc:
                raise env_mod.ValidationError(str(exc))
            return {"success": True}
        return self._ledger_run(work)

    def unlink_bill(self, req: Request) -> Any:
        txn_id = str(req.json().get("txn_id") or "")

        def work(c):
            try:
                billmatch.unlink(c, txn_id)
            except ValueError as exc:
                raise env_mod.ValidationError(str(exc))
            return {"success": True}
        return self._ledger_run(work)

    def allocation_queue(self, req: Request) -> Any:
        return self._ledger_run(lambda c: {"queue": ledger.fill_queue(c), "go_live_date": store.get_setting(c, "go_live_date") or None})

    def allocation_draft(self, req: Request) -> Any:
        txn_id = req.query.get("txn_id", "")
        return self._ledger_run(lambda c: ledger.draft_allocation(c, txn_id, self._today(req)))

    def allocation_suggest(self, req: Request) -> Any:
        amount = req.json().get("amount", 0)
        return self._ledger_run(lambda c: {"lines": ledger.suggest_allocation(c, amount, self._today(req))})

    def allocation_confirm(self, req: Request) -> Any:
        data = req.json()
        today = self._today(req)

        def work(c):
            tmpl = None
            if data.get("income_txn_id"):
                row = c.execute(
                    "SELECT t.id FROM fill_templates t JOIN transactions x ON x.income_source_id = t.income_source_id WHERE x.txn_id = ?",
                    (data["income_txn_id"],)).fetchone()
                tmpl = row["id"] if row else None
            alloc_id = ledger.confirm_allocation(
                c, data.get("income_txn_id") or None, data.get("lines") or [], today, data.get("date"),
                int(data["replace_allocation_id"]) if data.get("replace_allocation_id") else None, tmpl)
            return {"success": True, "allocation_id": alloc_id}
        out = self._ledger_run(work)
        return out

    def allocation_void(self, req: Request) -> Any:
        aid = req.json().get("allocation_id")

        def work(c):
            ledger.void_allocation(c, int(aid or 0))
            return {"success": True}
        return self._ledger_run(work)

    def move_transfer(self, req: Request) -> Any:
        d = req.json()

        def work(c):
            mid = ledger.transfer(c, int(d.get("from_envelope_id") or 0), int(d.get("to_envelope_id") or 0),
                                  d.get("amount"), d.get("date"), d.get("note") or "", self._today(req))
            return {"success": True, "id": mid}
        return self._ledger_run(work)

    def move_void(self, req: Request) -> Any:
        mid = req.json().get("id")
        return self._ledger_run(lambda c: (ledger.void_move(c, int(mid or 0)), {"success": True})[1])

    # ----------------------------------------------------------------- router

    def build_router(self) -> Router:
        r = Router()
        r.add("GET", "/", self.page)
        r.add("GET", "/index.html", self.page)
        r.add("GET", "/dashboard.html", self.page)
        r.add_prefix("GET", "/web/", self.static)
        r.add("GET", "/favicon.ico", lambda req: Response(b"", "image/x-icon", 204))
        r.add("GET", "/api/health", self.health)
        r.add("POST", "/api/heartbeat", self.heartbeat)
        r.add("POST", "/api/page-closed", self.page_closed)
        r.add("POST", "/api/shutdown", self.shutdown)
        r.add("GET", "/api/profiles", self.profiles_list)
        r.add("POST", "/api/profiles/open", self.profile_open)
        r.add("POST", "/api/profiles/create", self.profile_create)
        r.add("POST", "/api/profiles/migrate", self.profile_migrate)
        r.add("POST", "/api/profiles/close", self.profile_close)
        r.add("POST", "/api/profiles/pin", self.profile_pin)
        r.add("GET", "/api/transactions", self.get_transactions)
        r.add("POST", "/api/transactions/update", self.update_transaction)
        r.add("POST", "/api/transactions/delete", self.delete_transaction)
        r.add("POST", "/api/transactions/bulk-update", self.bulk_update)
        r.add("POST", "/api/transactions/reclassify", self.reclassify_transactions)
        r.add("GET", "/api/transactions/reimbursement-candidates", self.reimbursement_candidates)
        r.add("GET", "/api/envelopes", self.get_envelopes)
        r.add("POST", "/api/envelopes/add", self.add_envelope)
        r.add("POST", "/api/envelopes/rename", self.rename_envelope)
        r.add("POST", "/api/envelopes/delete", self.delete_envelope)
        r.add("GET", "/api/budget/envelopes", self.budget_envelopes)
        r.add("POST", "/api/budget/accept", self.accept_budget)
        r.add("POST", "/api/envelopes/save", self.save_envelope_settings)
        r.add("GET", "/api/bills", self.get_bills)
        r.add("POST", "/api/bills/save", self.save_bill)
        r.add("POST", "/api/bills/delete", self.delete_bill)
        r.add("GET", "/api/bills/preview", self.bill_preview)
        r.add("GET", "/api/bills/history", self.bill_history)
        r.add("GET", "/api/templates", self.get_templates)
        r.add("POST", "/api/templates/save", self.save_template)
        r.add("POST", "/api/templates/generate", self.generate_template)
        r.add("GET", "/api/settings", self.get_settings)
        r.add("POST", "/api/settings", self.save_settings)
        r.add("GET", "/api/setup/status", self.setup_status)
        r.add("POST", "/api/setup/go-live", self.setup_go_live)
        r.add("GET", "/api/envelopes/balances", self.envelope_balances)
        r.add("GET", "/api/envelopes/ledger", self.envelope_ledger)
        r.add("GET", "/api/envelopes/ledger.csv", self.ledger_csv)
        r.add("GET", "/api/bills/discover", self.discover_bills)
        r.add("GET", "/api/cards", self.get_cards)
        r.add("POST", "/api/cards/name", self.name_cards)
        r.add("GET", "/api/cards/bills", self.card_bills)
        r.add("POST", "/api/cards/bills/mark", self.mark_card_bill)
        r.add("POST", "/api/bills/dismiss", self.dismiss_bill_candidate)
        r.add("GET", "/api/bills/upcoming", self.upcoming_bills)
        r.add("POST", "/api/bills/link", self.link_bill)
        r.add("POST", "/api/bills/unlink", self.unlink_bill)
        r.add("GET", "/api/allocations/queue", self.allocation_queue)
        r.add("GET", "/api/allocations/draft", self.allocation_draft)
        r.add("POST", "/api/allocations/suggest", self.allocation_suggest)
        r.add("POST", "/api/allocations/confirm", self.allocation_confirm)
        r.add("POST", "/api/allocations/void", self.allocation_void)
        r.add("POST", "/api/moves/transfer", self.move_transfer)
        r.add("POST", "/api/moves/void", self.move_void)
        r.add("GET", "/api/audit/config", self.audit_config)
        r.add("POST", "/api/audit/settings", self.audit_save_settings)
        r.add("POST", "/api/audit/run", self.audit_run)
        r.add("POST", "/api/audit/dismiss", self.audit_dismiss)
        r.add("GET", "/api/audit/pending", self.audit_pending)
        r.add("GET", "/api/people", self.get_people)
        r.add("POST", "/api/people/save", self.save_person)
        r.add("GET", "/api/income/sources", self.get_income_sources)
        r.add("GET", "/api/income/sources/preview", self.income_source_preview)
        r.add("POST", "/api/income/sources/save", self.save_income_source)
        r.add("POST", "/api/income/sources/delete", self.delete_income_source)
        r.add("GET", "/api/income/summary", self.income_summary)
        r.add("GET", "/api/rules", self.get_rules)
        r.add("POST", "/api/rules/save", self.save_rule)
        r.add("POST", "/api/rules/delete", self.delete_rule)
        r.add("POST", "/api/rules/preview", self.rule_preview)
        r.add("POST", "/api/rules/apply-rename", self.rule_apply_rename)
        r.add("GET", "/api/import/scan", self.import_scan)
        r.add("POST", "/api/import/upload", self.import_upload)
        r.add("POST", "/api/import/inspect", self.import_inspect)
        r.add("POST", "/api/import/preview", self.import_preview)
        r.add("POST", "/api/import/commit", self.import_commit)
        r.add("GET", "/api/import/history", self.import_history)
        r.add("POST", "/api/import/vendor-lookup", self.vendor_lookup)
        return r


# ---------------------------------------------------------------------------
# Learned rules
# ---------------------------------------------------------------------------


def save_user_rule(conn: Any, fields: Dict[str, Any]) -> int:
    """Add a user rule, or update the user rule with the same phrase and stage.

    fields["scope"] is "shared" (everyone's rules) or "personal" (this budget only; the default).
    New rules go ahead of older ones so a more specific, newer rule wins.
    """
    shared = fields.get("scope") == "shared"
    table = "shared.rules" if shared else "rules"
    existing = conn.execute(
        f"SELECT id FROM {table} WHERE origin = 'user' AND stage = ? AND UPPER(phrase) = UPPER(?)",
        (fields["stage"], fields["phrase"]),
    ).fetchone()
    if existing:
        conn.execute(
            f"""UPDATE {table} SET match_mode=?, set_kind=?, set_envelope=?, set_payee=?, set_income_label=?,
                      set_needs_review=?, exclusion_reason=?, description=COALESCE(?, description), active=1 WHERE id=?""",
            (
                fields.get("match_mode", "contains"), fields.get("set_kind"), fields.get("set_envelope"),
                fields.get("set_payee"), fields.get("set_income_label"), int(fields.get("set_needs_review") or 0),
                fields.get("exclusion_reason"), fields.get("description"), existing["id"],
            ),
        )
        return int(existing["id"])
    low = conn.execute(f"SELECT MIN(priority) FROM {table} WHERE origin = 'user'").fetchone()[0]
    rule = {k: v for k, v in fields.items() if k != "scope"}
    rule["origin"] = "user"
    rule["priority"] = (low if low is not None else 100) - 1
    rule["label"] = rule.get("label") or "User Learned Rule"
    return store.insert_rule(conn, rule, shared=shared, table=table)


def save_user_rules_from_bulk(
    conn: Any, phrase: str, updates: Dict[str, Any], kind: Optional[str], included: Optional[int], scope: str = "personal"
) -> None:
    """Turn a bulk edit with 'always do this' into learned rules."""
    envelope = (updates.get("envelope") or "").strip() or None
    payee = (updates.get("name") or "").strip() or None
    # For refunds and other income the envelope belongs to that rule alone, so spending
    # with the same phrase isn't pulled into it.
    expense_envelope = None if kind in ("refund", "other_income") else envelope
    if expense_envelope or payee:
        save_user_rule(conn, {"scope": scope, "phrase": phrase, "stage": "envelope" if expense_envelope else "payee",
                              "set_envelope": expense_envelope, "set_payee": payee})
    if kind in rules.EXCLUDED_KINDS:
        save_user_rule(conn, {"scope": scope, "phrase": phrase, "stage": "exclude", "set_kind": kind,
                              "exclusion_reason": (updates.get("exclusion_reason") or "").strip() or "User Learned Rule"})
    elif kind in ("refund", "other_income"):
        save_user_rule(conn, {"scope": scope, "phrase": phrase, "stage": "refund" if kind == "refund" else "income", "set_kind": kind,
                              "set_envelope": envelope if kind == "refund" else None,
                              "set_income_label": (updates.get("income_label") or "").strip() or None})
    elif included is not None and kind is None:
        stage = "include_override" if int(included) == 1 else "exclude"
        save_user_rule(conn, {"scope": scope, "phrase": phrase, "stage": stage, "set_envelope": envelope,
                              "set_kind": "expense" if stage == "include_override" else None,
                              "exclusion_reason": (updates.get("exclusion_reason") or "").strip() or "User Learned Rule"})
