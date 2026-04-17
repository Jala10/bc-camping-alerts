#!/usr/bin/env python3
"""BC Parks campsite availability checker.

Usage:
  python check.py                           # normal run — check + notify
  python check.py --list-parks              # list all BC Parks IDs and exit
  python check.py --list-sites "Alice Lake" # list sites/maps for a park
  python check.py --debug                   # verbose API output
"""

import argparse
import json
import os
import smtplib
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import requests

try:
    from config import (
        EMAIL_FROM, EMAIL_TO, MONITOR_END, MONITOR_START,
        NTFY_TOPIC, PARKS, STAY_COMBOS,
    )
except ImportError:
    print("ERROR: config.py not found — copy it and edit your settings.")
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

# Map title keywords → skip (user requested no walk-in / group / backcountry)
SKIP_MAP_KEYWORDS = ("walk-in", "walk in", "walkin", "group", "backcountry",
                     "back country", "day use", "day-use")

# GoingToCamp availability flag that means "has bookable sites for this equipment"
AVAILABLE_FLAG = 7


# ---------------------------------------------------------------------------
# State  (tracks which park+date combinations were available on the prior run)
# ---------------------------------------------------------------------------
# seen.json: {"available": {"park|date|nights": true, ...}}
#
# We alert only when a key appears in the current run but was absent from the
# previous run — i.e. the park+date just became bookable (opening day or
# a cancellation after being fully sold out).

def load_state() -> dict:
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            data = json.load(f)
        if "available" not in data:          # migrate old format
            return {"available": {}}
        return data
    return {"available": {}}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------

def upcoming_stays() -> list[tuple[date, int, str]]:
    today = date.today()
    # BC Parks opens reservations exactly 90 days in advance.
    # Checking beyond that window wastes API calls (always returns [0]).
    booking_horizon = today + timedelta(days=90)
    stays: list[tuple[date, int, str]] = []
    current = max(MONITOR_START, today)
    while current <= min(MONITOR_END, booking_horizon):
        for checkin_wd, nights, label in STAY_COMBOS:
            if current.weekday() == checkin_wd:
                stays.append((current, nights, label))
        current += timedelta(days=1)
    return stays


# ---------------------------------------------------------------------------
# BC Parks API
# ---------------------------------------------------------------------------

