"""Order Tracker: scans an iCloud inbox (read-only) for order emails from
Target, Mattel, Amazon, Pokemon Center and Topps, extracts status / items /
prices / tracking, writes dashboard.html and pops a Windows notification when
an order changes status.

    python tracker.py                 # incremental scan + rebuild dashboard
    python tracker.py --open          # same, then open the dashboard
    python tracker.py --rescan        # forget cached emails and rescan --days back
    python tracker.py --set-password  # store iCloud app-specific password in Windows Credential Manager
    python tracker.py --show          # open the dashboard (served on localhost, with a Refresh button)
    python tracker.py --serve         # run that local server in the foreground
"""
import argparse
import base64
import email
import getpass
import hashlib
import imaplib
import json
import logging
import os
import re
import subprocess
import sys
import threading
import urllib.request
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime, parseaddr
from html import escape, unescape

import keyring

import market

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "data")
STATE_PATH = os.path.join(DATA_DIR, "state.json")
TEMPLATE_PATH = os.path.join(HERE, "dashboard_template.html")
DASHBOARD_PATH = os.path.join(HERE, "dashboard.html")
LOG_PATH = os.path.join(DATA_DIR, "tracker.log")
BROWSER_EXE = r"C:\Program Files\Google\Chrome\Application\chrome.exe"  # falls back to the default browser if missing
SERVE_PORT = 8765
SERVE_URL = f"http://127.0.0.1:{SERVE_PORT}/"

ACCOUNTS_PATH = os.path.join(HERE, "accounts.json")


def load_account_emails():
    """Inbox addresses live in accounts.json (git-ignored), e.g. {"icloud": "you@icloud.com", "gmail": "you@gmail.com"}.
    See accounts.example.json. An account without an address is skipped."""
    try:
        with open(ACCOUNTS_PATH, encoding="utf-8") as f:
            return {k: str(v).strip() for k, v in json.load(f).items()}
    except (OSError, ValueError, AttributeError):
        return {}


_EMAILS = load_account_emails()
ICLOUD_EMAIL = _EMAILS.get("icloud", "")
GMAIL_EMAIL = _EMAILS.get("gmail", "")
# Each inbox: state/email keys are prefixed with "prefix" (iCloud keeps bare UIDs from before Gmail was added).
# An account with no address in accounts.json or no stored password is skipped.
ACCOUNTS = {
    "icloud": {"email": ICLOUD_EMAIL, "host": "imap.mail.me.com", "mailbox": "INBOX",
               "keyring": "order-tracker-icloud", "prefix": "", "label": "iCloud app-specific password"},
    "gmail": {"email": GMAIL_EMAIL, "host": "imap.gmail.com", "mailbox": '"[Gmail]/All Mail"',
              "keyring": "order-tracker-gmail", "prefix": "gmail:", "label": "Gmail app password"},
}
DEFAULT_DAYS = 45
RETENTION_DAYS = 120

RETAILERS = {
    "Target": "target",
    "Mattel": "mattel",
    "Amazon": "amazon",
    "Pokémon Center": "pokemon",
    "Topps": "topps",
}
ORDER_LINKS = {
    "Target": "https://www.target.com/orders/{n}",
    "Amazon": "https://www.amazon.com/gp/your-account/order-details?orderID={n}",
    "Pokémon Center": "https://www.pokemoncenter.com/account/orders",
    "Mattel": "https://creations.mattel.com/account",
    "Topps": "https://www.topps.com/account",
}
# Marketing / non-order senders from these retailers
SKIP_SENDERS = re.compile(r"survey|newsletter|store-news|customer-reviews|protection|business|health|music|prime", re.I)

# ---------------------------------------------------------------- statuses
# First match wins. None = recognised but ignored.
SUBJECT_STATUS = [
    (None, r"could not be cancel|membership|pre-?order (now|yours|the)|preorder the|% off|\$\d+ off|weekly ad|deal days"),
    ("Item Cancelled", r"cancel+ed (an )?items? from|items? cancel+ed successfully"),
    ("Cancelled", r"\bcancel"),
    ("Delivered", r"(items?|package|order)\b.*\b(arrived|delivered)|^delivered\b|been delivered|review your latest order"),
    ("Out for Delivery", r"out for delivery|arriv\w* today"),
    ("Delayed", r"delay|new estimated delivery|delivery estimate update|running late"),
    ("Shipped", r"\bshipped\b|about to ship|on (its|the) way|shipping (update|confirmation)|in transit|arrive soon|arriv\w* tomorrow"),
    ("Pre-order", r"preorder payment|release day"),
    ("Order Confirmed", r"^ordered\b|order (is )?confirm|order #\S+ is confirmed|confirmation|order (received|placed)|thanks? (you )?for (shopping|your order|your purchase)|here's your order"),
]
STATUS_RANK = {"Order Confirmed": 1, "Pre-order": 1, "Item Cancelled": 0, "Delayed": 2, "Shipped": 3,
               "Out for Delivery": 4, "Delivered": 5, "Cancelled": 6}
NOTIFY_STATUSES = {"Shipped", "Out for Delivery", "Delivered", "Cancelled", "Delayed"}

# ---------------------------------------------------------------- parsing
INVISIBLE = re.compile(r"[\u034f\u00ad\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]")
QTY_LINE = re.compile(r"^(?:Qty|Quantity)\s*:?\s*(\d+)\b", re.I)
PRICE = re.compile(r"\$\s?(\d[\d,]*\.\d{2})")
ITEM_NOISE = re.compile(r"^(SKU|Item\s?#|\[IMG|Price|\$|Arriv|Qty|Quantity|\d+$|Color|Size|Sold and shipped by)", re.I)
PRICE_STOP = re.compile(r"total|qty|quantity|would these|check everything|perfect pairings|shop now|order summary|"
                        r"shipping address|receipt|rate & review", re.I)
