"""
WhatsApp Accommodation Monitor
================================
Scans WhatsApp groups for keyword mentions every 2 hours (6am–11pm) and sends
alerts via Telegram (primary) and/or Email (backup).

Usage:
    python monitor.py           # Run continuously within active hours
    python monitor.py --once    # Single scan then exit
"""

import asyncio
import json
import logging
import smtplib
import ssl
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from pathlib import Path

# Force UTF-8 output on Windows so emoji don't crash the console.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

from config import (
    EMAIL_ENABLED,
    EMAIL_FROM,
    EMAIL_PASSWORD,
    EMAIL_TO,
    GROUPS_TO_MONITOR,
    HEADLESS,
    KEYWORDS,
    LOG_FILE,
    MESSAGES_TO_SCAN,
    SCAN_END_HOUR,
    SCAN_INTERVAL_SECONDS,
    SCAN_START_HOUR,
    SESSION_DIR,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
)

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("monitor.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)


# ── Helpers ────────────────────────────────────────────────────────────────────

def load_seen() -> set[str]:
    """Load previously alerted message IDs to prevent duplicate alerts."""
    if not Path(LOG_FILE).exists():
        return set()
    with open(LOG_FILE, encoding="utf-8") as f:
        data = json.load(f)
    return set(data.get("seen", []))


def save_seen(seen: set[str]) -> None:
    """Persist seen message IDs, capped at 5000 entries."""
    with open(LOG_FILE, "w", encoding="utf-8") as f:
        json.dump(
            {"seen": list(seen)[-5000:], "updated": datetime.now().isoformat()},
            f,
            indent=2,
        )


def contains_keyword(text: str) -> str | None:
    """Return the first matching keyword found in text, or None."""
    lower = text.lower()
    for kw in KEYWORDS:
        if kw.lower() in lower:
            return kw
    return None


def make_uid(group: str, sender: str, text: str) -> str:
    """Stable dedup key: group + sender + first 60 chars of message."""
    return f"{group}|{sender}|{text[:60]}"


def normalise(s: str) -> str:
    return unicodedata.normalize("NFC", s).strip().lower()


# ── Alert: Telegram ────────────────────────────────────────────────────────────

def send_telegram_alert(text: str) -> bool:
    """Send alert via Telegram Bot API. Returns True on success."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram not configured — skipping.")
        return False
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = urllib.parse.urlencode({
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
        }).encode()
        req = urllib.request.Request(url, data=payload)
        with urllib.request.urlopen(req, timeout=10) as resp:
            success = resp.status == 200
            if success:
                log.info("  Telegram alert sent.")
            return success
    except Exception as e:
        log.error(f"  Telegram alert failed: {e}")
        return False


# ── Alert: Email ───────────────────────────────────────────────────────────────

def send_email_alert(subject: str, body: str) -> bool:
    """Send alert via Gmail SMTP. Returns True on success."""
    if not EMAIL_ENABLED or not EMAIL_FROM or not EMAIL_PASSWORD:
        return False
    try:
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"] = EMAIL_FROM
        msg["To"] = EMAIL_TO
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as server:
            server.login(EMAIL_FROM, EMAIL_PASSWORD)
            server.sendmail(EMAIL_FROM, EMAIL_TO, msg.as_string())
        log.info("  Email alert sent.")
        return True
    except Exception as e:
        log.error(f"  Email alert failed: {e}")
        return False


# ── Alert: build message ───────────────────────────────────────────────────────

def build_alert(hits: list[dict]) -> str:
    now = datetime.now().strftime("%d %b %Y, %H:%M")
    kw_list = ", ".join(dict.fromkeys(k.lower() for k in KEYWORDS))
    lines = [
        f"Accommodation Alert — {now}",
        f"Found {len(hits)} new mention(s) of: {kw_list}",
        "",
    ]
    for i, hit in enumerate(hits, 1):
        lines += [
            f"{i}. [{hit['group']}]",
            f"From: {hit['sender']}",
            f"Message: {hit['text'][:300]}",
            "",
        ]
    lines.append("Act fast — accommodation goes quickly!")
    return "\n".join(lines)


def dispatch_alerts(hits: list[dict]) -> None:
    """Send alerts via all configured channels."""
    alert_text = build_alert(hits)
    subject = f"Accommodation Alert — {len(hits)} new mention(s)"

    telegram_ok = send_telegram_alert(alert_text)
    email_ok = send_email_alert(subject, alert_text)

    if not telegram_ok and not email_ok:
        log.warning("All alert channels failed or unconfigured.")


# ── WhatsApp browser logic ─────────────────────────────────────────────────────

async def open_whatsapp(playwright):
    """Launch Chromium with a persistent session so QR scan is only needed once."""
    Path(SESSION_DIR).mkdir(parents=True, exist_ok=True)
    browser = await playwright.chromium.launch_persistent_context(
        SESSION_DIR,
        headless=HEADLESS,
        args=["--no-sandbox"],
        viewport={"width": 1280, "height": 900},
    )
    page = browser.pages[0] if browser.pages else await browser.new_page()
    return browser, page


async def wait_for_load(page) -> bool:
    """Navigate to WhatsApp Web and wait until the chat list is visible."""
    log.info("Loading WhatsApp Web...")
    await page.goto("https://web.whatsapp.com", wait_until="domcontentloaded")

    try:
        await page.wait_for_selector(
            'div[data-testid="chat-list"], canvas[aria-label="Scan me!"]',
            timeout=90_000,
        )
    except PlaywrightTimeout:
        log.warning("WhatsApp Web took too long to load — check your connection.")
        return False

    if await page.query_selector('canvas[aria-label="Scan me!"]'):
        log.info("=" * 60)
        log.info("QR CODE — scan it in the browser window (90 seconds).")
        log.info("=" * 60)
        try:
            await page.wait_for_selector('div[data-testid="chat-list"]', timeout=90_000)
            log.info("QR scanned successfully — session saved.")
        except PlaywrightTimeout:
            log.error("QR scan timed out. Restart and try again.")
            return False

    await page.wait_for_timeout(3_000)
    log.info("WhatsApp Web loaded.")
    return True


async def open_group(page, group_name: str) -> bool:
    """Find a group by name in the chat list and open its conversation."""
    norm_target = normalise(group_name)

    async def scan_rows() -> bool:
        rows = await page.query_selector_all('#pane-side div[role="row"]')
        exact_match = None
        partial_match = None

        for row in rows:
            title_el = await row.query_selector(
                '[data-testid="cell-frame-title"] span[dir="auto"]'
            )
            if not title_el:
                continue
            # WhatsApp renders emoji as <img> — title attribute has the full plain-text name.
            title = await title_el.get_attribute("title") or await title_el.inner_text()
            norm_title = normalise(title)
            if norm_title == norm_target:
                exact_match = (row, title)
                break  # exact match wins immediately
            if norm_target in norm_title and partial_match is None:
                partial_match = (row, title)

        best = exact_match or partial_match
        if best:
            row, title = best
            log.info(f"  Matched: '{title.strip()}'")
            await row.click()
            await page.wait_for_selector(
                'div[data-testid="conversation-panel-messages"]',
                timeout=20_000,
            )
            return True
        return False

    try:
        if await scan_rows():
            return True

        # Fallback: open search with Ctrl+F
        await page.keyboard.press("Control+f")
        await page.wait_for_timeout(800)
        search_input = await page.wait_for_selector(
            'div[contenteditable="true"][data-tab="3"], '
            'div[data-testid="search-input"] div[contenteditable="true"], '
            'div[role="combobox"][contenteditable="true"]',
            timeout=5_000,
        )
        await search_input.click()
        await page.keyboard.type(group_name, delay=50)
        await page.wait_for_timeout(2_500)

        if await scan_rows():
            return True

        log.warning(f"  Group not found: '{group_name}'")
        await page.keyboard.press("Escape")
        return False

    except PlaywrightTimeout:
        log.warning(f"  Timeout while finding group: '{group_name}'")
        return False


async def get_messages(page, n: int) -> list[dict]:
    """Scrape the last N text messages from the currently open chat."""
    messages: list[dict] = []
    try:
        panel = await page.query_selector('div[data-testid="conversation-panel-messages"]')
        if panel:
            await panel.evaluate("el => el.scrollTop = el.scrollHeight - 3000")
            await page.wait_for_timeout(1_500)

        bubbles = []
        for sel in [
            'div[data-testid="msg-container"]',
            'div[class*="message-in"], div[class*="message-out"]',
            'div[data-id]',
        ]:
            bubbles = await page.query_selector_all(sel)
            if bubbles:
                break

        if not bubbles:
            log.warning("  No message bubbles found.")
            return messages

        for bubble in bubbles[-n:]:
            try:
                sender_el = await bubble.query_selector('span[data-testid="author"]')
                sender = (await sender_el.inner_text()).strip() if sender_el else "You"

                text: str | None = None
                for sel in ['div.copyable-text', 'span[data-testid="selectable-text"]']:
                    el = await bubble.query_selector(sel)
                    if el:
                        text = (await el.inner_text()).strip()
                        if text:
                            break

                if text:
                    messages.append({"sender": sender, "text": text})
            except Exception:
                continue

    except Exception as e:
        log.warning(f"  Error scraping messages: {e}")

    return messages


# ── Main scan loop ─────────────────────────────────────────────────────────────

async def run_scan() -> None:
    seen = load_seen()
    new_hits: list[dict] = []

    async with async_playwright() as pw:
        browser, page = await open_whatsapp(pw)
        if not await wait_for_load(page):
            await browser.close()
            return

        for group in GROUPS_TO_MONITOR:
            log.info(f"Scanning: {group}")
            if not await open_group(page, group):
                log.warning(f"  Skipping — group not found.")
                continue

            messages = await get_messages(page, MESSAGES_TO_SCAN)
            log.info(f"  {len(messages)} message(s) checked.")

            for msg in messages:
                kw = contains_keyword(msg["text"])
                if not kw:
                    continue
                uid = make_uid(group, msg["sender"], msg["text"])
                if uid not in seen:
                    log.info(f"  MATCH [{kw}] from {msg['sender']}: {msg['text'][:80]}...")
                    new_hits.append({**msg, "group": group, "keyword": kw, "uid": uid})
                    seen.add(uid)

        await browser.close()

    if new_hits:
        log.info(f"{len(new_hits)} new hit(s) — sending alerts...")
        dispatch_alerts(new_hits)
    else:
        log.info(f"No new mentions of: {', '.join(KEYWORDS)}")

    save_seen(seen)


# ── Entry point ────────────────────────────────────────────────────────────────

def _next_wake(after: datetime) -> datetime:
    """Return the next scan time, skipping outside the active window."""
    candidate = after + timedelta(seconds=SCAN_INTERVAL_SECONDS)
    if SCAN_START_HOUR <= candidate.hour < SCAN_END_HOUR:
        return candidate
    # Candidate falls outside window — wake at 6am
    if candidate.hour >= SCAN_END_HOUR:
        base = candidate + timedelta(days=1)
    else:
        base = candidate
    return base.replace(hour=SCAN_START_HOUR, minute=0, second=0, microsecond=0)


async def main() -> None:
    if "--once" in sys.argv:
        log.info("Running single scan...")
        await run_scan()
        log.info("Done.")
        return

    interval_h = SCAN_INTERVAL_SECONDS / 3600
    log.info(
        f"Monitor started — scanning every {interval_h:.0f}h "
        f"between {SCAN_START_HOUR:02d}:00–{SCAN_END_HOUR:02d}:00. "
        "Press Ctrl+C to stop."
    )
    while True:
        now = datetime.now()
        if SCAN_START_HOUR <= now.hour < SCAN_END_HOUR:
            try:
                await run_scan()
            except Exception as e:
                log.error(f"Scan failed: {e}", exc_info=True)
            wake = _next_wake(datetime.now())
        else:
            # Outside active window — sleep until 6am
            if now.hour >= SCAN_END_HOUR:
                wake = (now + timedelta(days=1)).replace(
                    hour=SCAN_START_HOUR, minute=0, second=0, microsecond=0
                )
            else:
                wake = now.replace(hour=SCAN_START_HOUR, minute=0, second=0, microsecond=0)

        sleep_secs = max((wake - datetime.now()).total_seconds(), 0)
        log.info(f"Next scan at {wake.strftime('%d %b %H:%M')}. Sleeping...\n")
        await asyncio.sleep(sleep_secs)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nMonitor stopped.")
