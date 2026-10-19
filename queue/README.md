# Booking queue

One file per class waiting to be booked, named after its date and time — for example `2026-10-21_0800.json` is the 08:00 class on 21 October.

- **Add or remove a class:** Actions → **Add booking** → **Run workflow**.
- **When a file disappears:** its class has been booked, joined on the waiting list, or tried and failed. A Telegram message tells you which.

Don't edit these files by hand. The **Add booking** button keeps them in the right format.
