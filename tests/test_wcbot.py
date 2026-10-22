"""wcbot.py -- the Telegram bot -- against fake Telegram, GitHub and gym APIs,
plus the Vercel entry point (api/telegram.py) over a real local HTTP server.

Run from the project folder:  python -m unittest discover -s tests -v
"""

import base64
import html
import importlib.util
import json
import os
import re
import sys
import threading
import unittest
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from http.server import HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import wcbot  # noqa: E402

OWNER = 111
NOW = datetime(2026, 10, 19, 15, 0)  # a Monday afternoon


def cls(cid, t, name, **extra):
    base = {"id": cid, "time": f"{t}:00", "box_categories": {"name": name}, "free": 20,
            "max_users": 20, "stand_by": 0, "booking_option": "insertScheduleUser",
            "user_booked": None, "user_in_standby": None}
    base.update(extra)
    return base


def plain(text):
    """What Telegram hands back as message.text: the HTML rendered to plain text."""
    return html.unescape(re.sub(r"<[^>]+>", "", text))


class FakeWorld:
    def __init__(self):
        self.tg_calls, self.dispatches, self.arbox_calls, self.range_calls = [], [], [], []
        self.dispatch_status = 204
        self.queue_files = {}
        self.days = {}
        self.range_down = False
        self.next_id = 100

    def tg(self, method, **params):
        self.tg_calls.append((method, params))
        if method == "sendMessage":
            self.next_id += 1
            return {"message_id": self.next_id}
        return True

    def github(self, method, path, body=None):
        if method == "POST" and path.endswith("/dispatches"):
            self.dispatches.append(body["inputs"])
            return self.dispatch_status, None
        if method == "GET" and path.endswith("/contents/queue"):
            return 200, [{"name": n} for n in sorted(self.queue_files)] + [{"name": "README.md"}]
        if method == "GET" and "/contents/queue/" in path:
            entry = self.queue_files[path.rsplit("/", 1)[1]]
            return 200, {"content": base64.b64encode(json.dumps(entry).encode()).decode()}
        if method == "GET" and path.endswith("/actions/workflows"):
            return 200, {"workflows": []}
        return 404, None

    def arbox_day(self, ds):
        self.arbox_calls.append(ds)
        return self.days.get(ds, [])

    def arbox_range(self, first, last):
        self.range_calls.append((first.isoformat(), last.isoformat()))
        if self.range_down:
            return None
        out, d = [], first
        while d <= last:
            out += [dict(c, date=d.isoformat()) for c in self.days.get(d.isoformat()) or []]
            d += timedelta(days=1)
        return out

    # --- what the user sees -------------------------------------------------
    def last(self, method):
        return next(p for m, p in reversed(self.tg_calls) if m == method)

    def shown(self):
        """The picker message as Telegram would show it, from the last send or edit."""
        m, p = next((m, p) for m, p in reversed(self.tg_calls)
                    if m in ("sendMessage", "editMessageText"))
        return {"message_id": p.get("message_id", self.next_id), "chat": {"id": OWNER},
                "text": plain(p["text"]) if p.get("parse_mode") == "HTML" else p["text"],
                "reply_markup": p.get("reply_markup") or {"inline_keyboard": []}}

    def buttons(self):
        return [b for row in self.shown()["reply_markup"]["inline_keyboard"] for b in row]

    def button(self, starts_with):
        for b in self.buttons():
            if b["text"].lstrip("✅ ").startswith(starts_with) or b["text"].startswith(starts_with):
                return b
        raise AssertionError(f"no button {starts_with!r} in {[b['text'] for b in self.buttons()]}")


