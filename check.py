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

def upcoming_stays():
    today = date.today()
    # BC Parks opens reservations ~91 days in advance.
    # Use 95-day horizon to catch boundary dates early, but only alert once
    # the entire stay (including checkout night) is within the open window.
    # This prevents pre-open bleed: our API query endDate=checkout accidentally
    # picks up the next day's pre-release sites (not yet bookable but showing [7]).
    booking_horizon = today + timedelta(days=95)
    open_limit = today + timedelta(days=91)  # last date BC Parks has opened
    stays = []
    current = max(MONITOR_START, today)
    while current <= min(MONITOR_END, booking_horizon):
        for checkin_wd, nights, label in STAY_COMBOS:
            if current.weekday() == checkin_wd:
                checkout = current + timedelta(days=nights)
                if checkout <= open_limit:
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


def get_resource_info(resource_location_id: int) -> dict:
    """Return {resource_id_str: {"name": str, "is_double": bool, "partner_ids": [str]}}

    Uses /api/resourcelocation/resources which returns real site names and double-site
    pairing (linkedResourceType == 2 means the two sites form a bookable double).
    """
    try:
        data = api_get("/api/resourcelocation/resources",
                       {"resourceLocationId": resource_location_id})
    except Exception:
        return {}
    info = {}
    for rid_str, res in data.items():
        lv = res.get("localizedValues", [])
        name = (lv[0].get("name", "").strip() if lv else "") or str(abs(int(rid_str)))
        partners = [str(lr["linkedResourceId"]) for lr in res.get("linkedResources", [])
                    if lr.get("linkedResourceType") == 2]
        desc = (lv[0].get("description", "").strip() if lv else "")
        info[rid_str] = {
            "name": name,
            "description": desc,
            "order": res.get("order", 0),
            "is_double": bool(partners),
            "partner_ids": partners,
        }
    return info


def get_park_maps(resource_location_id: int) -> dict:
    """Return {root_map_id, campsite_maps, site_names, resource_info} for a park.

    root_map_id   — the overview/0-site map; queried to get mapLinkAvailabilities
    campsite_maps — {map_id_str: section_name} for regular campsites (walk-in/group excluded)
    site_names    — {map_id_str: {resource_id_str: display_name}}
    resource_info — {resource_id_str: {"name", "is_double", "partner_ids"}}
    """
    maps = api_get("/api/maps", {"resourceLocationId": resource_location_id})
    resource_info = get_resource_info(resource_location_id)
    root_map_id = None
    campsite_maps = {}  # map_id_str -> section name
    site_names = {}     # map_id_str -> {resource_id_str -> display_name}

    for m in maps:
        title = next(
            (v.get("title", "") for v in m.get("localizedValues", [])), ""
        )
        resources = m.get("mapResources", [])

        if len(resources) == 0:
            root_map_id = m["mapId"]   # overview map has no direct sites
            continue

        if any(kw in title.lower() for kw in SKIP_MAP_KEYWORDS):
            continue

        map_id_str = str(m["mapId"])
        campsite_maps[map_id_str] = title.strip()

        names = {}
        for r in resources:
            rid = str(r.get("resourceId", ""))
            if rid:
                names[rid] = resource_info.get(rid, {}).get("name", str(abs(int(rid))))
        site_names[map_id_str] = names

    return {"root_map_id": root_map_id, "campsite_maps": campsite_maps,
            "site_names": site_names, "resource_info": resource_info}


def get_available_sections(root_map_id: int, campsite_maps: dict, site_names: dict,
                           checkin: date, nights: int,
                           debug: bool = False) -> dict:
    """Return {section_name: {"map_id": int, "sites": [(resource_id_str, display_name)]}} for bookable sections.

    Two-step approach:
      1. Root map mapLinkAvailabilities — which sub-maps have flag [7]?
      2. Sub-map resourceAvailabilities — collect individual sites with availability==7.
    Empty dict means nothing available.
    """
    checkout = checkin + timedelta(days=nights)
    params = {
        "mapId": root_map_id,
        "bookingCategoryId": 0,
        "equipmentId": -32768,
        "subEquipmentId": -32768,
        "startDate": checkin.isoformat(),
        "endDate": checkout.isoformat(),
        "nights": nights,
        "isReserving": "true",
        "partySize": 1,
    }
    mla = api_get("/api/availability/map", params).get("mapLinkAvailabilities", {})

    if debug:
        print(f"      mapLinkAvailabilities: {mla}")

    sections = {}
    for map_id_str, flags in mla.items():
        if map_id_str not in campsite_maps or AVAILABLE_FLAG not in flags:
            continue
        section_name = campsite_maps[map_id_str]
        names = site_names.get(map_id_str, {})
        try:
            sub_params = {**params, "mapId": int(map_id_str)}
            ra = api_get("/api/availability/map", sub_params).get("resourceAvailabilities", {})
            avail_sites = [
                (sid, names.get(sid, str(abs(int(sid)))))
                for sid, entries in ra.items()
                if any(item.get("availability") == AVAILABLE_FLAG for item in entries)
            ]
            if avail_sites:
                sections[section_name] = {"map_id": int(map_id_str), "sites": avail_sites}
            elif debug:
                print(f"      {section_name}: root [7] but no sites with flag=7 in sub-map")
        except Exception as e:
            sections[section_name] = {"map_id": int(map_id_str), "sites": [], "error": str(e)}
            if debug:
                print(f"      {section_name}: sub-map query failed: {e}")
        time.sleep(0.2)

    return sections