ORDER_PATTERNS = [
    re.compile(r"[Oo]rder\s*[Nn]umber\s*:?\s*([A-Z]?\d[\d-]{5,}\d)"),
    re.compile(r"[Oo]rder\.?\s*#\s*:?\s*\[?([A-Z]{0,4}\d[\d-]{5,}\d)"),
    re.compile(r"[Oo]rder\s+([A-Z]{2,4}\d{5,})"),
]
TOTAL_PATTERNS = [
    re.compile(r"(?:Grand Total|Order Total|Order total)\s*:?\s*\$\s?([\d,]+\.\d{2})"),
    re.compile(r"payment of \$([\d,]+\.\d{2})"),
    re.compile(r"(?:^|\n)Total\s*:?\s*\$\s?([\d,]+\.\d{2})"),
    re.compile(r"Subtotal[^$\n]*:?\s*\$\s?([\d,]+\.\d{2})"),
]
TRACKING_PATTERNS = [
    re.compile(r"\b(1Z[0-9A-Z]{16})\b"),
    re.compile(r"\b(TBA\d{9,12})\b"),
    re.compile(r"Tracking\s*(?:#|Number|No\.?)?\s*:?\s*#?\s*([A-Z0-9]{10,34})\b", re.I),
    re.compile(r"tracknumbers=([A-Z0-9]{10,34})", re.I),
]
ETA_LINE = re.compile(r"^(?:Arriv(?:es|ing)|Estimated delivery(?: date)?(?: is)?:?)\s+(.{3,40})$", re.I)
NAME_PATTERNS = [
    re.compile(r"Thanks for your order, ([A-Z][a-z]+)!"),
    re.compile(r"^(?:Hi|Hello|Hey),?\s+([A-Z][a-z]+)\s*[,!]", re.M),
    re.compile(r"^([A-Z][a-z]+), (?:we're|your)", re.M),
]
PRODUCT_IMG = re.compile(r"scene7\.com/is/image/Target/GUEST_|media-amazon\.com/images/I/|cdn\.shopify\.com/s/files/", re.I)
PLACED = re.compile(r"(?:Placed|Date Ordered:?)\s+([A-Z][a-z]+ \d{1,2}, \d{4})")


def dec(v):
    try:
        return str(make_header(decode_header(v or "")))
    except Exception:
        return v or ""


def message_parts(msg):
    """(html, plain) bodies of an email; either may be None."""
    html, plain = None, None
    for part in msg.walk():
        ctype = part.get_content_type()
        if part.get_filename() or ctype not in ("text/html", "text/plain"):
            continue
        try:
            body = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "replace")
        except Exception:
            continue
        if ctype == "text/html" and html is None:
            html = body
        elif ctype == "text/plain" and plain is None:
            plain = body
    return html, plain


def message_text(msg, html=None, plain=None):
    """HTML part rendered to lines (image alt text kept, it often holds full product names);
    falls back to text/plain."""
    if html is None and plain is None:
        html, plain = message_parts(msg)
    if html:
        raw = re.sub(r"(?is)<(script|style|head).*?</\1>", " ", html)
        raw = re.sub(r"(?i)<img[^>]*alt=\"([^\"]+)\"[^>]*>", r"\n[IMG:\1]\n", raw)
        raw = re.sub(r"(?is)<br\s*/?>|</(p|div|tr|td|li|h\d|table)>", "\n", raw)
        raw = re.sub(r"(?s)<[^>]+>", " ", raw)
        text = unescape(raw)
    else:
        text = plain or ""
        text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)  # markdown links
    text = INVISIBLE.sub("", text).replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    lines = [l.strip() for l in text.splitlines()]
    text = "\n".join(l for l in lines if l)
    text = re.sub(r"\n:\s*", " : ", text)                       # "Qty\n: 1" -> "Qty : 1"
    text = re.sub(r"(?m)^\$ (\d+) (\d{2})$", r"$\1.\2", text)    # Amazon "$ 23 99"
    return text


def classify(subject):
    for status, pat in SUBJECT_STATUS:
        if re.search(pat, subject, re.I):
            return status or "skip"
    return None


def parse_items(text, subject):
    lines = text.splitlines()
    items = []
    for i, line in enumerate(lines):
        m = QTY_LINE.match(line)
        if not m:
            continue
        name, name_idx = None, None
        for j in range(i - 1, max(i - 5, -1), -1):
            if not ITEM_NOISE.match(lines[j]):
                name, name_idx = lines[j], j
                break
        if not name or len(name) < 4:
            continue
        # Prefer full image alt text when the visible name is truncated
        if name_idx and lines[name_idx - 1].startswith("[IMG:"):
            alt = lines[name_idx - 1][5:-1].strip()
            if len(alt) > len(name) and alt.lower().startswith(name.rstrip(". ").lower()[:15]):
                name = alt
        price = None
        for k in range(i, min(i + 5, len(lines))):
            if k > i and PRICE_STOP.search(lines[k]):
                break
            pm = PRICE.search(lines[k])
            if pm:
                price = float(pm.group(1).replace(",", ""))
                break
        items.append({"name": name.rstrip(". ") if name.endswith("...") else name,
                      "qty": int(m.group(1)), "price": price})
    if not items:
        # Amazon short emails: product name only in the subject
        m = re.search(r'(?:^|:\s*)(\d+)?\s*"(.+?)(?:\.\.\.)?"', subject)
        if m:
            items.append({"name": m.group(2).rstrip('". '), "qty": int(m.group(1) or 1), "price": None})
        else:
            m = re.search(r"(\d+) (?:items?: )?([A-Z][\w &]+?) items?$|(\d+) items?: (.+)$", subject)
            if m:
                qty = int(m.group(1) or m.group(3))
                items.append({"name": f"{(m.group(2) or m.group(4)).strip()} (unnamed)", "qty": qty, "price": None})
    return items


def name_tokens(name):
    """Comparable word set for product names ("Pokémon Trading Card Game" == "Pokémon TCG")."""
    s = name.lower().replace("é", "e").replace("trading card game", "tcg")
    return set(re.findall(r"[a-z0-9]+", s)) - {"the", "and", "of", "with", "a", "x"}


def names_match(a, b, threshold=0.6):
    a_n, b_n = re.sub(r"\W+", "", a.lower()), re.sub(r"\W+", "", b.lower())
    short = min(len(a_n), len(b_n))
    if short >= 12 and (a_n.startswith(b_n[:short]) or b_n.startswith(a_n[:short])):
        return True
    ta, tb = name_tokens(a), name_tokens(b)
    return bool(ta and tb) and len(ta & tb) / len(ta | tb) >= threshold


def parse_images(html):
    """(alt, src) of product photos in an email. Logos, banners and tracking pixels are skipped."""
    out = []
    for tag in re.findall(r"(?is)<img\b[^>]*>", html or ""):
        src = re.search(r'(?s)\ssrc\s*=\s*(["\'])(.+?)\1', tag)
        alt = re.search(r'(?s)\salt\s*=\s*(["\'])(.*?)\1', tag)
        if not src or not alt or not PRODUCT_IMG.search(src.group(2)):
            continue
        name = unescape(alt.group(2)).strip()
        name = re.sub(r"^Image of\s+", "", name).rstrip(". ")
        if len(name) >= 4:
            out.append((name, product_img_url(unescape(src.group(2)))))
    return out


def product_img_url(src):
    """Ask the retailer CDN for a ~200px thumbnail."""
    if "media-amazon.com/images/I/" in src:
        return re.sub(r"\._[A-Z]{2}\d+_\.", "._SS200_.", src)
    if "scene7.com/is/image/" in src:
        return src.split("?")[0] + "?wid=200&hei=200&fmt=webp&qlt=80"
    if "cdn.shopify.com/" in src:
        return src + ("&" if "?" in src else "?") + "width=200"
    return src


def attach_images(items, images):
    for it in items:
        for alt, src in images:
            if names_match(it["name"], alt):
                it["img"] = src
                break


