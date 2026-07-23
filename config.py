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
# Date windows to monitor (reservations open ~3 months ahead)
# ---------------------------------------------------------------------------
# Each window has its own date range and its own stay combos.
# A combo is (check-in weekday, number of nights, label).
# weekday(): Mon=0  Tue=1  Wed=2  Thu=3  Fri=4  Sat=5  Sun=6
# Use checkin weekday = None to match ANY day of the week (any check-in day).
MONITOR_WINDOWS = [
    {
        # Weekend of Aug 7
        "start": date(2026, 8, 7),
        "end":   date(2026, 8, 9),
        "combos": [
            (4, 2, "Fri+Sat"),
            (4, 1, "Fri-only"),
            (6, 1, "Sun-only"),
        ],
    },
    {
        # Weekend of Aug 14
        "start": date(2026, 8, 14),
        "end":   date(2026, 8, 16),
        "combos": [
            (4, 2, "Fri+Sat"),
            (4, 1, "Fri-only"),
            (6, 1, "Sun-only"),
        ],
    },
    {
        # Aug 21 – Sep 7: any check-in day, 2 or 3 night stays
        "start": date(2026, 8, 21),
        "end":   date(2026, 9, 7),
        "combos": [
            (None, 2, "2-night"),
            (None, 3, "3-night"),
        ],
    },
]

# Legacy bounds — used only as the date range for any per-park "extra_combos"
# override in PARKS (see the Golden Ears-style pattern below). Derived from
# the windows above so there's one source of truth; do not hardcode.
MONITOR_START = MONITOR_WINDOWS[0]["start"]
MONITOR_END   = MONITOR_WINDOWS[-1]["end"]

# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------
# GMAIL_USER, GMAIL_APP_PASSWORD, and EMAIL_TO are read from environment
# variables / GitHub Secrets — do NOT hardcode addresses here.
# EMAIL_TO is a comma-separated list, e.g. "a@gmail.com,b@gmail.com"
# Locally, export them before running check.py, e.g.:
#   export GMAIL_USER="you@gmail.com"
#   export EMAIL_TO="you@gmail.com,friend@gmail.com"
EMAIL_FROM = ""   # fallback only — set GMAIL_USER env var / secret instead
EMAIL_TO   = []   # fallback only — set EMAIL_TO env var / secret instead

# ---------------------------------------------------------------------------
# ntfy.sh push notifications (optional)
# ---------------------------------------------------------------------------
# The topic name is read from the NTFY_TOPIC env var / GitHub Secret — do NOT
# hardcode it here: ntfy topics have no access control, so anyone who knows
# the name can subscribe to (or publish on) it.
# 1. Pick a hard-to-guess topic name, e.g. "bc-camping-<random-suffix>"
# 2. Install the ntfy app → subscribe to that topic on every device
# 3. Set it in GitHub Secrets as NTFY_TOPIC (and export locally for testing)
NTFY_TOPIC = ""   # fallback only — set NTFY_TOPIC env var / secret instead