class BotTest(unittest.TestCase):
    def setUp(self):
        self.world = FakeWorld()
        self.saved = {n: getattr(wcbot, n) for n in ("tg", "github", "arbox_day", "arbox_range", "now")}
        wcbot.tg, wcbot.github, wcbot.arbox_day = self.world.tg, self.world.github, self.world.arbox_day
        wcbot.arbox_range = self.world.arbox_range
        wcbot.now = lambda: NOW
        self.env = {"BOT_TOKEN": "123:abc", "OWNER_CHAT_ID": str(OWNER), "ARBOX_EMAIL": "a@example.com",
                    "ARBOX_PASSWORD": "x", "GITHUB_TOKEN": "gh", "GITHUB_REPO": "ozargov/WC-Signup"}
        self.old_env = {k: os.environ.get(k) for k in self.env}
        os.environ.update(self.env)
        w = self.world
        w.days["2026-10-25"] = [cls(1, "07:00", "W.O.D Hall A"), cls(2, "07:00", "HYROX"),
                                cls(3, "18:00", "W.O.D Hall A")]
        w.days["2026-10-27"] = [cls(4, "08:00", "W.O.D Hall A"), cls(5, "19:00", "Gymnastics Hall B")]

    def tearDown(self):
        for n, v in self.saved.items():
            setattr(wcbot, n, v)
        for k, v in self.old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def say(self, text, user=OWNER):
        wcbot.handle_update({"message": {"chat": {"id": user}, "from": {"id": user}, "text": text}})

    def tap(self, data, user=OWNER):
        wcbot.handle_update({"callback_query": {"id": "cq", "from": {"id": user}, "data": data,
                                                "message": self.world.shown()}})

    def tap_label(self, starts_with):
        self.tap(self.world.button(starts_with)["callback_data"])

    # --- the main flow ------------------------------------------------------
    def test_any_message_shows_next_week(self):
        self.say("hi")
        sent = self.world.last("sendMessage")
        self.assertIn("showing <b>Sun 25 Oct</b>", sent["text"])
        self.assertEqual(self.world.arbox_calls, ["2026-10-25"])
        labels = [b["text"] for b in self.world.buttons()]
        self.assertIn("« Previous week", labels)
        self.assertIn("Next week »", labels)
        self.assertIn("▸Sun 25", labels)
        self.assertIn("Sat 31", labels)
        # Sunday 25 Oct is the clock-change day: it opens an hour later than usual.
        self.assertIn("07:00 W.O.D Hall A · opens Thu 08:00", labels)
        self.assertEqual(self.world.button("📌 Book")["callback_data"], "n")

    def test_pick_switch_days_and_book(self):
        self.say("next week please")
        self.tap_label("07:00 W.O.D Hall A")
        self.assertIn("• Sun 25/10 07:00 W.O.D Hall A", self.world.shown()["text"])
        self.assertTrue(self.world.button("✅ 07:00 W.O.D Hall A")["text"].startswith("✅"))
        self.assertEqual(self.world.button("▸Sun 25")["text"], "▸Sun 25 ✅")
        self.assertEqual(self.world.button("📌 Book")["text"], "📌 Book 1 class")
        self.assertEqual(self.world.arbox_calls, ["2026-10-25"], "ticking needs no gym call")

        # One class a day: HYROX at the same time replaces Hall A.
        self.tap_label("07:00 HYROX")
        text = self.world.shown()["text"]
        self.assertIn("• Sun 25/10 07:00 HYROX", text)
        self.assertEqual(text.count("• Sun 25/10"), 1)

        # Another day keeps the pick.
        self.tap("d:2026-10-27")
        self.assertEqual(self.world.arbox_calls[-1], "2026-10-27")
        self.assertIn("showing Tue 27 Oct", self.world.shown()["text"])
        self.assertIn("• Sun 25/10 07:00 HYROX", self.world.shown()["text"])
        self.tap_label("19:00 Gymnastics Hall B")
        self.assertEqual(self.world.button("📌 Book")["text"], "📌 Book 2 classes")

        self.tap("go")
        sent = self.world.dispatches[-1]
        self.assertEqual(sent["action"], "add")
        self.assertEqual(json.loads(sent["classes"]), [
            {"date": "2026-10-25", "time": "07:00", "category": "HYROX"},
            {"date": "2026-10-27", "time": "19:00", "category": "Gymnastics Hall B"},
        ])
        self.assertEqual(sent["summary"], "Sun 25/10 07:00, Tue 27/10 19:00")
        self.assertIn("Sending 2 classes", self.world.last("editMessageText")["text"])

    def test_untick(self):
        self.say("hi")
        self.tap_label("18:00 W.O.D Hall A")
        self.tap_label("✅ 18:00 W.O.D Hall A")
        self.assertIn("Nothing selected yet.", self.world.shown()["text"])
        self.assertEqual(self.world.button("📌 Book")["callback_data"], "n")

    def test_book_with_nothing_picked(self):
        self.say("hi")
        self.tap("n")
        self.assertEqual(self.world.dispatches, [])
        self.assertEqual(self.world.last("answerCallbackQuery")["text"], "Pick a class first.")

    def test_github_refuses_the_key(self):
        self.say("hi")
        self.tap_label("07:00 W.O.D Hall A")
        self.world.dispatch_status = 401
        self.tap("go")
        self.assertIn("refused the bot's key", self.world.last("sendMessage")["text"])

    def test_states_of_classes_this_week(self):
        w = self.world
        w.days["2026-10-20"] = [
            cls(10, "06:00", "Early", free=3),                             # signup open
            cls(11, "07:00", "Packed", free=0, stand_by=4),                # open and full
            cls(12, "08:00", "Mine", user_booked=999),                     # already booked
            cls(13, "09:00", "Waiting", user_in_standby=5, stand_by_position=2),
            cls(14, "10:00", "Queued one"),
        ]
        w.queue_files["2026-10-20_1000.json"] = {"date": "2026-10-20", "time": "10:00", "category": ""}
        self.say("hi")
        self.tap("d:2026-10-20")
        labels = [b["text"] for b in w.buttons()]
        self.assertIn("06:00 Early · 3 free", labels)
        self.assertIn("07:00 Packed · full, 4 waiting", labels)
        self.assertIn("☑️ 08:00 Mine · booked", labels)
        self.assertIn("🕒 09:00 Waiting · waiting list #2", labels)
        self.assertIn("📌 10:00 Queued one · queued", labels)
        self.tap("n:booked")
        self.assertIn("already booked", w.last("answerCallbackQuery")["text"])

    def test_past_classes_and_days_hidden(self):
        self.world.days["2026-10-19"] = [cls(20, "07:00", "Gone"), cls(21, "18:00", "Tonight")]
        self.say("today")
        labels = [b["text"] for b in self.world.buttons()]
        self.assertNotIn("Sun 18", " ".join(labels))
        self.assertFalse(any("Gone" in l for l in labels))
        self.assertTrue(any("Tonight" in l for l in labels))

    def test_gym_unreachable(self):
        self.world.days["2026-10-25"] = None
        self.say("hi")
        labels = [b["text"] for b in self.world.buttons()]
        self.assertIn("⚠️ Couldn't load the classes - tap to retry", labels)

    # --- typing instead of tapping -----------------------------------------
    def test_typed_day_and_time_preselects(self):
        self.world.days["2026-10-21"] = [cls(30, "18:00", "W.O.D Hall A"), cls(31, "18:00", "HYROX")]
        self.say("wed 18:00 hall a")
        self.assertIn("• Wed 21/10 18:00 W.O.D Hall A", plain(self.world.last("sendMessage")["text"]))

    def test_typed_hebrew_and_next_week_rollover(self):
        self.world.days["2026-10-21"] = [cls(32, "07:00", "W.O.D Hall A")]
        self.say("יום רביעי 7:00")
        self.assertIn("• Wed 21/10 07:00 W.O.D Hall A", plain(self.world.last("sendMessage")["text"]))
        self.world.days["2026-10-26"] = [cls(33, "07:00", "W.O.D Hall A")]
        self.say("mon 07:00")   # Monday 07:00 has passed today, so next Monday
        self.assertEqual(self.world.arbox_calls[-1], "2026-10-26")

    def test_typed_when_gym_unreachable_queues_as_typed(self):
        self.world.days["2026-10-21"] = None
        self.say("wed 18:00 hall a")
        self.assertEqual(json.loads(self.world.dispatches[-1]["classes"]),
                         [{"date": "2026-10-21", "time": "18:00", "category": "hall a"}])

    def test_typed_day_only_opens_that_day(self):
        self.say("tomorrow")
        self.assertIn("showing <b>Tue 20 Oct</b>", self.world.last("sendMessage")["text"])

    # --- the queue ----------------------------------------------------------
    def test_list_and_remove(self):
        w = self.world
        w.queue_files["2026-10-25_0700.json"] = {"date": "2026-10-25", "time": "07:00", "category": "HYROX"}
        w.queue_files["2026-10-27_1900.json"] = {"date": "2026-10-27", "time": "19:00", "category": "Gym"}
        self.say("/list")
        sent = w.last("sendMessage")
        self.assertIn("• Sun 25/10 07:00 HYROX - books Thu 22/10 08:00", sent["text"])
        self.tap_label("❌ Remove Tue 27/10 19:00")
        self.assertEqual(w.dispatches[-1]["action"], "remove")
        self.assertEqual(json.loads(w.dispatches[-1]["classes"]), [{"date": "2026-10-27", "time": "19:00"}])
        remaining = [b["text"] for b in w.buttons()]
        self.assertEqual(remaining, ["❌ Remove Sun 25/10 07:00"])

    def test_empty_list(self):
        self.say("list")
        self.assertIn("Nothing is queued", self.world.last("sendMessage")["text"])

    # --- what is booked -----------------------------------------------------
    def test_booked_shows_booked_and_waiting(self):
        w = self.world
        w.days["2026-10-19"] = [cls(40, "07:00", "Earlier today", user_booked=1),  # over: hidden
                                cls(41, "18:00", "Tonight", user_booked=2)]
        w.days["2026-10-21"] = [cls(42, "17:00", "W.O.D Hall A", user_booked=3),
                                cls(43, "19:00", "HYROX", is_booked=1)]
        w.days["2026-10-22"] = [cls(44, "08:00", "Gymnastics", user_in_standby=7,
                                    stand_by_position=3)]
        w.queue_files["2026-10-25_0800.json"] = {"date": "2026-10-25", "time": "08:00",
                                                 "category": "W.O.D Hall A"}
        self.say("/booked")
        text = plain(w.last("sendMessage")["text"])
        self.assertIn("✅ Booked (3)", text)
        self.assertIn("• Mon 19/10 18:00 Tonight", text)
        self.assertIn("• Wed 21/10 17:00 W.O.D Hall A", text)
        self.assertIn("• Wed 21/10 19:00 HYROX", text)
        self.assertNotIn("Earlier today", text)
        self.assertIn("🕒 Waiting list (1)", text)
        self.assertIn("• Thu 22/10 08:00 Gymnastics - #3", text)
        self.assertIn("📌 1 more queued", text)
        self.assertEqual(w.range_calls, [("2026-10-19", "2026-11-02")], "one request, 14 days")

    def test_what_is_booked_in_words(self):
        self.say("What is booked?")
        self.assertIn("Your classes", self.world.last("sendMessage")["text"])
        self.assertIn("Nothing booked yet", self.world.last("sendMessage")["text"])

    def test_booked_when_gym_unreachable(self):
        self.world.range_down = True
        self.say("/booked")
        self.assertIn("Couldn't reach the gym", self.world.last("sendMessage")["text"])

    def test_start_refreshes_the_menu(self):
        self.say("/start")
        commands = [c["command"] for c in self.world.last("setMyCommands")["commands"]]
        self.assertEqual(commands, ["book", "booked", "list", "test", "help"])

    def test_test_asks_github_to_message_you(self):
        self.say("/test")
        self.assertEqual(self.world.dispatches[-1]["action"], "ping")
        self.assertEqual(json.loads(self.world.dispatches[-1]["classes"]), [])
        self.assertIn("Asked GitHub to message you", self.world.last("sendMessage")["text"])
        self.world.dispatch_status = 401
        self.say("/test")
        self.assertIn("refused the bot's key", plain(self.world.last("sendMessage")["text"]))

    # --- who may use it -----------------------------------------------------
    def test_strangers_are_turned_away(self):
        self.say("hi", user=999)
        self.assertEqual(self.world.last("sendMessage")["text"], "This is a private bot.")
        self.assertEqual(self.world.arbox_calls, [])
        self.say("hi")
        self.tap("go", user=999)
        self.assertEqual(self.world.dispatches, [])

    def test_errors_are_reported_not_raised(self):
        def boom(ds):
            raise RuntimeError("gym exploded")
        wcbot.arbox_day = boom
        self.say("hi")
        self.assertIn("gym exploded", self.world.last("sendMessage")["text"])

    # --- helpers ------------------------------------------------------------
    def test_selection_year_rollover(self):
        self.assertEqual(wcbot.parse_selected("• Fri 01/01 07:00 X", date(2026, 12, 30)),
                         [("2027-01-01", "07:00", "X")])

    def test_secret(self):
        self.assertTrue(wcbot.secret_ok(wcbot.webhook_secret()))
        self.assertFalse(wcbot.secret_ok("guess"))
        self.assertFalse(wcbot.secret_ok(None))

    def test_setup(self):
        os.environ.pop("ARBOX_PASSWORD")
        ok, page = wcbot.setup_webhook("https://example.vercel.app/api/telegram")
        self.assertTrue(ok)
        hook = self.world.last("setWebhook")
        self.assertEqual(hook["url"], "https://example.vercel.app/api/telegram")
        self.assertEqual(hook["secret_token"], wcbot.webhook_secret())
        self.assertIn("Still missing on Vercel: ARBOX_PASSWORD", page)
        self.assertIn("The GitHub key works", page)