def carrier_for(num):
    if num.startswith("1Z"):
        return "UPS", f"https://www.ups.com/track?tracknum={num}"
    if num.startswith("TBA"):
        return "Amazon", None
    if re.fullmatch(r"9[2-5]\d{18,24}|[A-Z]{2}\d{9}US", num):
        return "USPS", f"https://tools.usps.com/go/TrackConfirmAction?tLabels={num}"
    if re.fullmatch(r"\d{12}|\d{15}|\d{20}", num):
        return "FedEx", f"https://www.fedex.com/fedextrack/?trknbr={num}"
    return "Carrier", f"https://parcelsapp.com/en/tracking/{num}"


SHIP_LABEL = re.compile(r"^(?:Shipping Address:?|.*delivered soon at this address:)\s*$", re.I)
TARGET_SHIP = re.compile(r"^Delivers to:\s*(.*)$", re.I)
AMAZON_CITY = re.compile(r"^[A-Za-z][\w .'-]* - ([A-Z][A-Z .'-]+, [A-Z]{2})$")
STREET_START = re.compile(r"^\d+\s+\S")
COUNTRY = re.compile(r"^(US|USA|United States)$", re.I)


def tidy_address(parts):
    """'100 example way', 'Testville, Ca 90000' -> '100 Example Way, Testville, CA 90000'."""
    out = ", ".join(p.strip(" ,") for p in parts if p.strip(" ,") and not COUNTRY.match(p.strip(" ,")))
    out = re.sub(r"\b([a-z])([a-z']*)\b", lambda m: m[1].upper() + m[2], out)  # all-lowercase words only
    out = re.sub(r"\bNorth Carolina\b", "NC", out)
    out = re.sub(r"\b([A-Z][a-z])(?=,? \d{5}\b)", lambda m: m[1].upper(), out)  # "Ca 90000" -> "CA 90000"
    return re.sub(r"\b([A-Z]{2}), (\d{5})\b", r"\1 \2", out)  # "CA, 90000" -> "CA 90000"


def parse_ship_to(text):
    """Delivery address as one line (without the recipient's name), or None.
    Mattel / Pokemon Center: labelled block; Target: 'Delivers to:' (confirmations only);
    Amazon: only 'Name - CITY, ST' is in the email."""
    lines = [l.strip() for l in text.splitlines()]
    for i, line in enumerate(lines):
        m = TARGET_SHIP.match(line)
        if m:
            parts = (m[1] or (lines[i + 1] if i + 1 < len(lines) else "")).split(",")
            if parts and not STREET_START.match(parts[0].strip()):
                parts = parts[1:]  # leading recipient name
            return tidy_address(parts) or None
        if SHIP_LABEL.match(line):
            block = []
            for l in lines[i + 1:i + 7]:
                if not l or COUNTRY.match(l) or re.match(r"^(Shipping Method|Billing|Payment|Order Summary|Thank you|My Account)", l, re.I):
                    break
                block.append(l)
            if block and not STREET_START.match(block[0]):
                block = block[1:]  # recipient name
            if block and STREET_START.match(block[0]):
                # Pokemon Center's shipped email splits city / state / zip over lines
                if len(block) >= 4 and re.fullmatch(r"[A-Za-z]{2}", block[-2]) and re.fullmatch(r"\d{5}(-\d{4})?", block[-1]):
                    block = block[:-3] + [f"{block[-3].rstrip(',')}, {block[-2]} {block[-1]}"]
                return tidy_address(block)
        m = AMAZON_CITY.match(line)
        if m:
            city, st = m[1].rsplit(", ", 1)
            return f"{city.title()}, {st} (city only)"
    return None


def parse_email(msg, retailer, subject, status, date):
    html, plain = message_parts(msg)
    text = message_text(msg, html, plain)
    hay = subject + "\n" + text
    order = None
    for pat in ORDER_PATTERNS:
        m = pat.search(hay)
        if m:
            order = m.group(1)
            break
    total = None
    for pat in TOTAL_PATTERNS:
        m = pat.search(text)
        if m:
            total = float(m.group(1).replace(",", ""))
            break
    tracking = []
    for pat in TRACKING_PATTERNS:
        for m in pat.finditer(text):
            num = m.group(1).upper()
            if num != (order or "").replace("-", "") and re.search(r"\d{6}", num) and num not in tracking:
                tracking.append(num)
    eta = None
    for line in text.splitlines():
        m = ETA_LINE.match(line)
        if m:
            eta = m.group(1)
            break
    name = None
    for pat in NAME_PATTERNS:
        m = pat.search(text)
        if m and m.group(1) not in ("Thanks", "Order", "Your", "Get"):
            name = m.group(1)
            break
    placed = PLACED.search(text)
    if retailer == "Amazon" and status == "Item Cancelled" and re.search(r"your order was cancel", text, re.I):
        status = "Cancelled"
    items = parse_items(text, subject)
    attach_images(items, parse_images(html))
    return {
        "retailer": retailer, "date": date, "subject": subject, "status": status,
        "order": order, "items": items, "total": total,
        "tracking": tracking, "eta": eta, "name": name,
        "placed": placed.group(1) if placed else None, "ship_to": parse_ship_to(text),
    }


# ---------------------------------------------------------------- mail fetch
def retailer_of(from_header):
    disp, addr = parseaddr(from_header)
    hay = (disp + " " + addr).lower().replace("é", "e")
    if SKIP_SENDERS.search(addr) or SKIP_SENDERS.search(disp):
        return None
    for name, key in RETAILERS.items():
        if key in hay:
            return name
    return None


