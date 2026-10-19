# WC Signup — Setup

Books CrossFit White City classes the moment signup opens. Runs on GitHub
Actions in a public repo, so it is free, and needs no computer of yours to be on.

## The problem this has to solve

Popular classes fill in **about five seconds**, so the booking request has to
arrive in the opening moment. These measurements shape the design:

| Measured fact | Consequence |
|---|---|
| A run on 2026-06-01 fired at 10:07 for an 08:00 window — over 2h late | GitHub's own timer can't be trusted to start anything on time. |
| Others report GitHub's timer 8–14 hours late, and runs dropped (July 2026) | Even "start a few hours early" isn't safe if GitHub's timer is the trigger. |
| HTTP **516** `"Schedule Is Full"` is how a full class is reported | That is the signal to switch to the waiting list. |
| **HTTP 429** after ~4 requests in ~1.1s | Sending faster than ~3 requests/second is counter-productive. |
| What the API answers to a request that lands a moment *early* | Never observed — so any unknown answer is retried, not taken as final. |

## How the timing works

1. **cron-job.org** (free) presses "Run workflow" on **Book due classes** every
   15 minutes, through the GitHub API. A start requested this way begins within
   seconds. GitHub's own timer (`:13` and `:43`) is kept only as a backup.
2. **Book due classes** (the planner) starts a **Book one class** run for every
   queued class whose signup opens within **50 minutes** — so each class gets
   three or more chances to be picked up in time.
3. **Book one class** logs in, finds the class, and sleeps:
   - **75s before:** logs in again (a fresh token) and re-reads the class, in case
     you booked it in the app meanwhile.
   - **4s before:** opens a fresh connection, so the opening request doesn't pay
     for the TCP and TLS handshakes.
   - **At the window:** sends early by half the fastest round trip seen (aiming
     to land 30ms after it opens), then retries ~3 times a second for 15 seconds.
     Booked, full (→ waiting list) and rate-limited are final; anything else is
     retried until the time is up.

Classes that open at the same moment (a Saturday and a Sunday class at the same
time both open on Thursday) get separate runs, side by side.

## Signup window rule

Classes open **48h** ahead, except **Sunday and Monday** classes, which open
**72h** ahead — Sunday's on Thursday, Monday's on Friday, at the same time of day.
Defined once, in `book_class.py:hours_before_for()`.

**Clock-change weekends.** When Israel's clocks change inside those 48/72 hours,
"the same time of day" and "72 hours of elapsed time" are an hour apart, and
Arbox doesn't say which it uses. `window_candidates_for()` returns both; the run
tries the earlier, and if signup isn't open yet, waits and tries the later.

## Waiting list ("standby")

The waiting list is unlimited, so a full class is never a dead end. On HTTP 516
the script stops retrying **immediately** and joins standby — it's ordered by
join time, so every moment spent retrying a full class costs a place. Arbox
promotes you automatically when someone drops out, and you're told your place.

**Endpoint: `POST /scheduleStandBy/insert`** (note the capital B), with the same
body as a booking: `{schedule_id, membership_user_id, extras}`. Confirmed
against the live API on 2026-09-09 (HTTP 200). Found from the schedule payload:
every class carries a `booking_option` naming the action the app should call.

| `booking_option` | State | Endpoint |
|---|---|---|
| `insertScheduleUser` | free spots | `/scheduleUser/insert` |
| `insertStandby` | full | `/scheduleStandBy/insert` |
| `cancelWaitList` | you are already queued | — |

Other useful fields on a class: `free` (0 or -1 when full), `stand_by` (how many
are queued), `stand_by_position` (**your** place), `user_in_standby`,
`user_booked`, `max_users`, `enable_registration_time` (the gym's own hours-ahead
setting; logged when it disagrees with the rule above).

