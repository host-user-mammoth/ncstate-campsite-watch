#!/usr/bin/env python3
"""Watch the NC State Fair Campground for open RV sites on NC State home-football
weekends, and alert (ntfy phone push and/or Gmail) when both the night before and
the night of a game have open sites. A single open night does not trigger an alert.

Availability comes from the Firefly Reservations availability calendar, which
reports how many sites are free for each night. Standard library only.

Usage:
  python camp_watch.py                # check, alert on changes, update state.json
  python camp_watch.py --dry-run      # print availability only; no alerts, no state
  python camp_watch.py --test-notify  # send a test alert on every configured channel
  python camp_watch.py --send-settings  # send a summary of what the monitor watches and how it alerts

Alert channels are configured with environment variables:
  NTFY_TOPIC            ntfy.sh topic your phone is subscribed to
  GMAIL_ADDRESS         Gmail account that sends the email
  GMAIL_APP_PASSWORD    Gmail App Password for that account
  ALERT_EMAIL_TO        optional; mailing list (commas, spaces or new lines), sent as BCC (GMAIL_ADDRESS always gets a copy)
"""

import argparse
import datetime as dt
import json
import os
import re
import smtplib
import sys
import time
import urllib.parse
import urllib.request
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path

# --- Settings -----------------------------------------------------------------

# NC State home games to watch at Carter-Finley Stadium, 2026 season (only Duke for now).
# The monitor alerts only when the night before AND the night of a game are both open.
HOME_GAMES = {
    "2026-11-07": "Duke",
}

# Rig used for the search. Site length limits change which sites count as open.
RV_LENGTH_FT = 25
RV_EQUIPMENT = "TT"  # FW fifth wheel, MHA/MHB/MHC motorhome, PU popup, TT travel trailer, TC truck camper
EQUIPMENT_NAMES = {"FW": "fifth wheel", "MHA": "Class A motorhome", "MHB": "Class B motorhome",
                   "MHC": "Class C motorhome", "PU": "popup", "TT": "travel trailer", "TC": "truck camper"}

# Sender name recipients see on alert emails, in place of the Gmail account's own name.
EMAIL_FROM_NAME = "NC State Campsite Alerts"

BOOKING_URL = "https://app.fireflyreservations.com/reserve/property/NCStateFairCampground"
CALENDAR_URL = "https://app.fireflyreservations.com/Reserve/GetPropertyAvailabilityCalendar"
PROPERTY_GUID = "98438fc0-fc1b-475d-b2c9-0a731b3dffd9"
STATE_FILE = Path(__file__).with_name("state.json")
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/129.0 Safari/537.36")

# --- Availability -------------------------------------------------------------

# Each calendar cell is <div class="day ...">; "day-number" and "day-head" are not cells.
DAY_CELL = re.compile(r'<div class="day( [^"]*)?">(.*?)(?=<div class="day[ "]|\Z)', re.S)
DAY_NUMBER = re.compile(r'class="day-number">\s*(\d+)')
# Open nights show <span class="count ...">3</span> or "10+"; full nights show "Unavailable" and no count.
SITE_COUNT = re.compile(r'class="count[^"]*"[^>]*>\s*([^<]*?)\s*<')


def fetch_month(first_day):
    """Return {date: sites_open_label} for every night in the month, e.g. "0", "3", "10+"."""
    search = {
        "PropertyGUID": PROPERTY_GUID,
        "ReservationType": "S",
        "Adults": 2,
        "Children": 0,
        "Pets": 0,
        "Vehicles": 1,
        "UnitType": "RV",
        "RVLength": RV_LENGTH_FT,
        "RVEquipmentType": RV_EQUIPMENT,
        "DurationMode": "NIGHTLY",
    }
    form = {f"searchParams[{key}]": value for key, value in search.items()}
    form["firstDayOfMonth"] = first_day.strftime("%m/%d/%Y")
    request = urllib.request.Request(
        CALENDAR_URL,
        data=urllib.parse.urlencode(form).encode(),
        headers={"User-Agent": USER_AGENT, "X-Requested-With": "XMLHttpRequest"},
    )
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return parse_calendar(response.read().decode("utf-8", "replace"), first_day)
        except Exception as error:
            if attempt == 2:
                raise
            print(f"Fetching {first_day:%b %Y} failed ({error}); retrying", file=sys.stderr)
            time.sleep(5 * (attempt + 1))