def fetch_new(state, days, name):
    acct = ACCOUNTS[name]
    pw = keyring.get_password(acct["keyring"], acct["email"]) if acct["email"] else None
    if not pw:
        return None
    sub = state["accounts"].setdefault(name, {"uidvalidity": None, "last_uid": 0})
    prefix = acct["prefix"]
    mine = (lambda k: k.startswith(prefix)) if prefix else (lambda k: ":" not in k)
    imap = imaplib.IMAP4_SSL(acct["host"], 993)
    imap.login(acct["email"], pw)
    typ, data = imap.select(acct["mailbox"], readonly=True)
    if typ != "OK":
        raise RuntimeError(f"{name}: can't open {acct['mailbox']}")
    uidvalidity = imap.response("UIDVALIDITY")[1][0].decode()
    if sub.get("uidvalidity") != uidvalidity:
        sub.update(uidvalidity=uidvalidity, last_uid=0)
        state["emails"] = {k: v for k, v in state["emails"].items() if not mine(k)}

    since = (datetime.now() - timedelta(days=days)).strftime("%d-%b-%Y")
    senders = " ".join(f'FROM "{k}"' for k in RETAILERS.values())
    query = f"(SINCE {since} {'OR ' * (len(RETAILERS) - 1)}{senders})"
    if sub["last_uid"]:
        query = f"UID {sub['last_uid'] + 1}:* {query}"
    typ, data = imap.uid("SEARCH", None, query)
    uids = [int(u) for u in (data[0] or b"").split()] if typ == "OK" else []
    uids = [u for u in uids if u > sub["last_uid"]]
    logging.info("%s: %d new retailer emails", name, len(uids))

    added = 0
    for i in range(0, len(uids), 200):
        chunk = ",".join(map(str, uids[i:i + 200]))
        typ, data = imap.uid("FETCH", chunk, "(UID BODY.PEEK[HEADER.FIELDS (SUBJECT FROM DATE)])")
        for part in data:
            if not isinstance(part, tuple):
                continue
            uid = int(re.search(rb"UID (\d+)", part[0]).group(1))
            h = email.message_from_bytes(part[1])
            retailer = retailer_of(dec(h["From"]))
            subject = INVISIBLE.sub("", dec(h["Subject"])).replace("\n", " ").strip()
            status = classify(subject) if retailer else None
            if status in (None, "skip"):
                continue
            typ, body = imap.uid("FETCH", str(uid), "(BODY.PEEK[])")
            raw = next((b[1] for b in body if isinstance(b, tuple)), None)
            if not raw:
                continue
            msg = email.message_from_bytes(raw)
            try:
                date = parsedate_to_datetime(h["Date"]).astimezone(timezone.utc).isoformat()
            except Exception:
                date = datetime.now(timezone.utc).isoformat()
            try:
                state["emails"][f"{prefix}{uid}"] = parse_email(msg, retailer, subject, status, date)
                added += 1
            except Exception:
                logging.exception("%s: parse failed for uid %s: %s", name, uid, subject)
    if uids:
        sub["last_uid"] = max(uids)
    imap.logout()
    return added


# ---------------------------------------------------------------- merge
def item_key(name):
    return re.sub(r"[^a-z0-9]", "", name.lower())


def find_item(items, key):
    """Match truncated names ("Fuxury Bubble Mailers 6x10 100 P...") to full ones."""
    if key in items:
        return key
    for k in items:
        if min(len(k), len(key)) >= 12 and (k.startswith(key) or key.startswith(k)):
            return k
    return None


WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def absolute_eta(eta, sent_iso):
    """'tomorrow' / 'Thursday' only make sense relative to the email date."""
    if not eta:
        return eta
    sent = datetime.fromisoformat(sent_iso).astimezone()
    low = eta.lower()
    rest = re.sub(r"^(today|tomorrow|\w+day)\b", "", eta, flags=re.I).strip()
    day = None
    if low.startswith("today"):
        day = sent
    elif low.startswith("tomorrow"):
        day = sent + timedelta(days=1)
    else:
        for i, wd in enumerate(WEEKDAYS):
            if low.startswith(wd):
                day = sent + timedelta(days=(i - sent.weekday()) % 7 or 7)
                break
    if not day:
        return eta
    return (day.strftime("%a, %b ") + str(day.day) + (" " + rest if rest else "")).strip()


def borrow_images(orders):
    """Items whose emails carry no photo (Pokémon Center, some Amazon) reuse the photo of the
    closest-named product seen in any other order."""
    library = {}
    for o in orders:
        for it in o["items"]:
            if it.get("img"):
                library.setdefault(it["name"], it["img"])
    for o in orders:
        for it in o["items"]:
            if it.get("img") or it["name"].endswith("(unnamed)"):
                continue
            best, best_score = None, 0.0
            ti = name_tokens(it["name"])
            for name, img in library.items():
                if not names_match(it["name"], name, threshold=0.7):
                    continue
                tn = name_tokens(name)
                score = len(ti & tn) / len(ti | tn) if ti | tn else 0
                if score > best_score:
                    best, best_score = img, score
            if best:
                it["img"], it["img_borrowed"] = best, True


def build_orders(emails):
    orders, by_tracking, seen = {}, {}, set()
    for e in sorted(emails, key=lambda e: e["date"]):
        # the same email can arrive in both inboxes (forwarding)
        sig = (e["retailer"], e["subject"], e["date"])
        if sig in seen:
            continue
        seen.add(sig)
        key = None
        if e["order"]:
            key = f"{e['retailer']}|{e['order']}"
        else:
            key = next((by_tracking[t] for t in e["tracking"] if t in by_tracking), None)
            if not key:
                key = f"{e['retailer']}|{e['tracking'][0] if e['tracking'] else hashlib.md5(e['subject'].encode()).hexdigest()[:8]}"
        o = orders.setdefault(key, {
            "key": key, "retailer": e["retailer"], "order": e["order"], "status": None, "delayed": False,
            "placed": e["placed"] or e["date"][:10], "updated": e["date"], "eta": None, "total": None,
            "items": {}, "cancelled_items": {}, "tracking": [], "name": None, "history": [], "ship_to": None,
        })
        # latest address wins, but a city-only one (Amazon) never replaces a full one
        if e.get("ship_to") and not (e["ship_to"].endswith("(city only)") and o["ship_to"]):
            o["ship_to"] = e["ship_to"]
        o["history"].append({"date": e["date"], "status": e["status"], "subject": e["subject"]})
        o["updated"] = e["date"]
        o["name"] = o["name"] or e["name"]
        if e["placed"]:
            o["placed"] = e["placed"]
        st = e["status"]
        if st == "Item Cancelled":
            for it in e["items"]:
                k = find_item(o["items"], item_key(it["name"])) or item_key(it["name"])
                o["cancelled_items"][k] = o["cancelled_items"].get(k, 0) + it["qty"]
            o["status"] = o["status"] or "Order Confirmed"
        elif st == "Delayed":
            o["delayed"] = True
            if STATUS_RANK[o["status"] or "Item Cancelled"] < STATUS_RANK["Delayed"]:
                o["status"] = "Delayed"
        elif o["status"] is None or STATUS_RANK[st] >= STATUS_RANK[o["status"]]:
            o["status"] = st
            if STATUS_RANK[st] >= 3:
                o["delayed"] = False
        if e["eta"] and st not in ("Delivered", "Cancelled", "Item Cancelled"):
            o["eta"] = absolute_eta(e["eta"], e["date"])
        if e["total"] and (o["total"] is None or st in ("Order Confirmed", "Pre-order")):
            o["total"] = e["total"]
        for t in e["tracking"]:
            if t not in o["tracking"]:
                o["tracking"].append(t)
                by_tracking[t] = key
        for it in e["items"]:
            k = find_item(o["items"], item_key(it["name"])) or item_key(it["name"])
            cur = o["items"].get(k)
            if not cur:
                o["items"][k] = dict(it)
            else:
                if len(it["name"]) > len(cur["name"]) and not it["name"].endswith("(unnamed)"):
                    cur["name"] = it["name"]
                cur["price"] = cur["price"] or it["price"]
                cur["img"] = cur.get("img") or it.get("img")
                if st in ("Order Confirmed", "Pre-order"):
                    cur["qty"] = it["qty"]
        # Drop "(unnamed)" placeholders once real item names are known
        if any(not v["name"].endswith("(unnamed)") for v in o["items"].values()):
            o["items"] = {k: v for k, v in o["items"].items() if not v["name"].endswith("(unnamed)")}

    out, home = [], redirect_json("target.json", {})
    for o in orders.values():
        items = []
        for k, it in o["items"].items():
            it["cancelled"] = min(o["cancelled_items"].get(k, 0), it["qty"])
            items.append(it)
        if o["status"] != "Cancelled" and items and all(it["cancelled"] >= it["qty"] for it in items):
            o["status"] = "Cancelled"
        o["items"] = items
        o["tracking"] = [{"number": t, "carrier": carrier_for(t)[0], "url": carrier_for(t)[1]} for t in o["tracking"]]
        o["link"] = ORDER_LINKS[o["retailer"]].format(n=o["order"] or "")
        o["away"] = ships_elsewhere(o["ship_to"], home)
        del o["cancelled_items"]
        out.append(o)
    borrow_images(out)
    out.sort(key=lambda o: o["updated"], reverse=True)
    return out


