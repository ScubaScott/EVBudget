"""Release check: a push to main that changes the app needs a new version, release notes, and a change log line.

Used by .githooks/pre-push (git runs it before every push) and by the /release command.

    python tools/check_release.py --pre-push <remote> <url>     (git feeds it the refs on stdin)
    python tools/check_release.py --against origin/main         (compare HEAD with a branch)
    python tools/check_release.py --status                      (what is on origin/main, what would the next version be)

Standard library only. If anything unexpected goes wrong the check lets the push through and says why, rather than
blocking work over a broken script. Override on purpose with: git push --no-verify
"""

import json
import re
import subprocess
import sys
from typing import List, Optional, Tuple

ZERO = "0" * 40
APP_FILES = ("budget_dashboard.py", "requirements.txt", "launch_dashboard.bat", "launch_dashboard.command", "setup_mac.sh")
APP_DIRS = ("web/",)


def git(*args: str, cwd: Optional[str] = None) -> Tuple[int, str]:
    r = subprocess.run(["git", *args], capture_output=True, text=True, cwd=cwd, encoding="utf-8")
    return r.returncode, r.stdout


def version_key(v: str) -> Tuple[int, ...]:
    try:
        return tuple(int(x) for x in str(v).split("."))
    except ValueError:
        return ()


def next_version(v: str) -> str:
    """3.5 -> 3.6, and 3.6.1 -> 3.6.2. A bigger jump (4.0) is a choice for a person to make."""
    parts = list(version_key(v)) or [0, 0]
    if len(parts) <= 2:
        parts = (parts + [0])[:2]
        parts[1] += 1
    else:
        parts[-1] += 1
    return ".".join(str(p) for p in parts)


def is_app_file(path: str) -> bool:
    if path in APP_FILES or path.startswith(APP_DIRS):
        return True
    return path.startswith("evbudget/") and path.endswith(".py")


def file_at(rev: str, path: str, cwd: Optional[str]) -> Optional[str]:
    code, out = git("show", f"{rev}:{path}", cwd=cwd)
    return out if code == 0 else None


def version_at(rev: str, cwd: Optional[str]) -> Optional[str]:
    text = file_at(rev, "evbudget/__init__.py", cwd)
    m = re.search(r"""^VERSION\s*=\s*["']?([0-9][0-9.]*)""", text or "", re.M)
    return m.group(1) if m else None


def check(base: str, head: str, cwd: Optional[str] = None) -> List[str]:
    """Problems with pushing head on top of base. Empty means fine."""
    code, names = git("diff", "--name-only", base, head, cwd=cwd)
    if code != 0:
        return []
    changed = [n for n in names.splitlines() if is_app_file(n)]
    if not changed:
        return []   # tests, docs, notes: no new version needed
    base_v, head_v = version_at(base, cwd), version_at(head, cwd)
    if not head_v:
        return ["Couldn't read VERSION from evbudget/__init__.py."]
    problems = []
    if base_v and version_key(head_v) <= version_key(base_v):
        problems.append(f"Version {head_v} is already on GitHub, and the app changed since ({len(changed)} file{'' if len(changed) == 1 else 's'}). "
                        f"Bump VERSION to {next_version(base_v)} (or higher).")
    try:
        entries = json.loads(file_at(head, "evbudget/releases.json", cwd) or "[]")
    except ValueError:
        entries = []
    if not any(isinstance(e, dict) and e.get("version") == head_v and e.get("notes") for e in entries):
        problems.append(f"evbudget/releases.json needs an entry for {head_v} with release notes.")
    log = file_at(head, "CHANGELOG.md", cwd) or ""
    if not any(ln.startswith(f"- **{head_v}**") for ln in log.splitlines()):
        problems.append(f"CHANGELOG.md needs a one-line entry starting '- **{head_v}**'.")
    return problems


def report(problems: List[str]) -> None:
    sys.stderr.write("\nPush stopped: this needs a new version first.\n")
    for p in problems:
        sys.stderr.write(f"  - {p}\n")
    sys.stderr.write("Ask Claude to run /release, or fix those and push again. (To push anyway: git push --no-verify)\n\n")


def pre_push(stdin_lines: List[str]) -> int:
    problems: List[str] = []
    for line in stdin_lines:
        parts = line.split()
        if len(parts) != 4:
            continue
        _, local_sha, remote_ref, remote_sha = parts
        if remote_ref != "refs/heads/main" or local_sha == ZERO or remote_sha == ZERO:
            continue
        if git("cat-file", "-e", remote_sha)[0] != 0:
            sys.stderr.write("Release check skipped: the remote's latest commit isn't here yet (run git fetch).\n")
            continue
        problems += check(remote_sha, local_sha)
    if problems:
        report(problems)
        return 1
    return 0


def status() -> int:
    git("fetch", "--quiet", "origin", "main")
    base_v, head_v = version_at("origin/main", None), version_at("HEAD", None)
    print(f"origin/main is at version {base_v}; this checkout is at {head_v}.")
    if base_v:
        print(f"The next release would be {next_version(base_v)}.")
    problems = check("origin/main", "HEAD")
    print("Needs a bump / notes: " + ("; ".join(problems) if problems else "no"))
    return 0


def main(argv: List[str]) -> int:
    try:
        if argv[:1] == ["--pre-push"]:
            return pre_push(sys.stdin.read().splitlines())
        if argv[:1] == ["--against"] and len(argv) > 1:
            problems = check(argv[1], "HEAD")
            if problems:
                report(problems)
                return 1
            print("Release check passed.")
            return 0
        if argv[:1] == ["--status"]:
            return status()
        sys.stderr.write(__doc__)
        return 2
    except Exception as exc:   # never block a push because this script broke
        sys.stderr.write(f"Release check skipped (it hit an error: {exc}).\n")
        return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
