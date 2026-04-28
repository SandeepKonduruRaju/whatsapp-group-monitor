# ============================================================
#  WhatsApp Accommodation Monitor — Configuration
#  Edit this file before running. All user settings live here.
# ============================================================

# Exact group names as they appear in WhatsApp.
# Copy-paste from WhatsApp — spelling, spaces, and emoji must match exactly.
GROUPS_TO_MONITOR = [
    "Dublin Accommodation 2025",
    # "Rooms & Flats Dublin",
    # Add more groups here
]

# Keywords to watch for (case-insensitive, partial match).
KEYWORDS = [
    "available",
    "room",
    "accommodation",
]

# Number of recent messages to scan per group on each check.
MESSAGES_TO_SCAN = 100

# Seconds between scans. Default: 10800 = 3 hours.
SCAN_INTERVAL_SECONDS = 10800

# Set True to run the browser invisibly (required on Linux servers).
# Set False (default) to see the browser window — useful for debugging.
HEADLESS = False

# Path where the browser session is saved (keeps you logged in between runs).
SESSION_DIR = "./whatsapp_session"

# Tracks which messages have already triggered alerts (prevents duplicates).
LOG_FILE = "./monitor_log.json"
