"""Release notes ("What's new") and the version details sent with feedback.

Notes live in releases.json, newest first. Each budget remembers the last release it was shown,
so the popup appears once per release and survives a git pull.
"""

import json
import os
import platform
import re
import subprocess
import sys
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

from . import VERSION, paths, store

RELEASES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "releases.json")
SEEN_KEY = "last_seen_release"
FEEDBACK_EMAIL = "scott.reiling@gmail.com"
FEEDBACK_SUBJECT = "EVBudget feedback"
MAX_MAILTO = 1900   # Windows refuses to open much longer links
VERSION_ID = re.compile(r"^[0-9A-Za-z.\-]{1,20}$")


def current_version() -> str:
    return VERSION


NOTE_TYPES = ("new", "improved", "fixed")
NEVER_SHOWN_LIMIT = 3   # a budget that has never been shown notes gets the latest few, not the whole history


def version_key(version: Any) -> Tuple[int, ...]:
    """"3.10" sorts after "3.9". Anything that isn't dotted numbers sorts before every real version."""
    try:
        return tuple(int(x) for x in str(version).split("."))
    except ValueError:
        return ()


def parse(data: Any) -> List[Dict[str, Any]]:
    """Release notes as the page uses them, newest first. A note is a sentence, or {"type": new|improved|fixed, "text": ...}.
    Anything malformed is skipped rather than breaking the dialog."""
    out = []
    for r in data if isinstance(data, list) else []:
        if not (isinstance(r, dict) and isinstance(r.get("version"), str) and isinstance(r.get("notes"), list)):
            continue
        notes = []
        for n in r["notes"]:
            if isinstance(n, dict) and n.get("text"):
                kind = n.get("type") if n.get("type") in NOTE_TYPES else ""
                notes.append({"type": kind, "text": str(n["text"])})
            elif isinstance(n, str) and n.strip():
                notes.append({"type": "", "text": n})
        out.append({"version": r["version"], "date": str(r.get("date") or ""), "title": str(r.get("title") or ""), "notes": notes})
    out.sort(key=lambda r: version_key(r["version"]), reverse=True)
    return out


def load() -> List[Dict[str, Any]]:
    """The release list, newest first. A missing or broken file gives an empty list rather than an error."""
    try:
        with open(RELEASES_PATH, encoding="utf-8") as f:
            return parse(json.load(f))
    except (OSError, ValueError):
        return []


def shipped() -> List[Dict[str, Any]]:
    """Releases up to the running version, so notes written ahead of a version bump never show early."""
    return [r for r in load() if version_key(r["version"]) <= version_key(current_version())]


def newer_than(releases: List[Dict[str, Any]], version: str) -> List[Dict[str, Any]]:
    """Every release after the given version, newest first: 3.4 to 3.6 gives 3.6 and 3.5."""
    floor = version_key(version)
    return [r for r in releases if version_key(r["version"]) > floor]


def unseen(releases: List[Dict[str, Any]], last_seen: str) -> List[Dict[str, Any]]:
    """What this budget hasn't been shown yet: every release after the last one it saw, even a version
    that has no entry of its own. A budget that has never been shown any gets the latest few."""
    if last_seen and version_key(last_seen):
        return newer_than(releases, last_seen)
    return releases[:NEVER_SHOWN_LIMIT]


def payload(conn: Any, brand_new: bool) -> Dict[str, Any]:
    """Everything the page needs. A brand-new empty budget is about to meet the setup walkthrough,
    so it is kept up to date silently instead of opening with a popup about changes it never saw."""
    releases = shipped()
    last = store.get_setting(conn, SEEN_KEY)
    if brand_new and releases:
        if last != releases[0]["version"]:
            store.set_setting(conn, SEEN_KEY, releases[0]["version"])
        last = releases[0]["version"]
        new = []
    else:
        new = unseen(releases, last)
    return {"current": current_version(), "releases": releases, "unseen": new, "from_version": last or None, "last_seen": last or None}


def mark_seen(conn: Any, version: str) -> None:
    if not isinstance(version, str) or not VERSION_ID.match(version) or version not in {r["version"] for r in load()}:
        raise ValueError("Unknown release")
    store.set_setting(conn, SEEN_KEY, version)


# ---------------------------------------------------------------------------
# Details that help identify a reported problem
# ---------------------------------------------------------------------------

_git_cache: Optional[Dict[str, Any]] = None


def _git(*args: str) -> Optional[str]:
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        r = subprocess.run(["git", "-C", paths.ROOT_DIR, *args], capture_output=True, text=True, timeout=4, creationflags=flags)
        return r.stdout.strip() if r.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def git_info() -> Dict[str, Any]:
    """Commit, branch, and date of the copy that is running. Empty when this isn't a git checkout. Looked up once."""
    global _git_cache
    if _git_cache is None:
        commit = _git("rev-parse", "--short", "HEAD")
        info: Dict[str, Any] = {}
        if commit:
            info = {
                "commit": commit, "branch": _git("rev-parse", "--abbrev-ref", "HEAD"),
                "commit_date": _git("log", "-1", "--format=%cs"), "local_changes": bool(_git("status", "--porcelain")),
            }
        _git_cache = info
    return _git_cache


def feedback_url(body: str) -> str:
    """A mailto link to the feedback address. The body is cut from the end if the link would be too long to open."""
    def build(text: str) -> str:
        q = urllib.parse.urlencode({"subject": FEEDBACK_SUBJECT, "body": text}, quote_via=urllib.parse.quote)
        return f"mailto:{FEEDBACK_EMAIL}?{q}"
    url = build(body)
    while len(url) > MAX_MAILTO and body:
        body = body[:max(0, len(body) - max(40, (len(url) - MAX_MAILTO) // 2))]
        url = build(body)
    return url


def open_external(url: str) -> None:
    """Hand a link to the operating system, so it opens in the user's mail app or default browser as its own window,
    not inside the EVBudget window (which has no back button)."""
    if sys.platform == "win32":
        os.startfile(url)  # type: ignore[attr-defined]
    else:
        opener = "open" if sys.platform == "darwin" else "xdg-open"
        subprocess.Popen([opener, url], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def about(conn: Any) -> Dict[str, Any]:
    row = conn.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()
    releases = load()
    return {
        "version": current_version(),
        "latest_release": releases[0]["version"] if releases else None,
        "release_date": releases[0]["date"] if releases else None,
        "schema_version": int(row[0]) if row and str(row[0]).isdigit() else None,
        "git": git_info(),
        "python": platform.python_version(),
        "os": f"{platform.system()} {platform.release()}".strip(),
        "feedback_email": FEEDBACK_EMAIL,
        "frozen": bool(getattr(sys, "frozen", False)),
    }
