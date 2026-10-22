"""Built-in classification rules, seeded into the rules table once.

These are the rules that used to be hardcoded in the old importer. After
seeding they live in the database and can be edited or turned off from the
Import tab. Priority numbers run low to high; user rules (100+) run before
vendor rules (500+), which run before these core envelope rules (1000+).
"""

from typing import Any, Dict, List

PRIORITY_INCLUDE = 10
PRIORITY_EXCLUDE = 100
PRIORITY_INCOME = 200
PRIORITY_CORE_ENVELOPE = 1000


def _rule(phrase: str, stage: str, priority: int, **kw: Any) -> Dict[str, Any]:
    row: Dict[str, Any] = {
        "phrase": phrase,
        "match_mode": "contains",
        "stage": stage,
        "set_kind": None,
        "set_envelope": None,
        "set_payee": None,
        "set_income_label": None,
        "set_needs_review": 0,
        "exclusion_reason": None,
        "label": None,
        "priority": priority,
        "origin": "core",
    }
    row.update(kw)
    return row


def _envelope_group(phrases: List[str], envelope: str, priority: int, mode: str = "contains") -> List[Dict[str, Any]]:
    return [
        _rule(p, "envelope", priority, set_envelope=envelope, match_mode=mode, label="Core Rule")
        for p in phrases
    ]


