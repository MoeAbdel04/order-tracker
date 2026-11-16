"""TCGplayer market prices for the card products in your orders.

Data comes from tcgcsv.com, a public mirror of TCGplayer's catalog and prices that refreshes
once a day (around 20:00 UTC). TCGplayer's own API is closed to new developers.

    catalog  data/tcg_catalog.json  sealed products of recent Pokemon / One Piece / Lorcana sets
    prices   data/tcg_prices.json   market price per product per day (history builds up from the
                                    first run; tcgcsv only serves the current day)
    links    data/tcg_links.json    manual matches set from the dashboard (item name -> product ids)
"""
import difflib
import json
import logging
import os
import re
import time
import unicodedata
import urllib.request
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "data")
CATALOG_PATH = os.path.join(DATA_DIR, "tcg_catalog.json")
PRICES_PATH = os.path.join(DATA_DIR, "tcg_prices.json")
LINKS_PATH = os.path.join(DATA_DIR, "tcg_links.json")
BASE = "https://tcgcsv.com/tcgplayer"
UA = {"User-Agent": "order-tracker/1.0 (personal price tracker)"}
CATEGORIES = {3: "Pokemon", 68: "One Piece", 71: "Lorcana"}
CATALOG_MAX_AGE = timedelta(days=2)   # new products (presales) show up between refreshes
CATALOG_YEARS = 4
HISTORY_DAYS = 120

CARD_ITEM = re.compile(r"pok[eé]mon|\btcg\b|trading card game|one piece card|lorcana", re.I)
NOT_TCG = re.compile(r"magazine|topps|bowman|panini|upper deck", re.I)
NOT_SEALED_ITEM = re.compile(r"^code card|\b(case|display)\b", re.I)  # only matched when the item says so
ALTERNATIVES = re.compile(r"\(([^()]*\bor\b[^()]*)\)", re.I)  # "(Sylveon or Greninja)"
# Retailer wording -> TCGplayer wording, applied before tokenizing
REWRITES = [
    (r"\(\s*\d+\s*packs?\s*\)", " "),
    (r"booster bundle box", "booster bundle"),
    (r"(three|3)[- ]booster blister", "3 pack blister"),
    (r"\b\d+\s*pk\b", " "),
    (r"&", " and "),
    (r"\bvolume\b", "vol"),
]
# Words that name the kind of product: missing one means a different product
KIND = {"booster", "bundle", "box", "pack", "elite", "trainer", "collection", "premium", "ultra", "super", "tin",
        "mini", "blister", "display", "case", "sticker", "figure", "binder", "poster", "deck", "battle", "build",
        "illustration", "checklane", "sleeved", "half", "center", "vol", "chest", "trove", "gift", "starter"}
WEIGHT = {"ex": 0.5}
STOP = {"pokemon", "trading", "card", "game", "tcg", "the", "of", "and", "or", "styles", "style", "may", "vary",
        "a", "with", "mega", "evolution", "scarlet", "violet", "sv", "me", "international", "version", "retail",
        "exclusive", "one", "piece", "disney", "lorcana", "for", "new", "set"}


# ---------------------------------------------------------------- helpers
def _load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _save(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)


def _get(url, raw=False):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        body = r.read()
    return body.decode("utf-8") if raw else json.loads(body)["results"]


def tokens(name):
    s = name.lower()
    for pat, rep in REWRITES:
        s = re.sub(pat, rep, s)
    s = re.sub(r"[^\w]+", " ", s)  # before dropping accents: "Evolution—Delta" stays two words
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    out = set()
    for t in re.findall(r"[a-z0-9]+", s):
        if len(t) > 3 and t.endswith("s") and not t.endswith("ss"):
            t = t[:-1]
        if t not in STOP:
            out.add(t)
    return out


def _covered(tok, itoks):
    if tok in itoks:
        return True
    # retailer typos ("Fraligatr") and joined words
    return len(tok) >= 5 and any(len(i) >= 5 and difflib.SequenceMatcher(None, tok, i).ratio() >= 0.85 for i in itoks)


def is_card_item(name):
    return bool(CARD_ITEM.search(name)) and not NOT_TCG.search(name) and not name.endswith("(unnamed)")


def category_of(name):
    n = name.lower()
    return 68 if "one piece" in n else 71 if "lorcana" in n else 3


