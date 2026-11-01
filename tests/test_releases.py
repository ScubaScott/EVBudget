"""What's new popup logic, the release notes file, and the version details sent with feedback."""

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
        self.assertEqual(releases.load()[0]["version"], f"{VERSION:.1f}")


class UnseenTests(unittest.TestCase):
    def test_nothing_new_when_up_to_date(self):
        self.assertEqual(releases.unseen(R, "3.5"), [])

    def test_everything_since_the_last_seen_release(self):
        self.assertEqual([r["version"] for r in releases.unseen(R, "3.3")], ["3.5", "3.4"])

    def test_first_time_or_unknown_shows_only_the_latest(self):
        self.assertEqual([r["version"] for r in releases.unseen(R, "")], ["3.5"])
        self.assertEqual([r["version"] for r in releases.unseen(R, "1.0")], ["3.5"])
        self.assertEqual(releases.unseen([], ""), [])


class ReleaseApiTests(TempAppCase):
    def get(self):
        return self.call(self.app.releases_get, method="GET")

    def test_existing_budget_sees_the_latest_once(self):
        self.add_txn()
        first = self.get()
        self.assertEqual([r["version"] for r in first["unseen"]], [first["releases"][0]["version"]])
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

    def test_bad_version_is_rejected(self):
        for v in ("9.9", "<b>", None, ""):
            with self.assertRaises(HttpError):
                self.call(self.app.releases_seen, {"version": v})

    def test_about_has_the_details_for_a_report(self):
        a = self.call(self.app.about_get, method="GET")
        self.assertEqual(a["version"], f"{VERSION:.1f}")
        self.assertEqual(a["schema_version"], 2)
        for key in ("python", "os", "git", "latest_release", "release_date"):
            self.assertIn(key, a)


if __name__ == "__main__":
    unittest.main()
