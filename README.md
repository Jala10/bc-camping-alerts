# BC Parks Camping Alerts

Automated cancellation alerts for BC Parks campsites. Runs every 15 minutes via GitHub Actions and sends an email + push notification the moment a Friday–Saturday site opens up at one of the watched parks.

## What it does

- Monitors **Alice Lake, Rolley Lake, Cultus Lake, Golden Ears, and Porteau Cove** for Fri+Sat (2-night) availability
- Watches **July – September 2026**
- Sends one batched **email** to up to 4 addresses and an **ntfy.sh push notification** to your phone when new availability is detected
- Tracks state in `seen.json` so you only get alerted on *new* openings, not the same one repeatedly
- Skips walk-in, group, backcountry, and day-use sites automatically

## How it works

BC Parks opens reservations ~91 days in advance. Popular dates sell out within minutes of opening. This script polls the BC Parks API every 15 minutes looking for cancellations — sites that were fully booked but just became available again.

```
GitHub Actions (every 15 min)
  → check.py polls BC Parks API
  → compares against seen.json (last known state)
  → if new availability found: sends email + push notification
  → commits updated seen.json back to repo
```

## Setup (for your own fork)

### 1. Add GitHub Secrets

Go to **Settings → Secrets and variables → Actions** and add:

| Secret | Value |
|--------|-------|
| `GMAIL_USER` | Your Gmail address |
| `GMAIL_APP_PASSWORD` | Gmail [App Password](https://myaccount.google.com/apppasswords) (not your regular password) |
| `NTFY_TOPIC` | Your ntfy.sh topic name (optional) |

### 2. Edit `config.py`

```python
PARKS = { ... }           # parks to watch
MONITOR_START / END       # date range
STAY_COMBOS               # check-in day + number of nights
EMAIL_TO = [...]          # alert recipients
NTFY_TOPIC = "..."        # ntfy.sh topic (install app, subscribe to topic)
```

### 3. Push and let it run

GitHub Actions picks up the workflow automatically. Trigger a manual run from the **Actions** tab to test.

## CLI usage

```bash
python check.py                        # normal run
python check.py --dry-run              # check availability, skip notifications
python check.py --list-parks           # list all BC Parks IDs
python check.py --list-sites "Alice Lake"  # list campsite maps for a park
python check.py --debug                # print raw API responses
```

## Requirements

```
requests>=2.31
```

---

*Uses the BC Parks GoingToCamp API. Personal use only.*
