"""Bank sync through SimpleFIN (protocol v2, https://www.simplefin.org/protocol.html).

Beyond the protocol, SimpleFIN Bridge sends `memo` (the bank's own wording), `payee` and `mcc` on transactions, and
`holdings` on investment accounts. `memo` is used as the bank text when present (see to_rows).

Connecting: the person pastes a setup token from SimpleFIN Bridge. It is a Base64 claim URL that works once; claiming
it returns the Access URL (https://user:pass@host/simplefin), which is stored in the budget's settings and never sent
back to the page.

Syncing: GET {access}/accounts for a date range (at most 90 days a request, posted transactions only). The rows go
through the same preview, rules and duplicate checks as a bank file (import_engine.preview_rows); nothing is saved
until the person presses Save.

What the protocol does not promise, and how this module copes:
- A transaction ID is unique only inside its account, and account IDs only inside their connection, so EVBudget's ID
  is a hash of host, connection, account and transaction ID (txn_id_for).
- IDs may not stay the same between requests, a pending charge may get a new ID when it posts, and a listing can be
  incomplete. Pending rows are skipped, the duplicate check backs up the ID check, and a transaction missing from a
  response is never treated as deleted.
- The Bridge asks apps for 24 requests a day or fewer, and turns off access tokens that keep going over. Requests are
  counted per budget per day (DAILY_LIMIT, with room to spare).
"""

import base64
import binascii
import hashlib
import json
import re
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import VERSION, cards, store

ACCESS_URL_KEY = "simplefin_access_url"
ACCOUNTS_KEY = "simplefin_accounts"          # {account key: {"name", "account_type", "ignore", "start_date"}}
REQUESTS_KEY = "simplefin_requests"          # {"day": "YYYY-MM-DD", "count": n}
SYNCED_THROUGH_KEY = "simplefin_synced_through"
CONNECTED_AT_KEY = "simplefin_connected_at"
SEEN_KEY = "simplefin_seen_accounts"         # {account key: {"sf_name", "connection", "currency", "balance", "updated"}}
BACKFILL_KEY = "simplefin_backfill_from"     # a starting day moved earlier: the next sync reaches back to it

SOURCE_LABEL = "SimpleFIN"   # what a synced row shows where a file name would be
ID_PREFIX = "SF-"
BRIDGE_URL = "https://beta-bridge.simplefin.org"
MAX_DAYS = 45                # per request: the Bridge warns above 45 days ("may be capped") and refuses above 90
FIRST_SYNC_DAYS = 90         # the first sync looks back this far (two requests)
OVERLAP_DAYS = 5             # the Bridge suggests fetching a few days already seen, to catch late postings
MAX_CHUNKS = 8               # one sync goes back at most this many requests (about a year)
DAILY_LIMIT = 20             # the Bridge asks for 24 or fewer a day
TIMEOUT = 60
STALE_HOURS = 24             # the Bridge checks each bank about once a day; older than that, say so

CREDIT = "Credit / Debit"
BANK = "Bank / Checking"


class SimpleFinError(Exception):
    """Something the person needs to know about, worded for them."""

    def __init__(self, message: str, status: int = 400, code: str = ""):
        super().__init__(message)
        self.status = status
        self.code = code


Opener = Callable[..., Any]   # urllib.request.urlopen, or a stand-in for tests


# ---------------------------------------------------------------------------
# Setup token and Access URL
# ---------------------------------------------------------------------------


def _is_access_url(text: str) -> bool:
    p = urllib.parse.urlsplit(text)
    return p.scheme == "https" and bool(p.hostname) and p.username is not None and p.password is not None


