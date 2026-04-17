#!/usr/bin/env python3
"""BC Parks campsite availability checker.

Usage:
  python check.py                           # normal run — check + notify
  python check.py --list-parks              # list all BC Parks IDs and exit
  python check.py --list-sites "Alice Lake" # list sites/maps for a park
  python check.py --debug                   # print raw API responses
"""

import argparse
import json
import os
import smtplib
import time
from datetime import date, datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import requests

try:
    from config import (
        EMAIL_FROM, EMAIL_TO, MONITOR_END, MONITOR_START,
        NTFY_TOPIC, PARKS, RESEND_HOURS, STAY_COMBOS,
    )
except ImportError:
    print("ERROR: config.py not found — copy it and edit your park / email settings.")
    raise SystemExit(1)

BASE_URL = "https://camping.bcparks.ca"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://camping.bcparks.ca/",
}
STATE_FILE = Path(__file__).parent / "seen.json"

# Map title keywords that identify non-standard areas to skip
SKIP_MAP_KEYWORDS = ("walk-in", "walk in", "walkin", "group", "backcountry",
                     "back country", "day use", "day-use")


# ---------------------------------------------------------------------------
# State helpers (de-duplication across runs)
# ---------------------------------------------------------------------------

def load_state() -> dict:
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)


def prune_state(state: dict, days: int = 30) -> dict:
    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
    return {k: v for k, v in state.items() if v >= cutoff}


def already_notified(state: dict, key: str) -> bool:
    if key not in state:
        return False
    last = datetime.fromisoformat(state[key])
    return (datetime.utcnow() - last).total_seconds() < RESEND_HOURS * 3600


def mark_notified(state: dict, key: str) -> None:
    state[key] = datetime.utcnow().isoformat()


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------

def upcoming_stays() -> list[tuple[date, int, str]]:
    """All (checkin_date, nights, label) combos in the monitor range."""
    today = date.today()
    stays: list[tuple[date, int, str]] = []
    current = max(MONITOR_START, today)
    while current <= MONITOR_END:
        for checkin_wd, nights, label in STAY_COMBOS:
            if current.weekday() == checkin_wd:
                stays.append((current, nights, label))
        current += timedelta(days=1)
    return stays


# ---------------------------------------------------------------------------
# BC Parks API helpers
# ---------------------------------------------------------------------------

