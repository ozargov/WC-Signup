#!/usr/bin/env python3
"""
WC Signup - the booking queue.

Each class you want is one small file in queue/, e.g. queue/2026-10-21_0800.json.
One file per class means two runs changing the queue at the same moment never
edit the same file, so saving back to GitHub cannot conflict.

Three GitHub Actions workflows drive it:

  add-booking.yml  "Add booking" button        ->  bookings.py add / remove, then plan
  book-due.yml     "Book due classes", started  ->  bookings.py plan
                   every 15 min by cron-job.org
                   (GitHub's own timer as backup)
  book-class.yml   "Book one class", one run    ->  bookings.py book
                   per class, started by plan

plan starts a "Book one class" run for every queued class whose signup window
opens within the lookahead. That run hands the class to book_class.py
--wait-for-window, which sleeps to the exact moment before firing. So GitHub
only has to start the run some time in the 50 minutes before -- the precision
comes from the sleep, not from any timer.

Usage:
  python bookings.py add    --date 2026-10-21 --time 08:00 --category "Hall A"
  python bookings.py remove --date 2026-10-21 --time 08:00
  python bookings.py list
  python bookings.py plan   --lookahead-minutes 50
  python bookings.py book   --date 2026-10-21 --time 08:00
"""

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from book_class import hours_before_for, window_candidates_for

HERE          = Path(__file__).resolve().parent
QUEUE_DIR     = Path(os.environ.get("WC_QUEUE_DIR") or HERE / "queue")
BOOK_SCRIPT   = HERE / "book_class.py"
BOOK_WORKFLOW = "book-class.yml"
BRANCH        = os.environ.get("GITHUB_REF_NAME") or "main"

# book_class.py exit codes
BOOKED_EXIT     = 0
NOT_FOUND_EXIT  = 4
WAITLISTED_EXIT = 6

# GitHub run states meaning "a booking run for this class is already on it".
ACTIVE_STATES = {"queued", "in_progress", "waiting", "pending", "requested"}

# How long GitHub may leave a run "queued" before it counts as stuck. A healthy
# run gets a runner within a minute; on 2026-10-19 one sat queued for over 30
# minutes, with no job ever created, while GitHub's status page was all green.
STUCK_MINUTES = 10
WATCHED_WORKFLOWS = ("bot-queue.yml", "add-booking.yml", BOOK_WORKFLOW)


def log(msg):
    print(msg, flush=True)


# ---------------------------------------------------------------------------
# Queue files
# ---------------------------------------------------------------------------
def parse_target(date_s, time_s):
    return datetime.strptime(f"{date_s} {time_s}", "%Y-%m-%d %H:%M")


def parse_input(date_s, time_s):
    """parse_target, with a friendly error for whoever typed into the form."""
    try:
        return parse_target(date_s, time_s)
    except ValueError:
        raise SystemExit(
            f"Could not read '{date_s} {time_s}'. Expected a date like 2026-09-14 "
            f"and a 24-hour time like 08:00 or 18:15."
        )


def entry_path(target):
    return QUEUE_DIR / f"{target:%Y-%m-%d_%H%M}.json"


def read_entry(path):
    return json.loads(path.read_text(encoding="utf-8"))


def load():
    """[(path, entry)] for every queued class, earliest class first."""
    out = []
    for path in sorted(QUEUE_DIR.glob("*.json")) if QUEUE_DIR.exists() else []:
        try:
            entry = read_entry(path)
            parse_target(entry["date"], entry["time"])
        except (ValueError, KeyError, TypeError) as e:
            log(f"  skipping unreadable {path.name}: {e}")
            continue
        out.append((path, entry))
    return out


def describe(entry):
    target = parse_target(entry["date"], entry["time"])
    cat = entry.get("category") or ""
    return f"{target:%a %d %b %H:%M}" + (f" ({cat})" if cat else "")


def window_text(target):
    windows = window_candidates_for(target)
    text = f"{windows[0]:%a %d %b at %H:%M}"
    if len(windows) > 1:
        text += f" (or {windows[1]:%H:%M} - the clocks change that weekend, so both are tried)"
    return text


def run_title(entry):
    """Must match run-name in book-class.yml."""
    return f"Book {entry['date']} {entry['time']}"


# ---------------------------------------------------------------------------
# Saving to GitHub
# ---------------------------------------------------------------------------
def in_actions():
    return os.environ.get("GITHUB_ACTIONS") == "true"


def git(*args, check=True):
    return subprocess.run(["git", *args], cwd=HERE, capture_output=True, text=True, check=check)


