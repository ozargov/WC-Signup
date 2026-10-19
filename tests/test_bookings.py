"""bookings.py -- the queue, the planner and the booking run -- with no GitHub,
no Telegram and no real booking. The race test uses throwaway local git repos.

Run from the project folder:  python -m unittest discover -s tests -v
"""

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import bookings  # noqa: E402
from book_class import window_opens_for  # noqa: E402

FAKE_BOOKER = r'''
import os, sys
print("[2026-10-19 08:00:00.000] Target found", flush=True)
print("READY_AT=2026-10-19 08:00:00|W.O.D Hall A", flush=True)
for line in os.environ.get("FAKE_LINES", "").split("\\n"):
    if line:
        print(line, flush=True)
sys.exit(int(os.environ.get("FAKE_EXIT", "0")))
'''


def target_with_window_in(minutes):
    """A class time whose signup window opens `minutes` from now, or None --
    no class opens on a Saturday (Monday's open on Friday, Tuesday's on Sunday)."""
    want = datetime.now() + timedelta(minutes=minutes)
    for hours in (48, 72):
        t = (want + timedelta(hours=hours, minutes=1)).replace(second=0, microsecond=0)
        if abs((window_opens_for(t) - want).total_seconds()) < 120:
            return t
    return None


class QueueTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = (bookings.QUEUE_DIR, bookings.notify, bookings.dispatch,
                      bookings.active_run_titles, bookings.BOOK_SCRIPT,
                      bookings.queued_runs, bookings.cancel_run)
        bookings.QUEUE_DIR = Path(self.tmp.name) / "queue"
        self.messages = []
        bookings.notify = self.messages.append
        self.dispatched = []
        bookings.dispatch = lambda entry, urgent: self.dispatched.append((entry["time"], urgent))
        self.active = set()
        bookings.active_run_titles = lambda exclude=frozenset(): self.active
        self.stuck_runs = {}   # workflow file -> runs GitHub reports as queued
        bookings.queued_runs = lambda workflow: self.stuck_runs.get(workflow, [])
        self.cancelled = []
        bookings.cancel_run = self.cancelled.append
        os.environ.pop("GITHUB_ACTIONS", None)

    def tearDown(self):
        (bookings.QUEUE_DIR, bookings.notify, bookings.dispatch,
         bookings.active_run_titles, bookings.BOOK_SCRIPT,
         bookings.queued_runs, bookings.cancel_run) = self.saved
        self.tmp.cleanup()

    def cli(self, *argv):
        out = io.StringIO()
        old, sys.argv = sys.argv, ["bookings.py", *argv]
        try:
            with contextlib.redirect_stdout(out):
                code = bookings.main()
        finally:
            sys.argv = old
        return code, out.getvalue()

    def queued(self):
        return sorted(p.name for p in bookings.QUEUE_DIR.glob("*.json"))

    def need(self, minutes):
        target = target_with_window_in(minutes)
        if target is None:
            self.skipTest(f"no class opens {minutes} min from now (Saturday)")
        return target

    def add(self, target, category=""):
        return self.cli("add", "--date", f"{target:%Y-%m-%d}", "--time", f"{target:%H:%M}",
                        "--category", category)

    # --- add / remove -------------------------------------------------------
    def test_add_writes_one_file_per_class(self):
        code, out = self.cli("add", "--date", "2099-01-07", "--time", "8:00", "--category", "Hall A")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.queued(), ["2099-01-07_0800.json"])
        entry = json.loads((bookings.QUEUE_DIR / "2099-01-07_0800.json").read_text("utf-8"))
        self.assertEqual((entry["date"], entry["time"], entry["category"]),
                         ("2099-01-07", "08:00", "Hall A"))
        self.assertTrue(self.messages[0].startswith("📌 Queued"))

    def test_add_twice_and_change_category(self):
        self.cli("add", "--date", "2099-01-07", "--time", "08:00", "--category", "Hall A")
        code, out = self.cli("add", "--date", "2099-01-07", "--time", "08:00", "--category", "Hall A")
        self.assertIn("Already queued", out)
        self.cli("add", "--date", "2099-01-07", "--time", "08:00", "--category", "HYROX")
        self.assertIn("Updated", self.messages[-1])
        self.assertEqual(len(self.queued()), 1)

    def test_add_rejects_past_and_garbage(self):
        with self.assertRaises(SystemExit):
            self.cli("add", "--date", "2020-01-01", "--time", "08:00")
        with self.assertRaises(SystemExit):
            self.cli("add", "--date", "tomorrow", "--time", "08:00")

    def test_remove(self):
        self.cli("add", "--date", "2099-01-07", "--time", "08:00")
        code, out = self.cli("remove", "--date", "2099-01-07", "--time", "08:00")
        self.assertEqual(self.queued(), [])
        self.assertIn("Removed", self.messages[-1])
        code, out = self.cli("remove", "--date", "2099-01-07", "--time", "08:00")
        self.assertIn("Nothing queued", out)

    # --- from the Telegram bot -----------------------------------------------
    def test_add_many_and_remove_many(self):
        classes = json.dumps([{"date": "2099-01-07", "time": "07:00", "category": "HYROX"},
                              {"date": "2099-01-08", "time": "19:00", "category": "Hall B"},
                              {"date": "2020-01-01", "time": "07:00", "category": "old"}])
        code, out = self.cli("add-many", "--classes", classes)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.queued(), ["2099-01-07_0700.json", "2099-01-08_1900.json"])
        self.assertTrue(self.messages[-1].startswith("📌 Queued 2 classes:"), self.messages)
        self.assertIn("(HYROX)", self.messages[-1])
        self.assertIn("Skipped, already started", self.messages[-1])

        code, out = self.cli("remove-many", "--classes",
                             json.dumps([{"date": "2099-01-07", "time": "07:00"}]))
        self.assertEqual(code, 0, out)
        self.assertEqual(self.queued(), ["2099-01-08_1900.json"])
        self.assertIn("🗑 Removed", self.messages[-1])

    def test_add_many_rejects_garbage(self):
        with self.assertRaises(SystemExit):
            self.cli("add-many", "--classes", "not json")
        with self.assertRaises(SystemExit):
            self.cli("add-many", "--classes", json.dumps([{"date": "someday", "time": "08:00"}]))

    # --- plan ---------------------------------------------------------------
    def test_plan_starts_only_what_is_due(self):
        due = self.need(30)
        later = self.need(300)
        opened = self.need(-90)   # signup already open: still worth a try
        for t in (due, later, opened):
            self.add(t)
        past = (datetime.now() - timedelta(hours=1)).replace(second=0, microsecond=0)
        bookings.QUEUE_DIR.joinpath(f"{past:%Y-%m-%d_%H%M}.json").write_text(json.dumps(
            {"date": f"{past:%Y-%m-%d}", "time": f"{past:%H:%M}", "category": ""}), "utf-8")
        self.messages.clear()

        code, out = self.cli("plan", "--lookahead-minutes", "50")
        self.assertEqual(code, 0, out)
        self.assertEqual(sorted(t for t, _ in self.dispatched),
                         sorted([f"{due:%H:%M}", f"{opened:%H:%M}"]))
        self.assertIn("waiting", out)
        self.assertNotIn(f"{past:%Y-%m-%d_%H%M}.json", self.queued(), "a started class is dropped")
        self.assertTrue(any("was still queued" in m for m in self.messages))

    def test_plan_skips_a_class_whose_run_is_already_waiting(self):
        due = self.need(20)
        self.add(due)
        self.active = {f"Book {due:%Y-%m-%d} {due:%H:%M}"}
        code, out = self.cli("plan")
        self.assertEqual(self.dispatched, [])
        self.assertIn("already waiting", out)

    def test_plan_dry_run_changes_nothing(self):
        self.add(self.need(10))
        code, out = self.cli("plan", "--dry-run")
        self.assertIn("DUE", out)
        self.assertEqual(self.dispatched, [])

    def test_urgent_flag_near_the_window(self):
        self.add(self.need(10))
        self.cli("plan")
        self.assertEqual(self.dispatched[0][1], True)

    # --- stuck runs ---------------------------------------------------------
    @staticmethod
    def ago(minutes):
        return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")

    def test_stuck_requests_are_cancelled_and_reported(self):
        self.stuck_runs = {
            "bot-queue.yml": [
                {"databaseId": 1, "displayTitle": "Queue from Telegram - add Sun 25/10 07:00",
                 "createdAt": self.ago(30)},
                {"databaseId": 2, "displayTitle": "Queue from Telegram - add Mon 26/10 07:00",
                 "createdAt": self.ago(2)},
            ],
            "book-class.yml": [
                {"databaseId": 3, "displayTitle": "Book 2099-01-07 08:00", "createdAt": self.ago(12)},
            ],
        }
        code, out = self.cli("plan", "--check-stuck")   # even with an empty queue
        self.assertEqual(code, 0, out)
        self.assertEqual(sorted(self.cancelled), [1, 3], "the 2-minute-old run is left alone")
        self.assertEqual(len(self.messages), 1, "only the queue request needs you")
        self.assertIn("Sun 25/10 07:00", self.messages[0])
        self.assertIn("Please send it again", self.messages[0])

    def test_planner_restarts_a_stuck_booking_run(self):
        due = self.need(20)
        self.add(due)
        self.messages.clear()
        title = f"Book {due:%Y-%m-%d} {due:%H:%M}"
        self.stuck_runs = {"book-class.yml": [
            {"databaseId": 9, "displayTitle": title, "createdAt": self.ago(15)}]}
        # GitHub may still list the run as queued just after the cancel request.
        bookings.active_run_titles = lambda exclude=frozenset(): set() if 9 in exclude else {title}
        code, out = self.cli("plan", "--check-stuck")
        self.assertEqual(self.cancelled, [9])
        self.assertEqual([t for t, _ in self.dispatched], [f"{due:%H:%M}"])
        self.assertEqual(self.messages, [], "nothing for you to do")

    def test_stuck_check_only_when_asked(self):
        self.stuck_runs = {"bot-queue.yml": [
            {"databaseId": 1, "displayTitle": "x", "createdAt": self.ago(60)}]}
        self.cli("plan")
        self.cli("plan", "--check-stuck", "--dry-run")
        self.assertEqual(self.cancelled, [])

    # --- book ---------------------------------------------------------------
    def book_with(self, exit_code, lines=""):
        script = Path(self.tmp.name) / "fake_booker.py"
        script.write_text(FAKE_BOOKER, "utf-8")
        bookings.BOOK_SCRIPT = script
        os.environ["FAKE_EXIT"], os.environ["FAKE_LINES"] = str(exit_code), lines
        self.cli("add", "--date", "2099-01-07", "--time", "08:00", "--category", "Hall A")
        self.messages.clear()
        return self.cli("book", "--date", "2099-01-07", "--time", "08:00")

    def test_book_success(self):
        code, out = self.book_with(0)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.queued(), [], "done classes leave the queue")
        self.assertTrue(self.messages[0].startswith("⏳ Ready"), self.messages)
        self.assertIn("08:00:00", self.messages[0])
        self.assertTrue(self.messages[-1].startswith("✅ Booked"))

    def test_book_waitlisted_reports_position(self):
        code, out = self.book_with(6, "STANDBY_POSITION=3")
        self.assertEqual(code, 0, out)
        self.assertIn("number 3", self.messages[-1])

    def test_book_not_found_lists_the_day(self):
        lines = ("[x] Available classes:\\n[x]   06:00  W.O.D Hall A  (Coach)\\n"
                 "[x]   07:00  HYROX  (Coach)")
        code, out = self.book_with(4, lines)
        self.assertEqual(code, 1)
        self.assertIn("06:00 W.O.D Hall A", self.messages[-1])
        self.assertIn("07:00 HYROX", self.messages[-1])

    def test_book_failure_quotes_the_gym(self):
        code, out = self.book_with(5, "[x]   HTTP 400: Registration is not open yet")
        self.assertEqual(code, 1)
        self.assertIn("Registration is not open yet", self.messages[-1])
        self.assertEqual(self.queued(), [])

    def test_book_when_no_longer_queued(self):
        code, out = self.cli("book", "--date", "2099-01-07", "--time", "08:00")
        self.assertEqual(code, 0)
        self.assertIn("no longer queued", out)


