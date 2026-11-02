"""Checking for and applying updates (against real throwaway git repositories), restarting, and the feedback email link."""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
import urllib.parse
from unittest import mock

from helpers import TempAppCase
from evbudget import releases, updater
from evbudget.http_util import HttpError

GIT_OK = shutil.which("git") is not None
ENV = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, env=ENV, capture_output=True, text=True, check=True).stdout.strip()


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def notes(version, title):
    return json.dumps([{"version": version, "date": "2026-11-02", "title": title, "notes": ["x"]}])


@unittest.skipUnless(GIT_OK, "git is not installed")
class UpdaterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="evb_upd_")
        self.origin = os.path.join(self.tmp, "origin.git")
        self.dev = os.path.join(self.tmp, "dev")
        self.app = os.path.join(self.tmp, "app")
        git(self.tmp, "init", "--bare", "-b", "main", self.origin)
        git(self.tmp, "clone", "-q", self.origin, self.dev)
        git(self.dev, "checkout", "-q", "-b", "main")
        write(os.path.join(self.dev, "evbudget", "releases.json"), notes("1.0", "First"))
        write(os.path.join(self.dev, "app.txt"), "one")
        self.publish("first")
        git(self.tmp, "clone", "-q", self.origin, self.app)
        self.up = updater.Updater(self.app, running_version="1.0")

    def tearDown(self):
        def unlock(func, path, _):
            os.chmod(path, 0o700)
            func(path)
        shutil.rmtree(self.tmp, onerror=unlock)

    def publish(self, message):
        git(self.dev, "add", "-A")
        git(self.dev, "commit", "-q", "-m", message)
        git(self.dev, "push", "-q", "origin", "main")

    def test_up_to_date(self):
        self.assertEqual(self.up.check()["state"], "current")

    def test_update_available_names_the_new_version(self):
        write(os.path.join(self.dev, "evbudget", "releases.json"), notes("1.1", "Shiny"))
        self.publish("second")
        st = self.up.check()
        self.assertEqual((st["state"], st["behind"], st["latest_version"], st["latest_title"]), ("available", 1, "1.1", "Shiny"))

    def test_apply_pulls_and_asks_for_a_restart(self):
        write(os.path.join(self.dev, "app.txt"), "two")
        write(os.path.join(self.dev, "evbudget", "releases.json"), notes("1.1", "Shiny"))
        self.publish("second")
        res = self.up.apply()
        self.assertTrue(res["updated"])
        self.assertEqual((res["version"], res["packages"]), ("1.1", None))
        with open(os.path.join(self.app, "app.txt")) as f:
            self.assertEqual(f.read(), "two")
        self.assertEqual(self.up.status()["state"], "restart")
        self.assertEqual(self.up.apply()["updated"], False)   # nothing more to get

    def test_changed_requirements_are_installed(self):
        write(os.path.join(self.dev, "requirements.txt"), "pypdf>=4.0\n")
        self.publish("deps")
        with mock.patch.object(updater.Updater, "_install_requirements", return_value="installed") as inst:
            self.assertEqual(self.up.apply()["packages"], "installed")
        inst.assert_called_once()

    def test_local_edits_in_the_way_stop_the_update(self):
        write(os.path.join(self.dev, "app.txt"), "server change")
        self.publish("second")
        write(os.path.join(self.app, "app.txt"), "my own edit")
        with self.assertRaises(updater.UpdateError) as ctx:
            self.up.apply()
        self.assertIn("in the way", str(ctx.exception))
        with open(os.path.join(self.app, "app.txt")) as f:
            self.assertEqual(f.read(), "my own edit")   # never overwritten

    def test_a_copy_with_its_own_commits_is_not_updated(self):
        write(os.path.join(self.app, "mine.txt"), "x")
        git(self.app, "add", "-A")
        git(self.app, "commit", "-q", "-m", "mine")
        write(os.path.join(self.dev, "app.txt"), "two")
        self.publish("second")
        st = self.up.check()
        self.assertEqual(st["state"], "unavailable")
        self.assertIn("changes of its own", st["message"])

    def test_other_branches_and_non_git_folders_are_left_alone(self):
        git(self.app, "checkout", "-q", "-b", "feature")
        self.assertEqual(self.up.check()["state"], "unavailable")
        plain = os.path.join(self.tmp, "plain")
        os.makedirs(plain)
        self.assertEqual(updater.Updater(plain).check()["state"], "unavailable")

    def test_unreachable_server_is_an_error_not_a_crash(self):
        git(self.app, "remote", "set-url", "origin", os.path.join(self.tmp, "nowhere.git"))
        st = self.up.check()
        self.assertEqual(st["state"], "error")
        self.assertIn("Couldn't reach", st["message"])


