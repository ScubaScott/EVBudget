"""Checking the git repository for a newer version, applying it, and restarting the app afterwards.

Updates are fast-forward only, so they never merge over or overwrite local edits: if the copy has changes
that are in the way, the update stops with a message instead. Git runs in a background thread because the
server handles one request at a time and a slow network must not freeze the page.
"""

import json
import os
import subprocess
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

CHECK_TIMEOUT = 20      # seconds for a fetch
PULL_TIMEOUT = 120
PIP_TIMEOUT = 240
CHECK_INTERVAL = 30 * 60
RESTART_FLAG = "--restart-port"
RELEASES_FILE = "evbudget/releases.json"


class UpdateError(Exception):
    """Something went wrong that the user can read and act on."""


def _git_env() -> Dict[str, str]:
    # Never wait for a password prompt: a copy that needs sign-in just reports it can't update.
    return dict(os.environ, GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never", LC_ALL="C")


def run_git(repo: str, *args: str, timeout: int = CHECK_TIMEOUT) -> Tuple[int, str, str]:
    try:
        r = subprocess.run(
            ["git", "-C", repo, *args], capture_output=True, text=True, timeout=timeout, env=_git_env(),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except FileNotFoundError:
        return 127, "", "Git is not installed."
    except subprocess.TimeoutExpired:
        return 124, "", "Git took too long to answer."
    except OSError as exc:
        return 126, "", str(exc)


def _first_line(text: str) -> str:
    return next((ln.strip() for ln in text.splitlines() if ln.strip()), "")


def _top_release(text: str) -> Dict[str, str]:
    try:
        data = json.loads(text)
        top = data[0]
        return {"version": str(top.get("version") or ""), "title": str(top.get("title") or "")}
    except (ValueError, IndexError, TypeError, AttributeError):
        return {"version": "", "title": ""}


class Updater:
    def __init__(self, repo_dir: str, remote: str = "origin", branch: str = "main") -> None:
        self.repo, self.remote, self.branch = repo_dir, remote, branch
        self.restart_needed = False
        self._lock = threading.Lock()
        self._checking = False
        self._state: Dict[str, Any] = {"state": "unknown", "message": "", "behind": 0, "latest_version": "", "latest_title": "", "checked_at": None}

    # ------------------------------------------------------------------ status

    def status(self) -> Dict[str, Any]:
        out = dict(self._state, checking=self._checking)
        if self.restart_needed:
            out["state"] = "restart"
            out["message"] = "Updated. Restart EVBudget to finish."
        return out

    def _set(self, state: str, message: str = "", **extra: Any) -> Dict[str, Any]:
        self._state = {"state": state, "message": message, "behind": 0, "latest_version": "", "latest_title": "",
                       "checked_at": time.time(), **extra}
        return self._state

    # ------------------------------------------------------------------ checking

    def _check_locked(self) -> Dict[str, Any]:
        code, branch, _ = run_git(self.repo, "rev-parse", "--abbrev-ref", "HEAD")
        if code != 0:
            return self._set("unavailable", "This copy of EVBudget isn't a git checkout, so it can't update itself.")
        if branch != self.branch:
            return self._set("unavailable", f"Updates only apply to the {self.branch} branch (this copy is on {branch}).")
        code, _, err = run_git(self.repo, "fetch", "--quiet", self.remote, self.branch, timeout=CHECK_TIMEOUT)
        if code != 0:
            return self._set("error", "Couldn't reach the update server: " + (_first_line(err) or "no answer") + ".")
        ref = f"{self.remote}/{self.branch}"
        code, counts, err = run_git(self.repo, "rev-list", "--left-right", "--count", f"HEAD...{ref}")
        if code != 0:
            return self._set("error", "Couldn't compare with the update server: " + _first_line(err))
        ahead, behind = (int(n) for n in counts.split())
        if behind == 0:
            return self._set("current", "You're up to date.")
        if ahead > 0:
            return self._set("unavailable", "This copy has changes of its own that aren't on the server, so it can't update automatically.")
        _, text, _ = run_git(self.repo, "show", f"{ref}:{RELEASES_FILE}")
        latest = _top_release(text)
        return self._set("available", "An update is ready.", behind=behind, latest_version=latest["version"], latest_title=latest["title"])

    def check(self) -> Dict[str, Any]:
        with self._lock:
            try:
                return self._check_locked()
            finally:
                self._checking = False

    def check_async(self) -> None:
        if self._checking:
            return
        self._checking = True
        threading.Thread(target=self.check, daemon=True).start()

    def start(self, interval: float = CHECK_INTERVAL, first_delay: float = 4.0) -> None:
        """Check shortly after startup, then every half hour while the app runs."""
        def loop() -> None:
            time.sleep(first_delay)
            while True:
                self.check()
                time.sleep(interval)
        threading.Thread(target=loop, daemon=True).start()

    # ------------------------------------------------------------------ updating

    def apply(self) -> Dict[str, Any]:
        with self._lock:
            state = self._check_locked()
            self._checking = False
            if state["state"] == "current":
                return {"updated": False, "message": "Already up to date."}
            if state["state"] != "available":
                raise UpdateError(state["message"])
            _, old, _ = run_git(self.repo, "rev-parse", "HEAD")
            code, out, err = run_git(self.repo, "pull", "--ff-only", "--quiet", self.remote, self.branch, timeout=PULL_TIMEOUT)
            if code != 0:
                text = err or out
                if "overwritten" in text or "local changes" in text:
                    raise UpdateError("Files in this copy have been edited and are in the way, so it can't update automatically. "
                                      "Ask whoever manages EVBudget to update it.")
                raise UpdateError("The update didn't go through: " + (_first_line(text) or "unknown error"))
            _, new, _ = run_git(self.repo, "rev-parse", "HEAD")
            _, changed, _ = run_git(self.repo, "diff", "--name-only", old, new)
            packages = None
            if "requirements.txt" in changed.splitlines():
                packages = self._install_requirements()
            self.restart_needed = True
            try:
                with open(os.path.join(self.repo, RELEASES_FILE), encoding="utf-8") as f:
                    latest = _top_release(f.read())
            except OSError:
                latest = {"version": state.get("latest_version", ""), "title": state.get("latest_title", "")}
            self._set("current", "Updated.")
            return {"updated": True, "version": latest["version"], "title": latest["title"], "packages": packages,
                    "message": "Updated. Restart EVBudget to finish."}

    def _install_requirements(self) -> str:
        try:
            r = subprocess.run(
                [sys.executable, "-m", "pip", "install", "--quiet", "--disable-pip-version-check", "-r",
                 os.path.join(self.repo, "requirements.txt")],
                capture_output=True, text=True, timeout=PIP_TIMEOUT, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return "installed" if r.returncode == 0 else "failed"
        except (OSError, subprocess.SubprocessError):
            return "failed"


# ---------------------------------------------------------------------------
# Restarting
# ---------------------------------------------------------------------------


def restart_command(script: str, port: int, argv: List[str]) -> List[str]:
    """The command that starts a fresh copy of this app on the same port. The new copy takes over the open window."""
    args: List[str] = []
    skip = False
    for a in argv:
        if skip:
            skip = False
        elif a == RESTART_FLAG:
            skip = True
        elif not a.startswith(RESTART_FLAG + "="):
            args.append(a)
    return [sys.executable, script, *args, RESTART_FLAG, str(port)]


def spawn_restart(script: str, port: int, argv: List[str]) -> None:
    kwargs: Dict[str, Any] = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=os.path.dirname(script))
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(restart_command(script, port, argv), **kwargs)


def restart_port(argv: List[str]) -> Optional[int]:
    """The port a restarted copy should take over, from its command line."""
    for i, a in enumerate(argv):
        try:
            if a == RESTART_FLAG:
                return int(argv[i + 1])
            if a.startswith(RESTART_FLAG + "="):
                return int(a.split("=", 1)[1])
        except (IndexError, ValueError):
            return None
    return None
