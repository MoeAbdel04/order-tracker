"""Pins / notes / "mark as received", and which changes raise a notification."""
import pytest

import tracker
from conftest import make_email

KNOWN = {"Target|1"}


def test_meta_is_saved_and_merged():
    tracker.set_meta("Target|1", {"pinned": True}, KNOWN)
    meta = tracker.set_meta("Target|1", {"note": "  gift for Sam  "}, KNOWN)
    assert meta == {"Target|1": {"pinned": True, "note": "gift for Sam"}}
    assert tracker.load_meta() == meta


def test_received_gets_a_timestamp_that_goes_away_when_unmarked():
    assert "received_at" in tracker.set_meta("Target|1", {"received": True}, KNOWN)["Target|1"]
    assert tracker.set_meta("Target|1", {"received": False}, KNOWN) == {}  # nothing left to keep


def test_note_is_capped_at_500_characters():
    assert len(tracker.set_meta("Target|1", {"note": "x" * 2000}, KNOWN)["Target|1"]["note"]) == 500


@pytest.mark.parametrize("key, patch", [("Target|999", {"pinned": True}), (None, {"pinned": True}),
                                        ("Target|1", "pinned"), ("Target|1", None)])
def test_bad_meta_requests_are_rejected(key, patch):
    with pytest.raises(ValueError):
        tracker.set_meta(key, patch, KNOWN)
    assert tracker.load_meta() == {}


def test_corrupt_meta_file_reads_as_empty():
    with open(tracker.META_PATH, "w", encoding="utf-8") as f:
        f.write("[1, 2")
    assert tracker.load_meta() == {}


# ---------------------------------------------------------------- notifications
@pytest.fixture
def toasts(monkeypatch):
    sent = []
    monkeypatch.setattr(tracker, "toast", lambda title, body: sent.append((title, body)))
    return sent


def orders(*emails):
    return tracker.build_orders(list(emails))


def test_first_run_does_not_notify(toasts):
    state = {}
    tracker.notify_changes(state, orders(make_email("Shipped")))
    assert toasts == []
    assert state["statuses"] == {"Target|900000000000001": "Shipped"}


def test_status_change_notifies_once(toasts):
    state = {"statuses": {"Target|900000000000001": "Order Confirmed"}}
    shipped = orders(make_email("Shipped"))
    tracker.notify_changes(state, shipped)
    tracker.notify_changes(state, shipped)
    assert len(toasts) == 1
    title, body = toasts[0]
    assert title == "1 order update"
    assert "Shipped: Target …0001" in body


def test_new_confirmation_is_too_minor_to_notify(toasts):
    tracker.notify_changes({"statuses": {}}, orders(make_email("Order Confirmed")))
    assert toasts == []


def test_failed_notification_is_tried_again(monkeypatch):
    def broken(*args):
        raise OSError("toast failed")
    monkeypatch.setattr(tracker, "toast", broken)
    state = {"statuses": {}}
    with pytest.raises(OSError):
        tracker.notify_changes(state, orders(make_email("Delivered")))
    assert state["statuses"] == {}  # not marked as seen


def test_package_shipped_to_another_address_notifies_once(toasts):
    away = orders(make_email("Shipped", tracking=["1Z999AA10123456784"], ship_to="500 Other Rd, Elsewhere, NC 28000"),
                  make_email("Shipped", order="900000000000002", tracking=["1Z999AA10123456795"],
                             ship_to="100 Example Way, Testville, NC 27000"))
    state = {"away_seen": []}
    tracker.notify_away(state, away)
    tracker.notify_away(state, away)
    assert [t[0] for t in toasts] == ["1 package shipped to another address"]
    assert "→ 500 Other Rd (UPS)" in toasts[0][1]
    assert state["away_seen"] == ["Target|900000000000001|1Z999AA10123456784"]


def test_away_packages_already_there_on_first_run_are_seeded_silently(toasts):
    state = {}
    tracker.notify_away(state, orders(make_email("Shipped", tracking=["1Z999AA10123456784"],
                                                 ship_to="500 Other Rd, Elsewhere, NC 28000")))
    assert toasts == []
    assert len(state["away_seen"]) == 1
