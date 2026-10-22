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


def reclassify(
    conn: Any,
    txn_ids: Optional[List[str]] = None,
    include_edited: bool = False,
    apply: bool = False,
    preview_limit: int = 200,
) -> Dict[str, Any]:
    """Re-run the rules over transactions.

    By default rows edited by hand (user_modified = 1) are left alone. With
    apply=False nothing is written; the result lists what would change.
    """
    selected = set(txn_ids) if txn_ids else None
    ctx = rules.load_context(conn)
    ctx.history = income.PaycheckHistory()  # rebuilt in date order below

    rows = conn.execute("SELECT * FROM transactions ORDER BY date ASC, txn_id ASC").fetchall()
    changes: List[Dict[str, Any]] = []
    checked = 0
    for row in rows:
        in_scope = (selected is None or row["txn_id"] in selected) and (include_edited or not row["user_modified"])
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
            })

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
        "changes": changes[:preview_limit], "truncated": len(changes) > preview_limit,
    }