def save_change(change, message):
    """Apply `change` (a function that edits files in queue/) and, on GitHub, push it.

    Each try starts from the latest copy of the repo and re-applies the change,
    so a push that loses a race with another run is simply redone on top of it.
    Off GitHub it only applies the change -- it never resets a local folder."""
    if not in_actions():
        change()
        return True
    git("config", "user.name", "WC Signup Bot")
    git("config", "user.email", "wc-signup-bot@users.noreply.github.com")
    for attempt in range(1, 6):
        git("fetch", "--quiet", "origin", BRANCH)
        git("reset", "--quiet", "--hard", f"origin/{BRANCH}")
        change()
        git("add", "--all", str(QUEUE_DIR))
        if git("diff", "--cached", "--quiet", check=False).returncode == 0:
            return True  # nothing to save: another run already made this change
        git("commit", "--quiet", "-m", message)
        if git("push", "--quiet", "origin", f"HEAD:{BRANCH}", check=False).returncode == 0:
            return True
        log(f"  (saving lost a race with another run - retrying, attempt {attempt})")
        time.sleep(2 * attempt)
    log("Could not save the queue to GitHub after 5 attempts.")
    return False


# ---------------------------------------------------------------------------
# Booking runs on GitHub (through the gh command, preinstalled on runners)
# ---------------------------------------------------------------------------
def gh_runs():
    out = subprocess.run(
        ["gh", "run", "list", "--workflow", BOOK_WORKFLOW, "--limit", "50",
         "--json", "databaseId,displayTitle,status"],
        capture_output=True, text=True, timeout=60, check=True,
    ).stdout
    return json.loads(out)


def active_run_titles(exclude=frozenset()):
    """Titles of booking runs still on the go, leaving out the run ids in `exclude`."""
    try:
        return {r["displayTitle"] for r in gh_runs()
                if r.get("status") in ACTIVE_STATES and r.get("databaseId") not in exclude}
    except Exception as e:
        log(f"  (could not see which booking runs are active: {e} - starting runs anyway; "
            f"a duplicate waits its turn and then stops at once)")
        return set()