# ---------------------------------------------------------------- shipping redirector
# Thin wrapper around RedirectShipment\redirect.py: the dashboard picks tracking numbers, this
# writes them to its tracking.json and runs it. All carrier logic and safety rules (one attempt
# per package, passcode limits, no fees, destination only from target.json) live in redirect.py.
REDIRECT_DIR = os.path.join(os.path.dirname(HERE), "RedirectShipment")
REDIRECT_PY = os.path.join(REDIRECT_DIR, "redirect.py")
REDIRECT_PYTHON = os.path.join(REDIRECT_DIR, ".venv", "Scripts", "python.exe")
REDIRECT_LOG = os.path.join(DATA_DIR, "redirect.log")
REDIRECT_RUN = os.path.join(DATA_DIR, "redirect_run.json")
REDIRECT_BATCH = 10
# Mirrors DONE_ACTIONS in redirect.py: outcomes the script never retries.
REDIRECT_FINAL = {"already correct", "redirected", "submitted, unconfirmed", "unknown carrier",
                  "needs paid redirect", "set aside: economy service", "redirect not offered"}
REDIRECT_CARRIERS = {"UPS", "FedEx"}
RUN_LINE = re.compile(r"^\[(\d+)/(\d+)\] (UPS|FedEx) (\S+)")
_redirect_proc = None


def redirect_json(name, default):
    try:
        with open(os.path.join(REDIRECT_DIR, name), encoding="utf-8-sig") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def pid_alive(pid):
    import ctypes
    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return False
    code = ctypes.c_ulong()
    ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
    k32.CloseHandle(h)
    return bool(ok) and code.value == 259  # STILL_ACTIVE


def redirect_run_state():
    """The current / last run: running flag, package in progress, WAITING package, log tail."""
    try:
        with open(REDIRECT_RUN, encoding="utf-8") as f:
            run = json.load(f)
    except (OSError, ValueError):
        return None
    if run.get("exit_code") is None:
        if _redirect_proc is not None and _redirect_proc.pid == run.get("pid"):
            code = _redirect_proc.poll()
            if code is not None:
                run["exit_code"] = code
        elif not pid_alive(run.get("pid") or 0):
            run["exit_code"] = "unknown"
        if run.get("exit_code") is not None:
            run["finished"] = datetime.now().astimezone().isoformat(timespec="seconds")
            with open(REDIRECT_RUN, "w", encoding="utf-8") as f:
                json.dump(run, f)
    try:
        with open(REDIRECT_LOG, encoding="utf-8", errors="replace") as f:
            lines = [l.rstrip() for l in f.read().splitlines() if l.strip()]
    except OSError:
        lines = []
    current = waiting = None
    for line in lines:
        m = RUN_LINE.match(line)
        if m:
            current, waiting = {"i": int(m[1]), "n": int(m[2]), "carrier": m[3], "number": m[4]}, None
        elif "WAITING" in line and current:
            waiting = current["number"]
        elif line.strip().startswith(("passcode entered", "->")):
            waiting = None
    running = run.get("exit_code") is None
    run.update(running=running, current=current if running else None, waiting=waiting if running else None,
               log=lines[-60:], stopped=next((l.strip() for l in lines if "STOPPED" in l), None))
    return run


def redirect_snapshot():
    """Everything the dashboard shows about redirects (no addresses beyond the destination)."""
    if not os.path.exists(REDIRECT_PY):
        return {"available": False}
    results = {}
    for r in redirect_json("results.json", []):
        results[r["tracking_number"]] = {k: r.get(k) for k in ("carrier", "action", "detail", "fee", "timestamp")}
    banked = {b["tracking_number"]: b.get("service", "") for b in redirect_json("economy-bank.json", [])}
    queue = redirect_json("tracking.json", [])
    pending = [n for n in queue if (results.get(n) or {}).get("action") not in REDIRECT_FINAL]
    t = redirect_json("target.json", {})
    dest = (f"{t.get('street_number', '')} {t.get('street_name', '')}, {t.get('city', '')}, "
            f"{t.get('state', '')} {t.get('zip', '')}") if t else None
    return {"available": True, "results": results, "banked": banked, "pending": pending, "destination": dest,
            "final": sorted(REDIRECT_FINAL), "batch": REDIRECT_BATCH, "run": redirect_run_state()}


def redirect_start(numbers, known):
    """Queue the chosen tracking numbers and start redirect.py in the background."""
    global _redirect_proc
    run = redirect_run_state()
    if run and run["running"]:
        raise ValueError("a redirect run is already in progress")
    if not os.path.exists(REDIRECT_PYTHON):
        raise ValueError(f"RedirectShipment is not set up ({REDIRECT_PYTHON} missing)")
    if not isinstance(numbers, list) or not numbers or not all(isinstance(n, str) for n in numbers):
        raise ValueError("no tracking numbers selected")
    numbers = list(dict.fromkeys(n.strip().upper() for n in numbers))
    unknown = [n for n in numbers if n not in known]
    if unknown:  # only packages from the user's own orders
        raise ValueError(f"not a UPS/FedEx tracking number from your orders: {', '.join(unknown)}")
    with open(os.path.join(REDIRECT_DIR, "tracking.json"), "w", encoding="utf-8") as f:
        json.dump(numbers, f, indent=2)
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    no_window = 0x08000000
    opened = subprocess.run([REDIRECT_PYTHON, REDIRECT_PY, "--open"], cwd=REDIRECT_DIR, env=env,
                            capture_output=True, text=True, timeout=60, creationflags=no_window)
    if opened.returncode != 0:
        raise ValueError(f"could not open the redirect Chrome window: {(opened.stdout + opened.stderr).strip()}")
    log = open(REDIRECT_LOG, "w", encoding="utf-8")
    _redirect_proc = subprocess.Popen([REDIRECT_PYTHON, "-u", REDIRECT_PY, "--batch", str(REDIRECT_BATCH)],
                                      cwd=REDIRECT_DIR, env=env, stdout=log, stderr=subprocess.STDOUT,
                                      stdin=subprocess.DEVNULL, creationflags=no_window)
    log.close()
    with open(REDIRECT_RUN, "w", encoding="utf-8") as f:
        json.dump({"pid": _redirect_proc.pid, "numbers": numbers, "exit_code": None,
                   "started": datetime.now().astimezone().isoformat(timespec="seconds")}, f)
    logging.info("redirect run started for %d tracking numbers", len(numbers))