class NotifyTest(unittest.TestCase):
    """A Telegram message that can't be sent leaves a warning on the GitHub run."""

    def setUp(self):
        self.old_env = {k: os.environ.get(k) for k in ("GITHUB_ACTIONS", "BOT_TOKEN", "OWNER_CHAT_ID")}
        self.urlopen = bookings.urllib.request.urlopen
        os.environ["GITHUB_ACTIONS"] = "true"

    def tearDown(self):
        bookings.urllib.request.urlopen = self.urlopen
        for k, v in self.old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def send(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            bookings.notify("hello")
        return out.getvalue()

    def test_missing_secret(self):
        os.environ.pop("BOT_TOKEN", None)
        os.environ["OWNER_CHAT_ID"] = "1"
        self.assertIn("::warning::No Telegram message sent", self.send())

    def test_refused_with_telegrams_reason(self):
        os.environ.update(BOT_TOKEN="123:abc", OWNER_CHAT_ID="1")

        def refuse(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 400, "Bad Request", {}, io.BytesIO(
                b'{"ok":false,"error_code":400,"description":"Bad Request: chat not found"}'))

        bookings.urllib.request.urlopen = refuse
        out = self.send()
        self.assertIn("::warning::Telegram refused the message (HTTP 400)", out)
        self.assertIn("chat not found", out)
        self.assertNotIn("123:abc", out, "the token never reaches the log")


class SaveRaceTest(unittest.TestCase):
    """Two runs save to GitHub at once: the loser redoes its change on top."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.remote = root / "remote.git"
        self.git(root, "init", "--quiet", "--bare", "--initial-branch=main", str(self.remote))
        seed = root / "seed"
        self.git(root, "clone", "--quiet", str(self.remote), str(seed))
        (seed / "queue").mkdir()
        (seed / "queue" / "README.md").write_text("queue\n", "utf-8")
        self.git(seed, "add", ".")
        self.git(seed, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "--quiet", "-m", "seed")
        self.git(seed, "push", "--quiet", "origin", "HEAD:main")
        self.a, self.b = root / "a", root / "b"
        for clone in (self.a, self.b):
            self.git(root, "clone", "--quiet", str(self.remote), str(clone))
        self.saved = (bookings.HERE, bookings.QUEUE_DIR, bookings.BRANCH)
        os.environ["GITHUB_ACTIONS"] = "true"

    def tearDown(self):
        bookings.HERE, bookings.QUEUE_DIR, bookings.BRANCH = self.saved
        os.environ.pop("GITHUB_ACTIONS", None)
        self.tmp.cleanup()

    @staticmethod
    def git(cwd, *args):
        return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)

    def save_in(self, clone, name):
        bookings.HERE, bookings.QUEUE_DIR, bookings.BRANCH = clone, clone / "queue", "main"
        path = clone / "queue" / name
        with contextlib.redirect_stdout(io.StringIO()):
            return bookings.save_change(lambda: path.write_text("{}", "utf-8"), f"add {name}")

    def test_both_changes_survive(self):
        self.assertTrue(self.save_in(self.a, "2099-01-07_0800.json"))
        # b was cloned before a's push, so its first push is rejected.
        self.assertTrue(self.save_in(self.b, "2099-01-08_0900.json"))
        self.git(self.a, "fetch", "--quiet")
        listed = self.git(self.a, "ls-tree", "--name-only", "origin/main", "queue/").stdout.split()
        self.assertEqual(sorted(listed), ["queue/2099-01-07_0800.json",
                                          "queue/2099-01-08_0900.json", "queue/README.md"])


class HelpersTest(unittest.TestCase):
    def test_classes_listed(self):
        lines = ["[t] No class found at 08:00", "[t] Available classes:",
                 "[t]   06:00  W.O.D Hall A  (Coach One)", "[t]   07:00  HYROX  (Coach Two)"]
        self.assertEqual(bookings.classes_listed(lines), ["06:00 W.O.D Hall A", "07:00 HYROX"])

    def test_run_title_matches_workflow(self):
        workflow = (Path(bookings.__file__).parent / ".github" / "workflows" / "book-class.yml")
        text = workflow.read_text("utf-8")
        self.assertIn("run-name: Book ${{ inputs.class_date }} ${{ inputs.class_time }}", text)
        self.assertEqual(bookings.run_title({"date": "2099-01-07", "time": "08:00"}),
                         "Book 2099-01-07 08:00")


if __name__ == "__main__":
    unittest.main()
