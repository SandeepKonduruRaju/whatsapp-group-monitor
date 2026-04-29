# ============================================================
#  WhatsApp Accommodation Monitor — Configuration
#  Edit this file before running. All user settings live here.
#  Sensitive values (tokens) are loaded from .env automatically.
# ============================================================

import os
from pathlib import Path

# Load .env file if it exists (stdlib only, no python-dotenv needed)
_env_file = Path(__file__).parent / ".env"
if _env_file.exists():
    for _line in _env_file.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())

# ── Groups to monitor ──────────────────────────────────────
# Copy-paste names exactly from WhatsApp — emoji and spacing must match.
GROUPS_TO_MONITOR = [
    "Namaste Ireland",
    "Accommodation 2️⃣| 🇮🇳Indians in Ireland🇮🇪",
    "Accommodation 1️⃣| 🇮🇳Indians in Ireland🇮🇪",
    "Students 1️⃣| 🇮🇳Indians in Ireland🇮🇪",
    "Indians in Ireland 🇮🇳🇮🇪",
    "Students in Ireland - 2025-26",
    "ACCOMODATION",
    "Indian Students in Ireland 2🧑‍🤝‍🧑👭👬👫",
    "Hunt for Accommodation",
    "SettleSmart Ireland 🏢🏡",
    "Accommodation Dublin Chapter",
    "ಕನ್ನಡಿಗರು 🇮🇳 ಐರ್ಲೆಂಡ್ 🇮🇪 2",
    "ಕನ್ನಡಿಗರು 🇮🇳 ಐರ್ಲೆಂಡ್ 🇮🇪",
    "Dublin Accommodation 2.0 Reloaded 😎",
    "தமிழ் மாணவர்கள் படை",
]

# ── Keywords to watch for (case-insensitive, partial match) ─
# "vantage" catches: vantage / Vantage / VANTAGE / Vantage Apartment etc.
KEYWORDS = [
    "vantage",
    "central park",
    "occu east",
]

# ── Scan settings ──────────────────────────────────────────
MESSAGES_TO_SCAN = 50           # Recent messages to check per group
SCAN_INTERVAL_SECONDS = 7200    # 7200 = every 2 hours
SCAN_START_HOUR = 6             # Don't scan before 6am
SCAN_END_HOUR = 23              # Don't scan at or after 11pm

# ── Browser settings ───────────────────────────────────────
HEADLESS = False                # Set True on a Linux server (no display)
SESSION_DIR = "./whatsapp_session"

# ── Deduplication log ──────────────────────────────────────
LOG_FILE = "./monitor_log.json"

# ── Telegram alert (primary) ───────────────────────────────
# Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in your .env file.
# 1. Open Telegram → search @BotFather → /newbot → copy token to .env
# 2. Send a message to your bot, then open:
#    https://api.telegram.org/bot<TOKEN>/getUpdates
#    Copy the number next to "chat":{"id": into .env
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# ── Email alert (backup) ───────────────────────────────────
# Uses Gmail SMTP. Generate an App Password at:
#   myaccount.google.com → Security → 2-Step Verification → App Passwords
EMAIL_ENABLED = False
EMAIL_FROM = ""                 # e.g. "yourname@gmail.com"
EMAIL_PASSWORD = ""             # Gmail App Password (not your login password)
EMAIL_TO = ""                   # Where to send alerts (can be same address)
