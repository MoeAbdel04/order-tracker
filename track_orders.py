"""Scan an iCloud mailbox (read-only) for order / shipping emails and summarize them.

Usage:
    set ICLOUD_EMAIL and ICLOUD_APP_PASSWORD env vars, then:
    python track_orders.py [--days 30]
"""
import argparse
import csv
import email
import imaplib
import os
import re
import sys
from datetime import datetime, timedelta
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime
from html import unescape

IMAP_HOST = "imap.mail.me.com"
IMAP_PORT = 993

SUBJECT_KEYWORDS = [
    "order", "shipped", "shipping", "shipment", "delivery", "delivered",
    "out for delivery", "tracking", "package", "on its way", "arriving",
    "dispatched", "confirmation",
]

# Checked against the subject first (most specific wins, in order)
SUBJECT_STATUS = [
    ("Delivered", r"items have arrived|please review your latest order|has been delivered|\bdelivered\b"),
    ("Out for delivery", r"arrives? today|out for delivery"),
    ("Delayed", r"delivery estimate update|\bdelay"),
    ("Shipped", r"get ready for something special|has shipped|\bshipped\b|on (its|the) way|shipping update|in transit"),
    ("Pre-order", r"release day is getting closer|preorder payment"),
    ("Cancelled", r"\bcancel"),
    ("Ordered", r"thanks for shopping|here's your order|order (confirmation|confirmed|received)|\bordered\b|\border\b"),
]

# Fallback: scan the body
BODY_STATUS = [
    ("Delivered", r"\bdelivered\b"),
    ("Out for delivery", r"out for delivery"),
    ("Shipped", r"\bshipped\b|on (its|the) way|dispatched|in transit"),
    ("Ordered", r"thanks for your order|thank you for your (order|purchase)"),
]

STATUS_RANK = {"Unknown": 0, "Ordered": 1, "Pre-order": 1, "Delayed": 2, "Shipped": 3,
               "Out for delivery": 4, "Delivered": 5, "Cancelled": 5}
DONE = ("Delivered", "Cancelled")

MARKETING = re.compile(
    r"pre-?order (now|yours|the)|get ready for .* pre-?order|% off|\$\d+ off|free shipping|"
    r"new .*delivery|delivery: |first orders?|prediction trade|apple news|survey|subnet",
    re.I,
)

TRACKING_PATTERNS = {
    "UPS": r"\b1Z[0-9A-Z]{16}\b",
    "USPS": r"\b(9[2345]\d{20,24}|E[A-Z]\d{9}US)\b",
    "FedEx": r"\b(\d{12}|\d{15}|\d{20})\b",
    "Amazon": r"\bTBA\d{9,12}\b",
    "DHL": r"\b(JD\d{18}|\d{10})\b",
}
CARRIER_HINT = {"FedEx": r"fedex", "DHL": r"\bdhl\b"}

ORDER_NUM = re.compile(
    r"order\s*(?:number|no\.?|#|id)?\s*[:#—-]?\s*#?\s*:?\s*"
    r"(INV-[A-Z0-9-]+|[A-Z]{2,4}\d{5,}|\d[\d-]{2,}\d)",
    re.I,
)
DATE_HINT = re.compile(
    r"(?:arriv\w*|deliver\w*|expected|estimated|get it)[^.\n]{0,40}?"
    r"((?:(?:mon|tue|wed|thu|fri|sat|sun)\w*,?\s+)?"
    r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*\.?\s+\d{1,2}\b(?:\s*-\s*\d{1,2})?)",
    re.I,
)


def dec(value):
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def body_text(msg):
    plain, html = [], []
    for part in msg.walk():
        ctype = part.get_content_type()
        if part.get_content_maintype() == "multipart" or part.get_filename():
            continue
        try:
            payload = part.get_payload(decode=True)
            if payload is None:
                continue
            text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        except Exception:
            continue
        if ctype == "text/plain":
            plain.append(text)
        elif ctype == "text/html":
            html.append(text)
    if plain:
        return "\n".join(plain)
    raw = "\n".join(html)
    raw = re.sub(r"(?is)<(script|style).*?</\1>", " ", raw)
    raw = re.sub(r"(?s)<br\s*/?>|</(p|div|tr|li)>", "\n", raw)
    raw = re.sub(r"(?s)<[^>]+>", " ", raw)
    return re.sub(r"[ \t]+", " ", unescape(raw))


def find_tracking(text, exclude=None):
    found = []
    low = text.lower()
    for carrier, pat in TRACKING_PATTERNS.items():
        if carrier in CARRIER_HINT and not re.search(CARRIER_HINT[carrier], low):
            continue  # bare digit patterns only count when the carrier is named
        for m in re.finditer(pat, text):
            num = m.group(0)
            if num != exclude and f"{carrier} {num}" not in found:
                found.append(f"{carrier} {num}")
    return found


def status_of(subject, text):
    for label, pat in SUBJECT_STATUS:
        if re.search(pat, subject, re.I):
            return label
    for label, pat in BODY_STATUS:
        if re.search(pat, text[:3000], re.I):
            return label
    return "Unknown"