# ---------------------------------------------------------------------------
# Notifications  (one batched email per run)
# ---------------------------------------------------------------------------

def booking_url(resource_location_id: int, map_id: int,
                checkin: date, nights: int, resource_id: str = None) -> str:
    checkout = checkin + timedelta(days=nights)
    url = (
        "https://camping.bcparks.ca/create-booking/results"
        f"?resourceLocationId={resource_location_id}"
        f"&mapId={map_id}"
        f"&searchTabGroupId=0&bookingCategoryId=0&nights={nights}"
        f"&isReserving=true&equipmentId=-32768&subEquipmentId=-32768"
        f"&partySize=1&startDate={checkin.isoformat()}"
        f"&endDate={checkout.isoformat()}"
    )
    if resource_id:
        url += f"&resourceId={resource_id}"
    return url


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


def send_whatsapp(message: str) -> None:
    recipients_env = os.environ.get("WHATSAPP_RECIPIENTS", "")
    if not recipients_env:
        return
    for entry in recipients_env.split(","):
        entry = entry.strip()
        if ":" not in entry:
            continue
        phone, apikey = entry.split(":", 1)
        try:
            requests.get(
                "https://api.callmebot.com/whatsapp.php",
                params={"phone": phone.strip(), "text": message, "apikey": apikey.strip()},
                timeout=10,
            )
            print(f"  [whatsapp] Sent to {phone.strip()}.")
        except Exception as e:
            print(f"  [whatsapp] Failed for {phone.strip()}: {e}")
        time.sleep(1)  # avoid CallMeBot rate limiting between recipients


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

    MAX_SITES_PER_SECTION = 5

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
        if f.get("sections"):
            loc_id = f["loc_id"]
            for sec_name, sec_data in f["sections"].items():
                sites = sec_data.get("sites", [])
                sec_map_id = sec_data.get("map_id")
                n = len(sites)
                if n == 0:
                    lines.append(f"    {sec_name}  (availability detected but site details unavailable)")
                    lines.append(f"    Book:  {f['url']}")
                    continue
                lines.append(f"    {sec_name}  ·  {n} site{'s' if n != 1 else ''} available")
                res_info = f.get("resource_info", {})
                for sid, sname in sites[:MAX_SITES_PER_SECTION]:
                    site_url = booking_url(loc_id, sec_map_id, f["checkin"], f["nights"], resource_id=sid)
                    desc = res_info.get(sid, {}).get("description", "")
                    note = f"  [{desc}]" if desc and "double with site" not in desc.lower() else ""
                    lines.append(f"      {sname:<10}  →  {site_url}{note}")
                if n > MAX_SITES_PER_SECTION:
                    section_url = booking_url(loc_id, sec_map_id, f["checkin"], f["nights"])
                    lines.append(f"      … and {n - MAX_SITES_PER_SECTION} more  →  {section_url}")
        else:
            lines.append(f"  Book:   {f['url']}")

    body = "\n".join(lines)
    send_email(subject, body)

    first = new_findings[0]

    def _ntfy_sec(sections):
        parts = []
        for s, d in sections.items():
            n = len(d.get("sites", []))
            parts.append(f"{s}({n})")
        return ", ".join(parts)

    whatsapp_lines = [subject]
    for f in sorted(new_findings, key=lambda x: (x["park"], x["checkin"], x["nights"])):
        checkout = f["checkin"] + timedelta(days=f["nights"])
        line = f"{f['park']} · {f['label']} {f['checkin']}–{checkout}"
        if f.get("sections"):
            line += f" · {_ntfy_sec(f['sections'])}"
        whatsapp_lines.append(line)
    whatsapp_lines.append(f"\nBook at: {first['url']}")
    send_whatsapp("\n".join(whatsapp_lines))

    send_ntfy(
        title=subject,
        message="\n".join(
            f"{f['park']} · {f['label']} {f['checkin']}"
            + (f" · {_ntfy_sec(f['sections'])}" if f.get("sections") else "")
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

def run(debug: bool = False, dry_run: bool = False) -> None:
    state = load_state()
    prev_available = state.get("available", {})
    curr_available = {}

    stays = upcoming_stays()
    if not stays:
        print("No upcoming weekend stays in the monitor range.")
        save_state({"available": {}})
        return

    print(f"Checking {len(stays)} date/night combos across {len(PARKS)} parks …")
    for checkin, nights, label in stays:
        print(f"  {label}  {checkin} – {checkin + timedelta(days=nights)}")
    print()

    new_findings = []

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
        campsite_maps = park_maps["campsite_maps"]
        site_names = park_maps["site_names"]
        resource_info = park_maps["resource_info"]

        excluded = {str(i) for i in park_cfg.get("excluded_map_ids", [])}
        if excluded:
            campsite_maps = {k: v for k, v in campsite_maps.items() if k not in excluded}
            print(f"  Excluded map(s): {excluded}")

        if root_id is None:
            print("  No root/overview map found — skipping.")
            continue
        if not campsite_maps:
            print("  No campsite maps found.")
            continue

        print(f"  Root map: {root_id}  |  Sections: {list(campsite_maps.values())}")

        for checkin, nights, label in stays:
            key = f"{park_name}|{checkin}|{nights}"
            try:
                avail_sections = get_available_sections(root_id, campsite_maps, site_names,
                                                        checkin, nights, debug=debug)
            except Exception as e:
                print(f"  [{label} {checkin}] API error: {e}")
                continue

            if avail_sections:
                # Annotate sites that are part of a fully-available double pair
                all_avail_ids = {sid for sec in avail_sections.values()
                                 for sid, _ in sec.get("sites", [])}
                for sec_data in avail_sections.values():
                    annotated = []
                    for sid, sname in sec_data["sites"]:
                        rinfo = resource_info.get(sid, {})
                        if rinfo.get("is_double"):
                            avail_partners = [p for p in rinfo["partner_ids"] if p in all_avail_ids]
                            if avail_partners:
                                pnames = "+".join(resource_info.get(p, {}).get("name", p)
                                                  for p in avail_partners)
                                sname = f"{sname}+{pnames} ★"
                        annotated.append((sid, sname))
                    annotated.sort(key=lambda x: resource_info.get(x[0], {}).get("order", 0))
                    sec_data["sites"] = annotated

                curr_available[key] = True
                if key not in prev_available:
                    url = booking_url(loc_id, root_id, checkin, nights)
                    sections_str = ", ".join(
                        f"{s} ({len(d.get('sites', []))})" for s, d in avail_sections.items()
                    )
                    new_findings.append({
                        "park": park_name,
                        "checkin": checkin,
                        "nights": nights,
                        "label": label,
                        "url": url,
                        "loc_id": loc_id,
                        "sections": avail_sections,
                        "resource_info": resource_info,
                    })
                    print(f"  + NEW [{label} {checkin}] — {sections_str}")
                elif debug:
                    sections_str = ", ".join(
                        f"{s} ({len(d.get('sites', []))})" for s, d in avail_sections.items()
                    )
                    print(f"  [{label} {checkin}] available (already known): {sections_str}")

            time.sleep(0.2)

    if new_findings:
        if dry_run:
            print(f"\n{len(new_findings)} new opening(s) found (dry-run — notifications skipped).")
            for f in sorted(new_findings, key=lambda x: (x["park"], x["checkin"])):
                print(f"  {f['park']} · {f['label']} {f['checkin']} → {f['url']}")
        else:
            print(f"\n{len(new_findings)} new opening(s) — sending summary …")
            send_summary(new_findings)
    else:
        print("\nNo new openings since last run.")

    if not dry_run:
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
    parser.add_argument("--dry-run", action="store_true",
                        help="Check availability but skip all notifications")
    args = parser.parse_args()

    if args.list_parks:
        cmd_list_parks()
    elif args.list_sites:
        cmd_list_sites(args.list_sites)
    else:
        run(debug=args.debug, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