def api_get(path: str, params: dict):
    resp = requests.get(f"{BASE_URL}{path}", params=params,
                        headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.json()


def get_campsite_maps(resource_location_id: int) -> list[dict]:
    """Return dicts {map_id, title} for regular campsite loops only."""
    maps = api_get("/api/maps", {"resourceLocationId": resource_location_id})
    result = []
    for m in maps:
        title = next(
            (v.get("title", "") for v in m.get("localizedValues", [])), ""
        )
        num_sites = len(m.get("mapResources", []))
        if num_sites == 0:
            continue
        if any(kw in title.lower() for kw in SKIP_MAP_KEYWORDS):
            continue
        result.append({"map_id": m["mapId"], "title": title})
    return result


def fetch_availability(map_id: int, checkin: date, nights: int) -> dict:
    """Check which sites in a map are available for (checkin, nights)."""
    checkout = checkin + timedelta(days=nights)
    return api_get("/api/availability/map", {
        "mapId": map_id,
        "bookingCategoryId": 0,
        "equipmentId": -32768,
        "subEquipmentId": -32768,
        "startDate": checkin.isoformat(),
        "endDate": checkout.isoformat(),
        "nights": nights,
        "isReserving": "true",
        "partySize": 1,
    })


def available_sites(data: dict, filter_sites: list,
                    debug: bool = False) -> list[str]:
    """Extract site IDs where availability > 0."""
    if debug:
        snippet = json.dumps(data, indent=2)[:3000]
        print(snippet)
        if len(json.dumps(data)) > 3000:
            print("… (truncated)")

    resource_avail: dict = data.get("resourceAvailabilities", {})
    if not resource_avail and debug:
        print("WARNING: 'resourceAvailabilities' missing — keys:", list(data.keys()))

    found = []
    for site_id, avail_list in resource_avail.items():
        if filter_sites and int(site_id) not in filter_sites:
            continue
        if isinstance(avail_list, list) and avail_list:
            avail = avail_list[0].get("availability", 0)
            if isinstance(avail, int) and avail > 0:
                found.append(site_id)
    return found


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------

def booking_url(resource_location_id: int, map_id: int,
                checkin: date, nights: int) -> str:
    checkout = checkin + timedelta(days=nights)
    return (
        f"https://camping.bcparks.ca/create-booking/results"
        f"?resourceLocationId={resource_location_id}"
        f"&mapId={map_id}"
        f"&searchTabGroupId=0&bookingCategoryId=0&nights={nights}"
        f"&isReserving=true&equipmentId=-32768&subEquipmentId=-32768"
        f"&partySize=1&startDate={checkin.isoformat()}"
        f"&endDate={checkout.isoformat()}"
    )


def send_email(subject: str, body: str) -> None:
    gmail_user = os.environ.get("GMAIL_USER", EMAIL_FROM)
    gmail_pass = os.environ.get("GMAIL_APP_PASSWORD", "")
    if not gmail_pass:
        print("    [email] GMAIL_APP_PASSWORD not set — skipping email.")
        return
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = gmail_user
    msg["To"]      = ", ".join(EMAIL_TO)
    msg.attach(MIMEText(body, "plain"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(gmail_user, gmail_pass)
        smtp.sendmail(gmail_user, EMAIL_TO, msg.as_string())
    print(f"    [email] Sent to {len(EMAIL_TO)} recipient(s).")


def send_ntfy(title: str, message: str, url: str) -> None:
    topic = os.environ.get("NTFY_TOPIC", NTFY_TOPIC)
    if not topic:
        return
    try:
        requests.post(
            f"https://ntfy.sh/{topic}",
            data=message.encode(),
            headers={
                "Title": title,
                "Priority": "high",
                "Tags": "tent,canada",
                "Click": url,
            },
            timeout=10,
        )
        print("    [ntfy] Push notification sent.")
    except Exception as e:
        print(f"    [ntfy] Failed: {e}")


def notify(park_name: str, site_id: str, checkin: date, nights: int,
           label: str, resource_location_id: int, map_id: int) -> None:
    checkout = checkin + timedelta(days=nights)
    url = booking_url(resource_location_id, map_id, checkin, nights)
    subject = f"BC Parks available: {park_name} — {label} ({checkin})"
    body = (
        f"Campsite availability alert!\n\n"
        f"Park:    {park_name}\n"
        f"Site:    #{site_id}\n"
        f"Dates:   {checkin} → {checkout}  ({label}, {nights} nights)\n\n"
        f"Book now:\n{url}\n"
    )
    send_email(subject, body)
    send_ntfy(
        title=f"BC Parks: {park_name}",
        message=f"Site #{site_id} open {label} ({checkin} – {checkout})",
        url=url,
    )


# ---------------------------------------------------------------------------
# CLI helpers
# ---------------------------------------------------------------------------

def cmd_list_parks() -> None:
    locations = api_get("/api/resourceLocation", {})
    print(f"\n{'ID':>15}  Name")
    print("-" * 60)
    for loc in sorted(locations, key=lambda x: next(
        (v.get("shortName", "") for v in x.get("localizedValues", [])), ""
    )):
        short = next(
            (v.get("shortName", v.get("fullName", "?"))
             for v in loc.get("localizedValues", [])), "?"
        )
        print(f"{loc.get('resourceLocationId', '?'):>15}  {short}")


def cmd_list_sites(park_name: str) -> None:
    cfg = PARKS.get(park_name)
    if not cfg:
        print(f"Park '{park_name}' not found in config.py")
        print("Available:", list(PARKS.keys()))
        return
    loc_id = cfg["resource_location_id"]
    print(f"\nMaps for {park_name} (resource_location_id={loc_id}):")
    maps = api_get("/api/maps", {"resourceLocationId": loc_id})
    for m in maps:
        title = next(
            (v.get("title", "(no title)") for v in m.get("localizedValues", [])), "?"
        )
        sites = m.get("mapResources", [])
        site_ids = sorted(abs(s["resourceId"]) for s in sites)
        print(f"\n  map_id={m['mapId']}  \"{title}\"  ({len(sites)} sites)")
        if sites:
            print(f"  site IDs: {site_ids[:10]}{'...' if len(site_ids)>10 else ''}")


# ---------------------------------------------------------------------------
# Main checker loop
# ---------------------------------------------------------------------------

def run(debug: bool = False) -> None:
    state  = load_state()
    state  = prune_state(state)
    stays  = upcoming_stays()

    if not stays:
        print("No upcoming weekend stays in the monitor range.")
        save_state(state)
        return

    print(f"Checking {len(stays)} date/night combos across {len(PARKS)} parks …\n")
    new_alerts = 0

    for park_name, park_cfg in PARKS.items():
        loc_id = park_cfg["resource_location_id"]
        filter_sites = park_cfg.get("sites", [])

        print(f"{'─'*50}")
        print(f"{park_name}")

        try:
            camp_maps = get_campsite_maps(loc_id)
        except Exception as e:
            print(f"  Could not fetch maps: {e}")
            continue

        if not camp_maps:
            print("  No campsite maps found (all filtered out).")
            continue

        print(f"  Maps: {[m['title'] or str(m['map_id']) for m in camp_maps]}")

        for checkin, nights, label in stays:
            for cmap in camp_maps:
                map_id = cmap["map_id"]
                try:
                    data = fetch_availability(map_id, checkin, nights)
                except Exception as e:
                    print(f"  [{label} {checkin}] API error: {e}")
                    continue

                sites = available_sites(data, filter_sites, debug=debug)

                for site_id in sites:
                    key = f"{park_name}|{site_id}|{checkin}|{nights}"
                    if already_notified(state, key):
                        continue
                    print(f"  *** [{label} {checkin}] Site #{site_id} AVAILABLE — notifying!")
                    notify(park_name, site_id, checkin, nights, label, loc_id, map_id)
                    mark_notified(state, key)
                    new_alerts += 1

                time.sleep(0.2)  # be polite to the API

    save_state(state)
    print(f"\nDone — {new_alerts} new alert(s) sent.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="BC Parks campsite availability checker"
    )
    parser.add_argument("--list-parks", action="store_true",
                        help="List all BC Parks resource IDs")
    parser.add_argument("--list-sites", metavar="PARK_NAME",
                        help='List maps/sites for a park, e.g. "Alice Lake"')
    parser.add_argument("--debug", action="store_true",
                        help="Print raw API responses")
    args = parser.parse_args()

    if args.list_parks:
        cmd_list_parks()
    elif args.list_sites:
        cmd_list_sites(args.list_sites)
    else:
        run(debug=args.debug)


if __name__ == "__main__":
    main()