def decode_setup_token(token: str) -> str:
    """The claim URL inside a setup token. Raises SimpleFinError if it isn't one."""
    text = re.sub(r"\s+", "", token or "")
    if not text:
        raise SimpleFinError("Paste the setup token from SimpleFIN Bridge first.")
    try:
        raw = base64.b64decode(text + "=" * (-len(text) % 4), altchars=b"-_" if ("-" in text or "_" in text) else None, validate=True)
        url = raw.decode("utf-8").strip()
    except (binascii.Error, ValueError, UnicodeDecodeError):
        raise SimpleFinError("That isn't a SimpleFIN setup token. Copy the whole token from SimpleFIN Bridge (a long run of letters and numbers) and paste it again.")
    p = urllib.parse.urlsplit(url)
    if p.scheme != "https" or not p.hostname:
        raise SimpleFinError("That setup token doesn't hold a secure (https) address, so EVBudget won't use it. Make a new token at SimpleFIN Bridge.")
    return url


def host_of(url: str) -> str:
    return (urllib.parse.urlsplit(url).hostname or "").lower()


def _split_access_url(access_url: str) -> Tuple[str, str]:
    """(address without the user name and password, the Basic auth header value)."""
    p = urllib.parse.urlsplit(access_url)
    user = urllib.parse.unquote(p.username or "")
    password = urllib.parse.unquote(p.password or "")
    netloc = p.hostname or ""
    if p.port:
        netloc += f":{p.port}"
    base = urllib.parse.urlunsplit((p.scheme, netloc, p.path.rstrip("/"), "", ""))
    auth = "Basic " + base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
    return base, auth


def _ssl_hint() -> str:
    if sys.platform == "darwin":
        return (" On a Mac this usually means Python's certificates aren't installed: open the Python folder in"
                " Applications and run \"Install Certificates.command\", then try again.")
    return " Check the computer's date and time, and any antivirus or network filter that inspects secure connections."


def _request(opener: Opener, req: urllib.request.Request, host: str) -> bytes:
    """Send one request. Turns every failure into a SimpleFinError that says what went wrong."""
    try:
        with opener(req, timeout=TIMEOUT, context=ssl.create_default_context()) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 402:
            raise SimpleFinError("SimpleFIN says payment is needed: the SimpleFIN Bridge subscription has run out. Renew it at SimpleFIN Bridge, then sync again.", 402, "payment")
        if exc.code == 403:
            raise SimpleFinError("SimpleFIN refused EVBudget's access (403). The connection may have been turned off at SimpleFIN Bridge. Make a new setup token there and connect again.", 403, "forbidden")
        raise SimpleFinError(f"SimpleFIN ({host}) answered with error {exc.code} {exc.reason}. Try again later.", 502, "http")
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, ssl.SSLCertVerificationError) or "CERTIFICATE_VERIFY_FAILED" in str(reason):
            raise SimpleFinError(f"EVBudget couldn't confirm that {host} is really SimpleFIN (its security certificate couldn't be checked)." + _ssl_hint(), 502, "ssl")
        if isinstance(reason, ssl.SSLError):
            raise SimpleFinError(f"The secure connection to {host} failed ({reason})." + _ssl_hint(), 502, "ssl")
        if isinstance(reason, TimeoutError) or "timed out" in str(reason):
            raise SimpleFinError(f"{host} didn't answer within {TIMEOUT} seconds. Try again in a few minutes.", 504, "timeout")
        raise SimpleFinError(f"Couldn't reach {host}: {reason}. Check the internet connection.", 502, "network")
    except TimeoutError:
        raise SimpleFinError(f"{host} didn't answer within {TIMEOUT} seconds. Try again in a few minutes.", 504, "timeout")
    except ssl.SSLError as exc:
        raise SimpleFinError(f"The secure connection to {host} failed ({exc})." + _ssl_hint(), 502, "ssl")
    except OSError as exc:
        raise SimpleFinError(f"Couldn't reach {host}: {exc}. Check the internet connection.", 502, "network")


