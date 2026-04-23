#!/usr/bin/env python3
"""scout.py — Wednesday pre-booking scout for BC Parks.

Run Wednesday evening to prepare a shortlist before the Thursday 7 am
booking rush.  Finds available sections and scores by water proximity.

Usage:
  python scout.py                              # use defaults below
  python scout.py --checkin 2026-07-23 --nights 2
  python scout.py --explore-parks              # list all park IDs
  python scout.py --explore-parks squamish     # filter by keyword
  python scout.py --explore-sites "Alice Lake" # see site layout + coords
  python scout.py --debug                      # verbose API output
"""

import argparse
import math  # used by _haversine_km
import time
from datetime import date, timedelta

import requests

# Coquitlam, BC — used for distance filtering
COQUITLAM_LAT = 49.2817
COQUITLAM_LON = -122.7932

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
AVAILABLE_FLAG = 0   # 0 = green/fully-available; 7 = purple/partial (not fully bookable)
SKIP_MAP_KEYWORDS = ("walk-in", "walk in", "walkin", "group", "backcountry",
                     "back country", "day use", "day-use")

# mapLegendItems legendItemType that means "Restroom with Showers" in BC Parks GoingToCamp.
# Verified empirically: present in Alouette North, Alice Lake A/B, Porteau Cove A, Rolley Lake,
# Cultus Lake sections; absent from North Beach (Golden Ears), Sasquatch Bench/Hicks.
LEGEND_SHOWERS = 638

# ---------------------------------------------------------------------------
# Target booking — edit defaults here
# ---------------------------------------------------------------------------
TARGET_CHECKIN = date(2026, 7, 23)   # Thursday July 23, 2026
TARGET_NIGHTS  = 2                   # Thu + Fri → checkout Saturday

# Name fragments → water proximity score (higher = closer to water)
WATER_KEYWORDS = {
    "oceanfront": 10, "beachfront": 10, "lakefront": 10, "waterfront": 10,
    "ocean front": 10, "lake front": 10, "water front": 10,
    "beach": 8, "lake": 7, "ocean": 7, "riverside": 7, "howe sound": 7,
    "river": 6, "shore": 6, "water": 5, "creek": 4, "pond": 3, "view": 2,
}

# ---------------------------------------------------------------------------
# Coastal mainland parks within ~3 hours of Coquitlam (flush toilets only)
#
# Add more parks with:  python scout.py --explore-parks <keyword>
# ---------------------------------------------------------------------------
COASTAL_PARKS = {
    "Porteau Cove": {
        "resource_location_id": -2147483550,
        "drive_hours": 0.75,
        "notes": "Oceanfront sites on Howe Sound — closest to water of all",
    },
    "Golden Ears": {
        "resource_location_id": -2147483606,
        "drive_hours": 0.75,
        "excluded_map_ids": [-2147483573],  # hike-in only, no drive-in sites
        "notes": "Alouette Lake; showers auto-detected per section via legend type 638",
    },
    "Rolley Lake": {
        "resource_location_id": -2147483543,
        "drive_hours": 0.75,
        "notes": "Small lakeside park near Mission",
    },
    "Alice Lake": {
        "resource_location_id": -2147483647,
        "drive_hours": 1.0,
        "notes": "4 lakes; popular Squamish-area park",
    },
    "Cultus Lake": {
        "resource_location_id": -2147483623,
        "drive_hours": 1.0,
        "notes": "Large warm lake near Chilliwack",
    },
    # Uncomment once you confirm resource_location_id via --explore-parks:

    "Porpoise Bay": {
        "resource_location_id": -2147483551,
        "drive_hours": 2.2,
        "notes": "Sechelt, Sunshine Coast (ferry from Horseshoe Bay); showers confirmed",
    },
    # Nairn Falls: no legendItemType 638 in any section — no showers, excluded.
    # "Nairn Falls": {
    #     "resource_location_id": -2147483564,
    #     "drive_hours": 2.2,
    #     "notes": "Green River; no showers detected",
    # },
    # Sasquatch Provincial Park: pit toilets only (no "Restroom with Showers" icon
    # visible on the BC Parks map) — does NOT meet flush-toilet requirement.
    # "Sasquatch Provincial Park": {
    #     "resource_location_id": -2147483539,
    #     "drive_hours": 1.45,
    #     "notes": "Pit toilets only — excluded",
    # },
    # "Chilliwack Lake": {
    #     "resource_location_id": -2147483627,
    #     "drive_hours": 2.0,
    #     "notes": "Remote glacial lake; flush toilets",
    # },
}


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------