def api_get(path: str, params: dict):
    resp = requests.get(f"{BASE_URL}{path}", params=params,
                        headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.json()


def get_park_maps(resource_location_id: int) -> dict:
    """Return {root_map_id, campsite_map_ids: set[str]} for a park.

    root_map_id   — the overview/0-site map; queried to get mapLinkAvailabilities
    campsite_map_ids — sub-map IDs for regular campsites (walk-in/group excluded)
    """
    maps = api_get("/api/maps", {"resourceLocationId": resource_location_id})
    root_map_id = None
    campsite_map_ids: set[str] = set()

    for m in maps:
        title = next(
            (v.get("title", "") for v in m.get("localizedValues", [])), ""
        )
        num_sites = len(m.get("mapResources", []))

        if num_sites == 0:
            root_map_id = m["mapId"]   # overview map has no direct sites
            continue

        if any(kw in title.lower() for kw in SKIP_MAP_KEYWORDS):
            continue

        campsite_map_ids.add(str(m["mapId"]))

    return {"root_map_id": root_map_id, "campsite_map_ids": campsite_map_ids}


def has_availability(root_map_id: int, campsite_map_ids: set,
                     checkin: date, nights: int,
                     debug: bool = False) -> bool:
    """True when any campsite sub-map has available sites (flag == 7).

    How the BC Parks API signals availability via the root/overview map:
      mapLinkAvailabilities[sub_map_id] == [7]  →  bookable sites exist
      mapLinkAvailabilities[sub_map_id] == [1]  →  fully booked
      mapLinkAvailabilities[sub_map_id] == [0]  →  booking window not open yet
    """
    checkout = checkin + timedelta(days=nights)
    data = api_get("/api/availability/map", {
        "mapId": root_map_id,
        "bookingCategoryId": 0,
        "equipmentId": -32768,
        "subEquipmentId": -32768,
        "startDate": checkin.isoformat(),
        "endDate": checkout.isoformat(),
        "nights": nights,
        "isReserving": "true",
        "partySize": 1,
    })
    mla: dict = data.get("mapLinkAvailabilities", {})

    if debug:
        print(f"      mapLinkAvailabilities: {mla}")

    for map_id_str, flags in mla.items():
        if map_id_str in campsite_map_ids and AVAILABLE_FLAG in flags:
            return True
    return False


# ---------------------------------------------------------------------------
# Notifications  (one batched email per run)
# ---------------------------------------------------------------------------

def booking_url(resource_location_id: int, root_map_id: int,
                checkin: date, nights: int) -> str:
    checkout = checkin + timedelta(days=nights)
    return (
        "https://camping.bcparks.ca/create-booking/results"
        f"?resourceLocationId={resource_location_id}"
        f"&mapId={root_map_id}"
        f"&searchTabGroupId=0&bookingCategoryId=0&nights={nights}"
        f"&isReserving=true&equipmentId=-32768&subEquipmentId=-32768"
        f"&partySize=1&startDate={checkin.isoformat()}"
        f"&endDate={checkout.isoformat()}"
    )


def send_email(subject: str, body: str) -> None:
    gmail_user = os.environ.get("GMAIL_USER", EMAIL_FROM)
    gmail_pass = os.environ.get("GMAIL_APP_PASSWORD", "")
    if not gmail_pass:
        print("  [email] GMAIL_APP_PASSWORD not set — skipping.")
        return
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = gmail_user
    msg["To"]      = ", ".join(EMAIL_TO)
    msg.attach(MIMEText(body, "plain"))
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(gmail_user, gmail_pass)
            smtp.sendmail(gmail_user, EMAIL_TO, msg.as_string())
        print(f"  [email] Sent to {len(EMAIL_TO)} recipient(s).")
    except Exception as e:
        print(f"  [email] Failed: {e}")


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
        print("  [ntfy] Push notification sent.")
    except Exception as e:
        print(f"  [ntfy] Failed: {e}")


def send_summary(new_findings: list) -> None:
    """One email + ntfy per run listing all new openings, grouped by park."""
    total = len(new_findings)
    subject = f"BC Parks: {total} new opening(s) found!"
    checked_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    lines = [f"BC Parks Availability Alert — {total} new opening(s)!\n",
             f"Checked: {checked_at}\n"]
    prev_park = None
    for f in sorted(new_findings, key=lambda x: (x["park"], x["checkin"], x["nights"])):
        if f["park"] != prev_park:
            lines.append(f"\n{'─'*40}")
            lines.append(f"{f['park']}")
            prev_park = f["park"]
        checkout = f["checkin"] + timedelta(days=f["nights"])
        lines.append(
            f"  {f['label']:14}  {f['checkin']} – {checkout}  ({f['nights']} nights)"
        )
        lines.append(f"  Book: {f['url']}")

    body = "\n".join(lines)
    send_email(subject, body)

    first = new_findings[0]
    send_ntfy(
        title=subject,
        message="\n".join(
            f"{f['park']} · {f['label']} {f['checkin']}"
            for f in sorted(new_findings, key=lambda x: (x["park"], x["checkin"]))[:8]
        ),
        url=first["url"],
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
        skip = any(kw in title.lower() for kw in SKIP_MAP_KEYWORDS)
        flag = "  (skipped)" if skip or len(sites) == 0 else ""
        print(f"\n  map_id={m['mapId']}  \"{title}\"  ({len(sites)} sites){flag}")
        if sites and not skip:
            print(f"  site IDs: {site_ids[:10]}{'...' if len(site_ids) > 10 else ''}")


# ---------------------------------------------------------------------------
# Main checker loop
# ---------------------------------------------------------------------------

def run(debug: bool = False) -> None:
    state = load_state()
    prev_available: dict = state.get("available", {})
    curr_available: dict = {}

    stays = upcoming_stays()
    if not stays:
        print("No upcoming weekend stays in the monitor range.")
        save_state({"available": {}})
        return

    print(f"Checking {len(stays)} date/night combos across {len(PARKS)} parks …\n")

    new_findings: list = []

    for park_name, park_cfg in PARKS.items():
        loc_id = park_cfg["resource_location_id"]

        print(f"{'─'*50}")
        print(f"{park_name}")

        try:
            park_maps = get_park_maps(loc_id)
        except Exception as e:
            print(f"  Could not fetch maps: {e}")
            continue

        root_id = park_maps["root_map_id"]
        campsite_ids = park_maps["campsite_map_ids"]

        if root_id is None:
            print("  No root/overview map found — skipping.")
            continue
        if not campsite_ids:
            print("  No campsite maps found.")
            continue

        print(f"  Root map: {root_id}  |  Campsite sub-maps: {campsite_ids}")

        for checkin, nights, label in stays:
            key = f"{park_name}|{checkin}|{nights}"
            try:
                avail = has_availability(root_id, campsite_ids, checkin, nights,
                                         debug=debug)
            except Exception as e:
                print(f"  [{label} {checkin}] API error: {e}")
                continue

            if avail:
                curr_available[key] = True
                if key not in prev_available:
                    url = booking_url(loc_id, root_id, checkin, nights)
                    new_findings.append({
                        "park": park_name,
                        "checkin": checkin,
                        "nights": nights,
                        "label": label,
                        "url": url,
                    })
                    print(f"  + NEW [{label} {checkin}] — available!")
                elif debug:
                    print(f"  [{label} {checkin}] available (already known)")

            time.sleep(0.2)

    if new_findings:
        print(f"\n{len(new_findings)} new opening(s) — sending summary …")
        send_summary(new_findings)
    else:
        print("\nNo new openings since last run.")

    state["available"] = curr_available
    save_state(state)
    print(f"Done — tracking {len(curr_available)} available slot(s).")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="BC Parks campsite availability checker"
    )
    parser.add_argument("--list-parks", action="store_true")
    parser.add_argument("--list-sites", metavar="PARK_NAME")
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