def merchant_of(from_header):
    name, addr = email.utils.parseaddr(from_header)
    if name:
        return name
    domain = addr.split("@")[-1]
    return domain.split(".")[-2].title() if "." in domain else domain


def build_search(days):
    since = (datetime.now() - timedelta(days=days)).strftime("%d-%b-%Y")
    terms = [f'SUBJECT "{k}"' for k in SUBJECT_KEYWORDS]
    query = terms[0]
    for t in terms[1:]:
        query = f"OR {query} {t}"
    return f"(SINCE {since} {query})"


def fetch_rows(user, pw, days):
    imap = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
    imap.login(user, pw.strip())
    imap.select("INBOX", readonly=True)

    typ, data = imap.search(None, build_search(days))
    ids = data[0].split() if typ == "OK" else []
    print(f"Found {len(ids)} candidate emails in the last {days} days.")

    rows = []
    for mid in ids:
        typ, msg_data = imap.fetch(mid, "(BODY.PEEK[])")
        if typ != "OK" or not msg_data or not isinstance(msg_data[0], tuple):
            continue
        msg = email.message_from_bytes(msg_data[0][1])
        subject = dec(msg.get("Subject")).replace("‎", "").strip()
        if MARKETING.search(subject):
            continue
        try:
            date = parsedate_to_datetime(msg.get("Date"))
        except Exception:
            date = None
        text = body_text(msg).replace("‎", "")
        order = ORDER_NUM.search(subject) or ORDER_NUM.search(text)
        order_num = order.group(1) if order else ""
        eta = DATE_HINT.search(text)
        rows.append({
            "date": date.strftime("%Y-%m-%d") if date else "",
            "merchant": merchant_of(dec(msg.get("From"))),
            "subject": subject,
            "status": status_of(subject, text),
            "order_number": order_num,
            "eta_hint": eta.group(1).strip() if eta else "",
            "tracking": "; ".join(find_tracking(subject + "\n" + text, exclude=order_num.replace("-", ""))),
        })
    imap.logout()
    return rows


def merge_orders(rows):
    """Merge emails per order: furthest status wins, tracking numbers accumulate."""
    orders = {}
    for r in sorted(rows, key=lambda r: r["date"]):
        if not r["order_number"] and not r["tracking"] and STATUS_RANK[r["status"]] < 3:
            continue  # no order id, no tracking, nothing shipped: probably noise
        key = (r["merchant"], r["order_number"] or r["subject"])
        o = orders.setdefault(key, {**r, "status": "Unknown", "ordered_on": "", "last_update": r["date"],
                                    "eta_hint": "", "tracking": []})
        if STATUS_RANK[r["status"]] >= STATUS_RANK[o["status"]]:
            o["status"] = r["status"]
            o["last_update"] = r["date"]
        if r["status"] in ("Ordered", "Pre-order") and not o["ordered_on"]:
            o["ordered_on"] = r["date"]
        if r["eta_hint"] and r["status"] in ("Ordered", "Pre-order", "Shipped", "Delayed"):
            o["eta_hint"] = r["eta_hint"]
        for t in filter(None, r["tracking"].split("; ")):
            if t not in o["tracking"]:
                o["tracking"].append(t)
    return list(orders.values())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--outdir", default=os.path.dirname(os.path.abspath(__file__)))
    args = ap.parse_args()

    user = os.environ.get("ICLOUD_EMAIL")
    pw = os.environ.get("ICLOUD_APP_PASSWORD")
    if not user or not pw:
        sys.exit("Set ICLOUD_EMAIL and ICLOUD_APP_PASSWORD environment variables first.")

    rows = fetch_rows(user, pw, args.days)
    rows.sort(key=lambda r: r["date"], reverse=True)
    emails_path = os.path.join(args.outdir, "orders_emails.csv")
    with open(emails_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["date", "merchant", "subject", "status", "order_number", "eta_hint", "tracking"])
        w.writeheader()
        w.writerows(rows)

    orders = sorted(merge_orders(rows), key=lambda o: o["last_update"], reverse=True)
    summary_path = os.path.join(args.outdir, "orders_summary.csv")
    with open(summary_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["merchant", "order_number", "status", "ordered_on", "last_update", "eta_hint", "tracking", "subject"])
        for o in orders:
            w.writerow([o["merchant"], o["order_number"], o["status"], o["ordered_on"], o["last_update"],
                        o["eta_hint"], "; ".join(o["tracking"]), o["subject"]])

    active = sorted((o for o in orders if o["status"] not in DONE),
                    key=lambda o: (-STATUS_RANK[o["status"]], o["last_update"]))
    delivered = [o for o in orders if o["status"] == "Delivered"]
    print(f"\n=== Still on the way: {len(active)}  |  Delivered: {len(delivered)} ===")
    for o in active:
        line = f"- [{o['status']}] {o['merchant']} #{o['order_number'] or '-'}  (updated {o['last_update']})"
        if o["eta_hint"]:
            line += f"  eta {o['eta_hint']}"
        if o["tracking"]:
            line += "  " + ", ".join(o["tracking"])
        print(line)
        if not o["order_number"]:
            print(f"    {o['subject'][:80]}")
    print(f"\nPer-order summary: {summary_path}\nAll emails:        {emails_path}")


if __name__ == "__main__":
    main()