def claim(token: str, opener: Opener = urllib.request.urlopen) -> str:
    """Trade a setup token for the Access URL. A token works only once."""
    text = (token or "").strip()
    if _is_access_url(text):
        return text     # someone pasted the Access URL itself
    url = decode_setup_token(text)
    host = host_of(url)
    req = urllib.request.Request(url, data=b"", method="POST", headers={"User-Agent": f"EVBudget/{VERSION}", "Content-Length": "0"})
    try:
        body = _request(opener, req, host)
    except SimpleFinError as exc:
        if exc.status == 403:
            # The protocol asks apps to say this: a token claimed by someone else may have been stolen.
            raise SimpleFinError(
                "This setup token has already been used, so it can't connect EVBudget. If you didn't use it yourself,"
                " someone else may have: at SimpleFIN Bridge, turn off the connection that used it, then make a new token"
                " and paste that here.", 403, "token_used")
        raise
    access = body.decode("utf-8", errors="replace").strip()
    if not _is_access_url(access):
        raise SimpleFinError(f"SimpleFIN ({host}) didn't send back a usable access address. Make a new setup token and try again.", 502, "bad_claim")
    return access


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------


def _epoch(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp())


def fetch(access_url: str, start: date, end: date, opener: Opener = urllib.request.urlopen) -> Dict[str, Any]:
    """One GET /accounts for [start, end) (end is the day after the last one wanted). Posted transactions only."""
    base, auth = _split_access_url(access_url)
    query = urllib.parse.urlencode({"version": "2", "start-date": _epoch(start), "end-date": _epoch(end)})
    req = urllib.request.Request(f"{base}/accounts?{query}", headers={
        "Authorization": auth, "Accept": "application/json", "User-Agent": f"EVBudget/{VERSION}"})
    host = host_of(base)
    body = _request(opener, req, host)
    try:
        data = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise SimpleFinError(f"SimpleFIN ({host}) sent something EVBudget can't read (not JSON). Try again later.", 502, "bad_json")
    if not isinstance(data, dict):
        raise SimpleFinError(f"SimpleFIN ({host}) sent an answer EVBudget can't read. Try again later.", 502, "bad_json")
    return data


def chunks(start: date, end: date) -> List[Tuple[date, date]]:
    """[start, end) split into pieces of at most MAX_DAYS."""
    out = []
    cur = start
    while cur < end:
        nxt = min(cur + timedelta(days=MAX_DAYS), end)
        out.append((cur, nxt))
        cur = nxt
    return out


def merge(responses: List[Dict[str, Any]]) -> Dict[str, Any]:
    """One response from several date ranges: accounts joined by connection and ID, transactions by ID."""
    connections: Dict[str, Dict[str, Any]] = {}
    accounts: Dict[str, Dict[str, Any]] = {}
    errlist: List[Any] = []
    for data in responses:
        for c in data.get("connections") or []:
            if isinstance(c, dict) and c.get("conn_id") is not None:
                connections[str(c["conn_id"])] = c
        for e in (data.get("errlist") or []) + [{"code": "gen.", "msg": m} for m in (data.get("errors") or []) if isinstance(m, str)]:
            if e not in errlist:
                errlist.append(e)
        for a in data.get("accounts") or []:
            if not isinstance(a, dict) or a.get("id") is None:
                continue
            key = account_key(a)
            have = accounts.get(key)
            if have is None:
                have = dict(a, transactions=[])
                accounts[key] = have
            else:  # the newest balance wins
                for f in ("balance", "available-balance", "balance-date", "name", "currency"):
                    if a.get(f) is not None and (f != "balance" or int(a.get("balance-date") or 0) >= int(have.get("balance-date") or 0)):
                        have[f] = a[f]
            seen = {str(t.get("id")) for t in have["transactions"]}
            for t in a.get("transactions") or []:
                if isinstance(t, dict) and str(t.get("id")) not in seen:
                    have["transactions"].append(t)
                    seen.add(str(t.get("id")))
    return {"connections": list(connections.values()), "accounts": list(accounts.values()), "errlist": errlist}


# ---------------------------------------------------------------------------
# Messages from the server
# ---------------------------------------------------------------------------


def sanitize(text: Any, limit: int = 300) -> str:
    """A server message made safe to show: printable characters only, no runs of spaces, not too long.
    (The page also escapes it as HTML.)"""
    # U+FFFD stands for a character the bank's data had already lost (the "®" in "WAY2SAVE®", say): drop it
    s = "".join(ch if ch.isprintable() else " " for ch in str(text or "").replace("\ufffd", ""))
    s = re.sub(r"\s+", " ", s).strip()
    return s[:limit] + ("…" if len(s) > limit else "")