# ---------------------------------------------------------------- catalog / prices
def update_catalog(force=False):
    """Refresh the sealed-product catalog (every couple of days; only changed sets are refetched)."""
    cat = _load(CATALOG_PATH, {"fetched": None, "groups": {}, "products": {}})
    if not force and cat["fetched"] and datetime.fromisoformat(cat["fetched"]) > datetime.now(timezone.utc) - CATALOG_MAX_AGE:
        return cat
    cutoff = (datetime.now(timezone.utc) - timedelta(days=365 * CATALOG_YEARS)).isoformat()[:10]
    groups, products = {}, {}
    for cid in CATEGORIES:
        for g in _get(f"{BASE}/{cid}/groups"):
            # promo / misc sets are old but keep getting new sealed products (e.g. One Piece Illustration Boxes)
            if g["publishedOn"][:10] < cutoff and not re.search(r"promo|misc|exclusive|collection", g["name"], re.I):
                continue
            gid = str(g["groupId"])
            groups[gid] = {"name": g["name"], "cid": cid, "modified": g["modifiedOn"]}
            old = cat["groups"].get(gid)
            if old and old["modified"] == g["modifiedOn"]:
                products.update({k: v for k, v in cat["products"].items() if v["gid"] == gid})
                continue
            for p in _get(f"{BASE}/{cid}/{gid}/products"):
                if any(e["name"] in ("Number", "Rarity") for e in p.get("extendedData", [])):
                    continue  # single cards
                products[str(p["productId"])] = {"name": p["name"], "gid": gid, "cid": cid,
                                                 "url": p["url"], "img": p["imageUrl"]}
            time.sleep(0.15)
    cat = {"fetched": datetime.now(timezone.utc).isoformat(), "groups": groups, "products": products}
    _save(CATALOG_PATH, cat)
    logging.info("tcg catalog: %d sets, %d sealed products", len(groups), len(products))
    return cat


def update_prices(cat, pids):
    """Fetch today's prices for the sets the matched products are in (once per tcgcsv update)."""
    pr = _load(PRICES_PATH, {"stamp": None, "groups": {}, "history": {}, "detail": {}})
    stamp = _get("https://tcgcsv.com/last-updated.txt", raw=True).strip()
    day = stamp[:10]
    need = {cat["products"][p]["gid"] for p in pids if p in cat["products"]}
    for gid in need:
        if pr["groups"].get(gid) == stamp:
            continue
        cid = cat["groups"][gid]["cid"]
        for row in _get(f"{BASE}/{cid}/{gid}/prices"):
            pid = str(row["productId"])
            if pid not in cat["products"] or row.get("marketPrice") is None:
                continue
            pr["history"].setdefault(pid, {})[day] = row["marketPrice"]
            pr["detail"][pid] = {k: row.get(k) for k in ("lowPrice", "midPrice", "marketPrice")}
        pr["groups"][gid] = stamp
        time.sleep(0.15)
    keep = (datetime.now(timezone.utc) - timedelta(days=HISTORY_DAYS)).isoformat()[:10]
    pr["history"] = {p: {d: v for d, v in h.items() if d >= keep} for p, h in pr["history"].items()}
    pr["stamp"] = stamp
    _save(PRICES_PATH, pr)
    return pr


# ---------------------------------------------------------------- matching
def _weight(t):
    return 2 if t in KIND else WEIGHT.get(t, 1)


def _best(name, cat, cid, allow_bulk):
    itoks = tokens(name)
    scored = []
    for pid, p in cat["products"].items():
        if p["cid"] != cid or p["name"].startswith("Code Card") or (NOT_SEALED_ITEM.search(p["name"]) and not allow_bulk):
            continue
        ptoks = tokens(p["name"])
        hit = [t for t in ptoks if _covered(t, itoks)]
        # at least one word naming the set or product, not only "Booster Bundle"
        if len(hit) < 2 or all(t in KIND for t in hit):
            continue
        cover = sum(map(_weight, hit)) / sum(map(_weight, ptoks))
        if cover >= 0.8:
            scored.append((round(cover, 3), len(hit), pid))
    if not scored:
        return []
    best = max(scored)[:2]
    return [pid for c, h, pid in scored if (c, h) == best]


