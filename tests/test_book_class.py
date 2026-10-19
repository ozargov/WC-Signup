"""book_class.py against a simulated Arbox API -- no network, no real account.

Run from the project folder:  python -m unittest discover -s tests -v
"""

import contextlib
import io
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import book_class  # noqa: E402

OK = (200, {"data": {"id": 1}})
NOT_OPEN = (400, '{"statusCode":400,"error":{"message":"Registration is not open yet"}}')
FULL = (516, '{"statusCode":516,"error":{"message":"Schedule Is Full","messageToUser":'
             '"Schedule is full,refresh the schedule page by dragging it down and subscribe '
             'to the waiting list","code":516},"data":null}')

BASE_CLASS = {
    "id": 123, "time": "08:00", "box_categories": {"name": "W.O.D Hall A"},
    "coach": {"full_name": "Coach"}, "free": 5, "max_users": 20, "stand_by": 0,
    "booking_option": "insertScheduleUser", "user_booked": None, "user_in_standby": None,
    "enable_registration_time": 48,
}


class FakeArbox:
    """Stands in for book_class._post and answers like the Arbox API.

    inserts: a list of replies (the last one repeats) or a function of the
    monotonic time. classes: class states served by successive schedule
    fetches (the last one repeats). after_insert: the state served once any
    booking attempt has been made."""

    def __init__(self, inserts, classes=None, after_insert=None,
                 standby=(200, {"data": {"stand_by_position": 3}})):
        self.inserts = inserts
        self.classes = [dict(BASE_CLASS, **c) for c in (classes or [{}])]
        self.after_insert = dict(BASE_CLASS, **after_insert) if after_insert else None
        self.standby = standby
        self.calls = []  # (monotonic time, path, token)

    def count(self, path):
        return sum(1 for c in self.calls if c[1] == path)

    def times(self, path):
        return [c[0] for c in self.calls if c[1] == path]

    def __call__(self, url, headers, body):
        path = url.split("/api/v2", 1)[1]
        now = time.monotonic()
        self.calls.append((now, path, headers.get("accesstoken")))
        if path == "/user/login":
            return 200, {"token": f"token{self.count(path)}", "refreshToken": "r"}
        if path == "/schedule/betweenDates":
            if self.after_insert and self.count("/scheduleUser/insert"):
                return 200, {"data": [self.after_insert]}
            n = self.count(path)
            return 200, {"data": [self.classes[min(n, len(self.classes)) - 1]]}
        if path == "/scheduleUser/insert":
            if callable(self.inserts):
                return self.inserts(now)
            n = self.count(path)
            return self.inserts[min(n, len(self.inserts)) - 1]
        if path == "/scheduleStandBy/insert":
            return self.standby
        return 404, "unknown path"


class BookClassTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = {name: getattr(book_class, name) for name in (
            "_post", "warm_connection", "window_candidates_for", "LOG_PATH",
            "PREFIRE_SECONDS", "PREFIRE_MARGIN", "WARM_SECONDS", "ATTEMPT_GAP",
            "MIN_POST_SECONDS", "LAST_POST_SECONDS")}
        # Never write into the real booking.log.
        book_class.LOG_PATH = os.path.join(self.tmp.name, "test.log")
        book_class.warm_connection = lambda url=None: None
        book_class.WARM_SECONDS = 0.5
        book_class.MIN_POST_SECONDS = None
        book_class.LAST_POST_SECONDS = 0.30
        os.environ["ARBOX_EMAIL"] = "test@example.com"
        os.environ["ARBOX_PASSWORD"] = "not-a-real-password"
        self.t0_wall, self.t0_mono = datetime.now(), time.monotonic()

    def tearDown(self):
        for name, value in self.saved.items():
            setattr(book_class, name, value)
        self.tmp.cleanup()

    def mono(self, wall):
        return self.t0_mono + (wall - self.t0_wall).total_seconds()

    def windows_in(self, *seconds):
        windows = [self.t0_wall + timedelta(seconds=s) for s in seconds]
        book_class.window_candidates_for = lambda target: windows
        return windows

    def run_main(self, fake, *extra):
        book_class._post = fake
        argv = ["book_class.py", "--class-date", "2026-10-21", "--class-time", "08:00",
                "--wait-for-window", *extra]
        out = io.StringIO()
        old_argv, sys.argv = sys.argv, argv
        try:
            with contextlib.redirect_stdout(out):
                book_class.main()
            code = 0
        except SystemExit as e:
            code = e.code
        finally:
            sys.argv = old_argv
        return code, out.getvalue()

    # --- the race ---------------------------------------------------------
    def test_books_on_time(self):
        (window,) = self.windows_in(2.0)
        fake = FakeArbox([OK])
        code, out = self.run_main(fake)
        self.assertEqual(code, 0, out)
        first = fake.times("/scheduleUser/insert")[0] - self.mono(window)
        # Sent half a round trip early (0.15s here) less the 30ms aim-after.
        self.assertTrue(-0.20 <= first <= 0.03, f"fired {first:+.3f}s from the window")
        self.assertIn("BOOKING CONFIRMED", out)
        self.assertIn("READY_AT=", out)

    def test_early_refusals_are_retried(self):
        self.windows_in(1.0)
        fake = FakeArbox([NOT_OPEN, NOT_OPEN, OK])
        code, out = self.run_main(fake)
        self.assertEqual(code, 0, out)
        self.assertEqual(fake.count("/scheduleUser/insert"), 3)
        self.assertEqual(fake.count("/scheduleStandBy/insert"), 0)

    def test_516_that_does_not_say_full_is_not_treated_as_full(self):
        self.windows_in(1.0)
        fake = FakeArbox([(516, '{"error":{"message":"Registration closed"}}'), OK])
        code, out = self.run_main(fake)
        self.assertEqual(code, 0, out)
        self.assertEqual(fake.count("/scheduleStandBy/insert"), 0)

    def test_full_class_joins_waiting_list_with_position(self):
        self.windows_in(1.0)
        fake = FakeArbox([FULL])
        code, out = self.run_main(fake)
        self.assertEqual(code, 6, out)
        self.assertEqual(fake.count("/scheduleUser/insert"), 1, "no retrying a full class")
        self.assertIn("STANDBY_POSITION=3", out)

    def test_waiting_list_position_read_back_from_the_class(self):
        self.windows_in(1.0)
        fake = FakeArbox([FULL], standby=(200, {"data": {}}),
                         after_insert={"stand_by_position": 7})
        code, out = self.run_main(fake)
        self.assertEqual(code, 6, out)
        self.assertIn("STANDBY_POSITION=7", out)

    def test_no_waitlist_flag(self):
        self.windows_in(1.0)
        code, out = self.run_main(FakeArbox([FULL]), "--no-waitlist")
        self.assertEqual(code, 5, out)

    def test_rate_limit_backs_off(self):
        self.windows_in(1.0)
        fake = FakeArbox([(429, ""), OK])
        code, out = self.run_main(fake)
        self.assertEqual(code, 0, out)
        a, b = fake.times("/scheduleUser/insert")[:2]
        self.assertGreaterEqual(b - a, book_class.RATE_LIMIT_BACKOFF - 0.01)

    def test_expired_token_logs_in_again(self):
        self.windows_in(1.0)
        fake = FakeArbox([(401, "Unauthorized"), OK])
        code, out = self.run_main(fake)
        self.assertEqual(code, 0, out)
        tokens = [c[2] for c in fake.calls if c[1] == "/scheduleUser/insert"]
        self.assertEqual(tokens, ["token1", "token2"])

    def test_network_error_is_retried(self):
        self.windows_in(1.0)
        fake = FakeArbox([(599, "network error: reset"), OK])
        code, out = self.run_main(fake)
        self.assertEqual(code, 0, out)

    def test_refused_throughout_fails_without_waitlist(self):
        self.windows_in(0.5)
        fake = FakeArbox([NOT_OPEN])
        code, out = self.run_main(fake, "--burst-seconds", "1.0")
        self.assertEqual(code, 5, out)
        self.assertEqual(fake.count("/scheduleStandBy/insert"), 0)
        self.assertIn("Registration is not open yet", out, "the refusal is reported")

    def test_lost_reply_but_class_shows_booked(self):
        self.windows_in(0.5)
        fake = FakeArbox([(599, "network error")], after_insert={"user_booked": 999})
        code, out = self.run_main(fake, "--burst-seconds", "0.8")
        self.assertEqual(code, 0, out)
        self.assertIn("a reply was lost", out)

    # --- before the race --------------------------------------------------
    def test_already_booked_does_nothing(self):
        self.windows_in(1.0)
        fake = FakeArbox([OK], classes=[{"user_booked": 555}])
        code, out = self.run_main(fake)
        self.assertEqual(code, 0, out)
        self.assertEqual(fake.count("/scheduleUser/insert"), 0)

    def test_already_on_waiting_list_does_nothing(self):
        self.windows_in(1.0)
        fake = FakeArbox([OK], classes=[{"booking_option": "cancelWaitList",
                                         "user_in_standby": 1, "stand_by_position": 4}])
        code, out = self.run_main(fake)
        self.assertEqual(code, 6, out)
        self.assertEqual(fake.count("/scheduleUser/insert"), 0)
        self.assertIn("STANDBY_POSITION=4", out)

    def test_logs_in_again_and_rechecks_before_a_long_wait(self):
        book_class.PREFIRE_SECONDS, book_class.PREFIRE_MARGIN = 1.5, 0.5
        (window,) = self.windows_in(3.0)
        fake = FakeArbox([OK])
        code, out = self.run_main(fake)
        self.assertEqual(code, 0, out)
        self.assertEqual(fake.count("/user/login"), 2)
        self.assertEqual(fake.count("/schedule/betweenDates"), 2)
        second_login = fake.times("/user/login")[1]
        self.assertLess(second_login, fake.times("/scheduleUser/insert")[0])
        self.assertAlmostEqual(second_login - self.mono(window), -1.5, delta=0.2)
        tokens = [c[2] for c in fake.calls if c[1] == "/scheduleUser/insert"]
        self.assertEqual(tokens, ["token2"], "fires with the fresh token")

    def test_recheck_spots_a_booking_made_in_the_app(self):
        book_class.PREFIRE_SECONDS, book_class.PREFIRE_MARGIN = 1.5, 0.5
        self.windows_in(3.0)
        fake = FakeArbox([OK], classes=[{}, {"user_booked": 777}])
        code, out = self.run_main(fake)
        self.assertEqual(code, 0, out)
        self.assertEqual(fake.count("/scheduleUser/insert"), 0)

    def test_clock_change_weekend_tries_the_second_time(self):
        first, second = self.windows_in(0.5, 2.5)
        second_mono = self.mono(second)
        fake = FakeArbox(lambda now: OK if now >= second_mono - 0.2 else NOT_OPEN)
        code, out = self.run_main(fake, "--burst-seconds", "0.8")
        self.assertEqual(code, 0, out)
        later = [t for t in fake.times("/scheduleUser/insert") if t > self.mono(first) + 1.0]
        self.assertTrue(later, "attempted again at the second time")
        self.assertAlmostEqual(later[0] - second_mono, -0.12, delta=0.1)
        self.assertIn("trying again at", out)

    def test_dry_run_books_nothing(self):
        self.windows_in(1.0)
        fake = FakeArbox([OK])
        code, out = self.run_main(fake, "--dry-run")
        self.assertEqual(code, 0, out)
        self.assertEqual(fake.count("/scheduleUser/insert"), 0)

    def test_class_not_found_lists_the_day(self):
        self.windows_in(1.0)
        fake = FakeArbox([OK], classes=[{"time": "09:00"}])
        code, out = self.run_main(fake)
        self.assertEqual(code, 4, out)
        self.assertIn("Available classes:", out)

    def test_missing_credentials(self):
        os.environ["ARBOX_PASSWORD"] = ""
        code, out = self.run_main(FakeArbox([OK]))
        self.assertEqual(code, 1, out)


