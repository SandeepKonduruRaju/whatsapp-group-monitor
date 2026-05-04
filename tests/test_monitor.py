"""
Unit tests for WhatsApp Accommodation Monitor.
Run with: pytest tests/ -v
"""

import pytest
from datetime import datetime, timedelta

import monitor
from monitor import (
    contains_keyword,
    make_uid,
    normalise,
    build_alert,
    _next_wake,
    _state,
)
from config import SCAN_START_HOUR, SCAN_END_HOUR, SCAN_INTERVAL_SECONDS


# ── Fixtures ───────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def reset_state():
    """Reset shared state before every test so tests don't affect each other."""
    _state["keywords"] = ["vantage", "central park", "occu east"]
    _state["last_scan_at"] = None
    _state["total_hits"] = 0
    yield


# ── contains_keyword ───────────────────────────────────────────────────────────

def test_keyword_exact_match():
    assert contains_keyword("vantage") == "vantage"

def test_keyword_case_insensitive():
    assert contains_keyword("VANTAGE apartments available") == "vantage"

def test_keyword_partial_match():
    assert contains_keyword("Vantage Apartments 2BHK available May") == "vantage"

def test_keyword_two_word_phrase():
    assert contains_keyword("Room near Central Park, DM me") == "central park"

def test_keyword_third_keyword():
    assert contains_keyword("Occu East rooms available from June") == "occu east"

def test_keyword_no_match():
    assert contains_keyword("Anyone looking for a flatmate in Rathmines?") is None

def test_keyword_empty_string():
    assert contains_keyword("") is None

def test_keyword_uses_runtime_state():
    """Keywords added via /addkeyword should be matched without restarting."""
    _state["keywords"].append("griffith")
    assert contains_keyword("Room near Griffith college") == "griffith"


# ── make_uid ───────────────────────────────────────────────────────────────────

def test_uid_format():
    uid = make_uid("Group A", "Alice", "Hello world")
    assert uid == "Group A|Alice|Hello world"

def test_uid_truncates_long_text():
    long_text = "x" * 100
    uid = make_uid("Group", "Bob", long_text)
    assert uid == f"Group|Bob|{'x' * 60}"

def test_uid_same_inputs_produce_same_uid():
    uid1 = make_uid("G", "S", "msg")
    uid2 = make_uid("G", "S", "msg")
    assert uid1 == uid2

def test_uid_different_groups_differ():
    uid1 = make_uid("Group A", "Alice", "same message")
    uid2 = make_uid("Group B", "Alice", "same message")
    assert uid1 != uid2


# ── normalise ──────────────────────────────────────────────────────────────────

def test_normalise_lowercases():
    assert normalise("HELLO") == "hello"

def test_normalise_strips_whitespace():
    assert normalise("  hello  ") == "hello"

def test_normalise_unicode_nfc():
    # NFC normalisation should not change already-normalised strings
    result = normalise("ಕನ್ನಡಿಗರು")
    assert isinstance(result, str)
    assert len(result) > 0


# ── build_alert ────────────────────────────────────────────────────────────────

def test_build_alert_contains_group_and_sender():
    hits = [{
        "group": "Test Group",
        "sender": "Priya K.",
        "text": "Vantage apartments available from May",
        "keyword": "vantage",
        "uid": "uid1",
    }]
    alert = build_alert(hits)
    assert "Test Group" in alert
    assert "Priya K." in alert
    assert "Vantage apartments available" in alert

def test_build_alert_truncates_long_message():
    long_text = "a" * 400
    hits = [{"group": "G", "sender": "S", "text": long_text, "keyword": "vantage", "uid": "u"}]
    alert = build_alert(hits)
    assert "a" * 301 not in alert  # capped at 300 chars

def test_build_alert_shows_hit_count():
    hits = [
        {"group": "G1", "sender": "S1", "text": "vantage room", "keyword": "vantage", "uid": "u1"},
        {"group": "G2", "sender": "S2", "text": "central park flat", "keyword": "central park", "uid": "u2"},
    ]
    alert = build_alert(hits)
    assert "2 new mention" in alert


# ── _next_wake ────────────────────────────────────────────────────────────────

def test_next_wake_stays_in_window():
    # A time well within the active window — next wake should also be in window
    within_window = datetime.now().replace(hour=8, minute=0, second=0, microsecond=0)
    wake = _next_wake(within_window)
    assert SCAN_START_HOUR <= wake.hour < SCAN_END_HOUR

def test_next_wake_after_last_scan_jumps_to_morning():
    # Last scan at 22:00 — next interval would be midnight, should jump to 6am
    late = datetime.now().replace(hour=22, minute=0, second=0, microsecond=0)
    wake = _next_wake(late)
    assert wake.hour == SCAN_START_HOUR

def test_next_wake_is_in_the_future():
    wake = _next_wake(datetime.now())
    assert wake > datetime.now()
