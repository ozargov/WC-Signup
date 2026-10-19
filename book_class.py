#!/usr/bin/env python3
"""
WC Signup - CrossFit White City class booking script
Reads credentials from environment variables (GitHub repository secrets).

Usage:
  python book_class.py --class-date 2026-06-04 --class-time 08:00
  python book_class.py --class-date 2026-06-04 --class-time 08:00 --category-filter "W.O.D Hall A" --wait-for-window
  python book_class.py --class-date 2026-06-04 --class-time 08:00 --dry-run
"""

import argparse
import http.client
import json
import os
import pathlib
import sys
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

try:
    from zoneinfo import ZoneInfo
    GYM_TZ = ZoneInfo("Asia/Jerusalem")
except Exception:  # no timezone database on this machine
    GYM_TZ = None

BASE_URL = "https://apiappv2.arboxapp.com/api/v2"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(SCRIPT_DIR, "booking.log")


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
def log(msg, color=None):
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    line = f"[{stamp}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Environment helpers
# ---------------------------------------------------------------------------
def _env(name, default=""):
    """Treat an unset AND an empty variable as "not provided". GitHub Actions
    substitutes an empty string for a secret that does not exist, which would
    otherwise turn int(os.environ[...]) into a crash."""
    v = os.environ.get(name)
    return v.strip() if v and v.strip() else default


def _env_int(name, default):
    try:
        return int(_env(name, str(default)))
    except ValueError:
        log(f"  {name} is not a whole number - using {default}")
        return int(default)


# ---------------------------------------------------------------------------
# Signup window rule (the single definition -- bookings.py imports these)
# ---------------------------------------------------------------------------
def hours_before_for(target_dt):
    """Classes open 48h ahead, except Sunday and Monday classes, which open 72h
    ahead (i.e. Thursday / Friday at the same time of day)."""
    return 72 if target_dt.weekday() in (0, 6) else 48


def window_candidates_for(target_dt):
    """Every moment the class might become bookable, earliest first.

    Almost always one. But on the two weekends a year when Israel's clocks
    change, "72h before" counted on the wall clock and "72h before" counted in
    elapsed time are an hour apart, and Arbox doesn't say which it uses -- so
    both come back, and the booker tries the earlier, then the later."""
    hours = hours_before_for(target_dt)
    candidates = {target_dt - timedelta(hours=hours)}
    if GYM_TZ is not None:
        aware = target_dt.replace(tzinfo=GYM_TZ)
        elapsed = (aware.astimezone(timezone.utc) - timedelta(hours=hours)).astimezone(GYM_TZ)
        candidates.add(elapsed.replace(tzinfo=None))
    return sorted(candidates)


def window_opens_for(target_dt):
    """The (earliest) moment a class becomes bookable."""
    return window_candidates_for(target_dt)[0]


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------
def _headers(token=None, refresh=None, whitelabel="HYPR-training"):
    h = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        # No "br": brotli is not in the standard library, and an
        # unreadable error body makes every failure impossible to diagnose.
        "Accept-Encoding": "gzip, deflate",
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148",
        "Origin": "https://app.arboxapp.com",
        "Referer": "https://app.arboxapp.com/",
        "whitelabel": whitelabel,
        "referername": "app",
        "version": "11",
    }
    if token:
        h["accesstoken"] = token
    if refresh:
        h["refreshtoken"] = refresh
    return h


def _decompress(data):
    """Decompress gzip/deflate response bytes if needed."""
    if data[:2] == b'\x1f\x8b':
        import gzip
        return gzip.decompress(data)
    if data[:2] in (b'x\x9c', b'x\x01', b'x\xda'):
        import zlib
        return zlib.decompress(data)
    return data

LAST_POST_SECONDS = 0.30   # duration of the last request
MIN_POST_SECONDS  = None   # fastest request so far: the best stand-in for the bare round trip
LAST_SERVER_DATE  = ""     # the API's Date header from the last response

# One kept-alive connection per host. A fresh HTTPS connection costs a TCP and a
# TLS handshake -- several round trips -- which the opening request must not pay.
_CONNECTIONS = {}


