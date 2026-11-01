"""The setup walkthrough: where the user is in it, and how far setup has got, so each step can show a checkmark."""

import json
import re
from datetime import datetime
from typing import Any, Dict

from . import store

STATE_KEY = "walkthrough"
STATUSES = ("not_started", "active", "paused", "dismissed", "done")
STEP_ID = re.compile(r"^[a-z_]{1,40}$")
MAX_SKIPPED = 20


class StateError(ValueError):
    pass


def get_state(conn: Any) -> Dict[str, Any]:
    try:
        data = json.loads(store.get_setting(conn, STATE_KEY) or "{}")
    except ValueError:
        data = {}
    if not isinstance(data, dict) or data.get("status") not in STATUSES:
        data = {}
    return {
        "status": data.get("status", "not_started"),
        "step": data.get("step") or None,
        "skipped": [s for s in data.get("skipped", []) if isinstance(s, str)],
        "updated_at": data.get("updated_at"),
    }


def save_state(conn: Any, data: Dict[str, Any]) -> Dict[str, Any]:
    status = data.get("status")
    if status not in STATUSES:
        raise StateError("Unknown walkthrough status")
    step = data.get("step") or None
    if step is not None and not (isinstance(step, str) and STEP_ID.match(step)):
        raise StateError("Unknown walkthrough step")
    skipped = data.get("skipped") or []
    if not isinstance(skipped, list) or len(skipped) > MAX_SKIPPED or not all(isinstance(s, str) and STEP_ID.match(s) for s in skipped):
        raise StateError("Skipped steps must be a short list of step names")
    state = {"status": status, "step": step, "skipped": list(dict.fromkeys(skipped)),
             "updated_at": datetime.now().isoformat(timespec="seconds")}
    store.set_setting(conn, STATE_KEY, json.dumps(state))
    return state


def get_status(conn: Any) -> Dict[str, Any]:
    """Counts for each setup step. Only active envelopes and sources count, and never the Unallocated row."""
    one = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
    first, last = conn.execute("SELECT MIN(date), MAX(date) FROM transactions").fetchone()
    kinds = {k: 0 for k in ("spending", "bill", "goal")}
    for r in conn.execute("SELECT kind, COUNT(*) FROM envelopes WHERE active = 1 AND kind != 'system' GROUP BY kind"):
        kinds[r[0]] = r[1]
    return {
        "transactions": one("SELECT COUNT(*) FROM transactions"),
        "first_date": first, "last_date": last,
        "import_batches": one("SELECT COUNT(*) FROM import_batches"),
        "people": one("SELECT COUNT(*) FROM people WHERE active = 1"),
        "income_sources": one("SELECT COUNT(*) FROM income_sources WHERE active = 1"),
        "envelopes": kinds,
        "spending_without_budget": one(
            "SELECT COUNT(*) FROM envelopes WHERE active = 1 AND kind = 'spending' AND monthly_budget_cents <= 0"),
        "bill_envelopes_without_bills": one(
            """SELECT COUNT(*) FROM envelopes e WHERE e.active = 1 AND e.kind = 'bill'
               AND NOT EXISTS (SELECT 1 FROM bills b WHERE b.envelope_id = e.id AND b.active = 1)"""),
        "sources_without_template": one(
            """SELECT COUNT(*) FROM income_sources s WHERE s.active = 1 AND NOT EXISTS (
                 SELECT 1 FROM fill_templates t JOIN fill_template_lines l ON l.template_id = t.id
                 WHERE t.income_source_id = s.id AND l.amount_cents > 0)"""),
        "go_live_date": store.get_setting(conn, "go_live_date") or None,
    }


def should_auto_start(state: Dict[str, Any], status: Dict[str, Any]) -> bool:
    """Only a brand-new, empty budget that has never seen the walkthrough."""
    return state["status"] == "not_started" and status["transactions"] == 0 and not status["go_live_date"]