def match(name, cat):
    """Product ids for an order item: the best-covered sealed product. Ties (the item doesn't say
    which style, or "styles may vary") and "(A or B)" alternatives give several ids; the price
    shown is their average."""
    cid = category_of(name)
    allow_bulk = bool(re.search(r"\b(case|display)\b", name, re.I))
    alt = ALTERNATIVES.search(name)
    names = ([name[:alt.start()] + a + name[alt.end():] for a in re.split(r"\s+or\s+", alt[1], flags=re.I)]
             if alt else [name])
    top = {pid for n in names for pid in _best(n, cat, cid, allow_bulk)}
    # same product sold as retail and international versions: keep the retail one
    by_base = {}
    for p in sorted(top):
        pname = cat["products"][p]["name"]
        base = re.sub(r"\s*\((International|Retail) Version\)", "", pname)
        if base not in by_base or "Retail Version" in pname:
            by_base[base] = p
    return sorted(by_base.values())


def _change(hist, days):
    if not hist:
        return None
    dates = sorted(hist)
    last = dates[-1]
    ref_day = (datetime.fromisoformat(last) - timedelta(days=days)).date().isoformat()
    older = [d for d in dates if d <= ref_day]
    if not older:
        return None
    a, b = hist[older[-1]], hist[last]
    return round((b - a) / a * 100, 1) if a else None


def attach_market(orders):
    """Add item["tcg"] to card items and return the dashboard's market payload.
    Never raises: a network problem only means stale or missing prices."""
    payload = {"stamp": None, "products": {}, "unmatched": [], "error": None}
    try:
        cat = update_catalog()
    except Exception as exc:
        logging.exception("tcg catalog update failed")
        cat = _load(CATALOG_PATH, None)
        payload["error"] = f"TCGplayer catalog: {exc}"
        if not cat:
            return payload
    links = _load(LINKS_PATH, {})
    matches = {}
    for o in orders:
        for it in o["items"]:
            if it["name"] in matches or not is_card_item(it["name"]):
                continue
            manual = links.get(it["name"].lower())
            ids = [i for i in manual["ids"] if i in cat["products"]] if manual else match(it["name"], cat)
            matches[it["name"]] = (ids, bool(manual))
    pids = {p for ids, _ in matches.values() for p in ids}
    try:
        pr = update_prices(cat, pids)
    except Exception as exc:
        logging.exception("tcg price update failed")
        pr = _load(PRICES_PATH, {"stamp": None, "history": {}, "detail": {}})
        payload["error"] = f"TCGplayer prices: {exc}"
    payload["stamp"] = pr.get("stamp")
    for pid in pids:
        p, hist = cat["products"][pid], pr["history"].get(pid, {})
        payload["products"][pid] = {
            "name": p["name"], "set": cat["groups"].get(p["gid"], {}).get("name"), "url": p["url"], "img": p["img"],
            "market": hist[max(hist)] if hist else None, "low": pr["detail"].get(pid, {}).get("lowPrice"),
            "history": sorted(hist.items()), "chg1": _change(hist, 1), "chg7": _change(hist, 7), "chg30": _change(hist, 30),
        }
    for o in orders:
        for it in o["items"]:
            if it["name"] not in matches:
                continue
            ids, manual = matches[it["name"]]
            prices = [payload["products"][p]["market"] for p in ids if payload["products"][p]["market"] is not None]
            if not ids:
                if it["name"] not in payload["unmatched"]:
                    payload["unmatched"].append(it["name"])
                it["tcg"] = {"ids": [], "manual": manual}
                continue
            it["tcg"] = {"ids": ids, "manual": manual,
                         "market": round(sum(prices) / len(prices), 2) if prices else None}
    return payload


def set_link(item_name, ref):
    """Manual match from the dashboard: a TCGplayer product URL / id, or "none" for no match."""
    cat = _load(CATALOG_PATH, {"products": {}})
    links = _load(LINKS_PATH, {})
    if str(ref).strip().lower() in ("", "auto"):
        links.pop(item_name.lower(), None)
    elif str(ref).strip().lower() == "none":
        links[item_name.lower()] = {"ids": []}
    else:
        m = re.search(r"(?:/product/)?(\d{4,})", str(ref))
        if not m:
            raise ValueError("paste a TCGplayer product link (tcgplayer.com/product/...)")
        if m[1] not in cat["products"]:
            raise ValueError("that product isn't in the tracked sealed Pokemon / One Piece / Lorcana sets")
        links[item_name.lower()] = {"ids": [m[1]]}
    _save(LINKS_PATH, links)
