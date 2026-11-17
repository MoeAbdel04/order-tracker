"""The inbox scan, run against a fake IMAP server: what gets stored, what gets retried, and the
promise that the mailbox is only ever read."""
import json

import tracker
from conftest import ACCOUNT, raw_fixture, run_scan


def test_scan_stores_order_emails_and_skips_marketing():
    state, _ = run_scan({5: raw_fixture("target_confirmed"), 6: raw_fixture("target_marketing"),
                         9: raw_fixture("amazon_shipped")})
    assert sorted(state["emails"]) == ["5", "9"]
    assert state["accounts"]["test"]["last_uid"] == 9
    assert state["accounts"]["test"]["retry"] == {}


def test_scan_is_read_only():
    _, imap = run_scan({1: raw_fixture("target_confirmed"), 2: raw_fixture("mattel_shipped")})
    assert ("SELECT", "INBOX", True) in imap.commands  # mailbox opened read-only
    for command, *args in imap.commands:
        assert command in ("LOGIN", "SELECT", "SEARCH", "FETCH"), f"scan sent {command}"
        if command == "FETCH":
            assert "BODY.PEEK[" in args[1], "a fetch without PEEK would mark the email as read"


def test_second_scan_only_asks_for_newer_emails():
    state, _ = run_scan({1: raw_fixture("target_confirmed")})
    state, imap = run_scan({1: raw_fixture("target_confirmed"), 2: raw_fixture("target_shipped")}, state)
    search = next(c for c in imap.commands if c[0] == "SEARCH")
    assert search[2].startswith("UID 2:* ")
    fetched = [c[1] for c in imap.commands if c[0] == "FETCH"]
    assert "1" not in fetched  # the cached email isn't downloaded again
    assert sorted(state["emails"]) == ["1", "2"]


def test_empty_body_is_retried_on_the_next_scan():
    state, _ = run_scan({3: raw_fixture("target_shipped").split(b"\n\n", 1)[0] + b"\n\n"})
    assert state["emails"]["3"]["order"] == "902000000005678"  # headers alone still give status and order

    broken = FakeBody(raw_fixture("target_shipped"))
    state, _ = run_scan({3: broken})
    assert state["emails"] == {}
    assert list(state["accounts"]["test"]["retry"]) == ["3"]
    assert state["accounts"]["test"]["last_uid"] == 3

    state, _ = run_scan({3: raw_fixture("target_shipped")}, state)  # the server answers properly this time
    assert state["emails"]["3"]["tracking"] == ["1Z999AA10123456784"]
    assert state["accounts"]["test"]["retry"] == {}


class FakeBody(bytes):
    """An email whose headers can be read but whose full body comes back empty."""

    def __bool__(self):
        return False


def test_changed_uidvalidity_drops_this_inbox_only():
    state = {"emails": {"7": {"date": "2026-11-01T00:00:00+00:00"}, "gmail:7": {"date": "2026-11-01T00:00:00+00:00"}},
             "accounts": {"test": {"uidvalidity": "1", "last_uid": 7}}}
    state, _ = run_scan({1: raw_fixture("target_confirmed")}, state, uidvalidity="2")
    assert sorted(state["emails"]) == ["1", "gmail:7"]  # old UIDs mean other emails now
    assert state["accounts"]["test"] == {"uidvalidity": "2", "last_uid": 1, "retry": {}}


def test_second_inbox_gets_prefixed_keys():
    gmail = dict(ACCOUNT, prefix="gmail:")
    state, _ = run_scan({4: raw_fixture("mattel_confirmed")}, account=gmail)
    assert list(state["emails"]) == ["gmail:4"]


def test_state_from_before_gmail_support_is_migrated(sandbox):
    with open(tracker.STATE_PATH, "w", encoding="utf-8") as f:
        json.dump({"emails": {}, "uidvalidity": "9", "last_uid": 42}, f)
    assert tracker.load_state() == {"emails": {}, "accounts": {"icloud": {"uidvalidity": "9", "last_uid": 42}}}


def test_missing_or_corrupt_state_starts_fresh(sandbox):
    assert tracker.load_state()["emails"] == {}
    with open(tracker.STATE_PATH, "w", encoding="utf-8") as f:
        f.write("{not json")
    assert tracker.load_state()["emails"] == {}


def test_state_round_trip(sandbox):
    state, _ = run_scan({1: raw_fixture("pokemon_confirmed")})
    tracker.save_state(state)
    assert tracker.load_state() == state