def redirect_passcode(number, code):
    """Hand the user's UPS passcode to redirect.py, only for the package that is WAITING."""
    run = redirect_run_state()
    if not run or not run["running"] or not run["waiting"]:
        raise ValueError("no package is waiting for a passcode")
    if number != run["waiting"]:
        raise ValueError(f"{run['waiting']} is the package waiting, not {number}")
    code = re.sub(r"\D", "", str(code))
    if len(code) != 6:
        raise ValueError("the passcode is 6 digits")
    with open(os.path.join(REDIRECT_DIR, "passcode.json"), "w", encoding="utf-8") as f:
        json.dump({number: code}, f)


def ships_elsewhere(ship_to, home=None):
    """True if the order's address isn't home (target.json), False if it is, None if unknown.
    Variants of home's number ('2009 N ...', '2009.9 ...', a unit) still count as home."""
    t = home if home is not None else redirect_json("target.json", {})
    if not ship_to or not t.get("street_number"):
        return None
    if ship_to.endswith("(city only)"):  # Amazon: only the city is known
        return None if ship_to.lower().startswith(t["city"].lower() + ",") else True
    street = r"\s+".join(map(re.escape, t["street_name"].split()[:2]))
    home = re.match(rf"^\D*\b{re.escape(t['street_number'])}(?:\.\d+)?\s+(?:[NSEW]{{1,2}}\.?\s+)?{street}\b", ship_to, re.I)
    zip_ = re.search(r"\b(\d{5})(?:-\d{4})?$", ship_to)
    return not (home and (not zip_ or zip_[1] == t["zip"]))


def away_packages(orders):
    """(order, tracking number or None) for shipped orders headed elsewhere that the redirector
    hasn't already handled."""
    final = {n for n, r in redirect_snapshot().get("results", {}).items() if r["action"] in REDIRECT_FINAL}
    out = []
    for o in orders:
        if o.get("away") and o["status"] in ("Shipped", "Out for Delivery", "Delayed"):
            nums = [t["number"] for t in o["tracking"] if t["number"] not in final]
            out += [(o, n) for n in nums] or ([] if o["tracking"] else [(o, None)])
    return out


def notify_away(state, orders, show=True):
    """One toast per newly shipped package going to an address other than home."""
    current = {f"{o['key']}|{n or ''}" for o, n in away_packages(orders)}
    seen = state.get("away_seen")
    state["away_seen"] = sorted(current | set(seen or []))
    if seen is None or not show:
        return  # first run / dashboard refresh: the dashboard banner shows these
    new = [(o, n) for o, n in away_packages(orders) if f"{o['key']}|{n or ''}" not in seen]
    if not new:
        return
    lines = [f"{o['retailer']} …{(o['order'] or '')[-4:]} → {o['ship_to'].split(',')[0]}"
             + (f" ({carrier_for(n)[0]})" if n else "") for o, n in new[:4]]
    if len(new) > 4:
        lines.append(f"+{len(new) - 4} more")
    toast(f"{len(new)} package{'s' if len(new) > 1 else ''} shipped to another address",
          "\n".join(lines) + "\nOpen the tracker to redirect.")


def redirectable_numbers(orders):
    return {t["number"] for o in orders if o["status"] not in ("Delivered", "Cancelled")
            for t in o["tracking"] if t["carrier"] in REDIRECT_CARRIERS}


# ---------------------------------------------------------------- your own marks on orders
# Pins, notes and "I received this" set from the dashboard. Kept apart from state.json so a
# --rescan never loses them.
META_PATH = os.path.join(DATA_DIR, "order_meta.json")
_meta_lock = threading.Lock()


def load_meta():
    try:
        with open(META_PATH, encoding="utf-8") as f:
            meta = json.load(f)
        return meta if isinstance(meta, dict) else {}
    except (OSError, ValueError):
        return {}


def set_meta(key, patch, known):
    """Merge {pinned, received, note} into one order's marks; empty marks are dropped."""
    if not isinstance(key, str) or key not in known:
        raise ValueError("unknown order")
    if not isinstance(patch, dict):
        raise ValueError("bad request")
    with _meta_lock:
        meta = load_meta()
        cur = dict(meta.get(key) or {})
        for flag in ("pinned", "received"):
            if flag in patch:
                cur[flag] = bool(patch[flag])
        if patch.get("received"):
            cur["received_at"] = datetime.now().astimezone().isoformat(timespec="minutes")
        if "note" in patch:
            cur["note"] = str(patch["note"] or "").strip()[:500]
        cur = {k: v for k, v in cur.items() if v}
        if not cur.get("received"):
            cur.pop("received_at", None)
        if cur:
            meta[key] = cur
        else:
            meta.pop(key, None)
        tmp = META_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=1)
        os.replace(tmp, META_PATH)
        return meta


# ---------------------------------------------------------------- outputs
def write_dashboard(orders, last_error=None, prices=None):
    with open(TEMPLATE_PATH, encoding="utf-8") as f:
        tpl = f.read()
    payload = {
        "generated": datetime.now().astimezone().isoformat(timespec="minutes"),
        "account": " + ".join(a["email"] for a in ACCOUNTS.values()
                              if a["email"] and keyring.get_password(a["keyring"], a["email"])),
        "error": last_error, "orders": orders, "redirect": redirect_snapshot(), "market": prices,
        "meta": load_meta(),
    }
    blob = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    with open(DASHBOARD_PATH, "w", encoding="utf-8") as f:
        f.write(tpl.replace("/*__DATA__*/null", blob))


def default_browser():
    """(progid, exe) of the user's default web browser, read from the http URL association.
    Opening file:/// URLs through the shell can land in other apps registered for .html (e.g. RStudio)."""
    import shlex
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\http\UserChoice") as k:
            progid = winreg.QueryValueEx(k, "ProgId")[0]
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, progid + r"\shell\open\command") as k:
            cmd = winreg.QueryValueEx(k, "")[0]
        exe = shlex.split(cmd, posix=False)[0].strip('"')
        return progid, exe if os.path.exists(exe) else None
    except OSError:
        return None, None


def browser_exe():
    """Preferred browser (BROWSER_EXE) if installed, else the system default."""
    if os.path.exists(BROWSER_EXE):
        return BROWSER_EXE
    return default_browser()[1]


