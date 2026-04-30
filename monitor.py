"""
WhatsApp Accommodation Monitor
================================
Scans WhatsApp groups for keyword mentions every 2 hours (6am–11pm) and sends
alerts via Telegram (primary) and/or Email (backup).

Telegram bot commands (send to the bot):
    /status         → last scan time, hits, schedule
    /keywords       → list active keywords
    /addkeyword X   → add keyword X without editing code
    /removekeyword X→ remove keyword X
    /scan           → trigger an immediate scan right now
    /help           → show all commands

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

# ── Runtime state (shared between scan loop and command listener) ──────────────
_state: dict = {
    "last_scan_at": None,       # datetime of last completed scan
    "total_hits": 0,            # total keyword matches found this session
    "keywords": list(KEYWORDS), # mutable — updated by /addkeyword and /removekeyword
    "tg_offset": 0,             # Telegram getUpdates offset (dedup)
}
_scan_event = asyncio.Event()   # set by /scan command to wake the scan loop early


# ── Helpers ────────────────────────────────────────────────────────────────────

def load_seen() -> set[str]:
    if not Path(LOG_FILE).exists():
        return set()
    with open(LOG_FILE, encoding="utf-8") as f:
        data = json.load(f)
    _state["total_hits"] = data.get("total_hits", 0)
    return set(data.get("seen", []))


def save_seen(seen: set[str]) -> None:
    with open(LOG_FILE, "w", encoding="utf-8") as f:
        json.dump(
            {
                "seen": list(seen)[-5000:],
                "total_hits": _state["total_hits"],
                "updated": datetime.now().isoformat(),
            },
            f,
            indent=2,
        )


def contains_keyword(text: str) -> str | None:
    lower = text.lower()
    for kw in _state["keywords"]:
        if kw.lower() in lower:
            return kw
    return None


def make_uid(group: str, sender: str, text: str) -> str:
    return f"{group}|{sender}|{text[:60]}"


def normalise(s: str) -> str:
    return unicodedata.normalize("NFC", s).strip().lower()


# ── Telegram: send ─────────────────────────────────────────────────────────────

def _tg_post(method: str, payload: dict) -> dict:
    """Synchronous Telegram API POST (runs in thread pool from async code)."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    data = urllib.parse.urlencode(payload).encode()
    req = urllib.request.Request(url, data=data)
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def _tg_get(method: str, params: dict) -> dict:
    """Synchronous Telegram API GET."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    qs = urllib.parse.urlencode(params)
    with urllib.request.urlopen(f"{url}?{qs}", timeout=35) as resp:
        return json.loads(resp.read())


def send_telegram_alert(text: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram not configured — skipping.")
        return False
    try:
        _tg_post("sendMessage", {"chat_id": TELEGRAM_CHAT_ID, "text": text})
        log.info("  Telegram alert sent.")
        return True
    except Exception as e:
        log.error(f"  Telegram alert failed: {e}")
        return False


async def tg_reply(text: str) -> None:
    """Send a Telegram message from async context without blocking the event loop."""
    await asyncio.to_thread(send_telegram_alert, text)


# ── Alert: Email ───────────────────────────────────────────────────────────────

def send_email_alert(subject: str, body: str) -> bool:
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


# ── Alert: build and dispatch ──────────────────────────────────────────────────

def build_alert(hits: list[dict]) -> str:
    now = datetime.now().strftime("%d %b %Y, %H:%M")
    kw_list = ", ".join(dict.fromkeys(k.lower() for k in _state["keywords"]))
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


async def dispatch_alerts(hits: list[dict]) -> None:
    alert_text = build_alert(hits)
    subject = f"Accommodation Alert — {len(hits)} new mention(s)"
    telegram_ok = await asyncio.to_thread(send_telegram_alert, alert_text)
    email_ok = await asyncio.to_thread(send_email_alert, subject, alert_text)
    if not telegram_ok and not email_ok:
        log.warning("All alert channels failed or unconfigured.")


# ── Telegram: bot commands ─────────────────────────────────────────────────────

async def handle_command(text: str) -> str:
    parts = text.strip().split(maxsplit=1)
    cmd = parts[0].lower().split("@")[0]   # strip @botname suffix if present
    arg = parts[1].strip() if len(parts) > 1 else ""

    if cmd == "/status":
        if _state["last_scan_at"]:
            delta = datetime.now() - _state["last_scan_at"]
            mins = int(delta.total_seconds() // 60)
            when = f"{mins} min ago" if mins < 60 else _state["last_scan_at"].strftime("%d %b %H:%M")
        else:
            when = "not yet (since restart)"
        return (
            f"WhatsApp Monitor — Status\n"
            f"{'─' * 28}\n"
            f"Last scan:   {when}\n"
            f"Total hits:  {_state['total_hits']}\n"
            f"Groups:      {len(GROUPS_TO_MONITOR)}\n"
            f"Keywords:    {', '.join(_state['keywords'])}\n"
            f"Schedule:    every {SCAN_INTERVAL_SECONDS//3600}h, "
            f"{SCAN_START_HOUR:02d}:00–{SCAN_END_HOUR:02d}:00"
        )

    elif cmd == "/keywords":
        kws = "\n".join(f"  • {k}" for k in _state["keywords"])
        return f"Active keywords ({len(_state['keywords'])}):\n{kws}"

    elif cmd == "/addkeyword":
        if not arg:
            return "Usage: /addkeyword <word>\nExample: /addkeyword griffith"
        kw = arg.lower()
        if kw in [k.lower() for k in _state["keywords"]]:
            return f"'{kw}' is already being monitored."
        _state["keywords"].append(kw)
        return f"Added '{kw}'.\nNow monitoring: {', '.join(_state['keywords'])}"

    elif cmd == "/removekeyword":
        if not arg:
            return "Usage: /removekeyword <word>"
        kw = arg.lower()
        before = len(_state["keywords"])
        _state["keywords"] = [k for k in _state["keywords"] if k.lower() != kw]
        if len(_state["keywords"]) < before:
            return f"Removed '{kw}'.\nNow monitoring: {', '.join(_state['keywords'])}"
        return f"'{kw}' not found in keywords."

    elif cmd == "/scan":
        _scan_event.set()
        return "Triggering an immediate scan now. Results coming shortly..."

    elif cmd == "/help":
        return (
            "WhatsApp Monitor — Commands\n"
            "─────────────────────────────\n"
            "/status           — last scan, hits, schedule\n"
            "/keywords         — list active keywords\n"
            "/addkeyword X     — start watching keyword X\n"
            "/removekeyword X  — stop watching keyword X\n"
            "/scan             — trigger an immediate scan\n"
            "/help             — show this message"
        )

    return f"Unknown command: {cmd}\nSend /help for available commands."


async def command_listener() -> None:
    """Poll Telegram every 2 seconds for bot commands from the configured chat."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram not configured — command listener disabled.")
        return

    log.info("Telegram command listener started. Send /help to the bot.")
    while True:
        try:
            data = await asyncio.to_thread(
                _tg_get,
                "getUpdates",
                {
                    "offset": _state["tg_offset"],
                    "timeout": 30,
                    "allowed_updates": "message",
                },
            )
            for update in data.get("result", []):
                _state["tg_offset"] = update["update_id"] + 1
                msg = update.get("message", {})
                text = msg.get("text", "")
                chat_id = str(msg.get("chat", {}).get("id", ""))
                if text.startswith("/") and chat_id == str(TELEGRAM_CHAT_ID):
                    log.info(f"Command received: {text}")
                    reply = await handle_command(text)
                    await tg_reply(reply)
        except Exception as e:
            log.warning(f"Command poll error: {e}")
            await asyncio.sleep(5)