class WindowRuleTest(unittest.TestCase):
    """The real rule, including the two clock-change weekends."""

    @unittest.skipIf(book_class.GYM_TZ is None, "no timezone database here")
    def test_rule(self):
        W = book_class.window_candidates_for
        d = datetime
        # Ordinary weekdays: 48h, one candidate.
        self.assertEqual(W(d(2026, 10, 21, 8, 0)), [d(2026, 10, 19, 8, 0)])        # Wed -> Mon
        self.assertEqual(W(d(2026, 10, 24, 18, 0)), [d(2026, 10, 22, 18, 0)])      # Sat -> Thu
        # Sunday and Monday: 72h.
        self.assertEqual(W(d(2026, 11, 1, 8, 0)), [d(2026, 10, 29, 8, 0)])         # Sun -> Thu
        self.assertEqual(W(d(2026, 11, 2, 8, 0)), [d(2026, 10, 30, 8, 0)])         # Mon -> Fri
        # Clocks go back on Sun 25 Oct 2026: the same time of day, or an hour later.
        self.assertEqual(W(d(2026, 10, 25, 8, 0)), [d(2026, 10, 22, 8, 0), d(2026, 10, 22, 9, 0)])
        self.assertEqual(W(d(2026, 10, 26, 8, 0)), [d(2026, 10, 23, 8, 0), d(2026, 10, 23, 9, 0)])
        # ...but a 48h class that opens after the change has one time.
        self.assertEqual(W(d(2026, 10, 27, 8, 0)), [d(2026, 10, 25, 8, 0)])
        # Clocks go forward on Fri 26 Mar 2027: an hour earlier, or the same time.
        self.assertEqual(W(d(2027, 3, 28, 8, 0)), [d(2027, 3, 25, 7, 0), d(2027, 3, 25, 8, 0)])


if __name__ == "__main__":
    unittest.main()