def _connection(host):
    conn = _CONNECTIONS.get(host)
    if conn is None:
        conn = http.client.HTTPSConnection(host, timeout=15)
        _CONNECTIONS[host] = conn
    return conn


def _drop_connection(host):
    conn = _CONNECTIONS.pop(host, None)
    if conn is not None:
        conn.close()


def warm_connection(url=BASE_URL):
    """Open a fresh connection now, so the request that follows goes straight out."""
    host = urllib.parse.urlsplit(url).netloc
    _drop_connection(host)
    try:
        _connection(host).connect()
    except OSError as e:
        log(f"  (could not open the connection ahead of time: {e})")


def _post(url, headers, body):
    """POST JSON. Returns (status, parsed JSON) on 2xx, else (status, body text)."""
    global LAST_POST_SECONDS, MIN_POST_SECONDS, LAST_SERVER_DATE
    parts = urllib.parse.urlsplit(url)
    data = json.dumps(body).encode("utf-8")
    for attempt in (1, 2):
        conn = _connection(parts.netloc)
        started = time.monotonic()
        try:
            conn.request("POST", parts.path, body=data, headers=headers)
            resp = conn.getresponse()
            raw = _decompress(resp.read())
        except (http.client.RemoteDisconnected, BrokenPipeError, ConnectionResetError) as e:
            # The server had already closed the kept-alive connection, so the
            # request never got through: one retry on a fresh one is safe.
            _drop_connection(parts.netloc)
            LAST_POST_SECONDS = time.monotonic() - started
            if attempt == 1:
                continue
            return 599, f"network error: {e}"
        except (http.client.HTTPException, OSError) as e:
            # A dropped connection or timeout is exactly what happens when everyone
            # hits the API in the same second. Report it as a transient 599 so the
            # caller's retry loop kicks in instead of the exception killing the run.
            _drop_connection(parts.netloc)
            LAST_POST_SECONDS = time.monotonic() - started
            return 599, f"network error: {e}"
        LAST_POST_SECONDS = time.monotonic() - started
        if MIN_POST_SECONDS is None or LAST_POST_SECONDS < MIN_POST_SECONDS:
            MIN_POST_SECONDS = LAST_POST_SECONDS
        LAST_SERVER_DATE = resp.getheader("Date") or ""
        text = raw.decode("utf-8", errors="replace")
        if 200 <= resp.status < 300:
            try:
                return resp.status, json.loads(text)
            except ValueError:
                return resp.status, text
        return resp.status, text
    return 599, "network error"


def server_clock_note():
    """How the API's clock compares with ours, from its Date header. The header
    only has 1-second resolution, so anything within a second counts as in step."""
    if not LAST_SERVER_DATE:
        return "unknown"
    try:
        server = parsedate_to_datetime(LAST_SERVER_DATE)
    except (TypeError, ValueError):
        return "unknown"
    ahead = (server - datetime.now(timezone.utc)).total_seconds()
    # The header is truncated to the second, so a perfectly matched clock reads 0 to -1.
    if -1.5 <= ahead <= 0.5:
        return "in step (to within a second)"
    return f"{ahead:+.1f}s off ours"


# ---------------------------------------------------------------------------
# Read-only inspection
#
# Used to discover how the API represents things (e.g. waiting-list membership)
# without booking anything. Other members' personal details are redacted: the
# useful signal is the field NAMES, not other people's data.
# ---------------------------------------------------------------------------
SENSITIVE_KEYS = (
    "full_name", "first_name", "last_name", "email", "phone", "mobile",
    "birthday", "birth_date", "address", "image", "avatar", "token",
    "password", "identity", "id_number", "personal",
)


def _is_sensitive(key):
    k = str(key).lower()
    return any(bad in k for bad in SENSITIVE_KEYS)


def _describe(value, key="", depth=0):
    """A compact, redacted rendering of a JSON value."""
    if _is_sensitive(key):
        return "<redacted>"
    if isinstance(value, dict):
        if depth >= 2:
            return "{" + ", ".join(sorted(value.keys())) + "}"
        inner = [f"{k}={_describe(v, k, depth + 1)}" for k, v in sorted(value.items())]
        return "{" + ", ".join(inner) + "}"
    if isinstance(value, list):
        if not value:
            return "[] (empty)"
        first = value[0]
        if isinstance(first, dict):
            return f"[{len(value)} items, keys: " + ", ".join(sorted(first.keys())) + "]"
        return f"[{len(value)} items] e.g. {str(first)[:40]}"
    text = str(value)
    return text if len(text) <= 60 else text[:60] + "..."


