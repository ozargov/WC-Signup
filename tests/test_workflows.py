"""The GitHub workflow files: valid YAML, and wired to what the scripts expect.
GitHub silently ignores a workflow it can't parse, so this is worth checking.

Needs PyYAML; skipped without it.
"""

import re
import sys
import unittest
from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"
KNOWN_SECRETS = {"ARBOX_EMAIL", "ARBOX_PASSWORD", "ARBOX_WHITELABEL", "ARBOX_BOXES_ID",
                 "ARBOX_LOCATIONS_BOX_ID", "ARBOX_MEMBERSHIP_USER_ID", "BOT_TOKEN",
                 "OWNER_CHAT_ID"}
COMMANDS = {"add", "remove", "add-many", "remove-many", "list", "plan", "book"}


@unittest.skipIf(yaml is None, "PyYAML not installed")
class WorkflowTest(unittest.TestCase):
    def load(self, name):
        doc = yaml.safe_load((WORKFLOWS / name).read_text("utf-8"))
        doc["on"] = doc.pop(True, doc.get("on"))  # YAML 1.1 reads the key `on` as True
        return doc

    def test_exactly_these_workflows(self):
        self.assertEqual(sorted(p.name for p in WORKFLOWS.glob("*.yml")),
                         ["add-booking.yml", "book-class.yml", "book-due.yml", "bot-queue.yml"])

    def test_all_parse_and_can_be_started_by_hand(self):
        for path in WORKFLOWS.glob("*.yml"):
            doc = self.load(path.name)
            self.assertIn("workflow_dispatch", doc["on"], path.name)
            self.assertIn("jobs", doc, path.name)

    def test_scripts_called_correctly(self):
        for path in WORKFLOWS.glob("*.yml"):
            text = path.read_text("utf-8")
            for cmd in re.findall(r"python3 bookings\.py \"?\$?(\w+)", text):
                if cmd != "QUEUE_ACTION":
                    self.assertIn(cmd, COMMANDS, path.name)
            for secret in re.findall(r"secrets\.(\w+)", text):
                self.assertIn(secret, KNOWN_SECRETS, path.name)

    def test_book_class(self):
        doc = self.load("book-class.yml")
        self.assertEqual(doc["run-name"], "Book ${{ inputs.class_date }} ${{ inputs.class_time }}")
        self.assertEqual(doc["concurrency"]["group"],
                         "book-${{ inputs.class_date }}-${{ inputs.class_time }}")
        self.assertFalse(doc["concurrency"]["cancel-in-progress"])
        # 50 min lookahead + 60 min second clock-change time + burst and margin.
        self.assertGreaterEqual(doc["jobs"]["book"]["timeout-minutes"], 125)
        self.assertEqual(doc["permissions"], {"contents": "write"})

    def test_planner_and_buttons_can_start_runs(self):
        for name in ("add-booking.yml", "book-due.yml", "bot-queue.yml"):
            self.assertEqual(self.load(name)["permissions"].get("actions"), "write", name)

    def test_bot_queue_matches_the_bot(self):
        sys.path.insert(0, str(ROOT))
        import wcbot
        doc = self.load(wcbot.QUEUE_WORKFLOW)
        inputs = doc["on"]["workflow_dispatch"]["inputs"]
        self.assertEqual(set(inputs), {"action", "classes", "summary"})
        self.assertEqual(inputs["action"]["options"], ["add", "remove"])
        self.assertFalse(inputs["summary"]["required"], "an older bot sends no summary")
        self.assertIn("inputs.summary", doc["run-name"])
        self.assertNotIn("concurrency", doc)

    def test_timer_checks_for_stuck_runs(self):
        steps = self.load("book-due.yml")["jobs"]["plan"]["steps"]
        self.assertTrue(any("--check-stuck" in str(s.get("run", "")) for s in steps))

    def test_requests_are_named_after_their_classes(self):
        self.assertIn("inputs.class_date", self.load("add-booking.yml")["run-name"])

    def test_backup_timer_avoids_the_top_of_the_hour(self):
        crons = [s["cron"] for s in self.load("book-due.yml")["on"]["schedule"]]
        for cron in crons:
            minutes = cron.split()[0].split(",")
            self.assertNotIn("0", minutes)
            self.assertNotIn("*/30", minutes)

    def test_no_shared_lock_between_button_and_runs(self):
        # A shared group lets GitHub cancel a waiting "Add booking" press.
        self.assertNotIn("concurrency", self.load("add-booking.yml"))
        self.assertNotIn("concurrency", self.load("add-booking.yml")["jobs"]["queue"])


if __name__ == "__main__":
    unittest.main()