def pythonw_exe():
    exe = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    return exe if os.path.exists(exe) else sys.executable


def ensure_protocol():
    """Register ordertracker: URLs (used by notification clicks) to open the dashboard via --show."""
    import winreg
    base = r"Software\Classes\ordertracker"
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, base) as k:
        winreg.SetValueEx(k, "", 0, winreg.REG_SZ, "URL:Order Tracker")
        winreg.SetValueEx(k, "URL Protocol", 0, winreg.REG_SZ, "")
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, base + r"\shell\open\command") as k:
        winreg.SetValueEx(k, "", 0, winreg.REG_SZ, f'"{pythonw_exe()}" "{os.path.abspath(__file__)}" --show')
    return True


def server_running():
    try:
        with urllib.request.urlopen(SERVE_URL + "ping", timeout=1) as r:
            return r.read() == b"order-tracker"
    except OSError:
        return False


def ensure_server():
    """Start the local dashboard server in the background if it isn't up yet."""
    if server_running():
        return True
    subprocess.Popen([pythonw_exe(), os.path.abspath(__file__), "--serve"], cwd=HERE,
                     creationflags=0x00000008 | 0x00000200 | 0x08000000)  # detached, new group, no window
    for _ in range(40):
        if server_running():
            return True
        threading.Event().wait(0.25)
    return False


def open_dashboard():
    target = SERVE_URL if ensure_server() else DASHBOARD_PATH
    exe = browser_exe()
    if exe:
        subprocess.Popen([exe, target])
    else:
        os.startfile(DASHBOARD_PATH)