def inspect_class(cls):
    log("")
    log("=" * 70)
    log("RAW CLASS FIELDS (personal details redacted)")
    log("=" * 70)
    for k in sorted(cls.keys()):
        log(f"  {k:34} = {_describe(cls[k], k)}")
    log("=" * 70)
    hits = [k for k in cls
            if any(w in str(k).lower()
                   for w in ("wait", "standby", "queue", "reserve", "pending"))]
    if hits:
        log("FIELDS THAT LOOK WAITING-LIST RELATED:")
        for k in hits:
            log(f"  {k} = {_describe(cls[k], k)}")
    else:
        log("No obviously waiting-list-named field on the class object.")
    log("=" * 70)


def inspect_day(classes, out_path):
    """Write every class's scalar fields to JSON, redacted, for offline diffing.

    The point is comparison: whichever field differs between a class you are on
    the waiting list for and the rest of the day is the field that marks it.
    """
    rows = []
    for cls in classes:
        row = {}
        for k, v in cls.items():
            if _is_sensitive(k):
                continue
            if isinstance(v, dict):
                # keep only a nested display name, which is not personal
                if "name" in v and not _is_sensitive("name"):
                    row[f"{k}.name"] = v.get("name")
                row[f"{k}.__keys"] = sorted(v.keys())
            elif isinstance(v, list):
                row[f"{k}.__count"] = len(v)
                if v and isinstance(v[0], dict):
                    row[f"{k}.__keys"] = sorted(v[0].keys())
            else:
                row[k] = v
        rows.append(row)

    pathlib.Path(out_path).write_text(
        json.dumps(rows, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
    )

    log("")
    log("=" * 70)
    log(f"Wrote {len(rows)} classes to {out_path}")
    log("=" * 70)
    # Surface anything waiting-list shaped straight away.
    interesting = set()
    for row in rows:
        for k in row:
            if any(w in k.lower() for w in ("wait", "standby", "queue", "reserve",
                                            "pending", "book")):
                interesting.add(k)
    if interesting:
        log("Fields that may indicate booking / waiting-list state:")
        for k in sorted(interesting):
            values = sorted({str(r.get(k)) for r in rows})
            log(f"  {k:32} values seen: {', '.join(values[:8])}")
    log("=" * 70)


# ---------------------------------------------------------------------------
# Race tuning
#
# Popular classes here fill in about five seconds, so the first request has to
# land in the opening moment. Two measured facts from booking.log shape this:
#   * HTTP 516 "Schedule Is Full" is how a full class is reported.
#   * The API rate-limits after roughly 4 requests in ~1.1s (HTTP 429), so
#     hammering harder than that is counter-productive.
# One thing has never been measured: what the API answers to a request that
# lands a moment BEFORE the window opens. So any other answer is retried until
# the burst ends rather than taken as final.
# ---------------------------------------------------------------------------
BURST_SECONDS      = 15.0   # keep trying to book for this long after the window
ATTEMPT_GAP        = 0.32   # ~3 requests/sec: under the observed 429 threshold
RATE_LIMIT_BACKOFF = 1.20   # pause this long after a 429
MIN_LEAD           = 0.05   # never fire more than this early...
MAX_LEAD           = 0.50   # ...and never more than this
ARRIVE_AFTER       = 0.03   # aim to land just after the window, not just before it
PREFIRE_SECONDS    = 75     # log in again and re-check the class this long before the window
PREFIRE_MARGIN     = 10     # ...but only if the wait is at least this much longer than that
WARM_SECONDS       = 4      # open a fresh connection this long before firing
FULL_STATUS        = 516    # "Schedule Is Full"

# The waiting list ("standby", in this API's words) is unlimited, so if a class
# fills we join it immediately -- standby is ordered by join time, and
# stand_by_position tells us where we landed.
#
# CONFIRMED against the live API on 2026-09-09: a real 200 from
# /scheduleStandBy/insert with {schedule_id, membership_user_id, extras}.
# Note the capital B. Discovered from the schedule payload, where each class
# carries booking_option telling the app which action to call:
#     free spots     -> "insertScheduleUser"  (i.e. /scheduleUser/insert)
#     full           -> "insertStandby"       (i.e. /scheduleStandBy/insert)
#     already queued -> "cancelWaitList"
WAITLIST_PATH = "/scheduleStandBy/insert"


# ---------------------------------------------------------------------------
# API calls
# ---------------------------------------------------------------------------
def try_login(email, password, whitelabel, attempts=3):
    """(token, refresh), or None. Transient failures are retried; rejected
    credentials are not."""
    status, resp = None, None
    for attempt in range(1, attempts + 1):
        status, resp = _post(
            f"{BASE_URL}/user/login",
            _headers(whitelabel=whitelabel),
            {"email": email, "password": password},
        )
        if status == 200:
            break
        permanent = isinstance(status, int) and 400 <= status < 500 and status not in (408, 425, 429)
        log(f"  login attempt {attempt} failed (HTTP {status}): {str(resp)[:200]}")
        if permanent:
            log("  credentials rejected -- not retrying.")
            return None
        if attempt < attempts:
            time.sleep(3)
    if status != 200:
        return None
    data = (resp.get("data") or resp) if isinstance(resp, dict) else {}
    token = data.get("token")
    if not token:
        log("  login returned no token.")
        return None
    return token, data.get("refreshToken") or data.get("refresh_token", "")


def login(email, password, whitelabel):
    log("Step 1: login...")
    # Retry transient failures. This runs well before the signup window, so a
    # few seconds spent here costs nothing -- but giving up means no booking.
    creds = try_login(email, password, whitelabel)
    if not creds:
        log("Login failed.")
        sys.exit(2)
    log(f"  logged in (token len {len(creds[0])})")
    return creds


def fetch_schedule(class_date, locations_box_id, boxes_id, token, refresh, whitelabel,
                   must=True, to_date=None):
    """The day's classes -- or, with to_date, every class from class_date to
    to_date. With must=False a failure returns None instead of ending the run,
    for the re-checks, where carrying on beats stopping."""
    if must:
        log(f"Step 2: fetching schedule for {class_date}...")
    iso = f"{class_date}T00:00:00.000Z"
    for attempt in range(1, 4 if must else 2):
        status, resp = _post(
            f"{BASE_URL}/schedule/betweenDates",
            _headers(token, refresh, whitelabel),
            {
                "from": iso,
                "to": f"{to_date or class_date}T00:00:00.000Z",
                "locations_box_id": locations_box_id,
                "boxes_id": boxes_id,
            },
        )
        if status == 200:
            break
        log(f"  schedule attempt {attempt} failed (HTTP {status}): {str(resp)[:200]}")
        if must and attempt < 3:
            time.sleep(3)
    if status != 200:
        log(f"Schedule fetch HTTP {status}: {str(resp)[:300]}")
        if must:
            sys.exit(3)
        return None
    classes = (resp.get("data") or resp) if isinstance(resp, dict) else resp
    if not isinstance(classes, list):
        classes = []
    if must:
        log(f"  got {len(classes)} classes for {class_date}")
    return classes


def find_class(classes, class_time, category_filter):
    found = []
    for cls in classes:
        tval = str(cls.get("time", ""))[:5]
        if tval != class_time:
            continue
        if category_filter:
            name = str((cls.get("box_categories") or {}).get("name", ""))
            if category_filter.lower() not in name.lower():
                continue
        found.append(cls)
    return found


def find_by_id(classes, schedule_id):
    for cls in classes or []:
        if cls.get("id") == schedule_id:
            return cls
    return None


def book_class(schedule_id, membership_user_id, token, refresh, whitelabel):
    status, resp = _post(
        f"{BASE_URL}/scheduleUser/insert",
        _headers(token, refresh, whitelabel),
        {
            "schedule_id": int(schedule_id),
            "membership_user_id": int(membership_user_id),
            "extras": None,
        },
    )
    return status, resp


def is_full(status, resp):
    """True when the API says the class has no free spots: HTTP 516 "Schedule Is
    Full". The wording is checked as well as the code: if 516 also carried some
    other refusal, reading it as "full" would put us on the waiting list for a
    class that still has spots."""
    text = str(resp).lower()
    if status == FULL_STATUS:
        return "full" in text
    return "schedule is full" in text


def join_waiting_list(schedule_id, membership_user_id, token, refresh, whitelabel):
    """Join the standby list. Returns (path, response) on success."""
    hdrs = _headers(token, refresh, whitelabel)
    body = {
        "schedule_id": int(schedule_id),
        "membership_user_id": int(membership_user_id),
        "extras": None,
    }
    url = f"{BASE_URL}{WAITLIST_PATH}"
    for attempt in (1, 2):
        status, resp = _post(url, hdrs, body)
        if status == 200:
            log(f"  standby JOINED via {WAITLIST_PATH}")
            return WAITLIST_PATH, resp
        log(f"  standby: {WAITLIST_PATH} -> HTTP {status} :: {str(resp)[:300]}")
        if status == 429 and attempt == 1:
            time.sleep(RATE_LIMIT_BACKOFF)
            continue
        break
    return None, None


def standby_position(resp):
    """Pull stand_by_position out of a standby-join response, if it is there."""
    if isinstance(resp, dict):
        data = resp.get("data")
        if isinstance(data, dict) and data.get("stand_by_position") is not None:
            return data.get("stand_by_position")
    return None


# ---------------------------------------------------------------------------
# The race
# ---------------------------------------------------------------------------
def stop_if_already_done(cls):
    """Exit rather than act twice: 0 if already booked, 6 if already on standby."""
    if cls.get("user_booked") is not None:
        log("Already booked into this class - nothing to do.")
        sys.exit(0)
    if str(cls.get("booking_option") or "") == "cancelWaitList" or cls.get("user_in_standby") is not None:
        pos = cls.get("stand_by_position")
        log("Already on the waiting list for this class"
            + (f" (position {pos})" if pos is not None else "") + " - nothing to do.")
        if pos is not None:
            print(f"STANDBY_POSITION={pos}", flush=True)
        sys.exit(6)


def sleep_until(moment):
    """Sleep until `moment` (naive local time): one long sleep, then a fine spin
    for the last second -- a class here fills in ~5s, so 10ms of slop matters."""
    delta = (moment - datetime.now()).total_seconds()
    if delta > 1.5:
        time.sleep(delta - 1.0)
    while datetime.now() < moment:
        time.sleep(0.002)


def get_ready(window, auth, relogin, recheck):
    """Sleep to the window and arrive ready to fire.

    A minute or so before, log in again (the first login may be up to an hour
    old by then) and re-read the class (you may have booked it in the app
    meanwhile). Then open a fresh connection and send slightly early -- by half
    the fastest round trip seen -- so the request lands as the window opens."""
    if (window - datetime.now()).total_seconds() > PREFIRE_SECONDS + PREFIRE_MARGIN:
        refresh_at = window - timedelta(seconds=PREFIRE_SECONDS)
        log(f"Waiting until {refresh_at.strftime('%H:%M:%S')} to log in again...")
        sleep_until(refresh_at)
        fresh = relogin()
        if fresh:
            auth["token"], auth["refresh"] = fresh
            log("  logged in again")
        else:
            log("  could not log in again - keeping the earlier token")
        recheck()
        log(f"  API clock vs ours: {server_clock_note()}")

    rtt = MIN_POST_SECONDS if MIN_POST_SECONDS is not None else LAST_POST_SECONDS
    lead = min(MAX_LEAD, max(MIN_LEAD, rtt / 2.0)) - ARRIVE_AFTER
    fire_at = window - timedelta(seconds=lead)
    warm_at = fire_at - timedelta(seconds=WARM_SECONDS)
    if datetime.now() < warm_at:
        sleep_until(warm_at)
    warm_connection()
    if datetime.now() < fire_at:
        log(f"Fastest round trip {rtt*1000:.0f}ms -> firing {lead*1000:.0f}ms before "
            f"{window.strftime('%H:%M:%S')}.")
        sleep_until(fire_at)
    else:
        log("Window already open (or past) - firing immediately.")
    log(f"FIRING at {datetime.now().strftime('%H:%M:%S.%f')[:-3]} "
        f"(window opens {window.strftime('%H:%M:%S')})")


def burst(schedule_id, membership_uid, auth, relogin, whitelabel, burst_seconds):
    """Book until it works, the class turns out full, or burst_seconds pass.

    Returns ("booked" | "full" | "refused", attempts). Every other answer -- a
    refusal from landing a moment early, a dropped connection, an expired token,
    a rate limit -- is retried until the time is up."""
    deadline = time.monotonic() + burst_seconds
    attempt = 0
    relogged = False
    refusals = {}
    while time.monotonic() < deadline:
        attempt += 1
        status, resp = book_class(schedule_id, membership_uid, auth["token"], auth["refresh"],
                                  whitelabel)
        stamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]

        if status == 200:
            log(f"Attempt {attempt} SUCCESS at {stamp}")
            return "booked", attempt

        if is_full(status, resp):
            # No point hammering a full class - and the waiting list is ordered
            # by join time, so every moment spent retrying costs us a place.
            log(f"Attempt {attempt}: class is FULL (HTTP {status}) at {stamp} - going "
                f"straight to the waiting list.")
            return "full", attempt

        if status == 429:
            log(f"Attempt {attempt}: rate limited - backing off {RATE_LIMIT_BACKOFF}s")
            time.sleep(RATE_LIMIT_BACKOFF)
            continue

        if status == 401 and not relogged:
            relogged = True
            log(f"Attempt {attempt}: token rejected (HTTP 401) - logging in again")
            fresh = relogin()
            if fresh:
                auth["token"], auth["refresh"] = fresh
            continue

        log(f"Attempt {attempt} HTTP {status} at {stamp} : {str(resp)[:200]}")
        refusals[status] = str(resp)[:200]
        time.sleep(ATTEMPT_GAP)

    log(f"Not booked after {attempt} attempt(s) in {burst_seconds:.0f}s. Answers seen:")
    for status, text in refusals.items():
        log(f"  HTTP {status}: {text}")
    return "refused", attempt


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Book a CrossFit White City class")
    parser.add_argument("--class-date", required=True, help="YYYY-MM-DD")
    parser.add_argument("--class-time", default="00:00", help="HH:MM (24h)")
    parser.add_argument("--category-filter", default="", help="Substring match on category name")
    parser.add_argument("--wait-for-window", action="store_true", help="Sleep until signup window opens")
    parser.add_argument("--dry-run", action="store_true", help="Don't actually book, just print what would happen")
    parser.add_argument("--burst-seconds", type=float, default=BURST_SECONDS,
                        help="How long to keep retrying the booking (default %(default)s)")
    parser.add_argument("--no-waitlist", action="store_true",
                        help="Do not join the waiting list if the class is full")
    parser.add_argument("--inspect-day", action="store_true",
                        help="Write every class of that day to JSON (redacted) and "
                             "exit. Books nothing, changes nothing.")
    parser.add_argument("--out", default="",
                        help="Where --inspect-day writes its JSON")
    parser.add_argument("--inspect", action="store_true",
                        help="Print the class's raw fields (redacted) and exit. "
                             "Books nothing, joins nothing, changes nothing.")
    parser.add_argument("--waitlist-only", action="store_true",
                        help="Skip booking; just probe the waiting-list endpoints. "
                             "Cannot accidentally book a class, so it cannot use "
                             "up a class from your monthly allowance.")
    args = parser.parse_args()

    # --- Credentials: environment only (repository secrets on GitHub) ---
    email    = _env("ARBOX_EMAIL")
    password = _env("ARBOX_PASSWORD")
    whitelabel     = _env("ARBOX_WHITELABEL", "HYPR-training")
    boxes_id       = _env_int("ARBOX_BOXES_ID", 59)
    loc_box_id     = _env_int("ARBOX_LOCATIONS_BOX_ID", 48)
    membership_uid = _env_int("ARBOX_MEMBERSHIP_USER_ID", 13705578)

    if not email or not password:
        log("No credentials. Set the ARBOX_EMAIL and ARBOX_PASSWORD environment variables "
            "(repository secrets on GitHub).")
        sys.exit(1)

    # --- Parse target time ---
    try:
        target_dt = datetime.strptime(f"{args.class_date} {args.class_time}", "%Y-%m-%d %H:%M")
    except ValueError:
        log("Invalid --class-date / --class-time. Expected YYYY-MM-DD and HH:MM.")
        sys.exit(1)

    hours_before = hours_before_for(target_dt)
    windows = window_candidates_for(target_dt)

    log("=" * 50)
    log(f"Target class: {target_dt.strftime('%Y-%m-%d %H:%M %A')}")
    log(f"Window opens: {windows[0].strftime('%Y-%m-%d %H:%M:%S')} ({hours_before}h before class)")
    if len(windows) > 1:
        log(f"          or: {windows[1].strftime('%Y-%m-%d %H:%M:%S')} - the clocks change in "
            f"between, so both are tried")
    if args.category_filter:
        log(f"Category filter: '{args.category_filter}'")
    log(f"Now:          {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log(f"DryRun:       {args.dry_run}")

    # --- Login + fetch schedule ---
    token, refresh = login(email, password, whitelabel)
    auth = {"token": token, "refresh": refresh}
    classes = fetch_schedule(args.class_date, loc_box_id, boxes_id, token, refresh, whitelabel)

    # --- Whole-day inspection: look, change nothing ---
    if args.inspect_day:
        out = args.out or os.path.join(SCRIPT_DIR, f"day-{args.class_date}.json")
        inspect_day(classes, out)
        log("Inspection only - nothing was booked and no waiting list was joined.")
        sys.exit(0)

    # --- Find target class ---
    candidates = find_class(classes, args.class_time, args.category_filter)
    if not candidates:
        log(f"No class found at {args.class_time} on {args.class_date} (filter='{args.category_filter}').")
        log("Available classes:")
        for cls in classes:
            t     = str(cls.get("time", ""))[:5]
            cat   = str((cls.get("box_categories") or {}).get("name", ""))
            coach = str((cls.get("coach") or {}).get("full_name", ""))
            log(f"  {t}  {cat}  ({coach})")
        sys.exit(4)

    if len(candidates) > 1:
        log("Multiple classes match — using the first. Refine with --category-filter.")
        for cls in candidates:
            log(f"  candidate id={cls['id']} cat='{(cls.get('box_categories') or {}).get('name','')}' coach='{(cls.get('coach') or {}).get('full_name','')}'")

    target      = candidates[0]
    schedule_id = target["id"]
    cat_name    = str((target.get("box_categories") or {}).get("name", ""))
    coach_name  = str((target.get("coach") or {}).get("full_name", ""))
    free        = target.get("free", "?")
    log(f"Target found: id={schedule_id}  {target.get('time','')}  {cat_name}  coach={coach_name}  free={free}")

    # --- What does the API itself say we should do with this class? ---
    booking_option = str(target.get("booking_option") or "")
    stand_by_now   = target.get("stand_by")
    if booking_option:
        log(f"  booking_option={booking_option}  stand_by={stand_by_now}  "
            f"max_users={target.get('max_users')}")
    reg_hours = target.get("enable_registration_time")
    if reg_hours not in (None, "") and str(reg_hours) != str(hours_before):
        log(f"  NOTE: the class itself says it opens {reg_hours}h ahead; the rule here "
            f"says {hours_before}h.")

    # --- Read-only inspection: look, change nothing ---
    if args.inspect:
        inspect_class(target)
        log("Inspection only - nothing was booked and no waiting list was joined.")
        sys.exit(0)

    # --- Already sorted? Then stop, rather than acting twice ---
    stop_if_already_done(target)

    def relogin():
        return try_login(email, password, whitelabel, attempts=2)

    def recheck():
        """Re-read the class shortly before the window; stop if it's already sorted."""
        latest = find_by_id(fetch_schedule(args.class_date, loc_box_id, boxes_id, auth["token"],
                                           auth["refresh"], whitelabel, must=False),
                            schedule_id)
        if latest is not None:
            log(f"  re-checked the class: free={latest.get('free', '?')}  "
                f"booking_option={latest.get('booking_option')}")
            stop_if_already_done(latest)

    # Tells bookings.py the class was found and this run is armed.
    print(f"READY_AT={windows[0].strftime('%Y-%m-%d %H:%M:%S')}|{cat_name}", flush=True)

    # --- Dry run ---
    if args.dry_run:
        if args.wait_for_window:
            get_ready(windows[0], auth, relogin, recheck)
        log(f"DRY RUN - would POST /scheduleUser/insert for schedule_id={schedule_id}")
        log(f"DRY RUN - would fall back to the waiting list on HTTP {FULL_STATUS}")
        log("Done (dry run).")
        sys.exit(0)

    # --- Probe the waiting list only, without trying to book ---
    if args.waitlist_only:
        if args.wait_for_window:
            get_ready(windows[0], auth, relogin, recheck)
        log("--waitlist-only: skipping the booking attempt entirely.")
        log(f"Free spots reported for this class: {free}")
        log("Step 6: joining the standby list ...")
        path, _resp = join_waiting_list(
            schedule_id, membership_uid, auth["token"], auth["refresh"], whitelabel
        )
        if path:
            log("")
            log(f"WAITLISTED for {target_dt.strftime('%Y-%m-%d %H:%M')} - {cat_name}")
            sys.exit(6)
        log("")
        log("The standby call was rejected - see the HTTP code above.")
        sys.exit(5)

    # --- Book: at each possible window, burst until booked, full or out of time ---
    outcome, attempts = "refused", 0
    tries = windows if args.wait_for_window else [None]
    for i, window in enumerate(tries):
        if window is not None:
            get_ready(window, auth, relogin, recheck)
        log(f"Step 5: POST /scheduleUser/insert (up to {args.burst_seconds:.0f}s, "
            f"~{1/ATTEMPT_GAP:.1f} req/s) ...")
        outcome, n = burst(schedule_id, membership_uid, auth, relogin, whitelabel,
                           args.burst_seconds)
        attempts += n
        later = tries[i + 1] if i + 1 < len(tries) else None
        if outcome != "refused" or later is None or datetime.now() >= later:
            break
        log(f"Not open at {window.strftime('%H:%M')} - the clocks change in between, so "
            f"trying again at {later.strftime('%H:%M')}.")

    if outcome == "booked":
        log(f"BOOKING CONFIRMED for {target_dt.strftime('%Y-%m-%d %H:%M')} - {cat_name}")
        sys.exit(0)

    if outcome == "refused":
        # A reply can be lost after the booking went through. Ask the class itself.
        latest = find_by_id(fetch_schedule(args.class_date, loc_box_id, boxes_id, auth["token"],
                                           auth["refresh"], whitelabel, must=False),
                            schedule_id)
        if latest is not None and latest.get("user_booked") is not None:
            log(f"BOOKING CONFIRMED for {target_dt.strftime('%Y-%m-%d %H:%M')} - {cat_name} "
                f"(a reply was lost, but the class shows you booked)")
            sys.exit(0)
        log("BOOKING FAILED - the API kept refusing; see the answers above.")
        sys.exit(5)

    if args.no_waitlist:
        log(f"Class full after {attempts} attempt(s), and --no-waitlist was given.")
        sys.exit(5)

    # --- Waiting list fallback (unlimited, so this is always worth doing) ---
    log("Step 6: joining the standby list ...")
    path, _resp = join_waiting_list(
        schedule_id, membership_uid, auth["token"], auth["refresh"], whitelabel
    )
    if path:
        # Re-read the class so we can tell the user where in the queue they are;
        # this happens after the race, so the extra request costs nothing.
        position = standby_position(_resp)
        if position is None:
            latest = find_by_id(fetch_schedule(args.class_date, loc_box_id, boxes_id,
                                               auth["token"], auth["refresh"], whitelabel,
                                               must=False),
                                schedule_id)
            if latest is not None:
                position = latest.get("stand_by_position")

        log(f"WAITLISTED for {target_dt.strftime('%Y-%m-%d %H:%M')} - {cat_name}"
            + (f" - position {position}" if position is not None else ""))
        if position is not None:
            print(f"STANDBY_POSITION={position}", flush=True)
        sys.exit(6)

    log("Could not book the class and could not join the standby list.")
    sys.exit(5)


if __name__ == "__main__":
    main()