def dispatch(entry, urgent):
    """Start a "Book one class" run for this entry."""
    result = subprocess.run(
        ["gh", "workflow", "run", BOOK_WORKFLOW, "--ref", BRANCH,
         "-f", f"class_date={entry['date']}",
         "-f", f"class_time={entry['time']}",
         "-f", f"category_filter={entry.get('category') or ''}"],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode == 0:
        return True
    reason = (result.stderr or result.stdout).strip()[:300]
    log(f"  could not start its booking run: {reason}")
    if urgent:  # earlier failures stay quiet: the next planner run tries again
        notify(f"⚠️ Could not start the booking for {describe(entry)}, and it opens in "
               f"minutes.\n{reason[:200]}")
    return False


def cancel_run(run_id):
    subprocess.run(["gh", "run", "cancel", str(run_id)], capture_output=True, text=True, timeout=60)


def cancel_active_runs(entry):
    """Stop a booking run that is already waiting for this class, if there is one."""
    if not in_actions():
        return
    try:
        runs = gh_runs()
    except Exception as e:
        log(f"  (could not check for a booking run already waiting: {e})")
        return
    for r in runs:
        if r.get("displayTitle") == run_title(entry) and r.get("status") in ACTIVE_STATES:
            cancel_run(r["databaseId"])
            log(f"  stopped booking run {r['databaseId']}, which was waiting for this class")


def queued_runs(workflow):
    out = subprocess.run(
        ["gh", "run", "list", "--workflow", workflow, "--status", "queued", "--limit", "20",
         "--json", "databaseId,displayTitle,createdAt"],
        capture_output=True, text=True, timeout=60, check=True,
    ).stdout
    return json.loads(out)


def clear_stuck_runs():
    """Cancel runs GitHub accepted but never started, so none can run hours late
    with old picks, and a stuck booking run can't stop its class being started
    again. Stuck queue requests are reported so you can resend them; stuck
    booking runs need nothing from you -- the planner starts them again.
    Returns the ids of the runs it cancelled."""
    cancelled = set()
    now = datetime.now(timezone.utc)
    for workflow in WATCHED_WORKFLOWS:
        try:
            runs = queued_runs(workflow)
        except Exception as e:
            log(f"  (could not check {workflow} for stuck runs: {e})")
            continue
        for r in runs:
            created = datetime.fromisoformat(r["createdAt"].replace("Z", "+00:00"))
            minutes = (now - created).total_seconds() / 60
            if minutes < STUCK_MINUTES:
                continue
            cancel_run(r["databaseId"])
            cancelled.add(r["databaseId"])
            title = r.get("displayTitle") or workflow
            log(f"  STUCK    {title} - queued {minutes:.0f} min and never started; cancelled")
            if workflow != BOOK_WORKFLOW:
                notify(f"⚠️ GitHub never started this request, so I cancelled it:\n{title} "
                       f"(sent {created.astimezone():%a %H:%M})\nPlease send it again.")
    return cancelled


# ---------------------------------------------------------------------------
# Telegram notification (a one-off send, so it needs no always-on server)
# ---------------------------------------------------------------------------
def notify(text):
    token = os.environ.get("BOT_TOKEN", "")
    chat  = os.environ.get("OWNER_CHAT_ID", "")
    if not token or not chat or chat == "0":
        return
    body = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
    req  = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage", data=body, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            r.read()
    except Exception as e:
        log(f"(could not send the Telegram message: {e})")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
def cmd_add(args):
    target = parse_input(args.date, args.time)
    if target < datetime.now():
        raise SystemExit(f"{target:%Y-%m-%d %H:%M} is in the past. Nothing to book.")

    entry = {
        "date":     f"{target:%Y-%m-%d}",
        "time":     f"{target:%H:%M}",
        "category": (args.category or "").strip(),
        "added":    datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    path = entry_path(target)
    verb = "Queued"
    if path.exists():
        if (read_entry(path).get("category") or "") == entry["category"]:
            log(f"Already queued: {describe(entry)} - leaving the queue unchanged.")
            return 0
        verb = "Updated"

    def change():
        QUEUE_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(entry, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    if not save_change(change, f"Queue: add {entry['date']} {entry['time']}"):
        notify(f"⚠️ Could not save {describe(entry)} to the queue. Please try again.")
        return 1
    log(f"{verb} {describe(entry)}")
    log(f"  signup window opens {window_text(target)} "
        f"({hours_before_for(target)}h before the class)")
    notify(f"📌 {verb}: {describe(entry)}\nBooks automatically on {window_text(target)}.")
    return 0


def cmd_remove(args):
    target = parse_input(args.date, args.time)
    path = entry_path(target)
    if not path.exists():
        log(f"Nothing queued for {target:%Y-%m-%d %H:%M}.")
        return 0
    entry = read_entry(path)
    if not save_change(lambda: path.unlink(missing_ok=True),
                       f"Queue: remove {entry['date']} {entry['time']}"):
        return 1
    cancel_active_runs(entry)
    log(f"Removed {describe(entry)} from the queue.")
    notify(f"🗑 Removed: {describe(entry)}")
    return 0


def read_classes(raw):
    """The JSON list the Telegram bot sends: [{"date", "time", "category"}, ...]."""
    try:
        items = json.loads(raw)
    except ValueError:
        raise SystemExit("--classes must be a JSON list of {date, time, category}.")
    if not isinstance(items, list):
        raise SystemExit("--classes must be a JSON list of {date, time, category}.")
    return [(parse_input(str(i.get("date", "")), str(i.get("time", ""))), i) for i in items]


def cmd_add_many(args):
    """Queue several classes in one save and one message -- what the bot's Book button does."""
    now = datetime.now()
    entries, past = [], []
    for target, item in read_classes(args.classes):
        entry = {
            "date":     f"{target:%Y-%m-%d}",
            "time":     f"{target:%H:%M}",
            "category": str(item.get("category") or "").strip(),
            "added":    now.strftime("%Y-%m-%d %H:%M"),
        }
        (past if target < now else entries).append((target, entry))

    def change():
        QUEUE_DIR.mkdir(parents=True, exist_ok=True)
        for target, entry in entries:
            entry_path(target).write_text(json.dumps(entry, indent=2, ensure_ascii=False) + "\n",
                                          encoding="utf-8")

    if entries and not save_change(change, f"Queue: add {len(entries)} from Telegram"):
        notify("⚠️ Could not save your classes to the queue. Please try again.")
        return 1
    count = len(entries)
    lines = [f"📌 Queued {count} class{'es' if count != 1 else ''}:"] if entries else []
    lines += [f"• {describe(e)} - books {window_text(t)}" for t, e in entries]
    if past:
        lines.append("Skipped, already started: " + ", ".join(describe(e) for _t, e in past))
    log("\n".join(lines))
    notify("\n".join(lines))
    return 0


def cmd_remove_many(args):
    """Remove several classes in one save, stopping any booking run already waiting."""
    found = []
    for target, _item in read_classes(args.classes):
        path = entry_path(target)
        if path.exists():
            found.append((path, read_entry(path)))
    if not found:
        log("None of those classes were queued.")
        notify("🗑 Nothing to remove - those classes weren't queued.")
        return 0

    def change():
        for path, _entry in found:
            path.unlink(missing_ok=True)

    if not save_change(change, f"Queue: remove {len(found)} from Telegram"):
        notify("⚠️ Could not remove those classes. Please try again.")
        return 1
    for _path, entry in found:
        cancel_active_runs(entry)
    text = "🗑 Removed: " + ", ".join(describe(e) for _p, e in found)
    log(text)
    notify(text)
    return 0


def cmd_list(args):
    entries = load()
    if not entries:
        log("The queue is empty.")
        return 0
    now = datetime.now()
    log(f"{len(entries)} class(es) queued:")
    for _path, entry in entries:
        target = parse_target(entry["date"], entry["time"])
        mins = (window_candidates_for(target)[0] - now).total_seconds() / 60
        if mins > 60:
            when = f"books {window_text(target)} (in {mins/60:.1f}h)"
        elif mins > 0:
            when = f"books in {mins:.0f} min"
        else:
            when = f"window opened {abs(mins):.0f} min ago"
        log(f"  - {describe(entry):<34} {when}")
    return 0


def cmd_plan(args):
    # Before the queue: a stuck request may be exactly why the queue is empty.
    cancelled = clear_stuck_runs() if args.check_stuck and not args.dry_run else set()
    entries = load()
    if not entries:
        log("The queue is empty - nothing to do.")
        return 0

    now = datetime.now()
    active = None  # fetched only if something is due
    for path, entry in entries:
        target = parse_target(entry["date"], entry["time"])
        if target <= now:
            log(f"  PAST     {describe(entry)} - the class has started; dropping it")
            if not args.dry_run:
                save_change(lambda p=path: p.unlink(missing_ok=True),
                            f"Queue: drop {entry['date']} {entry['time']} (class started)")
                notify(f"⚠️ {describe(entry)} was still queued when the class started, so it "
                       f"was dropped. If you didn't get a Booked message for it, check the "
                       f"Actions tab on GitHub.")
            continue

        first = window_candidates_for(target)[0]
        mins = (first - now).total_seconds() / 60
        if mins > args.lookahead_minutes:
            log(f"  waiting  {describe(entry)} - window in {mins/60:.1f}h")
            continue

        # Due -- including a window that has already opened: an open class can
        # still be booked if a spot is left, or else joined on the waiting list.
        if active is None:
            active = set() if args.dry_run else active_run_titles(cancelled)
        if run_title(entry) in active:
            log(f"  running  {describe(entry)} - its booking run is already waiting")
            continue
        log(f"  DUE      {describe(entry)} - window at {first:%H:%M} (in {mins:.1f} min) "
            f"- starting its booking run")
        if not args.dry_run:
            dispatch(entry, urgent=mins < 15)
    return 0


def classes_listed(lines):
    """The "Available classes" lines book_class.py prints when nothing matched."""
    out, started = [], False
    for line in lines:
        if "Available classes:" in line:
            started = True
            continue
        if started and "] " in line:
            text = line.split("] ", 1)[1].strip()
            out.append(" ".join(text.split("  (")[0].split()))
    return out[:15]


def run_booker(cmd, entry):
    """Run book_class.py, echoing its output live; return (exit code, output lines).
    Sends the "ready" message as soon as it reports the class found and armed."""
    env = dict(os.environ, PYTHONIOENCODING="utf-8")  # class and coach names are Hebrew
    lines = []
    with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                          encoding="utf-8", errors="replace", bufsize=1, env=env) as proc:
        for line in proc.stdout:
            print(line, end="", flush=True)
            lines.append(line.rstrip("\n"))
            if line.startswith("READY_AT="):
                when = line.strip()[len("READY_AT="):].split("|")[0]
                notify(f"⏳ Ready for {describe(entry)}\nIt books at {when[11:]} on "
                       f"{datetime.strptime(when, '%Y-%m-%d %H:%M:%S'):%a %d %b}.")
    return proc.returncode, lines


def cmd_book(args):
    target = parse_input(args.date, args.time)
    path = entry_path(target)
    if in_actions():
        # This run may have waited its turn behind another one for the same
        # class; look at the queue as it is now, not as it was when it started.
        git("pull", "--quiet", "--ff-only", "origin", BRANCH, check=False)
    if not path.exists():
        log(f"{target:%Y-%m-%d %H:%M} is no longer queued - nothing to do.")
        return 0
    entry = read_entry(path)

    cmd = [sys.executable, str(BOOK_SCRIPT),
           "--class-date", entry["date"],
           "--class-time", entry["time"],
           "--wait-for-window"]
    if entry.get("category"):
        cmd += ["--category-filter", entry["category"]]
    if args.dry_run:
        cmd.append("--dry-run")

    code, lines = run_booker(cmd, entry)
    if args.dry_run:
        log(f"--- dry run finished (exit {code}); the queue was left as it is ---")
        return 0 if code in (BOOKED_EXIT, WAITLISTED_EXIT) else 1

    result = {BOOKED_EXIT: "booked", WAITLISTED_EXIT: "waitlisted"}.get(code, f"failed, exit {code}")
    # Attempted classes always leave the queue, successful or not: the moment has passed.
    save_change(lambda: path.unlink(missing_ok=True),
                f"Done: {entry['date']} {entry['time']} ({result})")

    position = next((l.split("=", 1)[1] for l in lines if l.startswith("STANDBY_POSITION=")), None)
    if code == BOOKED_EXIT:
        log(f"--- BOOKED {describe(entry)} ---")
        notify(f"✅ Booked: {describe(entry)}")
    elif code == WAITLISTED_EXIT:
        log(f"--- WAITLISTED {describe(entry)} ---")
        notify(f"🕒 Class was full: {describe(entry)}\nYou're on the waiting list"
               + (f" - number {position}" if position else "")
               + ". Arbox moves you in automatically if someone cancels.")
    elif code == NOT_FOUND_EXIT:
        log(f"--- NOT FOUND {describe(entry)} ---")
        avail = classes_listed(lines)
        notify(f"❌ No class matches {describe(entry)}, so nothing was booked."
               + ("\nClasses that day:\n" + "\n".join(avail) if avail else ""))
    else:
        log(f"--- FAILED {describe(entry)} (exit {code}) ---")
        answers = [l.split("] ", 1)[-1].strip() for l in lines if "]   HTTP " in l]
        notify(f"❌ Booking FAILED: {describe(entry)}"
               + (f"\nThe gym answered: {answers[0][:160]}" if answers else "")
               + "\nThe Actions tab on GitHub has the full story.")
    return 0 if code in (BOOKED_EXIT, WAITLISTED_EXIT) else 1


# ---------------------------------------------------------------------------
def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # Hebrew class names
    ap = argparse.ArgumentParser(description="Manage the WC Signup booking queue")
    sub = ap.add_subparsers(dest="command", required=True)

    p_add = sub.add_parser("add", help="queue a class")
    p_add.add_argument("--date", required=True, help="YYYY-MM-DD")
    p_add.add_argument("--time", required=True, help="HH:MM (24h)")
    p_add.add_argument("--category", default="", help="part of the class name, optional")

    p_rm = sub.add_parser("remove", help="remove a queued class")
    p_rm.add_argument("--date", required=True)
    p_rm.add_argument("--time", required=True)
    p_rm.add_argument("--category", default="", help="ignored; matching is on date and time")

    p_addm = sub.add_parser("add-many", help="queue several classes (the Telegram bot)")
    p_addm.add_argument("--classes", required=True, help='JSON: [{"date", "time", "category"}]')

    p_rmm = sub.add_parser("remove-many", help="remove several classes (the Telegram bot)")
    p_rmm.add_argument("--classes", required=True, help='JSON: [{"date", "time"}]')

    sub.add_parser("list", help="show the queue")

    p_plan = sub.add_parser("plan", help="start a booking run for anything opening soon")
    p_plan.add_argument("--lookahead-minutes", type=int, default=50)
    p_plan.add_argument("--dry-run", action="store_true",
                        help="say what would start, without starting or dropping anything")
    p_plan.add_argument("--check-stuck", action="store_true",
                        help=f"cancel runs GitHub left queued over {STUCK_MINUTES} min, and say so")

    p_book = sub.add_parser("book", help="wait for one class's window and book it")
    p_book.add_argument("--date", required=True)
    p_book.add_argument("--time", required=True)
    p_book.add_argument("--category", default="", help="ignored; the queue file decides")
    p_book.add_argument("--dry-run", action="store_true",
                        help="go through the motions without actually booking")

    args = ap.parse_args()
    handlers = {"add": cmd_add, "remove": cmd_remove, "add-many": cmd_add_many,
                "remove-many": cmd_remove_many, "list": cmd_list,
                "plan": cmd_plan, "book": cmd_book}
    return handlers[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
