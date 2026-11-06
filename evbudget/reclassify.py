"""Re-run the classification rules over stored transactions."""

from datetime import datetime
from typing import Any, Dict, List, Optional

from . import income, rules

# Fields whose change counts as a real difference. Payee names and the "matched by"
# label are cosmetic: they are updated along with a real change but never trigger one.
COMPARE_FIELDS = [
    "txn_kind", "is_included", "exclusion_reason", "envelope", "income_source_id",
    "person_id", "income_subtype", "income_label", "needs_review",
]


def _stored_view(row: Any) -> Dict[str, Any]:
    return {
        "txn_kind": row["txn_kind"], "is_included": int(row["is_included"] or 0),
        "exclusion_reason": row["exclusion_reason"] or "", "envelope": (row["envelope"] or "").strip(),
        "name": row["name"], "income_source_id": row["income_source_id"], "person_id": row["person_id"],
        "income_subtype": row["income_subtype"], "income_label": row["income_label"],
        "needs_review": int(row["needs_review"] or 0), "match_source": row["match_source"] or "",
    }


def _new_view(res: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "txn_kind": res["txn_kind"], "is_included": 1 if res["is_included"] else 0,
        "exclusion_reason": res["exclusion_reason"] or "", "envelope": (res["envelope"] or "").strip(),
        "name": res["name"], "income_source_id": res["income_source_id"], "person_id": res["person_id"],
        "income_subtype": res["income_subtype"], "income_label": res["income_label"],
        "needs_review": int(res["needs_review"] or 0), "match_source": res["match_source"] or "",
    }


def _plan(conn: Any, selected: Optional[set], include_edited: bool, edited_ids: Optional[set] = None) -> Any:
    """What the rules would change: (list of changes, number of rows checked). Writes nothing.

    Hand-edited rows are left out unless include_edited is set or their id is in edited_ids."""
    ctx = rules.load_context(conn)
    ctx.history = income.PaycheckHistory()  # rebuilt in date order below

    rows = conn.execute("SELECT * FROM transactions ORDER BY date ASC, txn_id ASC").fetchall()
    changes: List[Dict[str, Any]] = []
    checked = 0
    for row in rows:
        edit_ok = include_edited or not row["user_modified"] or (edited_ids is not None and row["txn_id"] in edited_ids)
        in_scope = (selected is None or row["txn_id"] in selected) and edit_ok
        if not in_scope:
            if row["txn_kind"] == "paycheck" and row["income_subtype"] == "regular" and row["is_included"] and row["income_source_id"]:
                ctx.history.add(int(row["income_source_id"]), row["date"], float(row["amount"]))
            continue
        checked += 1
        res = rules.classify(
            ctx, raw_name=row["raw_name"] or row["name"], payee=row["name"], amount=float(row["amount"]),
            date_iso=row["date"], check_num=row["check_num"] or "",
        )
        before, after = _stored_view(row), _new_view(res)
        after["name"] = row["name"]  # keep the stored payee; renames are not part of a re-run
        changed = [f for f in COMPARE_FIELDS if before[f] != after[f]]
        if changed:
            changes.append({
                "txn_id": row["txn_id"], "date": row["date"], "raw_name": row["raw_name"], "amount": float(row["amount"]),
                "before": before, "after": after, "changed_fields": changed,
                "rule": res.get("match_detail") or after["match_source"],
                "edited_by_hand": bool(row["user_modified"]),
            })
    return changes, checked


def reclassify(
    conn: Any,
    txn_ids: Optional[List[str]] = None,
    include_edited: bool = False,
    apply: bool = False,
    preview_limit: int = 200,
    edited_ids: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Re-run the rules over transactions.

    By default rows edited by hand (user_modified = 1) are left alone. With
    apply=False nothing is written; the result lists what would change.

    edited_ids: apply the rules to just these hand-edited rows (on top of the rows that were never edited), so
    the caller can choose which hand edits to overwrite. include_edited means every hand-edited row.

    The result also says how many hand-edited rows are involved: `edited_changed` for those in this run, and
    `edited_pending` (how many more would change if they were included) for those left out, so they can be
    previewed before deciding.
    """
    selected = set(txn_ids) if txn_ids else None
    picked = set(edited_ids) if edited_ids else None
    changes, checked = _plan(conn, selected, include_edited, picked)
    edited_changed = sum(1 for c in changes if c["edited_by_hand"])
    if include_edited:
        edited_pending = 0
    else:
        # Hand-edited rows left out of this run: count the ones that would change, as if they were included.
        done = {c["txn_id"] for c in changes}
        edited_pending = sum(1 for c in _plan(conn, selected, True)[0] if c["edited_by_hand"] and c["txn_id"] not in done)
    changes.sort(key=lambda c: not c["edited_by_hand"])   # hand-edited rows first, so a long list can't hide them

    if apply and changes:
        now = datetime.now().isoformat()
        try:
            for c in changes:
                a = c["after"]
                conn.execute(
                    """UPDATE transactions SET txn_kind=?, is_included=?, exclusion_reason=?, envelope=?, name=?,
                              income_source_id=?, person_id=?, income_subtype=?, income_label=?, needs_review=?,
                              match_source=?, updated_at=? WHERE txn_id=?""",
                    (
                        a["txn_kind"], a["is_included"], a["exclusion_reason"], a["envelope"], a["name"],
                        a["income_source_id"], a["person_id"], a["income_subtype"], a["income_label"],
                        a["needs_review"], a["match_source"], now, c["txn_id"],
                    ),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    return {
        "success": True, "applied": bool(apply), "checked": checked, "changed_count": len(changes),
        # Every hand-edited row is listed (they are chosen one by one); only the ordinary rows are cut to the limit
        "changes": [c for c in changes if c["edited_by_hand"]] + [c for c in changes if not c["edited_by_hand"]][:preview_limit],
        "truncated": len([c for c in changes if not c["edited_by_hand"]]) > preview_limit,
        "edited_changed": edited_changed, "edited_pending": edited_pending,
    }