def serve():
    """Serve dashboard.html on localhost; POST /refresh checks the inbox and rebuilds it."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    lock, redirect_lock = threading.Lock(), threading.Lock()
    allowed_hosts = {f"127.0.0.1:{SERVE_PORT}", f"localhost:{SERVE_PORT}"}

    class Handler(BaseHTTPRequestHandler):
        def send(self, code, body, ctype):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def host_ok(self):
            if self.headers.get("Host") in allowed_hosts:  # blocks DNS-rebinding reads from other sites
                return True
            self.send(403, b"forbidden", "text/plain")
            return False

        def do_GET(self):
            if not self.host_ok():
                return
            if self.path == "/ping":
                return self.send(200, b"order-tracker", "text/plain")
            if self.path == "/redirect/status":
                return self.send(200, json.dumps(redirect_snapshot()).encode(), "application/json")
            if self.path == "/order/meta":
                return self.send(200, json.dumps(load_meta()).encode(), "application/json")
            if self.path.split("?")[0] not in ("/", "/index.html"):
                return self.send(404, b"not found", "text/plain")
            with open(DASHBOARD_PATH, "rb") as f:
                self.send(200, f.read(), "text/html; charset=utf-8")

        def do_POST(self):
            if not self.host_ok():
                return
            if self.path.startswith("/redirect/"):
                return self.redirect_post()
            if self.path == "/tcg/link":
                return self.tcg_link()
            if self.path == "/order/meta":
                return self.order_meta()
            if self.path != "/refresh":
                return self.send(404, b"not found", "text/plain")
            with lock:
                try:
                    orders, error = refresh(notify=False)
                    body = {"ok": not error, "error": error, "orders": len(orders)}
                except Exception as exc:
                    logging.exception("refresh from dashboard failed")
                    body = {"ok": False, "error": str(exc)}
            self.send(200, json.dumps(body).encode(), "application/json")

        def redirect_post(self):
            # These start real carrier changes: JSON only (other sites can't send it without a
            # CORS preflight this server never answers) and, when sent, our own Origin.
            origin = self.headers.get("Origin")
            if (self.headers.get("Content-Type", "").split(";")[0] != "application/json"
                    or (origin and origin.split("//")[-1] not in allowed_hosts)):
                return self.send(403, b"forbidden", "text/plain")
            try:
                req = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                with redirect_lock:
                    if self.path == "/redirect/start":
                        orders = build_orders(load_state()["emails"].values())
                        redirect_start(req.get("numbers"), redirectable_numbers(orders))
                    elif self.path == "/redirect/passcode":
                        redirect_passcode(req.get("number"), req.get("code"))
                    else:
                        return self.send(404, b"not found", "text/plain")
                body = {"ok": True, **redirect_snapshot()}
            except Exception as exc:
                if not isinstance(exc, ValueError):
                    logging.exception("redirect request failed")
                body = {"ok": False, "error": str(exc)}
            self.send(200, json.dumps(body).encode(), "application/json")

        def order_meta(self):
            """Pin / note / mark-received for one order (data/order_meta.json)."""
            origin = self.headers.get("Origin")
            if (self.headers.get("Content-Type", "").split(";")[0] != "application/json"
                    or (origin and origin.split("//")[-1] not in allowed_hosts)):
                return self.send(403, b"forbidden", "text/plain")
            try:
                req = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                known = {o["key"] for o in build_orders(load_state()["emails"].values())}
                body = {"ok": True, "meta": set_meta(req.get("key"), req.get("patch"), known)}
            except Exception as exc:
                if not isinstance(exc, ValueError):
                    logging.exception("order meta failed")
                body = {"ok": False, "error": str(exc)}
            self.send(200, json.dumps(body).encode(), "application/json")

        def tcg_link(self):
            """Manual TCGplayer match for an item, then rebuild the dashboard from cached emails."""
            origin = self.headers.get("Origin")
            if (self.headers.get("Content-Type", "").split(";")[0] != "application/json"
                    or (origin and origin.split("//")[-1] not in allowed_hosts)):
                return self.send(403, b"forbidden", "text/plain")
            try:
                req = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                names = req.get("names") or []
                if not isinstance(names, list) or not names:
                    raise ValueError("no item given")
                for name in names:
                    market.set_link(str(name), req.get("ref", ""))
                with lock:
                    refresh(offline=True, quiet=True)
                body = {"ok": True}
            except Exception as exc:
                if not isinstance(exc, ValueError):
                    logging.exception("tcg link failed")
                body = {"ok": False, "error": str(exc)}
            self.send(200, json.dumps(body).encode(), "application/json")

        def log_message(self, *a):
            pass

    ThreadingHTTPServer(("127.0.0.1", SERVE_PORT), Handler).serve_forever()


def toast(title, body):
    uri = "ordertracker:open" if ensure_protocol() else "file:///" + DASHBOARD_PATH.replace("\\", "/")
    xml = (f'<toast activationType="protocol" launch="{escape(uri)}"><visual><binding template="ToastGeneric">'
           f"<text>{escape(title)}</text><text>{escape(body)}</text></binding></visual></toast>")
    ps = (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null;"
        "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null;"
        "$x = New-Object Windows.Data.Xml.Dom.XmlDocument; $x.LoadXml('" + xml.replace("'", "''") + "');"
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("
        "'{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe')"
        ".Show([Windows.UI.Notifications.ToastNotification]::new($x))"
    )
    enc = base64.b64encode(ps.encode("utf-16-le")).decode()
    subprocess.run(["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-EncodedCommand", enc],
                   creationflags=0x08000000, timeout=30)


def notify_changes(state, orders):
    previous = state.get("statuses")
    current = {o["key"]: o["status"] for o in orders}
    state["statuses"] = current
    if previous is None:
        return  # first run: nothing to compare against
    changes = [o for o in orders if o["status"] in NOTIFY_STATUSES and previous.get(o["key"]) != o["status"]]
    if not changes:
        return
    lines = []
    for o in changes[:4]:
        first = o["items"][0]["name"][:40] if o["items"] else ""
        lines.append(f"{o['status']}: {o['retailer']} …{(o['order'] or '')[-4:]} {first}")
    if len(changes) > 4:
        lines.append(f"+{len(changes) - 4} more")
    toast(f"{len(changes)} order update{'s' if len(changes) > 1 else ''}", "\n".join(lines))


def print_summary(orders, retailer=""):
    sys.stdout.reconfigure(encoding="utf-8")
    rank = ["Out for Delivery", "Delayed", "Shipped", "Pre-order", "Order Confirmed"]
    meta = load_meta()
    active = [o for o in orders if o["status"] in rank and not (meta.get(o["key"]) or {}).get("received")
              and (not retailer or retailer.lower().replace("é", "e") in o["retailer"].lower().replace("é", "e"))]
    active.sort(key=lambda o: (rank.index(o["status"]), o["updated"]))
    recent = datetime.now(timezone.utc) - timedelta(days=7)
    delivered = [o for o in orders if o["status"] == "Delivered" and o["updated"] >= recent.isoformat()
                 and (not retailer or retailer.lower() in o["retailer"].lower())]
    redirects = redirect_snapshot().get("results", {})
    print(f"\nACTIVE ({len(active)})  |  delivered last 7d: {len(delivered)}")
    for o in active:
        items = "; ".join(f"{i['name'][:50]} x{i['qty'] - i.get('cancelled', 0)}"
                          + (f" @${i['price']:.2f}" if i["price"] else "") for i in o["items"])
        track = ", ".join(f"{t['carrier']} {t['number']}"
                          + (f" [redirect: {redirects[t['number']]['action']}]" if t["number"] in redirects else "")
                          for t in o["tracking"])
        total = f"${o['total']:.2f}" if o["total"] else "-"
        print(f"- {o['status']}{' (delayed)' if o['delayed'] and o['status'] != 'Delayed' else ''} | "
              f"{o['retailer']} #{o['order']} | {total} | eta {o['eta'] or '-'} | upd {o['updated'][:10]}"
              f"{' | ' + o['name'] if o['name'] else ''}\n    {items}{chr(10) + '    ' + track if track else ''}"
              + (f"{chr(10)}    note: {meta[o['key']]['note']}" if (meta.get(o["key"]) or {}).get("note") else ""))


def load_state():
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            state = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        state = {"emails": {}}
    if "accounts" not in state:  # pre-Gmail state: top-level UID bookkeeping was iCloud's
        state["accounts"] = {"icloud": {"uidvalidity": state.pop("uidvalidity", None),
                                        "last_uid": state.pop("last_uid", 0)}}
    return state


def save_state(state):
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)
    os.replace(tmp, STATE_PATH)


def refresh(rescan=False, days=DEFAULT_DAYS, offline=False, quiet=False, notify=True):
    """Check the inbox, rebuild dashboard.html, return (orders, error).
    notify=False (dashboard button) skips toasts but marks current statuses as seen."""
    state = load_state()
    if rescan:
        state.update(accounts={}, emails={})
    errors = []
    for name in ([] if offline else ACCOUNTS):
        try:
            added = fetch_new(state, days, name)
            if added is not None:
                logging.info("%s: parsed %d order emails", name, added)
        except Exception as exc:
            logging.exception("%s fetch failed", name)
            errors.append(f"{name}: {exc}")
    error = f"Last refresh failed: {'; '.join(errors)}" if errors else None
    if error and not quiet:
        try:
            toast("Order Tracker couldn't refresh", error[:120])
        except Exception:
            pass

    cutoff = (datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)).isoformat()
    state["emails"] = {u: e for u, e in state["emails"].items() if e["date"] >= cutoff}
    orders = build_orders(state["emails"].values())
    if not error and not notify:
        state["statuses"] = {o["key"]: o["status"] for o in orders}
        notify_away(state, orders, show=False)
    elif not quiet and not error:
        try:
            notify_changes(state, orders)
            notify_away(state, orders)
        except Exception:
            logging.exception("notify failed")
    else:
        state.setdefault("statuses", {o["key"]: o["status"] for o in orders})
    save_state(state)
    prices = market.attach_market(orders)  # network trouble only means stale prices
    write_dashboard(orders, error, prices)
    return orders, error


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=DEFAULT_DAYS, help="lookback for the first / re-scan")
    ap.add_argument("--rescan", action="store_true")
    ap.add_argument("--open", action="store_true")
    ap.add_argument("--set-password", nargs="?", const="icloud", choices=list(ACCOUNTS), metavar="ACCOUNT",
                    help="store an app password (icloud or gmail)")
    ap.add_argument("--quiet", action="store_true", help="no notifications")
    ap.add_argument("--summary", nargs="?", const="", metavar="RETAILER",
                    help="print active orders as text (optionally only one retailer)")
    ap.add_argument("--offline", action="store_true", help="skip the inbox check, use cached emails")
    ap.add_argument("--show", action="store_true", help="just open the dashboard (starts the local server)")
    ap.add_argument("--serve", action="store_true", help="run the local dashboard server")
    args = ap.parse_args()

    os.makedirs(DATA_DIR, exist_ok=True)
    logging.basicConfig(filename=LOG_PATH, level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.set_password:
        acct = ACCOUNTS[args.set_password]
        if not acct["email"]:
            sys.exit(f'Add your "{args.set_password}" address to accounts.json first (see accounts.example.json).')
        pw = getpass.getpass(f"{acct['label']} for {acct['email']}: ").replace(" ", "").strip()
        keyring.set_password(acct["keyring"], acct["email"], pw)
        print("Saved to Windows Credential Manager.")
        return
    if args.serve:
        serve()
        return
    if args.show:
        if not os.path.exists(DASHBOARD_PATH):
            refresh(offline=True, quiet=True)
        open_dashboard()
        return

    orders, error = refresh(args.rescan, args.days, args.offline, args.quiet)

    active = sum(o["status"] not in ("Delivered", "Cancelled") for o in orders)
    print(f"{len(orders)} orders ({active} active). Dashboard: {DASHBOARD_PATH}")
    if args.summary is not None:
        print_summary(orders, args.summary)
    if error:
        print(error)
    if args.open:
        open_dashboard()


if __name__ == "__main__":
    main()
