"""Shared fixtures. Every test runs against temp folders: no inbox, no keyring, no real order
data, no Windows notifications."""
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(ROOT))

import tracker  # noqa: E402

HOME = {"street_number": "100", "street_name": "Example Way", "city": "Testville", "state": "NC", "zip": "27000"}


@pytest.fixture(autouse=True)
def sandbox(tmp_path, monkeypatch):
    """Point every path the tracker reads or writes at a temp folder and make toasts fail loudly."""
    redirect_dir = tmp_path / "RedirectShipment"
    redirect_dir.mkdir()
    (redirect_dir / "target.json").write_text(json.dumps(HOME), encoding="utf-8")
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(tracker, "REDIRECT_DIR", str(redirect_dir))
    monkeypatch.setattr(tracker, "REDIRECT_PY", str(redirect_dir / "redirect.py"))
    monkeypatch.setattr(tracker, "DATA_DIR", str(data))
    monkeypatch.setattr(tracker, "STATE_PATH", str(data / "state.json"))
    monkeypatch.setattr(tracker, "META_PATH", str(data / "order_meta.json"))

    def no_toast(*args):
        raise AssertionError(f"unexpected Windows notification: {args}")

    monkeypatch.setattr(tracker, "toast", no_toast)
    return tmp_path


class FakeIMAP:
    """Stands in for imaplib.IMAP4_SSL: serves fixture emails by UID and records every command,
    so tests can also check that the scan never does anything but read."""

    def __init__(self, messages, uidvalidity="1"):
        self.messages = dict(messages)  # uid -> raw bytes (b"" = body fetch comes back empty)
        self.uidvalidity = uidvalidity
        self.commands = []

    def login(self, user, password):
        self.commands.append(("LOGIN",))

    def select(self, mailbox, readonly=False):
        self.commands.append(("SELECT", mailbox, readonly))
        return "OK", [b"1"]

    def response(self, code):
        return code, [self.uidvalidity.encode()]

    def uid(self, command, *args):
        self.commands.append((command, *args))
        if command == "SEARCH":
            return "OK", [" ".join(str(u) for u in sorted(self.messages)).encode()]
        assert command == "FETCH", f"unexpected IMAP command {command}"
        uids, what = args
        out = []
        for uid in map(int, uids.split(",")):
            if uid not in self.messages:
                continue
            raw = self.messages[uid]
            if "HEADER.FIELDS" in what:
                raw = raw.split(b"\n\n", 1)[0] + b"\n\n"
            elif not raw:
                continue
            out += [(f"{uid} (UID {uid} BODY[] {{{len(raw)}}}".encode(), raw), b")"]
        return "OK", out


ACCOUNT = {"email": "shopper@example.com", "mailbox": "INBOX", "prefix": ""}


def raw_fixture(name):
    return (FIXTURES / f"{name}.eml").read_bytes()


def run_scan(messages, state=None, account=ACCOUNT, uidvalidity="1"):
    """Run the real inbox scan against a fake mailbox. Returns (state, imap)."""
    state = state if state is not None else {"emails": {}, "accounts": {}}
    sub = state["accounts"].setdefault("test", {"uidvalidity": None, "last_uid": 0})
    imap = FakeIMAP(messages, uidvalidity)
    tracker.scan_mailbox(imap, state, sub, account, "app-password", 45, "test")
    return state, imap


@pytest.fixture
def parse():
    """parse("target_shipped") -> the parsed-email dict the scan stores for that fixture
    (None if the scan skipped it)."""
    def _parse(name):
        state, _ = run_scan({1: raw_fixture(name)})
        return state["emails"].get("1")
    return _parse


@pytest.fixture
def scan_orders():
    """scan_orders("a", "b") -> orders built from those fixtures, keyed by retailer|order."""
    def _orders(*names):
        state, _ = run_scan({i: raw_fixture(n) for i, n in enumerate(names, 1)})
        return {o["key"]: o for o in tracker.build_orders(state["emails"].values())}
    return _orders


def make_email(status, order="900000000000001", retailer="Target", date="2026-11-01T12:00:00+00:00", **kw):
    """A parsed-email dict as parse_email returns it, for build_orders tests."""
    n = int(re.sub(r"\D", "", date)[:12])
    e = {"retailer": retailer, "date": date, "subject": f"{status} {order} {n}", "status": status, "order": order,
         "items": [], "total": None, "tracking": [], "eta": None, "name": None, "placed": None, "ship_to": None}
    e.update(kw)
    return e


def item(name, qty=1, price=None):
    return {"name": name, "qty": qty, "price": price}