def parse_calendar(html, first_day):
    month = re.search(r'data-firstdayofmonth="(\d{4}-\d{2}-\d{2})"', html)
    if not month or dt.date.fromisoformat(month.group(1)) != first_day:
        raise ValueError(f"calendar for {first_day:%b %Y} is missing or shows the wrong month")
    nights = {}
    for classes, cell in DAY_CELL.findall(html):
        number = DAY_NUMBER.search(cell)
        if "other-month" in classes or not number:
            continue
        count = SITE_COUNT.search(cell)
        label = count.group(1) if count else "0"
        if not re.fullmatch(r"\d+\+?", label):
            raise ValueError(f"unexpected site count {label!r} on {first_day:%b} {number.group(1)}")
        nights[first_day.replace(day=int(number.group(1)))] = label
    if len(nights) < 28:
        raise ValueError(f"parsed only {len(nights)} nights for {first_day:%b %Y}; the page layout may have changed")
    return nights


def is_open(label):
    return int(label.rstrip("+")) > 0


# --- Game nights --------------------------------------------------------------

def today_eastern():
    try:
        from zoneinfo import ZoneInfo
        return dt.datetime.now(ZoneInfo("America/New_York")).date()
    except Exception:  # no time zone database available
        return dt.date.today()


def watched_games(today):
    """Return {game_date: opponent} for home games whose night before is still ahead."""
    games = {}
    for game_day, opponent in sorted(HOME_GAMES.items()):
        game = dt.date.fromisoformat(game_day)
        if stay(game)[0] >= today:
            games[game] = opponent
    return games


def stay(game):
    """The two nights to book for a game: the night before and the night of the game."""
    return (game - dt.timedelta(days=1), game)


def both_open(labels, game):
    return all(is_open(labels.get(night, "0")) for night in stay(game))


def day_name(day):
    return f"{day:%a %b} {day.day}"


def describe(label):
    if not is_open(label):
        return "FULL"
    return f"{label} site{'' if label == '1' else 's'} open"


def weekend_line(game, opponent, labels):
    """E.g. "Duke: Fri Nov 6 2 sites open | Sat Nov 7 FULL"."""
    return f"{opponent}: " + " | ".join(f"{day_name(night)} {describe(labels[night])}" for night in stay(game))


def settings_summary(today):
    games = watched_games(today)
    lines = ["Campground: NC State Fair Campground, RV sites",
             f"Rig: {RV_LENGTH_FT} ft {EQUIPMENT_NAMES.get(RV_EQUIPMENT, RV_EQUIPMENT)}",
             "Game weekends watched (night before + game night):"]
    lines += [f"  {opponent}: {day_name(stay(game)[0])} + {day_name(game)}" for game, opponent in games.items()]
    lines += ["  none left this season"] if not games else []
    lines.append("Alerts when: BOTH the night before and game night have an open site (one night alone is ignored)")
    lines.append("How you're alerted: by email"
                 + (", and by notification in the ntfy app (optional, setup below)"
                    if os.environ.get("NTFY_TOPIC", "").strip() else ""))
    return "\n".join(lines)


def ntfy_instructions():
    """How to get the phone alerts; includes the private topic name, so never print it to the public log."""
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        return ""
    return "\n".join([
        "Get instant phone alerts with ntfy (free, optional):",
        '1. Install the "ntfy" app from the App Store (iPhone) or Google Play (Android).',
        "2. Open it, tap +, type this topic name exactly, and tap Subscribe:",
        f"   {topic}",
        "   Leave the server setting as it is.",
        "3. Allow notifications when the app asks.",
        "When a campsite opens, you'll get a push; tap it to open the booking page.",
        "iPhone: if alerts only show inside the app, go to Settings > Notifications > ntfy and turn on "
        "Allow Notifications, or delete the subscription in the app and add it again.",
        "Keep the topic name private: anyone who has it can see these alerts.",
    ])


# --- Alerts -------------------------------------------------------------------

def send_push(topic, title, body, priority, tags):
    request = urllib.request.Request(
        f"https://ntfy.sh/{urllib.parse.quote(topic)}",
        data=body.encode("utf-8"),
        headers={"Title": title, "Priority": priority, "Tags": tags, "Click": BOOKING_URL},
    )
    urllib.request.urlopen(request, timeout=30).close()


def mailing_list(sender):
    """Addresses in ALERT_EMAIL_TO (separated by commas, spaces or new lines), minus the sender."""
    return [address for address in re.split(r"[\s,;]+", os.environ.get("ALERT_EMAIL_TO", ""))
            if "@" in address and address.lower() != sender.lower()]