def core_rules() -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []

    # Allowance transfers to the kids are real spending, not account transfers.
    rows.append(
        _rule(
            "ONLINE TRANSFER TO REILING J", "include_override", PRIORITY_INCLUDE,
            set_kind="expense", set_envelope="Shopping", label="Kid Transfer Exception",
        )
    )

    # Credit card payments and autopays are not spending.
    for phrase, reason in [
        ("AUTOMATIC PAYMENT - THANK", "Credit Card Payment / Autopay"),
        ("CHASE CREDIT CRD", "Chase Credit Card Autopay"),
        ("AMERICAN EXPRESS ACH PMT", "Amex ACH Payment"),
        ("AMEX EPAYMENT", "Amex ACH Payment"),
    ]:
        rows.append(_rule(phrase, "exclude", PRIORITY_EXCLUDE, set_kind="cc_payment", exclusion_reason=reason, label="Core Rule"))
    rows.append(
        _rule(
            r"SAMS CLUB.*PAYMENT|PAYMENT.*SAMS CLUB", "exclude", PRIORITY_EXCLUDE,
            match_mode="regex", set_kind="cc_payment", exclusion_reason="Sam's Club Card Payment", label="Core Rule",
        )
    )

    # Moves between our own accounts are not spending or income.
    for phrase in [
        "WAY2SAVE SAVINGS",
        "RECURRING TRANSFER TO REILING S",
        "ONLINE TRANSFER TO REILING S",
        "ONLINE TRANSFER FROM REILING S",
        "ONLINE TRANSFER FROM REILING J",
    ]:
        rows.append(_rule(phrase, "exclude", PRIORITY_EXCLUDE, set_kind="transfer", exclusion_reason="Internal Account Transfer", label="Core Rule"))

    # Positive amounts that are not paychecks.
    rows.append(
        _rule("IRS TREAS", "income", PRIORITY_INCOME, set_kind="other_income", set_income_label="Tax Refund", label="Core Rule")
    )
    for phrase in ("VENMO CASHOUT", "VENMO PAYMENT"):
        rows.append(
            _rule(phrase, "refund", PRIORITY_INCOME, set_kind="refund", set_needs_review=1, label="Reimbursement")
        )
    rows.append(_rule("STATEMENT CREDIT", "refund", PRIORITY_INCOME, set_needs_review=1, label="Statement Credit"))

    # Envelope heuristics, in the same order the old importer checked them.
    p = PRIORITY_CORE_ENVELOPE
    groups = [
        (["SCOTT COUNTY TRE", "COUNTY TREASURER"], "Property Taxes", "contains"),
        (["DAIRYLAND CYCLE"], "Insurance: Motorcycle", "contains"),
        (["TRAVELERS"], "Insurance: Auto", "contains"),
        (
            [
                "CENTERPOINT ENER", "QUANTUM FIBER", "DICKS SANITATION", "DICK'S SANITATION",
                "SHAKOPEE UTILITIE", "ATT*BILL PAYMENT", "AT&T MOBILITY", "VEILWILDORTHO",
            ],
            "Bills: Every Month", "contains",
        ),
        (
            [
                "NETFLIX", "PEACOCK", "SPOTIFY", "GOOGLE *GOOGLE ONE", "GOOGLE *G1", "PLANET FITNESS",
                "MICROSOFT", "XBOX", "PLAYSTATION", "AMAZON DIGIT", "APPLE.COM", "ANTHROPIC", "CLAUDE",
                "FRITZING", "CALIMOTO", "HABIT NEST", "AARP", "YOUTHONCOURSE",
            ],
            "Bills: Subscriptions", "contains",
        ),
        (
            [
                "BRIDGE CHIROPRACTIC", "HEALTHPARTNERS DENTAL", "PARK NICOLLET", "RAMP", "MAC PARKING",
                "SPOTHERO", "MN DEPT OF REVEN", "SHAKOPEE PUBLIC", "MLT (SCHOOL)", "SHAKOPEE SOCCER",
                "EPSC SOCCER", "SHAKOPEE ACTIVITIES", "CHAMPION FORCE ATHLET", "BOUND* SHAKOPEE",
                "SCOUTING*", "USSF LEARNING", "TPW LICENSE", "CAMERON COUNTY",
            ],
            "Bills: One Time", "contains",
        ),
        (
            [
                "COSTCO", "SAMS CLUB", "SAM'S CLUB", "SAMSCLUB", "LIQUOR", "WINE & SPIRITS", "SOBERISH",
                "ALDI", "CUB FOODS", "TRADER JOE",
            ],
            "Groceries", "contains",
        ),
        (["FAST & FRESH", "F&F"], "Gas", "contains"),
        (["HY-VEE"], "Groceries", "contains"),
        # In-store Walmart is groceries; walmart.com orders are not.
        ([r"^(?!.*WALMART\.COM)(?=.*(WALMART|WM SUPERCENTER))"], "Groceries", "regex"),
        (["CAR WASH", "CARWASH"], "Gas", "contains"),
        (
            [
                "KWIK TRIP", "CIRCLE K", "CIRCLEK", "HOLIDAY", "CK HOLIDAY", "CASEYS", "SPEEDWAY", "BP",
                "SHELL", "EXXON", "MOBIL", "PILOT", "MARATHON", "SINCLAIR", "CONOCO",
            ],
            "Gas", "word",
        ),
        (
            [
                "DOORDASH", "UBER EATS", "GRUBHUB", "MCDONALD", "TACO BELL", "BURGER KING", "WENDY",
                "CHIPOTLE", "SUBWAY", "CARIBOU", "STARBUCKS", "SEVERS FESTIVALS", "STONE HOUSE SALOON",
                "BRUEGGERS", "APPLEBEES", "DAIRY QUEEN", "LIONS TAP", "MARGARITAVILLE", "PIZZA RANCH",
                "OG ZAZA", "TACO JOHNS", "CHICK-FIL-A", "CULVER", "JIMMY JOHN", "FIVE GUYS", "DOMINO",
                "BAR & GRILL", "BAR AND GRILL", "TAVERN", "BREWING", "WINGSTOP", "BUFFALO WILD",
                "SHAKOPEE BOWL",
            ],
            "Restaurant", "contains",
        ),
        (["MENARDS", "HOME DEPOT", "LOWES", "LOWE'S", "FLEET FARM"], "Shopping", "contains"),
    ]
    for phrases, envelope, mode in groups:
        rows.extend(_envelope_group(phrases, envelope, p, mode))
        p += 10

    return rows


# Envelopes created on a brand-new database (an existing install keeps its own list).
DEFAULT_ENVELOPES = [
    "Groceries", "Gas", "Shopping", "Restaurant", "Bills: Every Month", "Bills: One Time",
    "Bills: Subscriptions", "Bills: Mortgage", "Insurance: Auto", "Property Taxes",
    "Insurance: Home", "Insurance: Motorcycle",
]