def api_get(path, params):
    resp = requests.get(f"{BASE_URL}{path}", params=params,
                        headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.json()


def _site_name(resource, fallback_id):
    """Extract best available display name from a map resource object."""
    for v in resource.get("localizedValues", []):
        for field in ("name", "title", "shortName", "fullName"):
            val = v.get(field, "").strip()
            if val:
                return val
    return str(abs(int(fallback_id))) if fallback_id else "?"


# ---------------------------------------------------------------------------
# Map + site data
# ---------------------------------------------------------------------------

def get_park_maps(resource_location_id, excluded_map_ids=None):
    """Return (root_map_id, campsite_maps: {map_id_str: {"title": str, "site_count": int}})."""
    excluded = {str(i) for i in (excluded_map_ids or [])}
    maps = api_get("/api/maps", {"resourceLocationId": resource_location_id})
    root_map_id = None
    campsite_maps = {}

    for m in maps:
        title = next(
            (v.get("title", "") for v in m.get("localizedValues", [])), ""
        )
        resources = m.get("mapResources", [])
        map_id_str = str(m["mapId"])

        if len(resources) == 0:
            root_map_id = m["mapId"]
            continue
        if map_id_str in excluded:
            continue
        if any(kw in title.lower() for kw in SKIP_MAP_KEYWORDS):
            continue

        legend_types = {item.get("legendItemType")
                        for item in m.get("mapLegendItems", [])}
        if LEGEND_SHOWERS not in legend_types:
            continue  # section has no restroom-with-showers icon

        campsite_maps[map_id_str] = {"title": title.strip(), "site_count": len(resources)}

    return root_map_id, campsite_maps


def get_available_site_ids(campsite_maps, checkin, nights, debug=False):
    """Return {map_id_str: set_of_available_site_id_strings}.

    Queries each section map directly rather than going through a root-map
    mapLinkAvailabilities hop, which is fragile when root_map_id is ambiguous.
    """
    checkout = checkin + timedelta(days=nights)
    base_params = {
        "bookingCategoryId": 0,
        "equipmentId": -32768,
        "subEquipmentId": -32768,
        "startDate": checkin.isoformat(),
        "endDate": checkout.isoformat(),
        "nights": nights,
        "isReserving": "true",
        "partySize": 1,
    }

    result = {}
    for map_id_str, section_info in campsite_maps.items():
        try:
            params = {**base_params, "mapId": int(map_id_str)}
            ra = api_get("/api/availability/map", params).get("resourceAvailabilities", {})
            available = {
                sid for sid, entries in ra.items()
                if any(item.get("availability") == AVAILABLE_FLAG for item in entries)
            }
            if available:
                result[map_id_str] = available
            if debug:
                status = f"{len(available)} available" if available else "none available"
                print(f"      {section_info['title']}: {status}")
        except Exception as e:
            if debug:
                print(f"      {section_info['title']}: error — {e}")
        time.sleep(0.2)

    return result



def water_score(text):
    """Heuristic: score a site or section name for proximity to water."""
    t = text.lower()
    return max((v for k, v in WATER_KEYWORDS.items() if k in t), default=0)


# ---------------------------------------------------------------------------
# Booking URLs
# ---------------------------------------------------------------------------

def section_booking_url(resource_location_id, section_map_id, checkin, nights):
    checkout = checkin + timedelta(days=nights)
    return (
        "https://camping.bcparks.ca/create-booking/results"
        f"?resourceLocationId={resource_location_id}"
        f"&mapId={section_map_id}"
        f"&searchTabGroupId=0&bookingCategoryId=0&nights={nights}"
        f"&isReserving=true&equipmentId=-32768&subEquipmentId=-32768"
        f"&partySize=1&startDate={checkin.isoformat()}"
        f"&endDate={checkout.isoformat()}"
    )


# ---------------------------------------------------------------------------
# Main scout run
# ---------------------------------------------------------------------------

def run_scout(checkin, nights, debug=False):
    checkout = checkin + timedelta(days=nights)
    print(f"\n{'=' * 62}")
    print(f"  BC Parks Pre-Booking Scout")
    print(f"  Check-in : {checkin}  ({checkin.strftime('%A, %B %-d, %Y')})")
    print(f"  Checkout : {checkout}  ({nights} night{'s' if nights != 1 else ''})")
    print(f"  Checking : {len(COASTAL_PARKS)} coastal mainland parks")
    print(f"  Goal     : sections with availability, ranked by water proximity")
    print(f"{'=' * 62}\n")

    all_results = []

    for park_name, cfg in COASTAL_PARKS.items():
        loc_id = cfg["resource_location_id"]
        excluded = cfg.get("excluded_map_ids", [])

        print(f"{'─' * 50}")
        print(f"{park_name}  ({cfg['drive_hours']}h from Coquitlam)")
        if cfg.get("notes"):
            print(f"  {cfg['notes']}")

        try:
            _, campsite_maps = get_park_maps(loc_id, excluded)
        except Exception as e:
            print(f"  ERROR fetching maps: {e}")
            continue

        if not campsite_maps:
            print("  No campsite sections found.")
            continue

        if debug:
            for mid, info in campsite_maps.items():
                print(f"  section {mid}: \"{info['title']}\"  ({len(info['sites'])} sites)")

        try:
            avail_by_section = get_available_site_ids(campsite_maps, checkin, nights, debug)
        except Exception as e:
            print(f"  ERROR fetching availability: {e}")
            continue

        if not avail_by_section:
            print("  No availability.")
            continue

        for map_id_str, avail_ids in avail_by_section.items():
            sec = campsite_maps[map_id_str]
            sec_title = sec["title"]
            sec_ws = water_score(sec_title)

            url = section_booking_url(loc_id, int(map_id_str), checkin, nights)

            all_results.append({
                "park": park_name,
                "drive_hours": cfg["drive_hours"],
                "section": sec_title,
                "avail_count": len(avail_ids),
                "water_score": sec_ws,
                "url": url,
            })

    print()

    if not all_results:
        print("No availability found across all parks for these dates.")
        print("Possible reasons:")
        print("  • Dates not yet open (BC Parks opens ~91 days in advance)")
        print("  • All sites fully booked — check again after midnight for cancellations")
        return

    # Sort: highest water score → closest drive time
    all_results.sort(key=lambda r: (-r["water_score"], r["drive_hours"]))

    print(f"{'=' * 62}")
    print(f"  RESULTS — {len(all_results)} section(s) with availability")
    print(f"  Sorted: water proximity → drive time")
    print(f"{'=' * 62}\n")

    for r in all_results:
        ws_tag = f"  [water ★{r['water_score']}]" if r["water_score"] > 0 else ""
        print(f"{'★' * 3}  {r['park']}  ›  {r['section']}{ws_tag}")
        print(f"       Drive: {r['drive_hours']}h  |  Sites available: {r['avail_count']}")
        if r["avail_count"] >= 2:
            print(f"       Open the map link → pick 2 adjacent green circles for you + friend")
        print(f"       Book: {r['url']}")
        print()

    print("─" * 62)
    print("THURSDAY 7 AM STRATEGY:")
    print("  1. Open two browser tabs — one per person — both already logged in")
    print("  2. Person A: navigate to section URL, select Site A, add to cart fast")
    print("  3. Person B: same section URL, select Site B simultaneously")
    print("  4. Both complete checkout independently")
    print("  Tip: screenshot the map now so you know exactly where each site is.")
    print()


# ---------------------------------------------------------------------------
# CLI helpers
# ---------------------------------------------------------------------------

def cmd_explore_parks(keyword=""):
    locations = api_get("/api/resourceLocation", {})
    print(f"\n{'ID':>16}  Name")
    print("─" * 60)
    for loc in sorted(locations, key=lambda x: next(
        (v.get("shortName", "") for v in x.get("localizedValues", [])), ""
    )):
        name = next(
            (v.get("shortName") or v.get("fullName", "?")
             for v in loc.get("localizedValues", [])), "?"
        )
        if keyword and keyword.lower() not in name.lower():
            continue
        print(f"{loc.get('resourceLocationId', '?'):>16}  {name}")
    print()
    print("Add parks to COASTAL_PARKS in scout.py with the IDs above.")


def cmd_explore_sites(park_name):
    cfg = COASTAL_PARKS.get(park_name)
    if not cfg:
        print(f"'{park_name}' not in COASTAL_PARKS.")
        print(f"Available: {list(COASTAL_PARKS.keys())}")
        return

    loc_id = cfg["resource_location_id"]
    excluded = {str(i) for i in cfg.get("excluded_map_ids", [])}
    print(f"\nSite layout for {park_name}  (resource_location_id={loc_id})\n")

    maps = api_get("/api/maps", {"resourceLocationId": loc_id})
    for m in maps:
        title = next(
            (v.get("title", "(no title)") for v in m.get("localizedValues", [])), "?"
        )
        resources = m.get("mapResources", [])
        map_id_str = str(m["mapId"])
        skip = (
            any(kw in title.lower() for kw in SKIP_MAP_KEYWORDS) or
            map_id_str in excluded or
            len(resources) == 0
        )
        skip_label = "  ← skipped" if skip else ""
        print(f"  map_id={m['mapId']}  \"{title}\"  ({len(resources)} sites){skip_label}")
        if resources and not skip:
            for r in resources:
                rid = r.get("resourceId", 0)
                name = _site_name(r, rid)
                print(f"    site {abs(rid):>12}  \"{name}\"")
    print()


# ---------------------------------------------------------------------------
# Park discovery — hardcoded since BC Parks API exposes no coordinates
#
# showers: True = confirmed restroom-with-showers icon on BC Parks map
#          False = confirmed pit toilets
#          None  = unverified — check camping.bcparks.ca before adding
# ferry:   True = BC Ferries required (Horseshoe Bay → Langdale)
# loc_id:  None = resource_location_id unknown, run --explore-parks to find
# ---------------------------------------------------------------------------

def _haversine_km(lat1, lon1, lat2, lon2):
    R = 6371.0
    φ1, φ2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin(math.radians(lat2 - lat1) / 2) ** 2
         + math.cos(φ1) * math.cos(φ2)
         * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


KNOWN_PARKS = [
    # (name,                     lat,      lon,       loc_id,        showers, ferry, notes)
    ("Porteau Cove",           49.5525, -123.2378, -2147483550,   True,  False, "Howe Sound oceanfront"),
    ("Golden Ears",            49.3167, -122.4667, -2147483606,   True,  False, "Alouette Lake, large"),
    ("Rolley Lake",            49.2961, -122.3453, -2147483543,   None,  False, "Near Mission — verify showers on map"),
    ("Alice Lake",             49.7753, -123.1197, -2147483647,   True,  False, "Squamish area, 4 lakes"),
    ("Stawamus Chief",         49.6752, -123.1470,  None,         True,  False, "Squamish — find ID: --explore-parks chief"),
    ("Cultus Lake",            49.0450, -122.0192, -2147483623,   True,  False, "Chilliwack, warm lake"),
    ("Sasquatch",              49.3956, -121.8394, -2147483539,   False, False, "Pit toilets — excluded"),
    ("Nairn Falls",            50.3220, -122.7780, -2147483564,   False, False, "Green River — no showers detected via legend type 638"),
    ("Birkenhead Lake",        50.5147, -122.5786,  None,         None,  False, "Pemberton — find ID: --explore-parks birkenhead"),
    ("Emory Creek",            49.4064, -121.5158,  None,         False, False, "Small, pit toilets"),
    ("Chilliwack Lake",        49.0697, -121.4236, -2147483627,   False, False, "Remote, pit toilets"),
    ("Porpoise Bay",           49.4756, -123.7428, -2147483551,   None,  True,  "Sechelt, ferry required — verify showers"),
    ("Roberts Creek",          49.4333, -123.6667,  None,         None,  True,  "Sunshine Coast, ferry — verify showers"),
]


def cmd_discover(max_km=150):
    """Show all known coastal mainland parks within max_km of Coquitlam,
    filtered to those with confirmed or unverified showers (excludes pit-toilets-only).
    Prints a ready-to-paste COASTAL_PARKS snippet for confirmed parks."""

    print(f"\nParks within {max_km}km of Coquitlam  (showers filter applied)\n")
    print(f"  {'Dist':>5}  {'Showers':9}  {'Ferry':5}  Name")
    print("  " + "─" * 58)

    to_add = []
    for name, lat, lon, loc_id, showers, ferry, notes in KNOWN_PARKS:
        dist = _haversine_km(COQUITLAM_LAT, COQUITLAM_LON, lat, lon)
        if dist > max_km:
            continue
        if showers is False:
            continue  # confirmed no showers — skip silently

        shower_tag = "✓ YES  " if showers else "? verify"
        ferry_tag  = "ferry" if ferry else "     "
        id_tag     = "" if loc_id else "  ← need ID"
        print(f"  {dist:>4.0f}km  {shower_tag}  {ferry_tag}  {name}{id_tag}")
        print(f"         {notes}")
        if loc_id and showers:
            to_add.append((dist, name, loc_id, ferry, notes))

    if to_add:
        print(f"\n{'─' * 62}")
        print("  Confirmed (showers=True, ID known) — paste into COASTAL_PARKS:\n")
        for dist, name, loc_id, ferry, notes in sorted(to_add):
            safe = name.replace('"', '\\"')
            ferry_note = "  # ferry required from Horseshoe Bay" if ferry else ""
            print(f'    "{safe}": {{')
            print(f'        "resource_location_id": {loc_id},{ferry_note}')
            drive = dist / 75
            print(f'        "drive_hours": {drive:.1f},  # ~{dist:.0f}km from Coquitlam')
            print(f'        "notes": "{notes}",')
            print(f'    }},')

    print()
    print("To find missing resource_location_ids:")
    print("  python scout.py --explore-parks <keyword>")
    print("To verify showers for '? verify' parks:")
    print("  Open camping.bcparks.ca → search park → look for Restroom-with-Showers icon")


# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="BC Parks Wednesday pre-booking scout"
    )
    parser.add_argument("--checkin", type=date.fromisoformat, default=TARGET_CHECKIN,
                        metavar="YYYY-MM-DD",
                        help=f"Check-in date (default: {TARGET_CHECKIN})")
    parser.add_argument("--nights", type=int, default=TARGET_NIGHTS,
                        help=f"Number of nights (default: {TARGET_NIGHTS})")
    parser.add_argument("--explore-parks", metavar="KEYWORD", nargs="?", const="",
                        help="List all BC Parks IDs, optionally filtered by keyword")
    parser.add_argument("--explore-sites", metavar="PARK_NAME",
                        help="Show site layout and map coordinates for a park")
    parser.add_argument("--discover", metavar="MAX_KM", nargs="?", const=150, type=int,
                        help="Auto-find parks within MAX_KM of Coquitlam with showers (default 150)")
    parser.add_argument("--debug", action="store_true",
                        help="Verbose API output")
    args = parser.parse_args()

    if args.discover is not None:
        cmd_discover(args.discover)
    elif args.explore_parks is not None:
        cmd_explore_parks(args.explore_parks)
    elif args.explore_sites:
        cmd_explore_sites(args.explore_sites)
    else:
        run_scout(args.checkin, args.nights, debug=args.debug)


if __name__ == "__main__":
    main()
