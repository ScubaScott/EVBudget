import json
import unittest

from helpers import TempAppCase
from evbudget import api
from evbudget.http_util import HttpError, Request


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class LifecycleTests(TempAppCase):
    def setUp(self):
        super().setUp()
        self.clock = Clock()
        self.app.clock = self.clock

    def beat(self, page="p1"):
        self.call(self.app.heartbeat, {"page": page})

    def close(self, page="p1"):
        self.call(self.app.page_closed, {"page": page})

    def test_never_exits_before_a_page_has_opened(self):
        self.clock.now += 10_000
        self.assertFalse(self.app.should_exit())

    def test_exits_after_the_grace_period_once_the_last_page_closes(self):
        self.beat()
        self.assertFalse(self.app.should_exit())
        self.close()
        self.assertFalse(self.app.should_exit())          # first look: starts the clock
        self.clock.now += api.CLOSE_GRACE_SECONDS - 1
        self.assertFalse(self.app.should_exit())
        self.clock.now += 2
        self.assertTrue(self.app.should_exit())

    def test_a_reload_does_not_stop_it(self):
        self.beat("old")
        self.close("old")                                  # the old page says goodbye...
        self.assertFalse(self.app.should_exit())
        self.clock.now += 3
        self.beat("new")                                   # ...and the new one checks in a moment later
        self.clock.now += api.CLOSE_GRACE_SECONDS + 5
        self.assertFalse(self.app.should_exit())

    def test_another_open_tab_keeps_it_running(self):
        self.beat("a")
        self.beat("b")
        self.close("a")
        self.clock.now += 60
        self.beat("b")
        self.assertFalse(self.app.should_exit())
        self.clock.now += api.CLOSE_GRACE_SECONDS + 5
        self.assertFalse(self.app.should_exit())

    def test_a_page_that_goes_silent_is_eventually_dropped(self):
        self.beat()
        self.clock.now += api.PAGE_TIMEOUT_SECONDS + 1
        self.assertFalse(self.app.should_exit())           # dropped now; grace period starts
        self.clock.now += api.CLOSE_GRACE_SECONDS + 1
        self.assertTrue(self.app.should_exit())

    def test_a_slow_background_tab_is_not_dropped(self):
        self.beat()
        self.clock.now += 70                               # browsers beat about once a minute for hidden tabs
        self.assertFalse(self.app.should_exit())
        self.clock.now += api.CLOSE_GRACE_SECONDS + 5
        self.assertFalse(self.app.should_exit())

    def test_shutdown_calls_the_server_hook(self):
        stopped = []
        self.app.request_shutdown = lambda: stopped.append(True)
        self.assertTrue(self.call(self.app.shutdown, {})["success"])
        self.assertEqual(stopped, [True])

    def test_shutdown_refuses_a_request_that_is_not_json(self):
        stopped = []
        self.app.request_shutdown = lambda: stopped.append(True)
        with self.assertRaises(HttpError):
            self.app.shutdown(Request("POST", "/x", {}, {"Content-Type": "text/plain"}, b""))
        self.assertEqual(stopped, [])

    def test_shutdown_without_a_server_is_an_error_not_a_crash(self):
        with self.assertRaises(HttpError):
            self.call(self.app.shutdown, {})


if __name__ == "__main__":
    unittest.main()
