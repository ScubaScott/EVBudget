"""Turn the old one-action-per-rule list into rules with a name and several actions."""

from typing import Any, Dict, Iterable, List, Optional

# Envelopes that rules still point at under an older name
STALE_ENVELOPES = {"bills: subscriptions": "Subscriptions", "bills: mortgage": "Mortgage"}

# Patterns are hard to read, so the ones that shipped with the app (and the ones already in use) get a plain name
KNOWN_PATTERN_NAMES = {
    r"SAMS CLUB.*PAYMENT|PAYMENT.*SAMS CLUB": "Sam's Club card payment",
    r"^(?!.*WALMART\.COM)(?=.*(WALMART|WM SUPERCENTER))": "Walmart in store (not walmart.com)",
    r"circle\s?k": "Circle K",
    r"(.*)\s?#": "Remove store numbers",
    r"^(?:SQ|TST|WCI)\s?\*\s?(.*)": "Remove Square / Toast prefixes",
    r"(Target) (?!Field)": "Target (not Target Field)",
}

# Positions: lower runs first. Old priorities were 10 (keep), 100 (exclude), 200 (income), 79-122 (yours),
# 500 (vendor list) and 1000+ (built-in), so the old order is kept as it was.
POS_PAYMENT, POS_CHECK, POS_FALLBACK = 50, 60, 100_000

_OLD_DIRECTION = {"income": "in", "refund": "in"}


def _readable_name(old: Dict[str, Any]) -> str:
    phrase = (old.get("phrase") or "").strip()
    if phrase in KNOWN_PATTERN_NAMES:
        return KNOWN_PATTERN_NAMES[phrase]
    payee = (old.get("set_payee") or "").strip()
    if payee and "\\" not in payee and "$" not in payee:
        return payee
    if old.get("description"):
        text = old["description"].strip()
        return text[:1].upper() + text[1:]
    if phrase.isupper() or phrase.islower():
        return phrase.title()
    return phrase


def _one(old: Dict[str, Any], renames: Dict[str, str]) -> Optional[Dict[str, Any]]:
    stage = old["stage"]
    env = (old.get("set_envelope") or "").strip() or None
    if env and env.lower() in renames:
        env = renames[env.lower()]
    kind = old.get("set_kind") or None
    if stage == "include_override":
        kind = kind or "expense"
    elif stage == "exclude":
        kind = kind or "transfer"
    mode = old.get("match_mode") or "contains"
    rule = {
        "name": _readable_name(old),
        "filter_type": "text",
        "phrase": old["phrase"],
        "match_mode": "contains" if mode == "vendor" else mode,
        "direction": _OLD_DIRECTION.get(stage, "any"),
        "set_payee": (old.get("set_payee") or "").strip() or None,
        "set_envelope": env,
        "set_kind": kind,
        "set_income_label": old.get("set_income_label") or None,
        "set_needs_review": 1 if old.get("set_needs_review") else 0,
        "exclusion_reason": old.get("exclusion_reason") or None,
        "description": old.get("description") or None,
        "priority": old.get("priority", 1000),
        "active": 1 if old.get("active", 1) else 0,
        "old_ids": [old["id"]],
    }
    return rule if (rule["set_payee"] or env or kind or rule["set_needs_review"]) else None


_SLOTS = ("set_payee", "set_envelope", "set_kind", "set_income_label", "exclusion_reason")


def convert_rules(
    old_rules: Iterable[Dict[str, Any]], renames: Optional[Dict[str, str]] = None, report: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """The new rules, in running order, with the code-level defaults added as rules.

    Old rules with the same text, pattern type and direction become one rule; the earliest one decides where it sits.
    Where two of them set the same thing differently the earlier one wins, as it did before (listed in report['conflicts']).
    """
    renames = STALE_ENVELOPES if renames is None else renames
    report = report if report is not None else {}
    report.setdefault("conflicts", [])
    report["merged"] = report["dropped"] = 0

    ordered = sorted((dict(r) for r in old_rules), key=lambda r: (r.get("priority", 0), r.get("id") or 0))
    merged: Dict[tuple, Dict[str, Any]] = {}
    out: List[Dict[str, Any]] = []
    for old in ordered:
        rule = _one(old, renames)
        if rule is None:
            report["dropped"] += 1
            continue
        key = (rule["phrase"].upper(), rule["match_mode"], rule["direction"], rule["active"])
        into = merged.get(key)
        if into is None:
            merged[key] = rule
            out.append(rule)
            continue
        report["merged"] += 1
        into["old_ids"] += rule["old_ids"]
        for slot in _SLOTS:
            if rule[slot] and not into[slot]:
                into[slot] = rule[slot]
            elif rule[slot] and into[slot] != rule[slot]:
                report["conflicts"].append((into["phrase"], slot, into[slot], rule[slot]))
        into["set_needs_review"] = into["set_needs_review"] or rule["set_needs_review"]
        into["description"] = into["description"] or rule["description"]
        if into["set_payee"] and into["name"] != into["set_payee"] and "\\" not in into["set_payee"]:
            into["name"] = into["set_payee"]

    defaults = [
        {"name": "Card payments (the bank marks them PAYMENT)", "filter_type": "payment", "phrase": "", "match_mode": "contains",
         "direction": "any", "set_kind": "cc_payment", "exclusion_reason": "Credit Card Payment / Autopay", "priority": POS_PAYMENT},
        {"name": "Checks", "filter_type": "check", "phrase": "", "match_mode": "contains", "direction": "out",
         "set_envelope": "Bills: One Time", "priority": POS_CHECK},
        {"name": "Everything else spent", "filter_type": "all", "phrase": "", "match_mode": "contains", "direction": "out",
         "set_envelope": "Shopping", "priority": POS_FALLBACK},
    ]
    for d in defaults:
        out.append(dict({"set_payee": None, "set_envelope": None, "set_kind": None, "set_income_label": None,
                         "set_needs_review": 0, "exclusion_reason": None, "description": None, "active": 1, "old_ids": []}, **d))
    out.sort(key=lambda r: r["priority"])
    for i, r in enumerate(out, 1):
        r["id"] = i
        r["priority"] = i * 10
    return out
