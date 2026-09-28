# Goal

-Build an agent that sends live update when a space opens up for reservation at NC Fairgrounds campgrounds on the day before and day of an NC State Wolfpack home football game.
-Text message or email update

## Where to find reservations

-<https://app.fireflyreservations.com/reserve/property/NCStateFairCampground>

## How it works

- `camp_watch.py` (Python stdlib only) posts to Firefly's `/Reserve/GetPropertyAvailabilityCalendar`, which returns an HTML month calendar with sites open per night and no reCAPTCHA. The unit search endpoints (`SearchForAvailableUnits*`) do require reCAPTCHA, so avoid them.
- Watched nights are the night before and the night of each game in `HOME_GAMES`. Alerts fire when a night goes from full to open. State is kept in `state.json`.
- Runs on GitHub Actions every 5 min (`.github/workflows/watch.yml`). Alerts go to ntfy push (`NTFY_TOPIC`) and Gmail (`GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD`, optional `ALERT_EMAIL_TO`), all set as repo secrets.
- The campground only has RV sites, and RV length/type affects the counts (currently 25 ft travel trailer). All of October is blocked for the State Fair.
- Test locally with `python camp_watch.py --dry-run`. No Python is installed on this PC; use a portable one.
