# WC Signup — Setup

Books CrossFit White City classes the moment signup opens. You pick classes in
a Telegram bot; GitHub Actions books each one on the second. All of it is free
and needs no computer of yours to be on.

## The Telegram bot

Send the bot any message and it shows **next week's classes**. Tap a day, tap
the class you want (one per day — a new pick replaces that day's old one), then
**📌 Book**. Each class shows its state: `opens Thu 07:00`, `12 free`,
`full, 4 waiting`, `☑️ booked`, `🕒 waiting list #2`, `📌 queued`.

- `wed 18:00 hall a`, `רביעי 7:00`, `25/10 07:00` — jump to that day with the class pre-ticked.
- `/booked` (or "what is booked") — what the gym says you're booked into over the next 14 days, plus your waiting-list places.
- `/list` — what's queued, each with a ❌ button to remove it.
- `/test` — GitHub sends you a test message. The booking messages come from GitHub, not from the bot, so this proves they'll reach you.
- `/help` — how it works. `/start` also refreshes the bot's command menu.

How it fits together:

1. The bot (`wcbot.py`) runs on **Vercel** as a webhook (`api/telegram.py`):
   Telegram calls it when you send something, so nothing runs in between.
   It reads the class list from the gym's API and keeps your picks in the
   message itself, so it needs no storage of its own.
2. **Book** starts the **Queue from Telegram** workflow (`bot-queue.yml`), which
   saves the picks to `queue/`, sends the 📌 confirmation, and starts a booking
   run at once for anything opening within 50 minutes.
3. From there, the timing below takes over.

The bot's GitHub key can only **start workflows** (Actions: read and write). It
can't change code or read the repo's secrets, which is why the queue edit
happens in a workflow rather than in the bot.

### Vercel settings

| Environment variable | Value |
|---|---|
| `BOT_TOKEN` | the Telegram bot token |
| `OWNER_CHAT_ID` | your Telegram user ID — the bot ignores everyone else |
| `ARBOX_EMAIL` / `ARBOX_PASSWORD` | your HYPR login, to read the class list |
| `GITHUB_TOKEN` | fine-grained token, this repo only, **Actions: read and write** |
| `GITHUB_REPO` | optional; defaults to `ozargov/WC-Signup` |

After the first deploy, open `https://<your-project>.vercel.app/api/telegram?setup=1`
once. It connects Telegram to the bot (with a secret only Telegram knows,
derived from the bot token), sets the bot's command menu, and checks the
settings. Open it again if the bot token ever changes.

Vercel redeploys on every push, except pushes that only change `queue/`
(`vercel.json` → `ignoreCommand`).

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

When the gym's answer is known, the day goes in `ONE_HOUR_LATER_DAYS` and only
that time is tried. For winter time on Sun 25 Oct 2026 the app showed the Sunday
and Monday classes opening an hour later (Thu/Fri at 09:00 for an 08:00 class),
which is 72 hours of elapsed time.

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
| `wcbot.py` | The Telegram bot: week picker, `/list`, typed shortcuts. |
| `api/telegram.py` | Vercel's entry point for the bot (and the `?setup=1` link). |
| `vercel.json`, `public/` | Vercel settings, and a placeholder page so nothing else is served. |
| `queue/` | The queue: one file per class you want, e.g. `2026-10-21_0800.json`. |
| `bookings.py` | Adds to / removes from the queue, plans which classes are due, runs one booking. |
| `book_class.py` | Logs in, finds the class, waits to the moment, fires, falls back to the waiting list. |
| `.github/workflows/bot-queue.yml` | **Queue from Telegram** — started by the bot's Book and Remove buttons. |
| `.github/workflows/add-booking.yml` | **Add booking** — a backup button in the GitHub app. |
| `.github/workflows/book-due.yml` | **Book due classes** — the planner. |
| `.github/workflows/book-class.yml` | **Book one class** — one run per class. |
| `tests/` | Runs everything against simulated gym, Telegram and GitHub APIs: `python -m unittest discover -s tests` (the workflow checks need PyYAML). |

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
| URL | `https://api.github.com/repos/ozargov/WC-Signup/actions/workflows/book-due.yml/dispatches` |
| Method | `POST` |
| Body | `{"ref":"main"}` |
| Headers | `Accept: application/vnd.github+json` · `Authorization: Bearer <token>` · `X-GitHub-Api-Version: 2022-11-28` · `Content-Type: application/json` |
| Schedule | every 15 minutes |
| Notifications | on: email me when an execution fails (that's how you'll hear the token expired) |

The token is a **fine-grained personal access token** for this repo only, with
one permission: **Actions — Read and write**. It can start the planner; it can't
read your secrets or change the code. It expires: GitHub emails you before it
does. Make a new one and paste it into cron-job.org. Until you do, only the late
backup timer runs.

## Backup: booking from the GitHub app

If the Telegram bot is ever down: GitHub app → your repo → **Actions** →
**Add booking** → **Run workflow**:

- **action** — `add`, or `remove` to cancel one (this also stops its booking run if it already started)
- **class_date** — `2026-10-21`
- **class_time** — `08:00` (24h, Israel time)
- **category_filter** — `Hall A`, or blank for any class at that time

If signup is already open, it books (or joins the waiting list) straight away.

## Telegram messages

📌 queued · ⏳ ready (its run has started and found the class) · ✅ booked ·
🕒 on the waiting list, with your place · ❌ failed or no such class · ⚠️ something needs a look.

These come from the GitHub runs, using the `BOT_TOKEN` and `OWNER_CHAT_ID`
repository secrets (the bot's own replies use Vercel's copies). If a run can't
send one, it leaves a yellow **warning** on the run's page with Telegram's
reason — "Unauthorized" means the token is wrong; "chat not found" means the
chat ID is wrong, or you never pressed Start in the bot's chat.

## Running costs

Free: standard GitHub runners are free in public repos, cron-job.org is free,
and the bot fits easily in Vercel's free Hobby plan (personal, non-commercial use).

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
