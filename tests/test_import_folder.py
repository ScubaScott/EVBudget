"""Choosing which folder bank files are read from."""

import os
import shutil
import tempfile
import unittest
from unittest import mock

from helpers import TempAppCase
from evbudget import folder_picker
from evbudget.http_util import HttpError, Request

CSV = "Date,Description,Amount\n01/05/2026,TEST,-5.00\n"


class ImportFolderTests(TempAppCase):
    def setUp(self):
        super().setUp()
        self.other = tempfile.mkdtemp(prefix="evb_other_")

    def tearDown(self):
        shutil.rmtree(self.other, ignore_errors=True)
        super().tearDown()

    def info(self):
        return self.call(self.app.import_folder_get, method="GET")

    def scan(self):
        return [f["filename"] for f in self.call(self.app.import_scan, method="GET")["files"]]

    def set_folder(self, path):
        return self.call(self.app.import_folder_set, {"path": path})

    def test_starts_on_the_budgets_own_folder(self):
        i = self.info()
        self.assertEqual((i["path"], i["is_default"], i["exists"]), (self.input_dir, True, True))

    def test_scan_reads_the_chosen_folder_instead(self):
        self.write_csv("in_default.csv", CSV)
        with open(os.path.join(self.other, "in_other.csv"), "w") as f:
            f.write(CSV)
        self.assertEqual(self.scan(), ["in_default.csv"])
        res = self.set_folder(self.other)
        self.assertFalse(res["is_default"])
        self.assertEqual(os.path.normcase(res["path"]), os.path.normcase(os.path.abspath(self.other)))
        self.assertEqual(self.scan(), ["in_other.csv"])

    def test_the_choice_is_kept_and_can_be_reset(self):
        self.set_folder(self.other)
        self.assertFalse(self.info()["is_default"])
        res = self.set_folder("")
        self.assertEqual((res["is_default"], res["path"]), (True, self.input_dir))

    def test_a_missing_folder_is_refused_and_changes_nothing(self):
        with self.assertRaises(HttpError):
            self.set_folder(os.path.join(self.other, "nope"))
        self.assertTrue(self.info()["is_default"])

    def test_choosing_the_built_in_folder_counts_as_the_default(self):
        self.set_folder(self.other)
        self.assertTrue(self.set_folder(self.input_dir)["is_default"])

    def test_a_folder_that_goes_missing_says_so_instead_of_looking_empty(self):
        self.set_folder(self.other)
        shutil.rmtree(self.other)
        with self.assertRaises(HttpError) as ctx:
            self.scan()
        self.assertIn("can't be found", str(ctx.exception))
        self.assertFalse(self.info()["exists"])

    def test_changing_the_folder_needs_a_json_request(self):
        with self.assertRaises(HttpError):
            self.app.import_folder_set(Request("POST", "/t", {}, {"Content-Type": "text/plain"}, b"x"))

    def test_browse_hands_off_to_the_picker(self):
        with mock.patch.object(self.app.folder_picker, "start") as start, \
                mock.patch.object(self.app.folder_picker, "status", return_value={"state": "done", "path": self.other}):
            self.call(self.app.import_folder_browse, {})
            start.assert_called_once_with(self.input_dir)
            self.assertEqual(self.call(self.app.import_folder_browse_status, method="GET")["path"], self.other)


class FakeProc:
    def __init__(self, code, out="", err="", running=False):
        self.returncode, self._out, self._err, self._running = code, out, err, running

    def poll(self):
        return None if self._running else self.returncode

    def communicate(self):
        return self._out, self._err


class PickerStatusTests(unittest.TestCase):
    def status(self, proc, mac=False):
        p = folder_picker.FolderPicker()
        p._proc, p._mac = proc, mac
        return p.status()

    def test_states(self):
        self.assertEqual(self.status(None)["state"], "idle")
        self.assertEqual(self.status(FakeProc(0, running=True))["state"], "waiting")
        self.assertEqual(self.status(FakeProc(0, "C:/Users/me/Downloads\n")), {"state": "done", "path": "C:/Users/me/Downloads"})
        self.assertEqual(self.status(FakeProc(0, ""))["state"], "cancelled")
        self.assertEqual(self.status(FakeProc(3))["state"], "unavailable")
        self.assertEqual(self.status(FakeProc(1, "", "boom"))["state"], "error")

    def test_mac_cancel_is_not_an_error_and_trailing_slash_is_dropped(self):
        self.assertEqual(self.status(FakeProc(1, "", "execution error: User canceled. (-128)"), mac=True)["state"], "cancelled")
        self.assertEqual(self.status(FakeProc(0, "/Users/me/Downloads/\n"), mac=True)["path"], "/Users/me/Downloads")


if __name__ == "__main__":
    unittest.main()
