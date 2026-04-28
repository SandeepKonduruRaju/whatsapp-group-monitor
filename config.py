# ============================================================
#  WhatsApp Accommodation Monitor — Configuration
#  Edit this file to match your groups and preferences.
# ============================================================

# Exact group names as they appear in WhatsApp (copy-paste to be safe)
GROUPS_TO_MONITOR = [
    "Accommodation_Test_Group",
]

# Keywords to watch for (case-insensitive)
KEYWORDS = [
    "Test2",
]

# How many recent messages to scan per group per check
MESSAGES_TO_SCAN = 100

# Seconds between scans — 10800 = 3 hours
SCAN_INTERVAL_SECONDS = 10800

# Path where the browser session is saved (keeps you logged in)
SESSION_DIR = "./whatsapp_session"

# Path for the deduplication log (prevents duplicate alerts)
LOG_FILE = "./monitor_log.json"
