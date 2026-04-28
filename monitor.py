"""
WhatsApp Accommodation Monitor
================================
Scans WhatsApp groups for keyword mentions and sends you a WhatsApp
alert message when a new match is found.

Usage:
    python monitor.py           # Run continuously (every SCAN_INTERVAL_SECONDS)
    python monitor.py --once    # Single scan then exit
"""

import asyncio
import json
import logging
import sys
import time
import unicodedata
from datetime import datetime
from pathlib import Path

# Force UTF-8 output on Windows so emoji don't crash the console.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

from config import (
    GROUPS_TO_MONITOR,
    HEADLESS,
    KEYWORDS,
    LOG_FILE,
    MESSAGES_TO_SCAN,
    SCAN_INTERVAL_SECONDS,
    SESSION_DIR,
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
            timeout=60_000,
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
        log.info(f"  Scanning {len(rows)} row(s) for '{group_name}'")
        for row in rows:
            title_el = await row.query_selector(
                '[data-testid="cell-frame-title"] span[dir="auto"]'
            )
            if not title_el:
                continue
            # WhatsApp renders emoji as <img> tags — inner_text() drops them.
            # The title attribute contains the full plain-text name with emoji.
            title = await title_el.get_attribute("title") or await title_el.inner_text()
            if norm_target in normalise(title):
                log.info(f"  Matched: '{title.strip()}'")
                await row.click()
                await page.wait_for_selector(
                    'div[data-testid="conversation-panel-messages"]',
                    timeout=10_000,
                )
                log.info("  Conversation panel loaded.")
                return True
        return False

    try:
        # Most active groups are visible in the default list without searching.
        if await scan_rows():
            return True

        # Fallback: open WhatsApp's search with Ctrl+F.
        log.info("  Not in visible list — trying Ctrl+F search...")
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
            # Scroll near the bottom to ensure recent messages are rendered.
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
                log.info(f"  {len(bubbles)} message bubble(s) found.")
                break

        if not bubbles:
            log.warning("  No message bubbles found in DOM.")
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


async def send_alert(page, alert_text: str) -> None:
    """Send the alert message to your own WhatsApp chat."""
    log.info("Sending alert to self...")
    try:
        # 'message-yourself-row' is always pinned at the top of the chat list.
        self_row = await page.wait_for_selector(
            'div[data-testid="message-yourself-row"]', timeout=10_000
        )
        await self_row.click()
        await page.wait_for_timeout(1_500)

        input_box = await page.wait_for_selector(
            'div[data-testid="conversation-compose-box-input"]', timeout=10_000
        )
        await input_box.click()
        await page.wait_for_timeout(300)

        # WhatsApp sends on plain Enter; use Shift+Enter for newlines within the message.
        lines = alert_text.split("\n")
        for i, line in enumerate(lines):
            if line:
                await page.keyboard.type(line, delay=20)
            if i < len(lines) - 1:
                await page.keyboard.press("Shift+Enter")

        await page.wait_for_timeout(500)
        await page.keyboard.press("Enter")
        await page.wait_for_timeout(1_000)
        log.info("  Alert sent!")

    except Exception as e:
        log.error(f"  Failed to send alert: {e}")


# ── Alert formatting ───────────────────────────────────────────────────────────

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
            f"Message: {hit['text'][:200]}",
            "",
        ]
    lines.append("Act fast — accommodation goes quickly!")
    return "\n".join(lines)


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
                continue

            messages = await get_messages(page, MESSAGES_TO_SCAN)
            log.info(f"  {len(messages)} message(s) to check.")

            for msg in messages:
                kw = contains_keyword(msg["text"])
                if not kw:
                    continue
                uid = make_uid(group, msg["sender"], msg["text"])
                if uid not in seen:
                    log.info(f"  MATCH from {msg['sender']}: {msg['text'][:80]}...")
                    new_hits.append({**msg, "group": group, "keyword": kw, "uid": uid})
                    seen.add(uid)

        if new_hits:
            await send_alert(page, build_alert(new_hits))
        else:
            log.info(f"No new mentions of: {', '.join(KEYWORDS)}")

        save_seen(seen)
        await browser.close()


# ── Entry point ────────────────────────────────────────────────────────────────

async def main() -> None:
    if "--once" in sys.argv:
        log.info("Running single scan...")
        await run_scan()
        log.info("Done.")
        return

    interval_h = SCAN_INTERVAL_SECONDS // 3600
    log.info(f"Monitor started — scanning every {interval_h}h. Press Ctrl+C to stop.")
    while True:
        try:
            await run_scan()
        except Exception as e:
            log.error(f"Scan failed: {e}", exc_info=True)
        next_run = datetime.fromtimestamp(time.time() + SCAN_INTERVAL_SECONDS)
        log.info(f"Next scan at {next_run.strftime('%H:%M:%S')}. Sleeping...\n")
        await asyncio.sleep(SCAN_INTERVAL_SECONDS)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nMonitor stopped.")
