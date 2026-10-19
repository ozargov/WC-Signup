#!/usr/bin/env python3
"""
WC Signup - the Telegram bot.

Send the bot any message and it shows next week's classes. Tap a day, tap the
class you want (one per day), then Book: the bot hands your picks to the
booking queue on GitHub, and from there each class is booked the second its
signup opens (see bookings.py and book_class.py).

It runs on Vercel as api/telegram.py, as a webhook: Telegram calls it when you
send something, so nothing has to stay running in between.

Vercel environment variables:
  BOT_TOKEN        the Telegram bot's token (from @BotFather)
  OWNER_CHAT_ID    your Telegram user id; the bot ignores everyone else
  ARBOX_EMAIL      to read the gym's class list
  ARBOX_PASSWORD
  GITHUB_TOKEN     fine-grained token for the repo with Actions: read and write.
                   It can start workflows; it can't change code or read secrets.
  GITHUB_REPO      optional, default ozargov/WC-Signup
"""

import base64
import hashlib
import hmac
import html
import json
import os
import re
import time
import traceback
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta

import book_class
from book_class import window_candidates_for

QUEUE_WORKFLOW = "bot-queue.yml"
WEEKS_AHEAD    = 2      # pick from this week and the next two
ARBOX_REUSE    = 600    # reuse a gym login for this many seconds

HELP = (
    "🏋️ <b>WC Signup</b>\n\n"
    "• Send me any message and I'll show next week's classes. Tap a day, tap the "
    "class you want (one per day), then tap <b>📌 Book</b>.\n"
    "• Or type a day and time, like <i>wed 18:00 hall a</i> or <i>רביעי 7:00</i>.\n"
    "• /list shows what's queued, with buttons to remove.\n\n"
    "I book each class the second its signup opens and message you: ⏳ ready, "
    "✅ booked, or 🕒 waiting list."
)

NOOP_TEXT = {
    "booked":  "You're already booked into this one.",
    "standby": "You're already on its waiting list.",
    "queued":  "Already queued - send /list to remove it.",
    "none":    "No classes left that day - pick another day.",
    "":        "Pick a class first.",
}

WEEKDAYS = {
    "sun": 6, "sunday": 6, "mon": 0, "monday": 0, "tue": 1, "tues": 1, "tuesday": 1,
    "wed": 2, "wednesday": 2, "thu": 3, "thur": 3, "thurs": 3, "thursday": 3,
    "fri": 4, "friday": 4, "sat": 5, "saturday": 5,
    "ראשון": 6, "שני": 0, "שלישי": 1, "רביעי": 2, "חמישי": 3, "שישי": 4, "שבת": 5,
}


def env(name, default=""):
    return (os.environ.get(name) or default).strip()


def repo():
    return env("GITHUB_REPO", "ozargov/WC-Signup")


def now():
    """Israel wall-clock time, naive, to compare with class times -- Vercel runs on UTC."""
    if book_class.GYM_TZ is not None:
        return datetime.now(book_class.GYM_TZ).replace(tzinfo=None)
    return datetime.now()


def esc(text):
    """Make text safe inside a Telegram HTML message (quotes can stay as they are)."""
    return html.escape(text, quote=False)


def log(msg):
    print(msg, flush=True)


# ---------------------------------------------------------------------------
# Webhook security: Telegram sends this secret with every update, and only
# Telegram knows it, because setup_webhook() gave it to Telegram. It's derived
# from the bot token, so there is no extra secret to keep.
# ---------------------------------------------------------------------------
def webhook_secret():
    return hashlib.sha256(f"wc-signup-webhook:{env('BOT_TOKEN')}".encode()).hexdigest()[:48]


def secret_ok(header_value):
    return bool(env("BOT_TOKEN")) and hmac.compare_digest(header_value or "", webhook_secret())


def is_owner(user_id):
    owner = env("OWNER_CHAT_ID")
    return bool(owner) and str(user_id) == owner


# ---------------------------------------------------------------------------
# Telegram, GitHub and the gym
# ---------------------------------------------------------------------------
def tg(method, **params):
    """Call the Telegram Bot API. Returns its result, or None on failure."""
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{env('BOT_TOKEN')}/{method}",
        data=json.dumps(params).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read()).get("result")
    except urllib.error.HTTPError as e:
        with e:
            body = e.read().decode("utf-8", "replace")
        if "message is not modified" not in body:
            log(f"Telegram {method} failed: HTTP {e.code} {body[:200]}")
    except (urllib.error.URLError, OSError, ValueError) as e:
        log(f"Telegram {method} failed: {e}")
    return None