def explain_errors(data: Dict[str, Any]) -> List[Dict[str, str]]:
    """Every problem SimpleFIN reported, with what it means. The protocol asks apps to show all of them."""
    conns = {str(c.get("conn_id")): sanitize(c.get("name"), 80) for c in data.get("connections") or [] if isinstance(c, dict)}
    accts = {str(a.get("id")): sanitize(a.get("name"), 80) for a in data.get("accounts") or [] if isinstance(a, dict)}
    items = list(data.get("errlist") or [])
    items += [{"code": "gen.", "msg": m} for m in data.get("errors") or [] if isinstance(m, str)]
    out = []
    for e in items:
        if not isinstance(e, dict):
            e = {"code": "gen.", "msg": e}
        code = str(e.get("code") or "gen.")
        conn = conns.get(str(e.get("conn_id")), "") if e.get("conn_id") is not None else ""
        acct = accts.get(str(e.get("account_id")), "") if e.get("account_id") is not None else ""
        where = acct or conn or "an account"
        if code.startswith("con.auth"):
            what = f"{conn or 'A bank'} needs you to sign in again. Open SimpleFIN Bridge and fix that connection, then sync again."
        elif code.startswith("con."):
            what = f"SimpleFIN couldn't update {conn or 'one of your banks'}. Transactions from it may be missing until it's fixed at SimpleFIN Bridge."
        elif code.startswith("act.missingdata"):
            what = f"Some transactions for {where} may be missing from this sync. Nothing is treated as deleted; sync again later to fill the gap."
        elif code.startswith("act."):
            what = f"SimpleFIN couldn't update {where} this time."
        elif code.startswith("gen.auth"):
            what = "SimpleFIN didn't accept EVBudget's access. Make a new setup token at SimpleFIN Bridge and connect again."
        else:
            what = "Note from SimpleFIN."
        # General messages are usually notices (a warning about the date range, say); the rest mean data is missing
        level = "note" if code.startswith("gen.") and not code.startswith("gen.auth") else "problem"
        out.append({"code": code, "message": sanitize(e.get("msg")), "explain": what, "level": level,
                    "connection": conn, "account": acct})
    return out


# ---------------------------------------------------------------------------
# Accounts and their EVBudget names
# ---------------------------------------------------------------------------


def account_key(account: Dict[str, Any]) -> str:
    return f"{account.get('conn_id') or ''}|{account.get('id')}"


def txn_id_for(host: str, conn_id: Any, account_id: Any, sf_txn_id: Any) -> str:
    """EVBudget's ID for a SimpleFIN transaction. SimpleFIN's own ID is unique only inside its account."""
    key = f"{host}|{conn_id or ''}|{account_id}|{sf_txn_id}"
    return ID_PREFIX + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16].upper()


def is_synced_id(txn_id: str) -> bool:
    return str(txn_id or "").startswith(ID_PREFIX)


def load_mapping(conn: Any) -> Dict[str, Dict[str, Any]]:
    try:
        data = json.loads(store.get_setting(conn, ACCOUNTS_KEY) or "{}")
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