class VercelEntryTest(unittest.TestCase):
    """api/telegram.py, served by a real HTTP server on this machine."""

    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("api_telegram", ROOT / "api" / "telegram.py")
        cls.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.mod)
        cls.server = HTTPServer(("127.0.0.1", 0), cls.mod.handler)
        cls.url = f"http://127.0.0.1:{cls.server.server_port}/api/telegram"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.saved = (wcbot.handle_update, wcbot.setup_webhook)
        self.updates, self.setups = [], []
        wcbot.handle_update = self.updates.append
        wcbot.setup_webhook = lambda url: (self.setups.append(url) or (True, "<h3>ok</h3>"))
        self.old_token = os.environ.get("BOT_TOKEN")
        os.environ["BOT_TOKEN"] = "123:abc"

    def tearDown(self):
        wcbot.handle_update, wcbot.setup_webhook = self.saved
        if self.old_token is None:
            os.environ.pop("BOT_TOKEN", None)
        else:
            os.environ["BOT_TOKEN"] = self.old_token

    def post(self, secret):
        req = urllib.request.Request(self.url, data=b'{"update_id": 1}', method="POST",
                                     headers={"X-Telegram-Bot-Api-Secret-Token": secret,
                                              "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status
        except urllib.error.HTTPError as e:
            with e:
                return e.code

    def test_only_telegram_gets_in(self):
        self.assertEqual(self.post("wrong"), 403)
        self.assertEqual(self.updates, [])
        self.assertEqual(self.post(wcbot.webhook_secret()), 200)
        self.assertEqual(self.updates, [{"update_id": 1}])

    def test_setup_link(self):
        with urllib.request.urlopen(self.url + "?setup=1", timeout=10) as r:
            self.assertEqual(r.status, 200)
            self.assertIn("ok", r.read().decode())
        self.assertEqual(self.setups, [self.url.replace("http://", "https://")])

    def test_plain_visit(self):
        with urllib.request.urlopen(self.url, timeout=10) as r:
            self.assertEqual(r.read().decode(), "WC Signup bot is running.")


if __name__ == "__main__":
    unittest.main()
