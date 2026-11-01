"""Rules that do more than one thing.

A rule has a filter (what to find) and any mix of actions: rename the payee, send it to an envelope, treat it
as a kind of transaction (a transfer, a card payment, a refund...), and flag it for review. All rules are equal
and live in one ordered list.

How a transaction is worked out:
  1. Rename pass. Rules run in order. A pattern rename edits the name and later renames carry on from the new
     name; a plain rename is final. This is how renames have always worked.
  2. Every other action. Each rule is checked against the bank's text and the new name. For each action
     (envelope, kind, review) the first rule in the list that sets it wins, so a general clean-up rule and a
     specific rule both apply.
  3. What no rule decided is worked out the way it always was: paychecks from income sources, refunds matched to
     a recent purchase, and unknown deposits parked as other income for review.
"""

from typing import Any, Dict, Iterable, List, Optional, Sequence

import html

from . import income
from .rules import (
    EXCLUDED_KINDS, KINDS, MATCH_MODES, ClassifyContext, check_rename_rule, rename_for, rule_matches,
)
from .textutil import collapse_ws

FILTER_TYPES = ("text", "check", "payment", "all")
DIRECTIONS = ("out", "in", "any")

# A rule with none of these set does nothing, so the editor asks for at least one.
ACTION_FIELDS = ("set_payee", "set_envelope", "set_kind", "set_needs_review")


def describe_actions(rule: Dict[str, Any]) -> List[str]:
    """The rule's actions in words, for the rule list and the import preview's chips."""
    bits = []
    if rule.get("set_payee"):
        bits.append("rename")
    if rule.get("set_envelope"):
        bits.append(f"→ {rule['set_envelope']}")
    if rule.get("set_kind"):
        bits.append(f"treat as {rule['set_kind']}")
    if rule.get("set_needs_review"):
        bits.append("flag for review")
    return bits


def validate_rule(rule: Dict[str, Any]) -> None:
    """Raise ValueError with a plain-English reason if the rule can't work."""
    if not (rule.get("name") or "").strip():
        raise ValueError("Give the rule a name")
    ftype = rule.get("filter_type") or "text"
    if ftype not in FILTER_TYPES:
        raise ValueError(f"Unknown filter '{ftype}'")
    if (rule.get("direction") or "any") not in DIRECTIONS:
        raise ValueError("Money direction must be out, in or either")
    mode = rule.get("match_mode") or "contains"
    if mode not in MATCH_MODES:
        raise ValueError(f"Unknown match mode '{mode}'")
    if ftype == "text" and not (rule.get("phrase") or "").strip():
        raise ValueError("Enter the text to look for")
    if rule.get("set_kind") and rule["set_kind"] not in KINDS:
        raise ValueError(f"Unknown kind '{rule['set_kind']}'")
    if not any(rule.get(f) for f in ACTION_FIELDS):
        raise ValueError("Choose at least one thing for the rule to do")
    if rule.get("set_payee") and ftype != "text":
        raise ValueError("Only a rule that looks for text can rename")
    if mode == "regex" and ftype == "text":
        check_rename_rule(rule["phrase"], (rule.get("set_payee") or "").strip())