# ── WhatsApp browser logic ─────────────────────────────────────────────────────

async def open_whatsapp(playwright):
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
            title = await title_el.get_attribute("title") or await title_el.inner_text()
            norm_title = normalise(title)
            if norm_title == norm_target:
                exact_match = (row, title)
                break
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


# ── Main scan logic ────────────────────────────────────────────────────────────

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
                log.warning("  Skipping — group not found.")
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

    _state["last_scan_at"] = datetime.now()

    if new_hits:
        _state["total_hits"] += len(new_hits)
        log.info(f"{len(new_hits)} new hit(s) — sending alerts...")
        await dispatch_alerts(new_hits)
    else:
        log.info(f"No new mentions of: {', '.join(_state['keywords'])}")

    save_seen(seen)


# ── Entry point ────────────────────────────────────────────────────────────────

def _next_wake(after: datetime) -> datetime:
    candidate = after + timedelta(seconds=SCAN_INTERVAL_SECONDS)
    if SCAN_START_HOUR <= candidate.hour < SCAN_END_HOUR:
        return candidate
    if candidate.hour >= SCAN_END_HOUR:
        base = candidate + timedelta(days=1)
    else:
        base = candidate
    return base.replace(hour=SCAN_START_HOUR, minute=0, second=0, microsecond=0)


async def scan_loop() -> None:
    interval_h = SCAN_INTERVAL_SECONDS / 3600
    log.info(
        f"Scan loop started — every {interval_h:.0f}h "
        f"between {SCAN_START_HOUR:02d}:00–{SCAN_END_HOUR:02d}:00."
    )
    while True:
        now = datetime.now()
        if SCAN_START_HOUR <= now.hour < SCAN_END_HOUR or _scan_event.is_set():
            _scan_event.clear()
            try:
                await run_scan()
            except Exception as e:
                log.error(f"Scan failed: {e}", exc_info=True)
            wake = _next_wake(datetime.now())
        else:
            if now.hour >= SCAN_END_HOUR:
                wake = (now + timedelta(days=1)).replace(
                    hour=SCAN_START_HOUR, minute=0, second=0, microsecond=0
                )
            else:
                wake = now.replace(hour=SCAN_START_HOUR, minute=0, second=0, microsecond=0)

        sleep_secs = max((wake - datetime.now()).total_seconds(), 0)
        log.info(f"Next scan at {wake.strftime('%d %b %H:%M')}. Sleeping...\n")
        try:
            # Wake early if /scan command is received
            await asyncio.wait_for(_scan_event.wait(), timeout=sleep_secs)
            log.info("Immediate scan requested via /scan.")
        except asyncio.TimeoutError:
            pass


async def main() -> None:
    if "--once" in sys.argv:
        log.info("Running single scan...")
        await run_scan()
        log.info("Done.")
        return

    log.info("Monitor started. Send /help to the Telegram bot for commands.")
    await asyncio.gather(
        scan_loop(),
        command_listener(),
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nMonitor stopped.")
