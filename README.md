# NC State Fair Campground monitor

Checks the [NC State Fair Campground](https://app.fireflyreservations.com/reserve/property/NCStateFairCampground)
every 5 minutes for open RV sites on NC State home football weekends. It alerts your phone (ntfy) and/or
email (Gmail) only when **both** the night before and the night of a game have open sites. If only one of
the two nights opens, you won't get an alert.

- `camp_watch.py` does the check and sends alerts. It has no dependencies beyond Python 3.
- `.github/workflows/watch.yml` runs it on GitHub Actions every 5 minutes.
- `state.json` holds the last availability seen, so you're only alerted when something changes.
  The workflow commits it back to the repo whenever it changes.

## Alerts you'll get

| When | Push | Email |
| --- | --- | --- |
| First run: status of every game weekend | yes | yes |
| Both nights of a game weekend are open (urgent) | yes | yes |
| A weekend you were alerted about is no longer open both nights | yes (low priority) | no |

Each alert links straight to the booking page.

## Setup

1. **Phone push.** Install the ntfy app ([Android](https://play.google.com/store/apps/details?id=io.heckel.ntfy),
   [iPhone](https://apps.apple.com/us/app/ntfy/id1625396347)), tap **+**, and subscribe to your topic name.
   Anyone who knows the topic name can read the alerts, so keep it private.
2. **Gmail.** Turn on 2-Step Verification, then create an App Password at
   <https://myaccount.google.com/apppasswords>.
3. **Secrets.** Add these under the repo's **Settings → Secrets and variables → Actions**, or with `gh`:

   ```sh
   gh secret set NTFY_TOPIC            # your ntfy topic name
   gh secret set GMAIL_ADDRESS         # the Gmail account that sends the alerts
   gh secret set GMAIL_APP_PASSWORD    # the App Password from step 2
   gh secret set ALERT_EMAIL_TO        # optional: mailing list, comma-separated
   ```

   Addresses in `ALERT_EMAIL_TO` get the alert emails as BCC, so they can't see each other, and
   `GMAIL_ADDRESS` always gets a copy. Recipients don't need to install or sign up for anything.
   See [Mailing list](#mailing-list) for the easy way to set it.
4. **Test.** `gh workflow run watch.yml -f action=test-notify`, or on GitHub open
   **Actions → Watch campsites → Run workflow** and choose `test-notify`.

To send everyone the alert rules (games, rig, the both-nights rule) plus steps for getting ntfy phone
alerts, run `gh workflow run watch.yml -f action=send-settings`. That message includes the ntfy topic name,
so it goes only to the people on the alerts and is never printed in the public run log.

## Mailing list

The list lives in `mailing_list.txt` in this folder, one email address per line. That file is in
`.gitignore`, so it stays on your computer and never goes to the public repo.

To add or remove someone, edit `mailing_list.txt`, save it, and run this in the VS Code terminal
(PowerShell) from this folder:

```powershell
Get-Content mailing_list.txt | gh secret set ALERT_EMAIL_TO -R host-user-mammoth/ncstate-campsite-watch
```

To check it worked, run `gh workflow run watch.yml -f action=send-settings`; everyone on the list should
get a copy.

## Changing what it watches

Edit the settings at the top of `camp_watch.py`:

- `HOME_GAMES`: game dates and opponents. Past dates are skipped automatically.
- `RV_LENGTH_FT` / `RV_EQUIPMENT`: your rig. Some sites have length limits, so this changes the counts.

Check from your own computer without sending alerts: `python camp_watch.py --dry-run`

## Things to know

- **October is blocked.** The campground closes reservations for all of October because of the State Fair,
  so the Oct 3, Oct 10 and Oct 31 games show as full. They're still watched in case that changes.
- **Counts are per night.** Fri and Sat can each show an open site without the same site being free both
  nights. Check before you book a 2-night stay.
- **On game day,** that weekend is no longer watched, because the night before has already passed.
- **GitHub's schedule is approximate.** Runs are nominally every 5 minutes but are often delayed 5–15 minutes.
- **Failures.** If the site is unreachable or its page layout changes, the run fails and GitHub emails you.
- **After the season,** stop it with `gh workflow disable watch.yml`.
