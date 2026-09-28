#!/usr/bin/env python3
"""Watch the NC State Fair Campground for open RV sites on NC State home-football
weekends, and alert (ntfy phone push and/or Gmail) when a sold-out night opens up.

Availability comes from the Firefly Reservations availability calendar, which
reports how many sites are free for each night. Standard library only.

Usage:
  python camp_watch.py                # check, alert on changes, update state.json
  python camp_watch.py --dry-run      # print availability only; no alerts, no state
  python camp_watch.py --test-notify  # send a test alert on every configured channel

Alert channels are configured with environment variables:
  NTFY_TOPIC            ntfy.sh topic your phone is subscribed to
  GMAIL_ADDRESS         Gmail account that sends the email
  GMAIL_APP_PASSWORD    Gmail App Password for that account
  ALERT_EMAIL_TO        optional; comma-separated recipients (default: GMAIL_ADDRESS)
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
from pathlib import Path

# --- Settings -----------------------------------------------------------------

# NC State home games at Carter-Finley Stadium, 2026 season.
# The monitor watches the night before and the night of each game.
HOME_GAMES = {
    "2026-09-11": "Richmond",
    "2026-09-26": "Appalachian State",
    "2026-10-03": "Louisville",
    "2026-10-10": "Wake Forest",
    "2026-10-31": "California",
    "2026-11-07": "Duke",
    "2026-11-14": "Syracuse",
}

# Rig used for the search. Site length limits change which sites count as open.
RV_LENGTH_FT = 25
RV_EQUIPMENT = "TT"  # FW fifth wheel, MHA/MHB/MHC motorhome, PU popup, TT travel trailer, TC truck camper

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


def watched_nights(today):
    """Return {night: (game_date, opponent, role)} for upcoming nights before and of each home game."""
    nights = {}
    for game_day, opponent in sorted(HOME_GAMES.items()):
        game = dt.date.fromisoformat(game_day)
        for night, role in ((game - dt.timedelta(days=1), "night before"), (game, "game night")):
            if night >= today:
                nights[night] = (game, opponent, role)
    return nights


def day_name(day):
    return f"{day:%a %b} {day.day}"


def describe(label):
    if not is_open(label):
        return "FULL"
    return f"{label} site{'' if label == '1' else 's'} open"


def weekend_lines(nights, labels, games=None):
    """One line per game weekend, e.g. "Duke (Sat Nov 7): Fri Nov 6 2 sites open | Sat Nov 7 FULL"."""
    weekends = {}
    for night, (game, opponent, _) in sorted(nights.items()):
        if games is None or game in games:
            weekends.setdefault((game, opponent), []).append(night)
    return [
        f"{opponent} ({day_name(game)}): "
        + " | ".join(f"{day_name(night)} {describe(labels[night])}" for night in weekend)
        for (game, opponent), weekend in weekends.items()
    ]


# --- Alerts -------------------------------------------------------------------

def send_push(topic, title, body, priority, tags):
    request = urllib.request.Request(
        f"https://ntfy.sh/{urllib.parse.quote(topic)}",
        data=body.encode("utf-8"),
        headers={"Title": title, "Priority": priority, "Tags": tags, "Click": BOOKING_URL},
    )
    urllib.request.urlopen(request, timeout=30).close()


def send_email(sender, title, body):
    password = os.environ.get("GMAIL_APP_PASSWORD", "").replace(" ", "")
    recipients = [a.strip() for a in (os.environ.get("ALERT_EMAIL_TO") or sender).split(",") if a.strip()]
    message = EmailMessage()
    message["Subject"] = title
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    message.set_content(f"{body}\n\nBook: {BOOKING_URL}\n")
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
        smtp.login(sender, password)
        smtp.send_message(message)


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
    args = parser.parse_args()

    if args.test_notify:
        errors = alert("Campsite monitor test",
                       "Test alert from the NC State Fair Campground monitor. If you got this, alerts work.",
                       priority="default", tags="white_check_mark")
        sys.exit(1 if errors else 0)

    today = today_eastern()
    nights = watched_nights(today)
    if not nights:
        print("No upcoming game nights to watch. Disable the workflow or add next season's games.")
        return

    calendar = {}
    for first_day in sorted({night.replace(day=1) for night in nights}):
        calendar.update(fetch_month(first_day))
    labels = {night: calendar[night] for night in nights}

    print(f"Checked {today} for a {RV_LENGTH_FT} ft {RV_EQUIPMENT}:")
    for night, (_, opponent, role) in sorted(nights.items()):
        print(f"  {day_name(night):<11} {opponent + ', ' + role:<32} {describe(labels[night])}")
    if args.dry_run:
        return

    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    previous = {dt.date.fromisoformat(day): label for day, label in state.get("nights", {}).items()}
    opened = [n for n in sorted(nights) if is_open(labels[n]) and not is_open(previous.get(n, "0"))]
    closed = [n for n in sorted(nights) if not is_open(labels[n]) and n in previous and is_open(previous[n])]

    if not state:
        alert("Campsite monitor started",
              "Watching the night before and night of each NC State home game:\n"
              + "\n".join(weekend_lines(nights, labels))
              + "\nYou'll get an alert when a full night opens up.",
              priority="default", tags="football")
    elif opened:
        title = (f"Campsite open: {day_name(opened[0])} ({nights[opened[0]][1]})" if len(opened) == 1
                 else f"Campsites open on {len(opened)} game nights")
        lines = [f"OPEN: {day_name(n)} ({nights[n][1]} {nights[n][2]}) - {describe(labels[n])}" for n in opened]
        lines += [f"Full again: {day_name(n)} ({nights[n][1]})" for n in closed]
        lines += weekend_lines(nights, labels, games={nights[n][0] for n in opened})
        lines.append("Counts are per night; for a 2-night stay, confirm one site is free both nights.")
        alert(title, "\n".join(lines), priority="urgent", tags="tent,rotating_light")
    elif closed:
        alert("Campsite full again",
              "\n".join(f"{day_name(n)} ({nights[n][1]} {nights[n][2]}) is full again." for n in closed),
              priority="low", tags="no_entry", email=False)

    new_state = {"nights": {night.isoformat(): labels[night] for night in sorted(nights)}}
    if new_state != state:
        STATE_FILE.write_text(json.dumps(new_state, indent=2) + "\n")
        print(f"Updated {STATE_FILE.name}")


if __name__ == "__main__":
    main()