class RestartTests(unittest.TestCase):
    def test_restart_command_keeps_options_and_replaces_the_port_flag(self):
        cmd = updater.restart_command("/x/budget_dashboard.py", 8061, ["--keep-running", "--restart-port", "8060"])
        self.assertEqual(cmd[1:], ["/x/budget_dashboard.py", "--keep-running", "--restart-port", "8061"])
        cmd = updater.restart_command("/x/b.py", 8060, ["--restart-port=8059", "--no-app-window"])
        self.assertEqual(cmd[1:], ["/x/b.py", "--no-app-window", "--restart-port", "8060"])

    def test_restart_port_is_read_from_the_command_line(self):
        self.assertEqual(updater.restart_port(["--restart-port", "8061"]), 8061)
        self.assertEqual(updater.restart_port(["--restart-port=8062"]), 8062)
        self.assertIsNone(updater.restart_port(["--keep-running"]))
        self.assertIsNone(updater.restart_port(["--restart-port", "abc"]))


class FeedbackLinkTests(unittest.TestCase):
    def test_link_goes_to_the_feedback_address_with_the_subject(self):
        url = releases.feedback_url("line one\nline two & more")
        self.assertTrue(url.startswith("mailto:scott.reiling@gmail.com?"))
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        self.assertEqual(q["subject"], ["EVBudget feedback"])
        self.assertEqual(q["body"], ["line one\nline two & more"])
        self.assertNotIn("+", url.split("?", 1)[1].replace("%2B", ""))   # spaces are %20, which every mail app reads

    def test_a_very_long_body_is_cut_to_fit(self):
        url = releases.feedback_url("word " * 2000)
        self.assertLessEqual(len(url), releases.MAX_MAILTO)
        self.assertTrue(urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["body"][0].startswith("word word"))


class UpdateApiTests(TempAppCase):
    def test_changing_endpoints_need_a_json_request(self):
        for fn in (self.app.update_check, self.app.update_apply, self.app.update_restart, self.app.feedback_open):
            with self.assertRaises(HttpError):
                fn(__import__("evbudget.http_util", fromlist=["Request"]).Request("POST", "/t", {}, {"Content-Type": "text/plain"}, b"x"))

    def test_status_is_available_without_git_work(self):
        st = self.call(self.app.update_status, method="GET")
        self.assertIn("state", st)

    def test_feedback_is_handed_to_the_operating_system(self):
        with mock.patch.object(releases, "open_external") as opened:
            self.call(self.app.feedback_open, {"body": "hello"})
        self.assertTrue(opened.call_args[0][0].startswith("mailto:scott.reiling@gmail.com?subject=EVBudget%20feedback"))

    def test_health_has_a_boot_id_that_differs_between_starts(self):
        from evbudget import api
        other = api.App(db_path=self.db, input_dir=self.input_dir, web_dir=self.app.web_dir)
        mine = self.call(self.app.health, method="GET")["boot"]
        self.assertTrue(mine)
        self.assertNotEqual(mine, other.boot_id)


if __name__ == "__main__":
    unittest.main()
