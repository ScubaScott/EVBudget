import ntpath
import unittest
from unittest import mock

from helpers import ROOT  # noqa: F401  (puts the project on sys.path)
from evbudget import browser

WIN_ENV = {"ProgramFiles": r"C:\Program Files", "ProgramFiles(x86)": r"C:\Program Files (x86)", "LocalAppData": r"C:\Users\x\AppData\Local"}
CHROME = ntpath.join(r"C:\Program Files", r"Google\Chrome\Application\chrome.exe")
EDGE_X86 = ntpath.join(r"C:\Program Files (x86)", r"Microsoft\Edge\Application\msedge.exe")


class FindBrowserTests(unittest.TestCase):
    def test_windows_prefers_chrome_then_edge(self):
        both = browser.find_app_browser("win32", WIN_ENV, exists=lambda p: p in (CHROME, EDGE_X86))
        self.assertEqual(both, ("Chrome", CHROME))
        edge_only = browser.find_app_browser("win32", WIN_ENV, exists=lambda p: p == EDGE_X86)
        self.assertEqual(edge_only, ("Edge", EDGE_X86))

    def test_per_user_chrome_install_is_found(self):
        path = ntpath.join(r"C:\Users\x\AppData\Local", r"Google\Chrome\Application\chrome.exe")
        self.assertEqual(browser.find_app_browser("win32", WIN_ENV, exists=lambda p: p == path), ("Chrome", path))

    def test_mac_checks_system_and_user_applications(self):
        env = {"HOME": "/Users/x"}
        user_edge = "/Users/x/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"
        self.assertEqual(browser.find_app_browser("darwin", env, exists=lambda p: p == user_edge), ("Edge", user_edge))
        chrome = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        self.assertEqual(browser.find_app_browser("darwin", env, exists=lambda p: True), ("Chrome", chrome))

    def test_linux_uses_the_path(self):
        found = browser.find_app_browser("linux", {}, exists=lambda p: True, which=lambda c: "/usr/bin/chromium" if c == "chromium" else None)
        self.assertEqual(found, ("Chromium", "/usr/bin/chromium"))

    def test_nothing_installed_gives_none(self):
        self.assertIsNone(browser.find_app_browser("win32", WIN_ENV, exists=lambda p: False))
        self.assertIsNone(browser.find_app_browser("darwin", {"HOME": "/h"}, exists=lambda p: False))


class SwitchTests(unittest.TestCase):
    def test_flag_and_environment_variable_turn_it_off(self):
        self.assertFalse(browser.app_window_disabled([], {}))
        self.assertTrue(browser.app_window_disabled(["--no-app-window"], {}))
        self.assertTrue(browser.app_window_disabled([], {browser.DISABLE_ENV: "1"}))
        self.assertFalse(browser.app_window_disabled([], {browser.DISABLE_ENV: "0"}))
        self.assertFalse(browser.app_window_disabled([], {browser.DISABLE_ENV: ""}))


class LaunchTests(unittest.TestCase):
    def test_app_window_runs_the_browser_with_app_flag(self):
        with mock.patch("subprocess.Popen") as popen, mock.patch("webbrowser.open") as opened:
            browser.launch("http://127.0.0.1:8060/", ("Chrome", "/x/chrome"))
        args = popen.call_args[0][0]
        self.assertEqual(args[:2], ["/x/chrome", "--app=http://127.0.0.1:8060/"])
        opened.assert_not_called()

    def test_no_browser_opens_a_normal_tab(self):
        with mock.patch("subprocess.Popen") as popen, mock.patch("webbrowser.open") as opened:
            browser.launch("http://127.0.0.1:8060/", None)
        popen.assert_not_called()
        opened.assert_called_once_with("http://127.0.0.1:8060/")

    def test_failure_to_start_falls_back_to_a_normal_tab(self):
        with mock.patch("subprocess.Popen", side_effect=OSError("nope")), mock.patch("webbrowser.open") as opened:
            browser.launch("http://127.0.0.1:8060/", ("Chrome", "/x/chrome"))
        opened.assert_called_once()


if __name__ == "__main__":
    unittest.main()
