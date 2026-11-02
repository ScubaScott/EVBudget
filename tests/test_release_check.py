"""The pre-push release check: a push that changes the app needs a new version, notes, and a change log line."""

import importlib.util
import json
import os
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("check_release", os.path.join(os.path.dirname(HERE), "tools", "check_release.py"))
cr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cr)


class PureTests(unittest.TestCase):
    def test_next_version(self):
        self.assertEqual(cr.next_version("3.5"), "3.6")
        self.assertEqual(cr.next_version("3.9"), "3.10")
        self.assertEqual(cr.next_version("3.6.1"), "3.6.2")

    def test_which_files_count_as_the_app(self):
        for path in ("evbudget/api.py", "web/js/budget.js", "web/dashboard.css", "budget_dashboard.py", "requirements.txt"):
            self.assertTrue(cr.is_app_file(path), path)
        for path in ("tests/test_api.py", "AGENTS.md", "CHANGELOG.md", "evbudget/releases.json", "WALKTHROUGH_PLAN.md", "tools/check_release.py"):
            self.assertFalse(cr.is_app_file(path), path)


class RepoTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "t@example.com")
        self.git("config", "user.name", "T")
        self.write("evbudget/__init__.py", 'VERSION = "1.0"\n')
        self.write("evbudget/api.py", "x = 1\n")
        self.release("1.0")
        self.base = self.commit("first")

    def tearDown(self):
        self.tmp.cleanup()

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.dir, capture_output=True, text=True, check=True).stdout.strip()

    def write(self, rel, text):
        path = os.path.join(self.dir, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)

    def release(self, version, notes=True, log=True):
        existing = []
        p = os.path.join(self.dir, "evbudget", "releases.json")
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                existing = json.load(f)
        existing.insert(0, {"version": version, "date": "2026-01-01", "title": "T", "notes": [{"type": "new", "text": "n"}] if notes else []})
        self.write("evbudget/releases.json", json.dumps(existing))
        old, log_path = "", os.path.join(self.dir, "CHANGELOG.md")
        if os.path.exists(log_path):
            with open(log_path, encoding="utf-8") as f:
                old = f.read()
        self.write("CHANGELOG.md", (f"- **{version}** (2026-01-01): x\n" if log else "") + old)

    def commit(self, msg):
        self.git("add", "-A")
        self.git("commit", "-q", "-m", msg)
        return self.git("rev-parse", "HEAD")

    def problems(self):
        return cr.check(self.base, "HEAD", self.dir)

    def test_app_change_without_a_bump_is_stopped(self):
        self.write("web/app.js", "a\n")
        self.commit("change")
        out = self.problems()
        self.assertTrue(any("already on GitHub" in p and "1.1" in p for p in out), out)

    def test_bug_fix_needs_a_bump_too(self):
        self.write("evbudget/api.py", "x = 2\n")
        self.commit("fix")
        self.assertTrue(self.problems())

    def test_bump_with_notes_and_log_line_passes(self):
        self.write("evbudget/api.py", "x = 2\n")
        self.write("evbudget/__init__.py", 'VERSION = "1.1"\n')
        self.release("1.1")
        self.commit("release")
        self.assertEqual(self.problems(), [])

    def test_bump_without_notes_or_log_line_is_stopped(self):
        self.write("evbudget/api.py", "x = 2\n")
        self.write("evbudget/__init__.py", 'VERSION = "1.1"\n')
        self.commit("forgot notes")
        out = self.problems()
        self.assertEqual(len(out), 2, out)
        self.assertTrue(any("releases.json" in p for p in out) and any("CHANGELOG" in p for p in out))

    def test_empty_notes_do_not_count(self):
        self.write("evbudget/api.py", "x = 2\n")
        self.write("evbudget/__init__.py", 'VERSION = "1.1"\n')
        self.release("1.1", notes=False)
        self.commit("empty")
        self.assertTrue(any("releases.json" in p for p in self.problems()))

    def test_tests_and_docs_only_need_no_bump(self):
        self.write("tests/test_x.py", "pass\n")
        self.write("AGENTS.md", "rules\n")
        self.commit("tests and docs")
        self.assertEqual(self.problems(), [])

    def test_ten_sorts_after_nine(self):
        self.write("evbudget/__init__.py", 'VERSION = "1.9"\n')
        self.release("1.9")
        self.base = self.commit("nine")
        self.write("evbudget/api.py", "x = 3\n")
        self.write("evbudget/__init__.py", 'VERSION = "1.10"\n')
        self.release("1.10")
        self.commit("ten")
        self.assertEqual(self.problems(), [])

    def test_pre_push_only_looks_at_main(self):
        self.write("web/app.js", "a\n")
        head = self.commit("change")
        cwd = os.getcwd()
        os.chdir(self.dir)
        try:
            self.assertEqual(cr.pre_push([f"refs/heads/feature {head} refs/heads/feature {self.base}"]), 0)
            self.assertEqual(cr.pre_push([f"refs/heads/main {head} refs/heads/main {self.base}"]), 1)
            self.assertEqual(cr.pre_push([f"refs/heads/main {head} refs/heads/main {'0' * 40}"]), 0)   # a brand-new branch
        finally:
            os.chdir(cwd)


if __name__ == "__main__":
    unittest.main()