The script reads these before acting, so it never books or queues twice.
`--inspect` and `--inspect-day` re-dump them at any time (read-only, other
members' personal details redacted) if the API ever changes shape.

## Exit codes (`book_class.py`)

| Code | Meaning |
|---|---|
| 0 | Booked (or already booked) |
| 1 | No credentials, or a bad date/time |
| 2 | Login failed |
| 3 | Could not read the schedule |
| 4 | No class matches that time and name |
| 5 | Not booked, and not on the waiting list |
| 6 | On the waiting list (not treated as a failure) |

## Files

| File | Role |
|---|---|
| `queue/` | The queue: one file per class you want, e.g. `2026-10-21_0800.json`. |
| `bookings.py` | Adds to / removes from the queue, plans which classes are due, runs one booking. |
| `book_class.py` | Logs in, finds the class, waits to the moment, fires, falls back to the waiting list. |
| `.github/workflows/add-booking.yml` | **Add booking** — the button you press from your phone. |
| `.github/workflows/book-due.yml` | **Book due classes** — the planner. |
| `.github/workflows/book-class.yml` | **Book one class** — one run per class. |
| `tests/` | Runs everything against a simulated gym API: `python -m unittest discover -s tests` (the workflow checks need PyYAML). |

One file per class means two runs changing the queue at the same moment never
touch the same file. Saving to GitHub retries from the latest copy if another
run saved first, so no change is lost.

## Repository secrets

Repo → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**.

| Secret | Value | Required |
|---|---|---|
| `ARBOX_EMAIL` | your HYPR app email | yes |
| `ARBOX_PASSWORD` | your HYPR app password | yes |
| `BOT_TOKEN` | Telegram bot token from @BotFather | for messages |
| `OWNER_CHAT_ID` | your Telegram user ID (from @userinfobot) | for messages |
| `ARBOX_WHITELABEL` | `HYPR-training` | no — code default |
| `ARBOX_BOXES_ID` | `59` | no — code default |
| `ARBOX_LOCATIONS_BOX_ID` | `48` | no — code default |
| `ARBOX_MEMBERSHIP_USER_ID` | `13705578` | no — code default |

An unset secret arrives as an empty string, which the code treats as "not set".

## The outside timer (cron-job.org)

| Field | Value |
|---|---|
| URL | `https://api.github.com/repos/ozargov/wc-signup/actions/workflows/book-due.yml/dispatches` |
| Method | `POST` |
| Body | `{"ref":"main"}` |
| Headers | `Accept: application/vnd.github+json` · `Authorization: Bearer <token>` · `X-GitHub-Api-Version: 2022-11-28` · `Content-Type: application/json` |
| Schedule | every 15 minutes, at :07, :22, :37 and :52 |

The token is a **fine-grained personal access token** for this repo only, with
one permission: **Actions — Read and write**. It can start the planner; it can't
read your secrets or change the code. It expires: GitHub emails you before it
does. Make a new one and paste it into cron-job.org. Until you do, only the late
backup timer runs.

## Booking a class from your phone

GitHub app → your repo → **Actions** → **Add booking** → **Run workflow**:

- **action** — `add`, or `remove` to cancel one (this also stops its booking run if it already started)
- **class_date** — `2026-10-21`
- **class_time** — `08:00` (24h, Israel time)
- **category_filter** — `Hall A`, or blank for any class at that time

If signup is already open, it books (or joins the waiting list) straight away.

## Telegram messages

📌 queued · ⏳ ready (its run has started and found the class) · ✅ booked ·
🕒 on the waiting list, with your place · ❌ failed or no such class · ⚠️ something needs a look.

## Running costs

Free: standard GitHub runners are free in public repos, and cron-job.org is free.

## Known edges

- **GitHub's rules** say Actions are meant for building and testing software. A
  light personal automation like this is a gray area — unlikely to be a problem,
  but GitHub could switch it off.
- **Public logs.** Anyone can read the Actions logs: they show class dates, times
  and coach names. Secrets are masked; other members' names are never printed.
- **If Arbox changes its app,** the bot may break until fixed. `--inspect-day`
  shows what the API returns now.
- **Plan limit.** The membership allows 12 classes a month. The bot doesn't track
  that — once it's used up, bookings fail and you get a message.
- **Running it on a PC** needs `ARBOX_EMAIL` and `ARBOX_PASSWORD` set as
  environment variables. Nothing reads `config.json` any more.

## Credentials

Never commit credentials. They live only in repository secrets (and the
cron-job.org token only in cron-job.org).
