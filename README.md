# WhatsApp Group Monitor

A Python automation tool that watches WhatsApp group chats for keyword mentions and delivers real-time alerts to your own WhatsApp chat.

Built to solve a real problem — Dublin's rental market moves fast, and manually checking 10+ WhatsApp accommodation groups every few hours is not practical.

---

## How it works

```
WhatsApp Web (Playwright browser)
        │
        ▼
  Scan group chat list  ──► Match group by name (emoji-safe)
        │
        ▼
  Scrape recent messages ──► Extract text from message bubbles
        │
        ▼
  Keyword matching  ──► Case-insensitive, multiple keywords
        │
        ▼
  Deduplication  ──► Skip already-alerted messages (persisted to disk)
        │
        ▼
  Alert  ──► Send formatted message to your own WhatsApp chat
```

The browser session is saved to disk after the first QR scan — subsequent runs reuse the session without needing to scan again.

---

## Technical challenges solved

**1. Dynamic DOM — WhatsApp Web changes selectors regularly**

WhatsApp renders group names with emoji as `<img>` tags, so `innerText()` drops them. Matching on the `title` HTML attribute captures the full name including emoji. A multi-selector fallback chain handles changes to message bubble selectors across WhatsApp Web versions.

**2. Virtual scrolling**

WhatsApp Web only renders visible messages in the DOM. The scraper scrolls the conversation panel to load the most recent N messages before extraction.

**3. React synthetic events**

WhatsApp Web's compose box is a React-controlled `contenteditable` div. `document.execCommand('insertText')` bypasses React's event system — the text appears in the DOM but WhatsApp doesn't register it, so Enter sends a blank message. Fixed by using Playwright's `keyboard.type()` which fires proper keyboard events, with `Shift+Enter` for newlines.

**4. Deduplication across runs**

A persistent JSON log stores a hash of `group|sender|first_60_chars` for every alerted message. The same message never triggers a second alert even if it stays in the scan window across multiple runs.

---

## Stack

| Layer | Technology |
|---|---|
| Browser automation | [Playwright](https://playwright.dev/python/) (async) |
| Language | Python 3.11+ |
| Session persistence | Chromium persistent context |
| Deduplication store | JSON flat file |
| Logging | Python `logging` to stdout + file |

---

## Setup

**1. Install dependencies**
```bash
pip install -r requirements.txt
playwright install chromium
```

**2. Configure** — edit `config.py`
```python
GROUPS_TO_MONITOR = [
    "Your Group Name Here",   # copy-paste exact name from WhatsApp
]

KEYWORDS = [
    "available",
    "room",
    "accommodation",
]
```

**3. First run — scan QR code**
```bash
python monitor.py
```
A browser opens WhatsApp Web. On your phone: **Settings → Linked Devices → Link a Device** → scan the QR code. Session is saved — you won't need to scan again.

---

## Usage

```bash
# Run once (useful for testing)
python monitor.py --once

# Run continuously (scans every 3 hours by default)
python monitor.py
```

---

## Alert format

When a keyword is found, you receive a WhatsApp message from yourself:

```
Accommodation Alert — 26 Apr 2026, 18:54
Found 2 new mention(s) of: temporary accommodation

1. Dublin Accommodation Group
From: Sarah M.
Message: Temporary accommodation available near Ranelagh, €850/month...

2. Dublin Accommodation Group
From: John D.
Message: Temporary accommodation in Rathmines — available from May 1st...

Reply quickly — accommodation goes fast!
```

---

## Project structure

```
.
├── monitor.py          # Core automation logic
├── config.py           # All user-configurable settings
├── requirements.txt    # Python dependencies
├── whatsapp_session/   # Saved browser session (gitignored)
└── monitor_log.json    # Seen message IDs for deduplication (gitignored)
```

---

## Running 24/7

**Oracle Cloud Free Tier (recommended)** — always-free Ubuntu VM:
```bash
# Install dependencies on the VM
pip install -r requirements.txt
playwright install chromium
playwright install-deps chromium

# Run headlessly in the background
nohup python monitor.py > monitor.log 2>&1 &
```

**Local machine** — just leave the terminal open. The browser runs visibly (non-headless) by default.

---

## Limitations

- Requires WhatsApp Web to be accessible (not usable on accounts with linked device restrictions)
- WhatsApp Web DOM structure changes occasionally; selectors may need updating
- Session expires if logged out from another device
