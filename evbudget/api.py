"""HTTP API: page, static files, transactions, envelopes, rules, and import."""

import mimetypes
import os
from datetime import datetime
from email.parser import BytesParser
from email.policy import HTTP as HTTP_POLICY
from typing import Any, Dict, List, Optional, Tuple

from . import VERSION, import_engine, paths, reclassify, rules, store
from .http_util import HttpError, Request, Response, Router
from .textutil import is_iso_date, to_iso_date

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
    "reimburses_txn_id",
]


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


def _find_envelope(conn: Any, name: str) -> Optional[Any]:
    return conn.execute("SELECT * FROM envelopes WHERE LOWER(name) = LOWER(?)", (name.strip(),)).fetchone()


class App:
    """Holds file locations and builds the route table."""

    def __init__(
        self,
        db_path: Optional[str] = None,
        input_dir: Optional[str] = None,
        web_dir: Optional[str] = None,
        master_csv_path: Optional[str] = None,
    ):
        self.db_path = db_path or paths.DB_PATH
        self.input_dir = input_dir or paths.INPUT_DIR
        self.web_dir = os.path.realpath(web_dir or paths.WEB_DIR)
        self.master_csv_path = master_csv_path or paths.MASTER_CSV_PATH

    def conn(self) -> Any:
        return store.connect(self.db_path)

    def sync_csv(self) -> None:
        store.sync_master_csv(self.db_path, self.master_csv_path)

    # ------------------------------------------------------------------ pages

    def page(self, req: Request) -> Response:
        path = os.path.join(self.web_dir, "dashboard.html")
        if not os.path.exists(path):
            return Response(f"<h1>dashboard.html not found at {path}</h1>".encode(), "text/html; charset=utf-8", 500)
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

    def health(self, req: Request) -> Dict[str, Any]:
        return {"status": "ok", "version": f"{VERSION:.1f}"}

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
                          "exclusion_reason", "match_source", "income_label", "income_subtype"):
                    d[k] = d[k] or ""
                d["is_included"] = int(d["is_included"]) if d["is_included"] is not None else 1
                d["needs_review"] = int(d["needs_review"] or 0)
                d["user_modified"] = int(d["user_modified"] or 0)
                txns.append(d)
            return {
                "version": f"{VERSION:.1f}",
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

    def delete_transaction(self, req: Request) -> Any:
        txn_id = req.json().get("txn_id", "")
        if not txn_id:
            raise HttpError(400, "Missing transaction ID")
        conn = self.conn()
        try:
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
                save_user_rules_from_bulk(conn, phrase, updates, kind, included)
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
            return {"envelopes": _envelope_names(conn), "counts": counts}
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
            order = conn.execute("SELECT COALESCE(MAX(sort_order), 0) + 1 FROM envelopes").fetchone()[0]
            conn.execute(
                "INSERT INTO envelopes (name, kind, rollover_mode, sort_order) VALUES (?, ?, ?, ?)",
                (name, guess["kind"], guess["rollover_mode"], order),
            )
            conn.commit()
            return {"success": True, "envelope": name, "envelopes": _envelope_names(conn)}
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
                      "envelopes": _envelope_names(conn)}
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
                      "reassigned_to": reassign_to, "envelopes": _envelope_names(conn)}
        finally:
            conn.close()
        self.sync_csv()
        return result

    # ------------------------------------------------------------------ rules

    def get_rules(self, req: Request) -> Any:
        conn = self.conn()
        try:
            rows = [dict(r) for r in conn.execute("SELECT * FROM rules ORDER BY stage, priority, id")]
            return {"rules": rows, "stages": list(rules.STAGES), "kinds": list(rules.KINDS)}
        finally:
            conn.close()

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
            import re
            try:
                re.compile(phrase)
            except re.error as exc:
                raise HttpError(400, f"Invalid regular expression: {exc}")
        fields = {
            "phrase": phrase, "match_mode": mode, "stage": stage, "set_kind": kind,
            "set_envelope": (data.get("set_envelope") or "").strip() or None,
            "set_payee": (data.get("set_payee") or "").strip() or None,
            "set_income_label": (data.get("set_income_label") or "").strip() or None,
            "set_needs_review": 1 if data.get("set_needs_review") else 0,
            "exclusion_reason": (data.get("exclusion_reason") or "").strip() or None,
        }
        conn = self.conn()
        try:
            rule_id = data.get("id")
            if rule_id:
                cur = conn.execute(
                    """UPDATE rules SET phrase=:phrase, match_mode=:match_mode, stage=:stage, set_kind=:set_kind,
                              set_envelope=:set_envelope, set_payee=:set_payee, set_income_label=:set_income_label,
                              set_needs_review=:set_needs_review, exclusion_reason=:exclusion_reason,
                              active=:active WHERE id=:id""",
                    dict(fields, id=int(rule_id), active=0 if data.get("active") in (0, False) else 1),
                )
                if cur.rowcount == 0:
                    raise HttpError(404, "Rule not found")
            else:
                rule_id = save_user_rule(conn, fields)
            conn.commit()
            return {"success": True, "id": rule_id}
        finally:
            conn.close()

    def delete_rule(self, req: Request) -> Any:
        rule_id = req.json().get("id")
        if not rule_id:
            raise HttpError(400, "Missing rule id")
        conn = self.conn()
        try:
            conn.execute("DELETE FROM rules WHERE id = ?", (int(rule_id),))
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
        finally:
            conn.close()
        out = []
        for name in names:
            try:
                path = import_engine.resolve_input_file(name, self.input_dir)
            except FileNotFoundError:
                out.append({"valid": False, "filename": name, "error": "File not found"})
                continue
            info = import_engine.inspect_file_header(path, presets)
            info.pop("filepath", None)
            info["imported_rows"] = counts.get(info.get("filename", name), 0)
            out.append(info)
        return {"files": out}

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
                "is_mapped": bool(mapping.get("date_col") and mapping.get("name_col")
                                  and (mapping.get("amount_col") or mapping.get("is_dual"))),
                "headers": fc.get("headers") or [],
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

    # ----------------------------------------------------------------- router

    def build_router(self) -> Router:
        r = Router()
        r.add("GET", "/", self.page)
        r.add("GET", "/index.html", self.page)
        r.add("GET", "/dashboard.html", self.page)
        r.add_prefix("GET", "/web/", self.static)
        r.add("GET", "/favicon.ico", lambda req: Response(b"", "image/x-icon", 204))
        r.add("GET", "/api/health", self.health)
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
        r.add("GET", "/api/rules", self.get_rules)
        r.add("POST", "/api/rules/save", self.save_rule)
        r.add("POST", "/api/rules/delete", self.delete_rule)
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

    New rules go ahead of older ones so a more specific, newer rule wins.
    """
    existing = conn.execute(
        "SELECT id FROM rules WHERE origin = 'user' AND stage = ? AND UPPER(phrase) = UPPER(?)",
        (fields["stage"], fields["phrase"]),
    ).fetchone()
    if existing:
        conn.execute(
            """UPDATE rules SET match_mode=?, set_kind=?, set_envelope=?, set_payee=?, set_income_label=?,
                      set_needs_review=?, exclusion_reason=?, active=1 WHERE id=?""",
            (
                fields.get("match_mode", "contains"), fields.get("set_kind"), fields.get("set_envelope"),
                fields.get("set_payee"), fields.get("set_income_label"), int(fields.get("set_needs_review") or 0),
                fields.get("exclusion_reason"), existing["id"],
            ),
        )
        return int(existing["id"])
    low = conn.execute("SELECT MIN(priority) FROM rules WHERE origin = 'user'").fetchone()[0]
    rule = dict(fields)
    rule["origin"] = "user"
    rule["priority"] = (low if low is not None else 100) - 1
    rule.setdefault("label", "User Learned Rule")
    rule["label"] = rule.get("label") or "User Learned Rule"
    return store.insert_rule(conn, rule)


def save_user_rules_from_bulk(conn: Any, phrase: str, updates: Dict[str, Any], kind: Optional[str], included: Optional[int]) -> None:
    """Turn a bulk edit with 'always do this' into learned rules."""
    envelope = (updates.get("envelope") or "").strip() or None
    payee = (updates.get("name") or "").strip() or None
    # For refunds and other income the envelope belongs to that rule alone, so spending
    # with the same phrase isn't pulled into it.
    expense_envelope = None if kind in ("refund", "other_income") else envelope
    if expense_envelope or payee:
        save_user_rule(conn, {"phrase": phrase, "stage": "envelope" if expense_envelope else "payee",
                              "set_envelope": expense_envelope, "set_payee": payee})
    if kind in rules.EXCLUDED_KINDS:
        save_user_rule(conn, {"phrase": phrase, "stage": "exclude", "set_kind": kind,
                              "exclusion_reason": (updates.get("exclusion_reason") or "").strip() or "User Learned Rule"})
    elif kind in ("refund", "other_income"):
        save_user_rule(conn, {"phrase": phrase, "stage": "refund" if kind == "refund" else "income", "set_kind": kind,
                              "set_envelope": envelope if kind == "refund" else None,
                              "set_income_label": (updates.get("income_label") or "").strip() or None})
    elif included is not None and kind is None:
        stage = "include_override" if int(included) == 1 else "exclude"
        save_user_rule(conn, {"phrase": phrase, "stage": stage, "set_envelope": envelope,
                              "set_kind": "expense" if stage == "include_override" else None,
                              "exclusion_reason": (updates.get("exclusion_reason") or "").strip() or "User Learned Rule"})