def github(method, path, body=None):
    """Call the GitHub API. Returns (status, parsed JSON or None); 599 if unreachable."""
    req = urllib.request.Request(
        f"https://api.github.com{path}",
        method=method,
        data=None if body is None else json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {env('GITHUB_TOKEN')}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "wc-signup-bot",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        e.close()
        log(f"GitHub {method} {path} -> HTTP {e.code}")
        return e.code, None
    except (urllib.error.URLError, OSError, ValueError) as e:
        log(f"GitHub {method} {path} failed: {e}")
        return 599, None


def send_to_queue(action, classes):
    """Start the "Queue from Telegram" workflow. Returns None, or what went wrong."""
    status, _ = github("POST", f"/repos/{repo()}/actions/workflows/{QUEUE_WORKFLOW}/dispatches",
                       {"ref": "main", "inputs": {"action": action, "classes": json.dumps(classes)}})
    if status in (200, 204):
        return None
    if status in (401, 403):
        return ("GitHub refused the bot's key - it may have expired. Make a new one and "
                "update GITHUB_TOKEN on Vercel.")
    if status == 404:
        return ("GitHub couldn't find the queue workflow - check that the bot's key covers "
                "the WC-Signup repo.")
    return f"GitHub didn't answer properly (HTTP {status}). Try again in a minute."


QUEUE_FILE = re.compile(r"^(\d{4}-\d{2}-\d{2})_(\d{2})(\d{2})\.json$")


def queued_slots():
    """{(date, "HH:MM")} already in the queue, read from the repo."""
    status, items = github("GET", f"/repos/{repo()}/contents/queue")
    slots = set()
    if status == 200 and isinstance(items, list):
        for item in items:
            m = QUEUE_FILE.match(item.get("name", ""))
            if m:
                slots.add((m.group(1), f"{m.group(2)}:{m.group(3)}"))
    return slots


def queued_entries():
    """[(date, "HH:MM", class name)] in the queue, earliest first; None if unreadable."""
    status, items = github("GET", f"/repos/{repo()}/contents/queue")
    if status == 404:
        return []
    if status != 200 or not isinstance(items, list):
        return None
    out = []
    for item in items:
        m = QUEUE_FILE.match(item.get("name", ""))
        if not m:
            continue
        name = ""
        s, data = github("GET", f"/repos/{repo()}/contents/queue/{item['name']}")
        if s == 200 and isinstance(data, dict) and data.get("content"):
            try:
                name = json.loads(base64.b64decode(data["content"])).get("category", "")
            except ValueError:
                pass
        out.append((m.group(1), f"{m.group(2)}:{m.group(3)}", name))
    return sorted(out)


_ARBOX = {"auth": None, "at": 0.0}


def arbox_day(date_s):
    """The gym's classes on that day, or None if the gym can't be reached."""
    email, password = env("ARBOX_EMAIL"), env("ARBOX_PASSWORD")
    if not email or not password:
        return None
    whitelabel = book_class._env("ARBOX_WHITELABEL", "HYPR-training")
    boxes_id = book_class._env_int("ARBOX_BOXES_ID", 59)
    loc_box_id = book_class._env_int("ARBOX_LOCATIONS_BOX_ID", 48)
    for fresh in (False, True):
        if fresh or _ARBOX["auth"] is None or time.time() - _ARBOX["at"] > ARBOX_REUSE:
            _ARBOX["auth"] = book_class.try_login(email, password, whitelabel, attempts=1)
            _ARBOX["at"] = time.time()
            if _ARBOX["auth"] is None:
                return None
        token, refresh = _ARBOX["auth"]
        classes = book_class.fetch_schedule(date_s, loc_box_id, boxes_id, token, refresh,
                                            whitelabel, must=False)
        if classes is not None:
            return classes
    return None


# ---------------------------------------------------------------------------
# Messages and buttons
# ---------------------------------------------------------------------------
def send(chat_id, text, keyboard=None, as_html=True):
    params = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
    if as_html:
        params["parse_mode"] = "HTML"
    if keyboard:
        params["reply_markup"] = keyboard
    return tg("sendMessage", **params)


def edit(msg, text, keyboard=None, as_html=True):
    params = {"chat_id": msg["chat"]["id"], "message_id": msg["message_id"], "text": text,
              "disable_web_page_preview": True,
              "reply_markup": keyboard or {"inline_keyboard": []}}
    if as_html:
        params["parse_mode"] = "HTML"
    return tg("editMessageText", **params)


def answer(cq, text="", alert=False):
    tg("answerCallbackQuery", callback_query_id=cq["id"], text=text, show_alert=alert)


def btn(text, data):
    return {"text": text, "callback_data": data}


def week_start(d):
    """The Sunday that starts d's week (Israeli weeks run Sunday to Saturday)."""
    return d - timedelta(days=(d.weekday() + 1) % 7)


def selection_line(sel):
    d = date.fromisoformat(sel[0])
    return f"• {d:%a} {d.day:02d}/{d.month:02d} {sel[1]} {sel[2]}"


SELECTION = re.compile(r"^• \w{3} (\d{1,2})/(\d{1,2}) (\d{2}:\d{2}) (.+)$")


def parse_selected(text, today):
    """The picks, [(date, "HH:MM", class name)], read back from the message's
    "• Sun 25/10 07:00 W.O.D Hall A" lines. The message itself holds the state,
    so the bot needs no storage of its own."""
    out = []
    for line in (text or "").splitlines():
        m = SELECTION.match(line.strip())
        if not m:
            continue
        found = None
        for year in (today.year, today.year + 1):
            try:
                d = date(year, int(m.group(2)), int(m.group(1)))
            except ValueError:
                continue
            if d >= today - timedelta(days=1):
                found = d
                break
        if found:
            out.append((found.isoformat(), m.group(3), m.group(4).strip()))
    return sorted(out)


def picker_text(shown, selected, note=""):
    lines = ["📅 <b>Pick your classes</b>",
             f"Week of {week_start(shown):%a %d %b} · showing <b>{shown:%a %d %b}</b>", ""]
    if selected:
        lines.append(f"<b>Selected ({len(selected)}):</b>")
        lines += [esc(selection_line(s)) for s in selected]
    else:
        lines.append("Nothing selected yet.")
    lines += ["", "Tap a day, then the class you want (one per day). Then tap <b>📌 Book</b>."]
    if note:
        lines += ["", note]
    return "\n".join(lines)


def book_button(count):
    if count:
        return btn(f"📌 Book {count} class{'es' if count != 1 else ''}", "go")
    return btn("📌 Book (pick classes first)", "n")


def as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def class_rows(shown, classes, selected, queued, now_dt):
    """One button per class still to come that day, saying what state it's in."""
    ds = shown.isoformat()
    chosen = set(selected)
    rows = []
    ordered = sorted(classes, key=lambda c: (str(c.get("time", "")),
                                             str((c.get("box_categories") or {}).get("name", ""))))
    for cls in ordered:
        t = str(cls.get("time", ""))[:5]
        name = str((cls.get("box_categories") or {}).get("name", "")).strip()
        try:
            start = datetime.strptime(f"{ds} {t}", "%Y-%m-%d %H:%M")
        except ValueError:
            continue
        if start <= now_dt:
            continue
        base = f"{t} {name}"
        if cls.get("user_booked") is not None:
            rows.append([btn(f"☑️ {base} · booked", "n:booked")])
            continue
        if cls.get("user_in_standby") is not None or str(cls.get("booking_option")) == "cancelWaitList":
            pos = cls.get("stand_by_position")
            rows.append([btn(f"🕒 {base} · waiting list" + (f" #{pos}" if pos is not None else ""),
                             "n:standby")])
            continue
        if (ds, t) in queued:
            rows.append([btn(f"📌 {base} · queued", "n:queued")])
            continue
        window = window_candidates_for(start)[0]
        if now_dt >= window:
            free = as_int(cls.get("free"))
            if free is not None and free <= 0:
                waiting = as_int(cls.get("stand_by"))
                status = "full" + (f", {waiting} waiting" if waiting else "")
            else:
                status = f"{free} free" if free is not None else "open"
        else:
            status = f"opens {window:%a %H:%M}"
        mark = "✅ " if (ds, t, name) in chosen else ""
        rows.append([btn(f"{mark}{base} · {status}", f"t:{ds}:{cls.get('id')}")])
    return rows


def picker_keyboard(shown, classes, selected, queued, now_dt):
    today = now_dt.date()
    ws, this_ws = week_start(shown), week_start(today)
    rows = []
    nav = []
    if ws > this_ws:
        nav.append(btn("« Previous week", f"w:{(ws - timedelta(days=7)).isoformat()}"))
    if ws < this_ws + timedelta(days=7 * WEEKS_AHEAD):
        nav.append(btn("Next week »", f"w:{(ws + timedelta(days=7)).isoformat()}"))
    if nav:
        rows.append(nav)
    picked_days = {s[0] for s in selected}
    days = []
    for i in range(7):
        d = ws + timedelta(days=i)
        if d < today:
            continue
        label = ("▸" if d == shown else "") + f"{d:%a} {d.day}" + (" ✅" if d.isoformat() in picked_days else "")
        days.append(btn(label, f"d:{d.isoformat()}"))
    rows += [days[i:i + 4] for i in range(0, len(days), 4)]
    if classes is None:
        rows.append([btn("⚠️ Couldn't load the classes - tap to retry", f"d:{shown.isoformat()}")])
    else:
        found = class_rows(shown, classes, selected, queued, now_dt)
        rows += found or [[btn("No classes left this day", "n:none")]]
    rows.append([book_button(len(selected)), btn("✖ Close", "x")])
    return {"inline_keyboard": rows}


def remark_keyboard(keyboard, selected):
    """The same buttons with the ✅ marks and the Book count brought up to date --
    so ticking a class needs no trip to the gym's API."""
    chosen = set(selected)
    picked_days = {s[0] for s in selected}
    rows = []
    for row in keyboard:
        new_row = []
        for b in row:
            label, data = b.get("text", ""), b.get("callback_data", "")
            if data.startswith("t:"):
                ds = data[2:].split(":")[0]
                clean = label[2:] if label.startswith("✅ ") else label
                base = clean.split(" · ")[0]
                label = ("✅ " if (ds, base[:5], base[6:]) in chosen else "") + clean
            elif data.startswith("d:") and not label.startswith("⚠️"):
                core = label.lstrip("▸").replace(" ✅", "")
                label = ("▸" if label.startswith("▸") else "") + core + (" ✅" if data[2:] in picked_days else "")
            elif data == "go" or (data == "n" and label.startswith("📌 Book")):
                new_row.append(book_button(len(selected)))
                continue
            new_row.append(btn(label, data))
        rows.append(new_row)
    return {"inline_keyboard": rows}


# ---------------------------------------------------------------------------
# Typed shortcuts: "wed 18:00 hall a", "רביעי 7:00", "25/10 07:00", "tomorrow"
# ---------------------------------------------------------------------------
def parse_day(token, today):
    t = token.lower().strip(",.!?")
    if t in ("today", "היום"):
        return today
    if t in ("tomorrow", "מחר"):
        return today + timedelta(days=1)
    if t in WEEKDAYS:
        return today + timedelta(days=(WEEKDAYS[t] - today.weekday()) % 7)
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", t)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    m = re.match(r"^(\d{1,2})[/.](\d{1,2})$", t)
    if m:
        for year in (today.year, today.year + 1):
            try:
                d = date(year, int(m.group(2)), int(m.group(1)))
            except ValueError:
                return None
            if d >= today:
                return d
    return None


def parse_typed(text, now_dt):
    """(day, "HH:MM" or None, class name) for a message that starts with a day; else None."""
    words = text.replace("יום ", "").split()
    if not words:
        return None
    day = parse_day(words[0], now_dt.date())
    if day is None:
        return None
    m = re.match(r"^(\d{1,2})(?::(\d{2}))?$", words[1]) if len(words) > 1 else None
    if not m or int(m.group(1)) > 23 or int(m.group(2) or 0) > 59:
        return day, None, ""
    hh, mm = int(m.group(1)), int(m.group(2) or 0)
    if datetime(day.year, day.month, day.day, hh, mm) <= now_dt and words[0].lower() in WEEKDAYS:
        day += timedelta(days=7)  # "wed 07:00" said on Wednesday afternoon means next week
    return day, f"{hh:02d}:{mm:02d}", " ".join(words[2:])


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------
def show_picker(chat_id, shown=None, preselect=None, classes=None):
    now_dt = now()
    today = now_dt.date()
    shown = shown or (week_start(today) + timedelta(days=7))
    if classes is None:
        classes = arbox_day(shown.isoformat())
    selected = []
    if preselect and classes:
        _, t, wanted = preselect
        for cls in classes:
            name = str((cls.get("box_categories") or {}).get("name", "")).strip()
            if str(cls.get("time", ""))[:5] == t and wanted.lower() in name.lower():
                selected = [(shown.isoformat(), t, name)]
                break
    send(chat_id, picker_text(shown, selected),
         picker_keyboard(shown, classes, selected, queued_slots(), now_dt))


def show_list(chat_id):
    entries = queued_entries()
    if entries is None:
        send(chat_id, "⚠️ Couldn't read the queue from GitHub. Try again in a minute.", as_html=False)
        return
    if not entries:
        send(chat_id, "📋 Nothing is queued. Send me any message to pick classes.", as_html=False)
        return
    lines, rows = [f"📋 Queued ({len(entries)}):"], []
    for ds, t, name in entries:
        d = date.fromisoformat(ds)
        start = datetime.strptime(f"{ds} {t}", "%Y-%m-%d %H:%M")
        opens = window_candidates_for(start)[0]
        lines.append(f"{selection_line((ds, t, name))} - books {opens:%a %d/%m %H:%M}")
        rows.append([btn(f"❌ Remove {d:%a} {d.day:02d}/{d.month:02d} {t}",
                         f"rm:{ds}:{t.replace(':', '')}")])
    send(chat_id, "\n".join(lines), {"inline_keyboard": rows}, as_html=False)


def queue_typed(chat_id, typed):
    """Typed with a time but the gym's list couldn't be read: queue it as typed."""
    day, t, name = typed
    err = send_to_queue("add", [{"date": day.isoformat(), "time": t, "category": name}])
    if err:
        send(chat_id, f"⚠️ {esc(err)}")
        return
    send(chat_id, f"⏳ Queuing {esc(selection_line((day.isoformat(), t, name or 'any class')))} "
                  f"- you'll get a 📌 confirmation in about a minute.\n"
                  f"(I couldn't read the gym's class list just now, so it's queued as you typed it.)")


def on_message(msg):
    chat_id = (msg.get("chat") or {}).get("id")
    if not is_owner((msg.get("from") or {}).get("id")):
        send(chat_id, "This is a private bot.", as_html=False)
        return
    text = (msg.get("text") or "").strip()
    command = text.split()[0].lower().split("@")[0] if text else ""
    if command in ("/list", "list", "queue", "/queue"):
        show_list(chat_id)
        return
    if command in ("/help", "help", "/start"):
        send(chat_id, HELP)
        if command != "/start":
            return
    typed = parse_typed(text, now()) if text and not text.startswith("/") else None
    if typed and typed[1]:
        classes = arbox_day(typed[0].isoformat())
        if classes is None:
            queue_typed(chat_id, typed)
        else:
            show_picker(chat_id, typed[0], preselect=typed, classes=classes)
        return
    show_picker(chat_id, typed[0] if typed else None)


def on_show_day(cq, msg, shown):
    answer(cq, f"Loading {shown:%a %d %b}…")
    now_dt = now()
    shown = max(shown, now_dt.date())
    selected = parse_selected(msg.get("text", ""), now_dt.date())
    classes = arbox_day(shown.isoformat())
    edit(msg, picker_text(shown, selected),
         picker_keyboard(shown, classes, selected, queued_slots(), now_dt))


def on_toggle(cq, msg, data):
    keyboard = (msg.get("reply_markup") or {}).get("inline_keyboard", [])
    label = next((b.get("text", "") for row in keyboard for b in row
                  if b.get("callback_data") == data), None)
    if label is None:
        answer(cq, "That list is out of date - tap the day again.")
        return
    ds = data[2:].split(":")[0]
    clean = label[2:] if label.startswith("✅ ") else label
    base = clean.split(" · ")[0]
    pick = (ds, base[:5], base[6:])
    today = now().date()
    selected = parse_selected(msg.get("text", ""), today)
    if pick in selected:
        selected.remove(pick)
        answer(cq, "Removed")
    else:
        # One class a day is the membership's rule: a new pick replaces that day's old one.
        selected = sorted([s for s in selected if s[0] != ds] + [pick])
        answer(cq, "Selected")
    edit(msg, picker_text(date.fromisoformat(ds), selected), remark_keyboard(keyboard, selected))


def on_book(cq, msg):
    now_dt = now()
    selected = [s for s in parse_selected(msg.get("text", ""), now_dt.date())
                if datetime.strptime(f"{s[0]} {s[1]}", "%Y-%m-%d %H:%M") > now_dt]
    if not selected:
        answer(cq, "Pick at least one class first.")
        return
    err = send_to_queue("add", [{"date": d, "time": t, "category": n} for d, t, n in selected])
    if err:
        answer(cq, "Couldn't queue them - see below.")
        send(msg["chat"]["id"], f"⚠️ {esc(err)}")
        return
    answer(cq, "Sent!")
    count = len(selected)
    edit(msg, f"⏳ <b>Sending {count} class{'es' if count != 1 else ''} to the queue…</b>\n"
              + "\n".join(esc(selection_line(s)) for s in selected)
              + "\n\nYou'll get a 📌 confirmation here in about a minute. Then I'll message "
                "you as each one is booked.")


def on_remove(cq, msg, rest):
    ds, _, hhmm = rest.partition(":")
    t = f"{hhmm[:2]}:{hhmm[2:]}"
    err = send_to_queue("remove", [{"date": ds, "time": t}])
    if err:
        answer(cq, err[:190], alert=True)
        return
    answer(cq, "Removing…")
    keyboard = (msg.get("reply_markup") or {}).get("inline_keyboard", [])
    rows = [row for row in keyboard if not any(b.get("callback_data") == f"rm:{rest}" for b in row)]
    d = date.fromisoformat(ds)
    edit(msg, (msg.get("text") or "") + f"\n⏳ Removing {d:%a} {d.day:02d}/{d.month:02d} {t}…",
         {"inline_keyboard": rows}, as_html=False)


def on_callback(cq):
    if not is_owner((cq.get("from") or {}).get("id")):
        answer(cq, "This is a private bot.")
        return
    msg = cq.get("message")
    if not msg:
        answer(cq)
        return
    data = cq.get("data", "")
    kind, _, rest = data.partition(":")
    if kind == "t":
        on_toggle(cq, msg, data)
    elif kind == "d":
        on_show_day(cq, msg, date.fromisoformat(rest))
    elif kind == "w":
        on_show_day(cq, msg, date.fromisoformat(rest))
    elif kind == "go":
        on_book(cq, msg)
    elif kind == "rm":
        on_remove(cq, msg, rest)
    elif kind == "x":
        answer(cq)
        edit(msg, "Closed. Send me any message to pick classes again.", as_html=False)
    else:
        answer(cq, NOOP_TEXT.get(rest, NOOP_TEXT[""]))


def handle_update(update):
    """Handle one update from Telegram. Never raises: a failed update is
    reported to you instead, and Telegram isn't left retrying it."""
    try:
        if "callback_query" in update:
            on_callback(update["callback_query"])
        elif "message" in update:
            on_message(update["message"])
    except Exception as e:  # noqa: BLE001 - anything at all is better reported than lost
        traceback.print_exc()
        if env("OWNER_CHAT_ID"):
            send(env("OWNER_CHAT_ID"), f"⚠️ The bot hit an error: {e}", as_html=False)


def setup_webhook(url):
    """Point Telegram at this deployment and check its settings. Returns (ok, HTML page)."""
    if not env("BOT_TOKEN"):
        return False, "<h3>BOT_TOKEN isn't set on Vercel yet.</h3>"
    ok = tg("setWebhook", url=url, secret_token=webhook_secret(),
            allowed_updates=["message", "callback_query"], drop_pending_updates=True)
    tg("setMyCommands", commands=[
        {"command": "book", "description": "Pick classes to book"},
        {"command": "list", "description": "What's queued"},
        {"command": "help", "description": "How it works"},
    ])
    checks = []
    missing = [n for n in ("OWNER_CHAT_ID", "ARBOX_EMAIL", "ARBOX_PASSWORD", "GITHUB_TOKEN")
               if not env(n)]
    checks.append("⚠️ Still missing on Vercel: " + ", ".join(missing) if missing
                  else "✅ All settings are filled in")
    if env("GITHUB_TOKEN"):
        status, _ = github("GET", f"/repos/{repo()}/actions/workflows")
        checks.append("✅ The GitHub key works" if status == 200
                      else f"⚠️ GitHub refused the key (HTTP {status})")
    if not ok:
        return False, "<h3>⚠️ Telegram didn't accept the connection - check BOT_TOKEN.</h3>"
    items = "".join(f"<li>{esc(c)}</li>" for c in checks)
    return True, (f"<h3>✅ Your bot is connected.</h3><ul>{items}</ul>"
                  "<p>Open Telegram and send your bot any message.</p>")