def save_mapping(conn: Any, choices: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Store which EVBudget account each SimpleFIN account goes to. Only accounts already on the Accounts list can be
    chosen (raises ValueError saying to add it there first); the type is that account's."""
    mapping = load_mapping(conn)
    reach_back: List[str] = []
    for c in choices:
        key = str(c.get("key") or "")
        if not key:
            continue
        ignore = bool(c.get("ignore"))
        name = cards.clean_name(c.get("name"))
        kind = BANK
        start = str(c.get("start_date") or "").strip()
        if start:
            try:
                start = date.fromisoformat(start).isoformat()
            except ValueError:
                raise ValueError(f"\"{start}\" isn't a date.")
        if not ignore:
            account = cards.require_account(conn, name, sanitize(c.get("sf_name"), 80) or "a bank account")
            name, kind = account["name"], account["account_type"]
            before = mapping.get(key)
            # Newly brought in, or starting earlier than before: the next sync may need to reach further back
            if before is None or before.get("ignore") or (before.get("start_date") and (not start or start < before["start_date"])):
                reach_back.append(start or (date.today() - timedelta(days=FIRST_SYNC_DAYS)).isoformat())
        mapping[key] = {"name": name, "account_type": kind, "ignore": ignore, "start_date": start or None}
    store.set_setting(conn, ACCOUNTS_KEY, json.dumps(mapping))
    if reach_back:
        have = store.get_setting(conn, BACKFILL_KEY) or ""
        store.set_setting(conn, BACKFILL_KEY, min([d for d in (have,) if d] + reach_back))
    return mapping


def guess_name(account: Dict[str, Any], known: List[str]) -> str:
    """The existing EVBudget account this bank account probably is (same last four digits, or the same name), or ""
    when none fits: then the person chooses one, adding it under Accounts first if it isn't there."""
    name = sanitize(account.get("name"), 60)
    for d in reversed(re.findall(r"\d{4}", name)):
        for k in known:
            if d in k:
                return k
    for k in known:
        if k.lower() == name.lower():
            return k
    return ""


def with_current_types(conn: Any, mapping: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """The mapping with each account's type as the Accounts list has it now (it can be changed there). A bank account
    pointed at an account that has since been deleted is left out, so it is asked about again."""
    types = {r[0].lower(): r[1] for r in conn.execute("SELECT name, account_type FROM accounts")}
    out = {}
    for k, v in mapping.items():
        kind = types.get(str(v.get("name") or "").lower())
        if v.get("ignore"):
            out[k] = v
        elif kind:
            out[k] = dict(v, account_type=kind)
    return out


def latest_saved_dates(conn: Any) -> Dict[str, str]:
    return {r[0].lower(): r[1] for r in conn.execute(
        "SELECT account_name, MAX(date) FROM transactions WHERE COALESCE(account_name, '') != '' GROUP BY account_name COLLATE NOCASE")}


def _when(ts: Any) -> Optional[str]:
    """A SimpleFIN timestamp as an ISO date and time (UTC), or None."""
    try:
        n = int(ts)
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(n, tz=timezone.utc).isoformat() if n > 0 else None


def account_info(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """What a download says about each bank account. `updated` is when SimpleFIN last got news from the bank
    (the balance date)."""
    conns = {str(c.get("conn_id")): sanitize(c.get("name"), 60) for c in data.get("connections") or [] if isinstance(c, dict)}
    out = []
    for a in data.get("accounts") or []:
        if not isinstance(a, dict) or a.get("id") is None:
            continue
        txns = [t for t in a.get("transactions") or [] if isinstance(t, dict)]
        posted = [t for t in txns if _is_posted(t)]
        dates = sorted(d for d in (_txn_date(t) for t in posted) if d)
        out.append({
            "key": account_key(a), "connection": conns.get(str(a.get("conn_id")), ""), "sf_name": sanitize(a.get("name"), 80),
            "currency": sanitize(a.get("currency"), 10), "balance": sanitize(a.get("balance"), 30),
            "updated": _when(a.get("balance-date")),
            "posted": len(posted), "pending": len(txns) - len(posted),
            "first_date": dates[0] if dates else None, "last_date": dates[-1] if dates else None,
        })
    return out


REMEMBERED = ("sf_name", "connection", "currency", "balance", "updated")


def load_seen(conn: Any) -> Dict[str, Dict[str, Any]]:
    try:
        data = json.loads(store.get_setting(conn, SEEN_KEY) or "{}")
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


def remember_accounts(conn: Any, data: Dict[str, Any]) -> None:
    """Keep what a download said about each bank account, so its choices can be changed (and how current SimpleFIN
    is can be shown) before the next download. Accounts missing from this download keep what was known."""
    seen = load_seen(conn)
    for info in account_info(data):
        seen[info["key"]] = {k: info[k] for k in REMEMBERED}
    store.set_setting(conn, SEEN_KEY, json.dumps(seen))


def is_stale(updated: Optional[str], now: Optional[datetime] = None) -> bool:
    """True when SimpleFIN last got news from the bank more than STALE_HOURS ago."""
    if not updated:
        return False
    try:
        when = datetime.fromisoformat(updated)
    except ValueError:
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return (now or datetime.now(timezone.utc)) - when > timedelta(hours=STALE_HOURS)


def account_rows(conn: Any, data: Optional[Dict[str, Any]] = None, now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """The SimpleFIN accounts with what EVBudget will do with each (saved or suggested): from a download, or, with no
    download, from the accounts remembered from the last one (then the transaction counts are None)."""
    mapping = with_current_types(conn, load_mapping(conn))
    if data is not None:
        infos = account_info(data)
    else:
        seen = load_seen(conn)
        infos = []
        for key in list(seen) + [k for k in mapping if k not in seen]:
            info = {k: seen.get(key, {}).get(k) for k in REMEMBERED}
            # accounts chosen before EVBudget remembered what the bank calls them
            info["sf_name"] = info["sf_name"] or mapping.get(key, {}).get("name") or "Bank account"
            info["connection"] = info["connection"] or ""
            infos.append(dict(info, key=key, posted=None, pending=None, first_date=None, last_date=None))
    known = cards.known_names(conn)
    latest = latest_saved_dates(conn)
    out = []
    for info in infos:
        saved = mapping.get(info["key"])
        if saved:
            choice = dict(saved, mapped=True)
        else:
            name = guess_name({"name": info["sf_name"]}, known)
            last = latest.get(name.lower()) if name else None
            # An account that already has transactions from files: start a week before the newest one, so the overlap
            # is checked for duplicates but older history isn't brought in again.
            start = (date.fromisoformat(last) - timedelta(days=7)).isoformat() if last else None
            types = {k.lower(): t for k, t in conn.execute("SELECT name, account_type FROM accounts")}
            choice = {"name": name, "account_type": types.get(name.lower()) if name else None,
                      "ignore": False, "start_date": start, "mapped": False}
        out.append(dict(
            info, balance_date=(info["updated"] or "")[:10] or None,
            stale=not choice.get("ignore") and is_stale(info["updated"], now),
            saved_through=latest.get((choice.get("name") or "").lower()),
            **choice,
        ))
    return out


# ---------------------------------------------------------------------------
# Transactions into import rows
# ---------------------------------------------------------------------------


def _day(ts: Any) -> Optional[str]:
    try:
        n = int(ts)
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    return datetime.fromtimestamp(n, tz=timezone.utc).date().isoformat()


def _is_posted(t: Dict[str, Any]) -> bool:
    if t.get("pending") is True:
        return False
    try:
        return int(t.get("posted") or 0) > 0
    except (TypeError, ValueError):
        return False


def _txn_date(t: Dict[str, Any]) -> Optional[str]:
    """The day the purchase happened when the bank says so, otherwise the day it posted. Bank files use the purchase
    date too, which keeps the two close for the duplicate check."""
    return _day(t.get("transacted_at")) or _day(t.get("posted"))


_PAYMENT = re.compile(r"\b(PAYMENT|AUTOPAY|AUTO PAY|THANK YOU)\b")


def to_rows(data: Dict[str, Any], host: str, mapping: Dict[str, Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """Import rows (the shape import_engine.parse_file makes) for the posted transactions of mapped accounts.
    Returns (rows, counts of what was left out and why)."""
    rows: List[Dict[str, Any]] = []
    counts = {"pending": 0, "unmapped": 0, "ignored": 0, "before_start": 0, "unreadable": 0}
    for a in data.get("accounts") or []:
        if not isinstance(a, dict):
            continue
        choice = mapping.get(account_key(a))
        txns = [t for t in a.get("transactions") or [] if isinstance(t, dict)]
        if not choice:
            counts["unmapped"] += len(txns)
            continue
        if choice.get("ignore"):
            counts["ignored"] += len(txns)
            continue
        account_type = choice.get("account_type") or BANK
        start = choice.get("start_date") or ""
        for t in txns:
            if not _is_posted(t):
                counts["pending"] += 1
                continue
            day = _txn_date(t)
            try:
                amount = round(float(Decimal(str(t.get("amount")))), 2)
            except (InvalidOperation, ValueError, TypeError):
                amount = None
            if day is None or amount is None or t.get("id") in (None, ""):
                counts["unreadable"] += 1
                continue
            if start and day < start:
                counts["before_start"] += 1
                continue
            # Rules are written against the bank's own wording, which the Bridge sends as `memo` (beyond the protocol);
            # `description` and `payee` are its tidied versions ("JOHNS FISHIN SHACK BAIT" / "Fishing bait").
            texts = [sanitize(t.get(f), 200) for f in ("memo", "description", "payee")]
            texts = [x for i, x in enumerate(texts) if x and x not in texts[:i]]
            text = texts[0] if texts else "(no description)"
            m = re.search(r"\bCHECK\s*#?\s*(\d+)\b", text.upper())
            # Rules can match card payments; bank files mark those in a Type column, which SimpleFIN doesn't have.
            trans_type = "Payment" if account_type == CREDIT and amount > 0 and _PAYMENT.search(text.upper()) else ""
            rows.append({
                "txn_id": txn_id_for(host, a.get("conn_id"), a.get("id"), t.get("id")), "legacy_id": None,
                "date": day, "raw_name": text, "amount": amount, "check_num": m.group(1) if m else "",
                "account_type": account_type, "trans_type": trans_type, "source_file": SOURCE_LABEL,
                "account_name": choice.get("name") or "", "other_texts": texts[1:],
            })
    return rows, counts


# ---------------------------------------------------------------------------
# Requests per day, and what range the next sync asks for
# ---------------------------------------------------------------------------


def requests_today(conn: Any, today: Optional[date] = None) -> int:
    today = today or date.today()
    try:
        data = json.loads(store.get_setting(conn, REQUESTS_KEY) or "{}")
    except ValueError:
        return 0
    return int(data.get("count") or 0) if data.get("day") == today.isoformat() else 0


def count_request(conn: Any, today: Optional[date] = None) -> int:
    today = today or date.today()
    n = requests_today(conn, today) + 1
    store.set_setting(conn, REQUESTS_KEY, json.dumps({"day": today.isoformat(), "count": n}))
    conn.commit()
    return n


def sync_range(conn: Any, today: Optional[date] = None) -> Tuple[date, date, bool]:
    """(start, end, cut_short) for the next sync. The first one covers the last FIRST_SYNC_DAYS; later ones start a few days
    before the last saved sync, or at an account's starting day if that was moved earlier (backfill_from). cut_short says the gap was too long and the oldest part wasn't asked for."""
    today = today or date.today()
    end = today + timedelta(days=1)
    through = store.get_setting(conn, SYNCED_THROUGH_KEY)
    try:
        start = date.fromisoformat(through) - timedelta(days=OVERLAP_DAYS) if through else end - timedelta(days=FIRST_SYNC_DAYS)
    except ValueError:
        start = end - timedelta(days=FIRST_SYNC_DAYS)
    back = backfill_from(conn)
    if back:
        start = min(start, back)
    earliest = end - timedelta(days=MAX_DAYS * MAX_CHUNKS)
    cut_short = start < earliest
    return max(start, earliest), end, cut_short


def backfill_from(conn: Any) -> Optional[date]:
    """The earliest starting day moved back since the last saved sync, or None."""
    try:
        text = store.get_setting(conn, BACKFILL_KEY)
        return date.fromisoformat(text) if text else None
    except ValueError:
        return None


def note_saved(conn: Any, start: date, end: date) -> None:
    """A download of [start, end) was saved: the next sync starts a few days before its end, and an earlier
    starting day that it reached no longer needs reaching back for."""
    store.set_setting(conn, SYNCED_THROUGH_KEY, (end - timedelta(days=1)).isoformat())
    back = backfill_from(conn)
    if back and start <= back:
        store.set_setting(conn, BACKFILL_KEY, "")