class RuleBook:
    """Active rules in the order they run."""

    def __init__(self, rules: Iterable[Dict[str, Any]], known_envelopes: Optional[Iterable[str]] = None):
        self.rules = sorted((dict(r) for r in rules if r.get("active", 1)), key=lambda r: (r.get("priority", 0), r.get("id") or 0))
        self.known_envelopes = None if known_envelopes is None else {e.strip().lower() for e in known_envelopes}

    @staticmethod
    def _applies(rule: Dict[str, Any], texts: Sequence[str], amount: float, check_num: str, trans_type: str) -> bool:
        direction = rule.get("direction") or "any"
        if (direction == "out" and amount >= 0) or (direction == "in" and amount < 0):
            return False
        ftype = rule.get("filter_type") or "text"
        if ftype == "all":
            return True
        if ftype == "check":
            return bool((check_num or "").strip())
        if ftype == "payment":
            return (trans_type or "").strip().upper() == "PAYMENT"
        return rule_matches(rule, texts)

    def _rename(self, hay_raw: str, name: str, amount: float, applied: Dict[int, Dict[str, Any]]) -> str:
        for rule in self.rules:
            if (rule.get("filter_type") or "text") != "text" or not rule.get("set_payee"):
                continue
            direction = rule.get("direction") or "any"
            if (direction == "out" and amount >= 0) or (direction == "in" and amount < 0):
                continue
            new = rename_for(rule, hay_raw, name)
            if new is None:
                continue
            if new != name:
                applied.setdefault(rule["id"], {"id": rule["id"], "name": rule["name"], "did": []})["did"].append("rename")
            name = new
            if (rule.get("match_mode") or "contains") != "regex":
                break
        return name

    def decide(self, hay_raw: str, name: str, amount: float, check_num: str, trans_type: str) -> Dict[str, Any]:
        """Everything the rules decide about one transaction."""
        applied: Dict[int, Dict[str, Any]] = {}
        new_name = self._rename(hay_raw, name, amount, applied)
        texts = (hay_raw, new_name.upper())
        out: Dict[str, Any] = {
            "name": new_name, "envelope": None, "kind": None, "exclusion_reason": None, "income_label": None,
            "review": False, "skipped": [], "applied": applied,
        }

        def did(rule: Dict[str, Any], what: str) -> None:
            applied.setdefault(rule["id"], {"id": rule["id"], "name": rule["name"], "did": []})["did"].append(what)

        for rule in self.rules:
            if not self._applies(rule, texts, amount, check_num, trans_type):
                continue
            env = (rule.get("set_envelope") or "").strip()
            if env and out["envelope"] is None:
                if self.known_envelopes is not None and env.lower() not in self.known_envelopes:
                    out["skipped"].append(rule)
                else:
                    out["envelope"] = env
                    did(rule, f"→ {env}")
            if rule.get("set_kind") and out["kind"] is None:
                out["kind"] = rule["set_kind"]
                out["exclusion_reason"] = rule.get("exclusion_reason")
                did(rule, f"treat as {rule['set_kind']}")
            if rule.get("set_income_label") and out["income_label"] is None:
                out["income_label"] = rule["set_income_label"]
            if rule.get("set_needs_review") and not out["review"]:
                out["review"] = True
                did(rule, "flag for review")
        return out


def classify(
    ctx: ClassifyContext, book: RuleBook, *, raw_name: str, payee: str, amount: float, date_iso: str,
    check_num: str = "", trans_type: str = "",
) -> Dict[str, Any]:
    """Classify one transaction. Same fields as rules.classify, plus `rules`: what each rule did."""
    hay_raw = collapse_ws(html.unescape(raw_name or "")).upper()
    d = book.decide(hay_raw, payee, amount, check_num, trans_type)
    out: Dict[str, Any] = {
        "txn_kind": "expense", "is_included": True, "exclusion_reason": "", "envelope": "", "name": d["name"],
        "match_source": "", "income_source_id": None, "person_id": None, "income_subtype": None,
        "income_label": None, "needs_review": 1 if d["review"] else 0, "reimburses_txn_id": None,
    }
    notes: List[str] = []
    kind, env = d["kind"], d["envelope"] or ""

    if kind in EXCLUDED_KINDS:
        out.update(txn_kind=kind, is_included=False, envelope="",
                   exclusion_reason=d["exclusion_reason"] or "Internal Account Transfer" if kind == "transfer"
                   else d["exclusion_reason"] or "Credit Card Payment / Autopay")
    elif kind == "expense":
        out.update(envelope=env)
    elif amount > 0:
        src = income.match_income_source(ctx.income_sources, hay_raw, payee.upper())
        if src:
            override = (src.get("typical_amount_cents") or 0) / 100.0 or None
            typical = ctx.history.typical_before(src["id"], date_iso, override)
            threshold = src.get("bonus_threshold_pct") or ctx.bonus_threshold_pct
            subtype = income.subtype_for(amount, typical, float(threshold))
            if subtype == "regular":
                ctx.history.add(src["id"], date_iso, amount)
            out.update(txn_kind="paycheck", income_source_id=src["id"], person_id=src.get("person_id"),
                       income_subtype=subtype, needs_review=0)
            notes.append(f"Paycheck: {src.get('name') or 'income source'}")
        elif kind in ("refund", "other_income"):
            out.update(txn_kind=kind, envelope=env, income_label=d["income_label"])
        elif env:
            out.update(txn_kind="refund", envelope=env)
            notes.append("Refund to that envelope")
        else:
            found = ctx.expenses.find(date_iso, payee, ctx.refund_lookback_days)
            if found:
                out.update(txn_kind="refund", envelope=found)
                notes.append("Refund: recent purchase")
            else:
                out.update(txn_kind="other_income", income_label=payee[:60], needs_review=1)
                notes.append("Other income (review)")
    else:
        out.update(envelope=env)

    if d["skipped"]:
        out["needs_review"] = 1
        notes.append(f"rule '{d['skipped'][0]['name']}' skipped: no '{d['skipped'][0]['set_envelope']}' envelope")
    out["rules"] = list(d["applied"].values())
    out["notes"] = notes
    out["match_source"] = ", ".join([r["name"] for r in out["rules"]] + notes)
    return out
