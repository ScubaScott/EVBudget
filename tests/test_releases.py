"""What's new popup logic, the release notes file, and the version details sent with feedback."""

import os
import unittest

from helpers import TempAppCase
from evbudget import VERSION, releases
from evbudget.http_util import HttpError

R = [{"version": "3.5"}, {"version": "3.4"}, {"version": "3.3"}]


class ReleaseFileTests(unittest.TestCase):
    def test_every_entry_is_complete_and_newest_first(self):
        rels = releases.load()
        self.assertTrue(rels)
        for r in rels:
            self.assertTrue(r["version"] and r["date"] and r["title"] and r["notes"], r)
        nums = [tuple(int(x) for x in r["version"].split(".")) for r in rels]
        self.assertEqual(nums, sorted(nums, reverse=True))

    def test_current_version_has_release_notes(self):
        # Bumping VERSION without writing notes in evbudget/releases.json fails here, so notes aren't forgotten.
        self.assertEqual(releases.load()[0]["version"], VERSION)


class UnseenTests(unittest.TestCase):
    def test_nothing_new_when_up_to_date(self):
        self.assertEqual(releases.unseen(R, "3.5"), [])

    def test_everything_since_the_last_seen_release(self):
        self.assertEqual([r["version"] for r in releases.unseen(R, "3.3")], ["3.5", "3.4"])

    def test_skipping_versions_shows_every_release_in_between(self):
        # Updating from 3.2 straight to 3.5 shows 3.3, 3.4 and 3.5, even though 3.2 has no entry of its own
        self.assertEqual([r["version"] for r in releases.unseen(R, "3.2")], ["3.5", "3.4", "3.3"])
        self.assertEqual([r["version"] for r in releases.unseen(R, "3.4")], ["3.5"])

    def test_versions_compare_as_numbers_not_text(self):
        r = [{"version": "3.10"}, {"version": "3.9"}, {"version": "3.2"}]
        self.assertEqual([x["version"] for x in releases.unseen(r, "3.9")], ["3.10"])
        self.assertEqual([x["version"] for x in releases.newer_than(r, "3.2")], ["3.10", "3.9"])

    def test_never_shown_gets_only_the_latest_few(self):
        many = [{"version": f"3.{n}"} for n in range(9, 0, -1)]
        self.assertEqual(len(releases.unseen(many, "")), releases.NEVER_SHOWN_LIMIT)
        self.assertEqual(releases.unseen([], ""), [])

    def test_notes_can_be_plain_or_typed_and_bad_ones_are_skipped(self):
        got = releases.parse([
            {"version": "1.0", "notes": ["Plain", {"type": "fixed", "text": "Typed"}, {"type": "weird", "text": "Odd"}, "", 7]},
            {"version": 2}, "junk",
        ])
        self.assertEqual(got[0]["notes"], [{"type": "", "text": "Plain"}, {"type": "fixed", "text": "Typed"}, {"type": "", "text": "Odd"}])
        self.assertEqual(len(got), 1)


class ChangeLogTests(unittest.TestCase):
    def test_every_release_has_a_one_line_entry_in_the_change_log(self):
        # AGENTS.md: each version gets a short line in CHANGELOG.md as well as notes in releases.json
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "CHANGELOG.md")
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
        for r in releases.load():
            entry = [ln for ln in lines if ln.startswith(f"- **{r['version']}**")]
            self.assertEqual(len(entry), 1, f"CHANGELOG.md needs exactly one line starting '- **{r['version']}**'")


class ReleaseApiTests(TempAppCase):
    def get(self):
        return self.call(self.app.releases_get, method="GET")

    def test_existing_budget_sees_the_latest_once(self):
        self.add_txn()
        first = self.get()
        self.assertEqual([r["version"] for r in first["unseen"]], [r["version"] for r in first["releases"][:releases.NEVER_SHOWN_LIMIT]])
        self.call(self.app.releases_seen, {"version": first["releases"][0]["version"]})
        again = self.get()
        self.assertEqual(again["unseen"], [])
        self.assertEqual(again["last_seen"], first["releases"][0]["version"])

    def test_brand_new_empty_budget_starts_up_to_date(self):
        r = self.get()
        self.assertEqual(r["unseen"], [])
        self.add_txn()   # later, with data, still nothing to announce
        self.assertEqual(self.get()["unseen"], [])

    def test_empty_budget_stays_quiet_across_new_releases(self):
        self.get()   # starts up to date
        c = self.conn()
        from evbudget import store
        store.set_setting(c, releases.SEEN_KEY, "0.1")   # as if a newer release arrived since
        c.commit()
        c.close()
        self.assertEqual(self.get()["unseen"], [])

    def test_an_update_from_an_older_version_says_where_it_started(self):
        from evbudget import store
        self.add_txn()
        c = self.conn()
        store.set_setting(c, releases.SEEN_KEY, "3.0")
        c.commit()
        c.close()
        r = self.get()
        self.assertEqual(r["from_version"], "3.0")
        self.assertEqual([x["version"] for x in r["unseen"]], [x["version"] for x in r["releases"]])
        self.assertTrue(all(n["text"] for x in r["unseen"] for n in x["notes"]))

    def test_bad_version_is_rejected(self):
        for v in ("9.9", "<b>", None, ""):
            with self.assertRaises(HttpError):
                self.call(self.app.releases_seen, {"version": v})

    def test_about_has_the_details_for_a_report(self):
        a = self.call(self.app.about_get, method="GET")
        self.assertEqual(a["version"], VERSION)
        self.assertEqual(a["schema_version"], 2)
        for key in ("python", "os", "git", "latest_release", "release_date"):
            self.assertIn(key, a)


if __name__ == "__main__":
    unittest.main()
