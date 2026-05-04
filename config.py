# ============================================================
# CONFIGURATION — Edit this file to customize your alerts.
# check.py reads this; do not rename variables.
# ============================================================
from datetime import date

# ---------------------------------------------------------------------------
# Parks to monitor
# ---------------------------------------------------------------------------
# "sites": []  →  alert on ANY available campsite
# "sites": [123, 456]  →  alert only on those specific site numbers
# Run  python check.py --list-sites Alice Lake  to find site numbers.
# Walk-In, Group, Backcountry, and Day-Use areas are skipped automatically.
PARKS = {
    "Alice Lake": {
        "resource_location_id": -2147483647,
        "sites": [],
    },
    "Rolley Lake": {
        "resource_location_id": -2147483543,
        "sites": [],
    },
    "Cultus Lake": {
        "resource_location_id": -2147483623,
        "sites": [],
    },
    "Golden Ears": {
        "resource_location_id": -2147483606,
        "sites": [],
        # Gold Creek (-2147483573): hike-in only, causes availability bleed.
        # North Beach (-2147483572): no flush toilets — excluded per preference.
        "excluded_map_ids": [-2147483573, -2147483572],
    },
    "Porteau Cove": {
        "resource_location_id": -2147483550,
        "sites": [],
    },
    "Porpoise Bay": {
        "resource_location_id": -2147483551,
        "sites": [],
        # Sechelt, Sunshine Coast — requires BC Ferries (Horseshoe Bay → Langdale), ~2.2h total
    },
}

# ---------------------------------------------------------------------------
# Date range to monitor (reservations open 3 months ahead)
# ---------------------------------------------------------------------------
MONITOR_START = date(2026, 7, 1)
MONITOR_END   = date(2026, 9, 30)

# ---------------------------------------------------------------------------
# Weekend stay combinations to check
# Each entry: (check-in weekday, number of nights, label)
# weekday(): Mon=0  Tue=1  Wed=2  Thu=3  Fri=4  Sat=5  Sun=6
# ---------------------------------------------------------------------------
STAY_COMBOS = [
    (4, 2, "Fri+Sat"),   # check-in Friday, 2 nights
]

# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------
# GMAIL_USER and GMAIL_APP_PASSWORD are read from environment variables /
# GitHub Secrets — do NOT hardcode the password here.
EMAIL_FROM = "jaktor1088@gmail.com"   # ← change to your sending Gmail
EMAIL_TO   = [
    "jaktor1088@gmail.com",
    "deepikashathish@gmail.com",
    "pamela.rubio@gmail.com",
    # "person4@gmail.com",
]

# ---------------------------------------------------------------------------
# ntfy.sh push notifications (optional)
# ---------------------------------------------------------------------------
# 1. Pick a unique topic name, e.g. "bc-camping-yourname-2026"
# 2. Install the ntfy app → subscribe to that topic on every device
# 3. Set the same topic in GitHub Secrets as NTFY_TOPIC (overrides this value)
# Set to "" to disable push notifications.
NTFY_TOPIC = "bc-camping-jala-2026"   # e.g. "bc-camping-yourname-2026"
