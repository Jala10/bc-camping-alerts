# BC Parks Camping Alerts

Automated cancellation alerts for BC Parks campsites. Scans every 2 minutes via cron-job.org + GitHub Actions and sends an email, WhatsApp message, and push notification the moment a site opens up at one of the watched parks — including the specific site number and a direct booking link.

## What it does

- Monitors **Alice Lake, Rolley Lake, Cultus Lake, Golden Ears (Alouette South/North only), Porteau Cove, and Porpoise Bay** for Friday and Saturday 1-night availability separately
- Watches **July – September 2026**
- Alerts include the **specific site number** and a **direct link** that lands on that exact site in the BC Parks booking flow
- Sends one batched **email**, a **WhatsApp message** (via CallMeBot), and an **ntfy.sh push notification** when new availability is detected
- Tracks state in `seen.json` so you only get alerted on *new* openings, not the same one repeatedly
- Skips walk-in, group, backcountry, day-use, and no-flush-toilet sections automatically

## How it works

BC Parks opens reservations ~91 days in advance. Popular dates sell out within minutes. This script polls the BC Parks API every 2 minutes looking for cancellations.

```
cron-job.org (every 2 min) ──┐
                              ├─→ triggers workflow_dispatch on GitHub Actions
GitHub cron (every 5 min) ───┘       (5-min schedule is a fallback)

  → check.py polls BC Parks API
  → compares against seen.json (last known state)
  → if new availability: sends email + WhatsApp + push notification
  → commits updated seen.json back to repo
```

## Parks monitored

| Park | Drive from Coquitlam | Notes |
|---|---|---|
| Porteau Cove | ~0.75h | Oceanfront on Howe Sound |
| Golden Ears | ~0.75h | Alouette South + North only (showers confirmed) |
| Rolley Lake | ~0.75h | Small lakeside park near Mission |
| Alice Lake | ~1.0h | 4 lakes, Squamish area |
| Cultus Lake | ~1.0h | Large warm lake near Chilliwack |
| Porpoise Bay | ~2.2h | Sechelt, Sunshine Coast — BC Ferries required (Horseshoe Bay → Langdale) |

## Setup (for your own fork)

### 1. Add GitHub Secrets

Go to **Settings → Secrets and variables → Actions** and add:

| Secret | Value |
|--------|-------|
| `GMAIL_USER` | Your Gmail address |
| `GMAIL_APP_PASSWORD` | Gmail [App Password](https://myaccount.google.com/apppasswords) |
| `NTFY_TOPIC` | Your ntfy.sh topic name |
| `WHATSAPP_RECIPIENTS` | Comma-separated `+1XXXXXXXXXX:APIKEY` pairs (see below) |

### 2. Set up WhatsApp notifications (CallMeBot)

Each recipient does this once:
1. Save `+34 644 60 49 18` as a contact in WhatsApp
2. Send `I allow callmebot to send me messages` to that number
3. You'll receive your API key via WhatsApp

Then add all recipients to the `WHATSAPP_RECIPIENTS` secret:
```
+16041234567:APIKEY1,+16049876543:APIKEY2
```

### 3. Set up 2-minute scanning (cron-job.org)

GitHub Actions only supports 5-minute minimum schedules. To get 2-minute scans:
1. Create a free account at [cron-job.org](https://cron-job.org)
2. Create a new cron job with:
   - **URL:** `https://api.github.com/repos/YOUR_USERNAME/bc-camping-alerts/actions/workflows/check_camping.yml/dispatches`
   - **Schedule:** every 2 minutes
   - **Method:** POST
   - **Headers:** `Authorization: Bearer YOUR_GITHUB_PAT`, `Accept: application/vnd.github+json`, `Content-Type: application/json`
   - **Body:** `{"ref": "main"}`
3. Generate a GitHub [fine-grained PAT](https://github.com/settings/tokens) with Actions read+write on this repo

The workflow's built-in `*/5` cron schedule acts as a fallback if cron-job.org has downtime.

### 4. Edit `config.py`

```python
PARKS = { ... }           # parks to watch
MONITOR_START / END       # date range
STAY_COMBOS               # check-in days to monitor (Fri + Sat by default)
EMAIL_TO = [...]          # alert recipients
NTFY_TOPIC = "..."        # ntfy.sh topic
```

### 5. Push and let it run

GitHub Actions picks up the workflow automatically. Trigger a manual run from the **Actions** tab to verify.

## CLI usage

```bash
# Availability checker
python check.py                            # normal run
python check.py --dry-run                  # check availability, skip notifications
python check.py --list-parks               # list all BC Parks IDs
python check.py --list-sites "Alice Lake"  # list campsite maps for a park
python check.py --debug                    # verbose API output

# Wednesday pre-booking scout (run before Thursday 7am booking rush)
python scout.py                            # scan all parks for upcoming weekend
python scout.py --checkin 2026-07-23 --nights 2
python scout.py --explore-parks squamish   # find park IDs by keyword
python scout.py --explore-sites "Alice Lake"
python scout.py --discover                 # find all nearby parks with showers
```

## Requirements

```
requests>=2.31
```

---

*Uses the BC Parks GoingToCamp API. Personal use only.*