def send_email(sender, title, body):
    password = os.environ.get("GMAIL_APP_PASSWORD", "").replace(" ", "")
    # The sender always gets a copy; the mailing list is BCC'd so recipients can't see each other.
    bcc = mailing_list(sender)
    message = EmailMessage()
    message["Subject"] = title
    message["From"] = formataddr((EMAIL_FROM_NAME, sender))
    message["To"] = formataddr((EMAIL_FROM_NAME, sender))
    if bcc:
        message["Bcc"] = ", ".join(bcc)
    message.set_content(f"{body}\n\nBook: {BOOKING_URL}\n")
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
        smtp.login(sender, password)
        smtp.send_message(message)
    print("Email sent")


def alert(title, body, priority="high", tags="tent", email=True):
    """Send on every configured channel and return a list of errors (empty if all succeeded)."""
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    sender = os.environ.get("GMAIL_ADDRESS", "").strip()
    if not topic and not sender:
        raise RuntimeError("No alert channel configured: set NTFY_TOPIC and/or GMAIL_ADDRESS + GMAIL_APP_PASSWORD")
    attempted, errors = 0, []
    if topic:
        attempted += 1
        try:
            send_push(topic, title, body, priority, tags)
        except Exception as error:
            errors.append(f"ntfy push failed: {error}")
    if sender and email:
        attempted += 1
        try:
            send_email(sender, title, body)
        except Exception as error:
            errors.append(f"email failed: {error}")
    for error in errors:
        print(f"::warning::{error}")
    if attempted and len(errors) == attempted:
        raise RuntimeError(f"Every alert channel failed for {title!r}")
    return errors


# --- Main ---------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--dry-run", action="store_true", help="print availability only; no alerts, no state")
    parser.add_argument("--test-notify", action="store_true", help="send a test alert on every configured channel")
    parser.add_argument("--send-settings", action="store_true", help="send a summary of the monitor's settings")
    parser.add_argument("--note", default="", help="with --send-settings: a line at the top saying what changed")
    args = parser.parse_args()

    if args.test_notify:
        errors = alert("Campsite monitor test",
                       "Test alert from the NC State Fair Campground monitor. If you got this, alerts work.",
                       priority="default", tags="white_check_mark")
        sys.exit(1 if errors else 0)
    if args.send_settings:
        note = args.note.strip()
        summary = settings_summary(today_eastern())
        print("\n\n".join(part for part in (note, summary) if part))
        body = "\n\n".join(part for part in (note, summary, ntfy_instructions()) if part)
        title = "Campsite alerts update" if note else "Campsite alert settings and rules"
        errors = alert(title, body, priority="default", tags="gear")
        sys.exit(1 if errors else 0)

    today = today_eastern()
    games = watched_games(today)
    if not games:
        print("No upcoming game weekends to watch. Disable the workflow or add next season's games.")
        return

    nights = [night for game in games for night in stay(game)]
    calendar = {}
    for first_day in sorted({night.replace(day=1) for night in nights}):
        calendar.update(fetch_month(first_day))
    labels = {night: calendar[night] for night in nights}

    print(f"Checked {today} for a {RV_LENGTH_FT} ft {RV_EQUIPMENT}:")
    for game, opponent in games.items():
        print(f"  {weekend_line(game, opponent, labels)}{'  <- BOTH NIGHTS OPEN' if both_open(labels, game) else ''}")
    if args.dry_run:
        return

    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    previous = {dt.date.fromisoformat(day): label for day, label in state.get("nights", {}).items()}
    opened = [game for game in games if both_open(labels, game) and not both_open(previous, game)]
    closed = [game for game in games if not both_open(labels, game) and both_open(previous, game)]

    if not state:
        alert("Campsite monitor started",
              "You'll get an alert only when both the night before and the night of a home game are open:\n"
              + "\n".join(weekend_line(game, opponent, labels) for game, opponent in games.items()),
              priority="default", tags="football")
    elif opened:
        game = opened[0]
        title = (f"Both nights open: {games[game]} ({day_name(stay(game)[0])} + {day_name(game)})"
                 if len(opened) == 1 else f"Both nights open on {len(opened)} game weekends")
        lines = [weekend_line(game, games[game], labels) for game in opened]
        lines += [f"No longer open both nights: {games[game]}" for game in closed]
        lines.append("Counts are per night; when booking, make sure one site is free both nights.")
        alert(title, "\n".join(lines), priority="urgent", tags="tent,rotating_light")
    elif closed:
        alert("Game weekend no longer open",
              "\n".join(weekend_line(game, games[game], labels) for game in closed),
              priority="low", tags="no_entry", email=False)

    new_state = {"nights": {night.isoformat(): labels[night] for night in nights}}
    if new_state != state:
        STATE_FILE.write_text(json.dumps(new_state, indent=2) + "\n")
        print(f"Updated {STATE_FILE.name}")


if __name__ == "__main__":
    main()
