# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Storage layer for the outboard price monitor (stdlib only)."""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("OUTBOARD_DB", os.path.join(APP_DIR, "prices.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    id           INTEGER PRIMARY KEY,
    label        TEXT    NOT NULL,
    dealer       TEXT,
    brand        TEXT,
    hp           REAL,
    shaft        TEXT,
    url          TEXT    NOT NULL UNIQUE,
    rule         TEXT    NOT NULL DEFAULT 'auto',
    currency     TEXT    NOT NULL DEFAULT 'USD',
    target_price REAL,
    active       INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT    NOT NULL,
    notes        TEXT,
    model_code   TEXT          -- manufacturer code, e.g. DF6AS, MFS6DDS, BF5DH, F6CMHS
);

CREATE TABLE IF NOT EXISTS prices (
    id         INTEGER PRIMARY KEY,
    listing_id INTEGER NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
    price      REAL,
    currency   TEXT,
    status     TEXT    NOT NULL,
    detail     TEXT,
    checked_at TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_prices_listing ON prices(listing_id, checked_at);

CREATE TABLE IF NOT EXISTS alerts (
    id         INTEGER PRIMARY KEY,
    listing_id INTEGER REFERENCES listings(id) ON DELETE CASCADE,
    kind       TEXT    NOT NULL,
    message    TEXT    NOT NULL,
    old_price  REAL,
    new_price  REAL,
    created_at TEXT    NOT NULL,
    seen       INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- Reputation notes per brand (or brand+hp), shown beside each listing.
CREATE TABLE IF NOT EXISTS reviews (
    brand          TEXT NOT NULL,
    hp             REAL,          -- NULL = applies to the whole brand
    verdict        TEXT,          -- one-word summary, e.g. "excellent", "budget"
    summary        TEXT,
    warranty       TEXT,          -- term and conditions, in prose
    corrosion      TEXT,          -- what salt corrosion cover there is
    warranty_years INTEGER,       -- for the cost-of-ownership sums
    dealer_service TEXT,          -- yes|no|partial|unknown - is dealer servicing required
    source         TEXT,
    updated_at     TEXT,
    PRIMARY KEY (brand, hp)
);

-- Delivery terms per dealer, so listings compare on landed cost, not sticker price.
CREATE TABLE IF NOT EXISTS delivery (
    dealer     TEXT PRIMARY KEY,
    kind       TEXT NOT NULL DEFAULT 'quote',   -- flat|free|threshold|collect|quote
    amount     REAL,        -- flat fee, or fee below the threshold
    free_over  REAL,        -- order value at/above which delivery is free
    miles      REAL,        -- road distance from you, for collection costing
    note       TEXT,
    source     TEXT,
    updated_at TEXT
);
"""

DEFAULT_SETTINGS = {
    "drop_alert_pct": "1.0",     # alert when a price falls at least this %
    "min_plausible": "400",      # ignore "prices" below this (accessories, shipping)
    "max_plausible": "150000",   # ignore absurd numbers (phone numbers, part codes)
    "respect_robots": "1",
    "populate_workers": "10",  # dealers crawled at once by the dashboard button
    "min_host_interval": "4",  # seconds between requests to the SAME dealer
    "notify_macos": "1",
    "postcode": "",            # your postcode, for delivery quotes
    "delivery_city": "you",    # shown in column headings
    "budget": "",              # alert when a motor's DELIVERED price falls to/below this
    "min_hp": "",              # ignore anything smaller than this
    "max_hp": "",              # optional upper bound
    "shaft": "",               # S, L, XL, UL - blank means any
    "service_year1": "",       # dealer service cost, first year
    "service_year_n": "",      # dealer service cost, each year after
    "own_years": "5",          # how long you plan to keep it
    "travel_per_mile": "0.25", # running cost per mile, applied to the ROUND trip
    "road_factor": "1.25",     # straight-line -> road, only if routing is down
    "max_travel_miles": "150", # how far you will drive to collect
    "free_collect": "",        # comma-separated dealers you would collect from anyway
}


# Describes every setting so the CLI, the dashboard and the setup flow all agree
# on what it means, what type it is, and where it belongs.
SETTINGS_SPEC = [
    ("What you are shopping for", [
        ("budget", "Budget", "number",
         "The most you want to pay, ALL IN - price plus delivery, or plus the fuel "
         "to go and collect. Motors at or under it are flagged, and you are alerted "
         "the moment one falls into range. e.g. 1100"),
        ("min_hp", "Minimum HP", "number",
         "Smaller motors are hidden. Set both this and the maximum to the same "
         "number to track one size only. Blank for no limit. e.g. 6"),
        ("max_hp", "Maximum HP", "number",
         "Larger motors are hidden. Blank for no limit. e.g. 6"),
        ("shaft", "Shaft length", "choice:|S|L|XL|UL",
         "Match your boat's transom: S short (15in, most tenders and small "
         "dinghies), L long (20in), XL extra-long (25in), UL ultra-long. Blank "
         "accepts any. Listings that do not state a shaft are always kept, since "
         "most dealers leave it off."),
    ]),
    ("Where you are", [
        ("delivery_city", "Your town or city", "text",
         "A label only - it heads the price column, e.g. 'Delivered to Walkden'. "
         "Nothing is calculated from it."),
        ("postcode", "Your postcode", "text",
         "This one does real work: it sets how far every dealer is from you, so "
         "collection can be costed and dealers beyond your driving limit are "
         "marked delivery-only. Looked up via postcodes.io (free, no key, UK "
         "only). e.g. M28 7JF"),
    ]),
    ("Collecting in person", [
        ("max_travel_miles", "Furthest you will drive", "number",
         "ONE-WAY miles. Dealers further than this are never costed as collection, "
         "only delivery. e.g. 50"),
        ("travel_per_mile", "Running cost per mile", "number",
         "Your fuel and wear per mile, charged on the ROUND trip - so 0.15 on a "
         "dealer 40 miles away costs 80 x 0.15 = 12 pounds to collect. e.g. 0.15"),
        ("road_factor", "Straight-line to road multiplier", "number",
         "Only used if the routing service cannot be reached. Distances are "
         "normally a real driving route. On real UK journeys the old 1.25 came "
         "out 6-12% short, so 1.35 is a safer guess when falling back."),
        ("free_collect", "Dealers you would collect from anyway", "text",
         "Comma-separated dealer names you pass anyway, so collecting costs you "
         "nothing extra - no detour. e.g. SSI Marine, Dulas Boats"),
    ]),
    ("Cost of ownership", [
        ("service_year1", "Dealer service, first year", "number",
         "Most warranties are void without dealer servicing, so this is a real "
         "cost of owning it. Added to the lifetime total. Blank to ignore."),
        ("service_year_n", "Dealer service, each later year", "number",
         "Charged for every year after the first, for as long as you keep it."),
        ("own_years", "How long you will keep it", "number",
         "How many years of servicing to count into the total cost. e.g. 10"),
    ]),
    ("Alerts", [
        ("drop_alert_pct", "Notify on drops of at least", "number",
         "Percent. A 1 here means a 1000 pound motor must fall by 10 pounds "
         "before you hear about it - it keeps pennies of rounding quiet. e.g. 1"),
        ("notify_macos", "Desktop notifications", "bool",
         "Pop up a macOS notification on a price drop or a motor coming into "
         "budget. Ignored on Linux."),
    ]),
    ("Scraping", [
        ("min_plausible", "Ignore prices below", "number",
         "A floor, so propellers, spares and 'from 39 pounds a month' finance "
         "figures are not mistaken for the motor. e.g. 400"),
        ("max_plausible", "Ignore prices above", "number",
         "A ceiling, so phone numbers and part codes on the page are not read as "
         "prices. e.g. 150000"),
        ("populate_workers", "Dealers to crawl at once", "number",
         "How many different dealers Populate works through in parallel. Each "
         "site is still fetched at the rate below, so this costs no single "
         "dealer anything - it only shortens the wall-clock. 10 is about five "
         "minutes for the full list; 1 is nearly an hour."),
        ("min_host_interval", "Seconds between requests to one dealer", "number",
         "The politeness rate. Four seconds is gentle on the small businesses "
         "this fetches from. Lowering it makes you a heavier guest on their "
         "server without making the whole run much quicker, since dealers are "
         "already fetched in parallel."),
        ("respect_robots", "Honour robots.txt", "bool",
         "Skip pages a dealer's robots.txt asks automated tools not to fetch. "
         "Leave this on: most of these are small businesses on modest hosting, "
         "and it is their stated wish."),
    ]),
]


TRUE_WORDS = ("1", "y", "yes", "on", "true", "t")
FALSE_WORDS = ("0", "n", "no", "off", "false", "f")


def parse_bool(answer):
    """'yes'/'y'/'on'/'1' -> '1', 'no'/'n'/'off'/'0' -> '0', anything else None.

    Settings are stored as "1"/"0" so everything reading them keeps working;
    only how we ASK changes.
    """
    text = (answer or "").strip().lower()
    if text in TRUE_WORDS:
        return "1"
    if text in FALSE_WORDS:
        return "0"
    return None


def bool_label(value):
    """How a stored '1'/'0' should read back to a person."""
    return "yes" if str(value).strip() in TRUE_WORDS else "no"


def settings_spec_flat():
    for group, items in SETTINGS_SPEC:
        for key, label, kind, help_text in items:
            yield group, key, label, kind, help_text


def is_configured(conn) -> bool:
    """Has anyone set this up yet, or is it a fresh clone?"""
    if conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0]:
        return True
    return bool((get_setting(conn, "delivery_city", "") or "").strip() not in ("", "you"))


# Straight-line distance under-reads what you actually drive; roads wander.
ROAD_FACTOR = 1.25          # straight-line -> road miles, when routing is unavailable
_road_factor = [ROAD_FACTOR]

# Public OSRM instances. No key, no signup. Both are TLS 1.3 only, which the
# macOS system Python (LibreSSL 2.8.3) cannot negotiate, so the fetch falls
# back to curl - present on macOS and virtually every Linux.
ROUTING_HOSTS = (
    "https://routing.openstreetmap.de/routed-car",
    "https://router.project-osrm.org",
)


def set_road_factor(value):
    """Override the straight-line multiplier used when routing is unavailable."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return
    if 1.0 <= number <= 3.0:
        _road_factor[0] = number


def route_miles(lat1, lon1, lat2, lon2):
    """Real driving miles between two points, or None if no router answers.

    Asks a public OSRM instance for an actual road route rather than guessing
    from the straight line. Tries urllib first and falls back to curl, because
    the routers require TLS 1.3 and some system Pythons cannot speak it.
    """
    import json as _json
    import subprocess as _subprocess
    import urllib.request as _request
    for host in ROUTING_HOSTS:
        url = ("%s/route/v1/driving/%f,%f;%f,%f?overview=false"
               % (host, lon1, lat1, lon2, lat2))
        raw = None
        try:
            raw = _request.urlopen(_request.Request(
                url, headers={"User-Agent": "outboard-monitor"}), timeout=20).read()
        except Exception:
            try:
                done = _subprocess.run(
                    ["curl", "-sS", "--max-time", "20", "-A", "outboard-monitor", url],
                    capture_output=True, timeout=25)
                raw = done.stdout
            except Exception:
                continue
        try:
            routes = _json.loads(raw).get("routes") or []
            if routes:
                return round(routes[0]["distance"] / 1609.344, 1)
        except Exception:
            continue
    return None


def geocode(postcode):
    """UK postcode -> (lat, lon, district) via postcodes.io. None if not found.

    postcodes.io is free and needs no key. Returns None rather than raising so
    callers can fall back to a hand-entered distance.
    """
    import json as _json
    import scrape as _scrape
    cleaned = (postcode or "").replace(" ", "").upper()
    if not cleaned:
        return None
    try:
        raw = _scrape.fetch("https://api.postcodes.io/postcodes/" + cleaned,
                            respect_robots=False)
        result = _json.loads(raw).get("result") or {}
    except Exception:
        return None
    if not result.get("latitude"):
        return None
    return (result["latitude"], result["longitude"],
            result.get("admin_district") or result.get("region") or "")


def distance_miles(from_postcode, to_postcode, allow_network=True):
    """ROAD miles between two UK postcodes, or None.

    Asks a routing service for the real driving route. Only if no router
    answers does it fall back to the straight line times a fudge factor, which
    on real routes runs 6-12% short. Returns the miles; distance_miles_how
    says which method produced them.
    """
    import math
    a = geocode(from_postcode)
    b = geocode(to_postcode)
    if not a or not b:
        return None
    (lat1, lon1, _), (lat2, lon2, _) = a, b
    if allow_network:
        real = route_miles(lat1, lon1, lat2, lon2)
        if real is not None:
            return real
    radius, rad = 3958.8, math.pi / 180
    h = (math.sin((lat2 - lat1) * rad / 2) ** 2
         + math.cos(lat1 * rad) * math.cos(lat2 * rad)
         * math.sin((lon2 - lon1) * rad / 2) ** 2)
    straight = 2 * radius * math.asin(math.sqrt(h))
    return round(straight * _road_factor[0], 1)


def distance_miles_how(from_postcode, to_postcode):
    """(miles, "route"|"estimate") - the distance and how it was arrived at."""
    a = geocode(from_postcode)
    b = geocode(to_postcode)
    if not a or not b:
        return None, "unknown"
    real = route_miles(a[0], a[1], b[0], b[1])
    if real is not None:
        return real, "route"
    return distance_miles(from_postcode, to_postcode, allow_network=False), "estimate"


def nearby_districts(postcode, limit=30):
    """Council districts near a postcode - useful as local search terms."""
    import json as _json
    import re as _re
    import scrape as _scrape
    # the outward code is everything except the final three characters
    cleaned = (postcode or "").upper().replace(" ", "")
    outcode = cleaned[:-3] if len(cleaned) > 3 else cleaned
    if not _re.match(r"^[A-Z]{1,2}\d{1,2}[A-Z]?$", outcode):
        return []
    try:
        raw = _scrape.fetch("https://api.postcodes.io/outcodes/%s/nearest?limit=%d"
                            % (outcode, limit), respect_robots=False)
        results = _json.loads(raw).get("result") or []
    except Exception:
        return []
    seen = []
    for row in results:
        for value in (row.get("admin_district") or []):
            if value and "unparished" not in value.lower() and value not in seen:
                seen.append(value)
    return seen


# Where dealers usually put their delivery terms. Shopify's first two are
# standard, so they cover most of the trade.
DELIVERY_PAGE_PATHS = (
    "/policies/shipping-policy", "/pages/delivery", "/pages/delivery-payment",
    "/pages/delivery-information", "/pages/shipping", "/delivery",
    "/delivery-information", "/shipping", "/shipping-policy", "/pages/faq",
)

# Almost every "free over £X" offer excludes exactly what we are buying, so
# these words mean the headline figure must not be believed for an outboard.
DELIVERY_EXCLUSION = _re_excl = (
    "bulky", "oversize", "over-size", "heavy", "large item", "large/heavy",
    "outboard", "engine", "excluded", "exclusion", "does not include",
    "does not apply", "surcharge", "pallet", "kerbside", "quotation",
)


def _readable(html):
    """The body text of a policy page, without nav, script or style noise."""
    import re as _re
    for pattern in (r'<div[^>]*class="[^"]*shopify-policy__body[^"]*".*?</div>\s*</div>',
                    r'<div[^>]*class="[^"]*rte[^"]*"[^>]*>(.*?)</div>',
                    r'<main[^>]*>(.*?)</main>'):
        found = _re.search(pattern, html, _re.S | _re.I)
        if found and len(found.group(0)) > 200:
            chunk = found.group(0)
            chunk = _re.sub(r"<script.*?</script>|<style.*?</style>", " ", chunk,
                            flags=_re.S | _re.I)
            chunk = _re.sub(r"<[^>]+>", " ", chunk)
            chunk = _re.sub(r"&nbsp;?", " ", chunk)
            return _re.sub(r"\s+", " ", chunk).strip()
    return ""


def fetch_delivery_policy(site):
    """(text, url) of a dealer's delivery page, or (None, None).

    Follows one "read our policy here" hop, because some shops put a link on
    the policy page rather than the policy itself.
    """
    import re as _re
    import scrape as _scrape
    from urllib.parse import urljoin as _urljoin
    site = site.rstrip("/")
    for path in DELIVERY_PAGE_PATHS:
        url = site + path
        try:
            html = _scrape.fetch(url, respect_robots=True)
        except Exception:
            continue
        text = _readable(html)
        if not text:
            continue
        # a stub that only points somewhere else - follow it once
        if len(text) < 300:
            link = _re.search(r'https?://[^\s"\'<>]*(?:deliver|shipping|postage)[^\s"\'<>]*',
                              html, _re.I)
            if link:
                try:
                    deeper = _readable(_scrape.fetch(link.group(0), respect_robots=True))
                    if deeper and len(deeper) > len(text):
                        return deeper, link.group(0)
                except Exception:
                    pass
        if len(text) > 300:
            return text, url
    return None, None


def read_delivery_terms(text):
    """What a delivery page says, as {free_over, flat, excludes, evidence}.

    `excludes` being true means the page says bulky, heavy or oversized goods
    are treated differently - which an outboard always is. The figures are then
    almost certainly not the ones you would be charged, so they are offered as
    something to check rather than something to apply.
    """
    import re as _re
    if not text:
        return {}
    found = {"free_over": None, "flat": None, "excludes": False, "evidence": []}
    over = _re.search(r"free[^.]{0,60}?(?:over|above)\s*£\s?([\d,]+)", text, _re.I)
    if over:
        try:
            found["free_over"] = float(over.group(1).replace(",", ""))
            found["evidence"].append(over.group(0).strip()[:140])
        except ValueError:
            pass
    flat = _re.search(r"(?:deliver\w*|shipping|postage|carriage)[^.£]{0,40}"
                      r"(?:from|costs?|is|:)?\s*£\s?([\d,]+(?:\.\d\d)?)", text, _re.I)
    if flat:
        try:
            found["flat"] = float(flat.group(1).replace(",", ""))
            found["evidence"].append(flat.group(0).strip()[:140])
        except ValueError:
            pass
    # the "free over £150" sentence often matches the flat pattern too
    if found["flat"] is not None and found["flat"] == found["free_over"]:
        found["flat"] = None
        found["evidence"] = [e for e in found["evidence"]
                             if not e.lower().startswith("delivery we offer")]
    low = text.lower()
    for word in DELIVERY_EXCLUSION:
        if word in low:
            found["excludes"] = True
            spot = _re.search(r"[^.]{0,110}%s[^.]{0,110}\." % _re.escape(word), text, _re.I)
            if spot:
                found["evidence"].append("EXCLUSION: " + spot.group(0).strip()[:150])
            break
    return found


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


def fmt_local(value: str, with_time: bool = True) -> str:
    """Render a stored UTC timestamp in the machine's local timezone."""
    try:
        dt = parse_iso(value).astimezone()
    except (ValueError, TypeError):
        return value or ""
    return dt.strftime("%Y-%m-%d %H:%M" if with_time else "%Y-%m-%d")


def connect(path: str = None) -> sqlite3.Connection:
    conn = sqlite3.connect(path or DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


# Columns added after the first release. Existing databases get them on open,
# so an older prices.db keeps working instead of raising "No item with that key".
MIGRATIONS = [
    ("listings", "model_code", "TEXT"),
    ("delivery", "miles", "REAL"),
    ("delivery", "postcode", "TEXT"),
    ("reviews", "warranty", "TEXT"),
    ("reviews", "corrosion", "TEXT"),
    ("reviews", "warranty_years", "INTEGER"),
    ("reviews", "dealer_service", "TEXT"),
]


def _migrate(conn) -> None:
    for table, column, decl in MIGRATIONS:
        existing = {row[1] for row in conn.execute("PRAGMA table_info(%s)" % table)}
        if column not in existing:
            conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, column, decl))
    conn.commit()


def init_db(path: str = None) -> sqlite3.Connection:
    conn = connect(path)
    conn.executescript(SCHEMA)
    _migrate(conn)
    for key, value in DEFAULT_SETTINGS.items():
        conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (key, value))
    conn.commit()
    return conn


def get_setting(conn, key: str, default=None):
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    if row is None:
        return DEFAULT_SETTINGS.get(key, default)
    return row["value"]


def get_float_setting(conn, key: str, default: float) -> float:
    try:
        return float(get_setting(conn, key, default))
    except (TypeError, ValueError):
        return default


def set_setting(conn, key: str, value) -> None:
    conn.execute(
        "INSERT INTO settings(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )
    conn.commit()


# ---------------------------------------------------------------- listings ---

def add_listing(conn, label, url, dealer=None, brand=None, hp=None, shaft=None,
                rule="auto", currency="USD", target_price=None, notes=None) -> int:
    cur = conn.execute(
        "INSERT INTO listings(label, dealer, brand, hp, shaft, url, rule, currency,"
        " target_price, active, created_at, notes)"
        " VALUES (?,?,?,?,?,?,?,?,?,1,?,?)",
        (label, dealer, brand, hp, shaft, url, rule, currency, target_price, now_iso(), notes),
    )
    conn.commit()
    return cur.lastrowid


def update_listing(conn, listing_id: int, **fields) -> None:
    allowed = {"label", "dealer", "brand", "hp", "shaft", "url", "rule",
               "currency", "target_price", "active", "notes", "model_code"}
    sets, values = [], []
    for key, value in fields.items():
        if key in allowed:
            sets.append("%s = ?" % key)
            values.append(value)
    if not sets:
        return
    values.append(listing_id)
    conn.execute("UPDATE listings SET %s WHERE id = ?" % ", ".join(sets), values)
    conn.commit()


def delete_listing(conn, listing_id: int) -> None:
    conn.execute("DELETE FROM listings WHERE id = ?", (listing_id,))
    conn.commit()


def get_listing(conn, listing_id: int):
    return conn.execute("SELECT * FROM listings WHERE id = ?", (listing_id,)).fetchone()


def listings(conn, active_only: bool = False):
    sql = "SELECT * FROM listings"
    if active_only:
        sql += " WHERE active = 1"
    sql += " ORDER BY brand IS NULL, brand, hp, label"
    return conn.execute(sql).fetchall()


# ------------------------------------------------------------------ prices ---

def record_price(conn, listing_id, price, currency, status, detail=None) -> None:
    conn.execute(
        "INSERT INTO prices(listing_id, price, currency, status, detail, checked_at)"
        " VALUES (?,?,?,?,?,?)",
        (listing_id, price, currency, status, detail, now_iso()),
    )
    conn.commit()


def last_price(conn, listing_id, ok_only: bool = True):
    sql = "SELECT * FROM prices WHERE listing_id = ?"
    if ok_only:
        sql += " AND status = 'ok' AND price IS NOT NULL"
    sql += " ORDER BY checked_at DESC, id DESC LIMIT 1"
    return conn.execute(sql, (listing_id,)).fetchone()


def last_check(conn, listing_id):
    return last_price(conn, listing_id, ok_only=False)


def history(conn, listing_id, ok_only: bool = True, limit: int = 5000):
    sql = "SELECT * FROM prices WHERE listing_id = ?"
    if ok_only:
        sql += " AND status = 'ok' AND price IS NOT NULL"
    sql += " ORDER BY checked_at ASC, id ASC LIMIT ?"
    return conn.execute(sql, (listing_id, limit)).fetchall()


def price_changes(conn, listing_id):
    """History collapsed to points where the price actually moved."""
    out = []
    for row in history(conn, listing_id):
        if not out or abs(out[-1]["price"] - row["price"]) > 0.005:
            out.append(row)
    return out


def stats(conn, listing_id):
    row = conn.execute(
        "SELECT MIN(price) AS lo, MAX(price) AS hi, COUNT(*) AS n, AVG(price) AS avg"
        " FROM prices WHERE listing_id = ? AND status = 'ok' AND price IS NOT NULL",
        (listing_id,),
    ).fetchone()
    return row


# ---------------------------------------------------------------- delivery ---

def set_delivery(conn, dealer, kind, amount=None, free_over=None, note=None, source=None):
    conn.execute(
        "INSERT INTO delivery(dealer, kind, amount, free_over, note, source, updated_at)"
        " VALUES (?,?,?,?,?,?,?)"
        " ON CONFLICT(dealer) DO UPDATE SET kind=excluded.kind, amount=excluded.amount,"
        " free_over=excluded.free_over, note=excluded.note, source=excluded.source,"
        " updated_at=excluded.updated_at",
        (dealer, kind, amount, free_over, note, source, now_iso()))
    conn.commit()


def get_delivery(conn, dealer):
    if not dealer:
        return None
    return conn.execute("SELECT * FROM delivery WHERE dealer = ?", (dealer,)).fetchone()


def all_delivery(conn):
    return conn.execute("SELECT * FROM delivery ORDER BY dealer").fetchall()


def travel_cost(conn, dealer):
    """Cost of driving to collect, or None if it is further than you will go.

    Dealers listed in the free_collect setting cost nothing - you are going anyway.
    """
    if not dealer:
        return None, "unknown dealer"
    free = [d.strip() for d in (get_setting(conn, "free_collect", "") or "").split(",")]
    if dealer in free:
        return 0.0, "collect (no detour)"
    row = get_delivery(conn, dealer)
    miles = row["miles"] if row is not None else None
    try:
        miles = float(miles) if miles is not None else None
    except (TypeError, ValueError):
        miles = None
    if miles is None:
        return None, "distance unknown"
    limit = get_float_setting(conn, "max_travel_miles", 0)
    if limit and miles > limit:
        return None, "%.0f miles - beyond your %.0f mile limit" % (miles, limit)
    rate = get_float_setting(conn, "travel_per_mile", 0)
    return round(miles * 2 * rate, 2), "%.0f mi round trip" % (miles * 2)


def delivery_cost(conn, dealer, price):
    """Delivery for one order. Returns (cost_or_None, short_label).

    None means the cost is not known yet - the caller must not pretend it is zero.
    """
    row = get_delivery(conn, dealer)
    if row is None:
        return None, "not set"
    kind = row["kind"]
    if kind == "free":
        return 0.0, "free"
    if kind == "collect":
        return None, "collection only"
    if kind == "flat":
        return (row["amount"], "flat") if row["amount"] is not None else (None, "flat?")
    if kind == "threshold":
        if row["free_over"] is not None and price is not None and price >= row["free_over"]:
            return 0.0, "free over threshold"
        return (row["amount"], "under threshold") if row["amount"] is not None else (None, "?")
    return None, "quote needed"


def effective_price(conn, dealer, price):
    """Best comparable figure for ranking, plus whether it is complete.

    Returns (amount, is_complete). When delivery is unknown the LIST price is used
    as a lower bound and is_complete is False - so a cheap listing with unquoted
    delivery still ranks near its true position instead of vanishing.
    """
    if price is None:
        return None, False
    land_value, _, _ = landed(conn, dealer, price)
    if land_value is not None:
        return land_value, True
    return price, False


def landed(conn, dealer, price):
    """price + the cheaper of delivery or driving to collect.

    Returns (total, added_cost, label). None total means neither route is known.
    """
    if price is None:
        return None, None, "no price"
    deliver, deliver_label = delivery_cost(conn, dealer, price)
    drive, drive_label = travel_cost(conn, dealer)
    options = [(c, l) for c, l in ((deliver, deliver_label), (drive, drive_label))
               if c is not None]
    if not options:
        return None, None, deliver_label
    cost, label = min(options, key=lambda o: o[0])
    return price + cost, cost, label


# Manufacturer model codes, most specific pattern first.
MODEL_PATTERNS = [
    (r"\bDF\s?(\d{1,3}(?:\.\d)?)\s?([A-Z]{0,6})\b", "DF"),      # Suzuki  DF6AS
    (r"\bMFS\s?(\d{1,3}(?:\.\d)?)\s?([A-Z0-9]{0,6})\b", "MFS"), # Tohatsu MFS6DDS
    (r"\bBF\s?(\d{1,3}(?:\.\d)?)\s?([A-Z]{0,8})\b", "BF"),      # Honda   BF5DH
    (r"\bFT\s?(\d{1,3}(?:\.\d)?)\s?([A-Z]{0,8})\b", "FT"),      # Yamaha high-thrust
    (r"\bF\s?(\d{1,3}(?:\.\d)?)\s?([A-Z]{2,8})\b", "F"),        # Yamaha  F6CMHS
    (r"\bM\s?(\d{1,3}(?:\.\d)?)\s?([A-Z]\d?)\b", "M"),          # Tohatsu 2-stroke M3.5B2
]


# Electric outboards print kW or pounds of thrust in the model name, not HP,
# so a number lifted from their code would be meaningless.
NON_HP_BRANDS = ("epropulsion", "torqeedo", "minn kota", "minn-kota", "haswing")


def hp_from_title(title):
    """Horsepower stated outright in a title: "6hp", "2.5 H.P", "60 BHP".

    Dealers write it every which way, so the periods and spacing in "H.P." are
    all optional. This is the most reliable signal there is - a title that says
    the size is never second-guessed.
    """
    import re as _re
    if not title:
        return None
    match = _re.search(r"(\d{1,3}(?:\.\d)?)\s*(?:h\.?\s*p\.?|bhp)(?![a-z])",
                       title, _re.I)
    if not match:
        return None
    try:
        value = float(match.group(1))
    except ValueError:
        return None
    return value if 1 <= value <= 700 else None


HP_BRANDS = ("mercury", "mariner", "yamaha", "suzuki", "tohatsu", "honda",
             "parsun", "hidea", "selva")


def hp_from_model_text(title):
    """Horsepower read out of a maker's model designation in a title.

    "Mercury 9.9EL", "Mercury F20ML", "Mercury F3.5M" - the number after the
    brand IS the horsepower, with the letters saying shaft length and start
    type. Only a last resort: it is wrong for a title like "3.5HP TOHATSU
    M3.5B2", which hp_from_title reads correctly first.
    """
    import re as _re
    if not title:
        return None
    match = _re.search(r"\b(?:%s)\s+(?:[A-Z]{1,3})?(\d{1,3}(?:\.\d)?)\s*[A-Z]{0,4}\b"
                       % "|".join(HP_BRANDS), title, _re.I)
    if not match:
        return None
    try:
        value = float(match.group(1))
    except ValueError:
        return None
    return value if 1 <= value <= 700 else None


def infer_hp(title, code=None, brand=None):
    """Best guess at horsepower, most trustworthy signal first."""
    return (hp_from_title(title)
            or hp_from_code(code, brand)
            or hp_from_model_text(title))


def hp_from_code(code, brand=None):
    """Infer HP from a manufacturer model code, e.g. DF6AS -> 6, MFS9.8B -> 9.8.

    Outboard codes are a maker's prefix followed by the horsepower: Suzuki DF6,
    Tohatsu MFS9.8, Honda BF5, Yamaha F20. Used only as a fallback when the
    title never says "6hp" outright, which is common on dealer sites that title
    a page just "Suzuki DF6".

    Returns None for electric motors and for anything outside 1-700 HP.
    """
    import re as _re
    if not code:
        return None
    if brand and brand.strip().lower() in NON_HP_BRANDS:
        return None
    match = _re.match(r"^[A-Z]{1,4}(\d{1,3}(?:\.\d)?)", code.strip().upper())
    if not match:
        return None
    try:
        value = float(match.group(1))
    except ValueError:
        return None
    return value if 1 <= value <= 700 else None


def model_code(label, url=""):
    """Pull a manufacturer model code out of a listing title or URL.

    Returns e.g. "DF6AS" / "MFS6DDS" / "BF5DH", or None when the seller only gave
    a vague description ("DF 4 - 5 - 6 Suzuki ~ ALL Models").
    """
    import re as _re
    from urllib.parse import urlparse as _urlparse
    path = ""
    if url:
        # only the path - the scheme and host add noise like "HTTPS" and "CO UK"
        path = _urlparse(url).path.replace("-", " ").replace("/", " ").replace("_", " ")
    label_up = (label or "").upper()
    hay = ("%s %s" % (label_up, path)).upper()
    # a listing naming several sizes is not one model
    if _re.search(r"\d\s*[-–/]\s*\d\s*[-–/]\s*\d|ALL MODELS|OTHER MODELS", hay):
        return None
    # A code printed in the title beats one reconstructed from the URL slug:
    # "Tohatsu MFS6CD S" at /products/tohatsu-mfs6-6hp-... must give MFS6CD, not MFS66HP.
    for source in (label_up, hay):
        best = _best_code(source, _re)
        if best:
            return best
    return None


def _best_code(hay, _re):
    best = None
    for pattern, prefix in MODEL_PATTERNS:
        for m in _re.finditer(pattern, hay):
            size, suffix = m.group(1), (m.group(2) or "")
            if prefix == "F" and not suffix:
                continue
            if suffix in ("HP", "PS", "KW"):      # "BF20HP" is a size, not a code
                suffix = ""
            if _re.match(r"^\d", suffix or ""):   # "MFS6" + "6HP" glued together
                suffix = ""
            code = "%s%s%s" % (prefix, size, suffix)
            # the most specific spelling wins: DF6AL beats a bare DF6
            if best is None or len(code) > len(best):
                best = code
        if best and len(best) > len(_re.match(r"[A-Z]+", best).group(0)) + len(size):
            break
    return best


def model_key(listing):
    """Group variants of the same motor together: brand + horsepower.

    Shaft length is deliberately NOT part of the key, so a short- and long-shaft
    version of one motor collapse to a single row. Falls back to the first word of
    the label when no brand was detected, so own-brand stock (Orca, Hidea) groups too.
    """
    import re as _re
    brand = (listing["brand"] or "").strip().lower()
    if not brand:
        words = _re.findall(r"[A-Za-z][\w.-]+", listing["label"] or "")
        brand = words[0].lower() if words else "?"
    hp = listing["hp"]
    return (brand, round(hp, 1) if hp is not None else None)


def variant_key(listing):
    """Tighter grouping than model_key: the exact manufacturer code when known."""
    try:
        code = listing["model_code"]
    except (IndexError, KeyError):
        code = None
    if code:
        return ("code", code)
    return ("loose",) + model_key(listing)


def cheapest_per_model(conn, listings, price_of):
    """One row per model - the cheapest - plus how many alternatives it beat.

    `listings` must already be sorted cheapest-first. Returns
    [(listing, alternatives_count), ...] preserving that order.
    """
    best = {}
    order = []
    for listing in listings:
        key = model_key(listing)
        if key in best:
            best[key][1] += 1
            continue
        best[key] = [listing, 0]
        order.append(key)
    return [(best[k][0], best[k][1]) for k in order]


def _upsert_review(conn, brand, hp, fields):
    """Write review columns for a brand (hp=None) or a brand+hp."""
    brand = brand.lower()
    sets = ", ".join("%s = ?" % k for k in fields) + ", updated_at = ?"
    values = list(fields.values()) + [now_iso(), brand]
    if hp is None:
        cur = conn.execute("UPDATE reviews SET %s WHERE brand = ? AND hp IS NULL" % sets,
                           values)
    else:
        cur = conn.execute("UPDATE reviews SET %s WHERE brand = ? AND hp = ?" % sets,
                           values + [hp])
    if cur.rowcount == 0:
        cols = ["brand", "hp", "updated_at"] + list(fields)
        conn.execute("INSERT INTO reviews(%s) VALUES (%s)"
                     % (", ".join(cols), ", ".join("?" * len(cols))),
                     [brand, hp, now_iso()] + list(fields.values()))
    conn.commit()


def warranty_terms(conn, brand):
    """(years, dealer_service) for a brand. dealer_service is yes|no|unknown."""
    if not brand:
        return None, "unknown"
    row = conn.execute("SELECT warranty_years, dealer_service FROM reviews"
                       " WHERE brand = ? AND hp IS NULL", (brand.lower(),)).fetchone()
    if not row:
        return None, "unknown"
    return row["warranty_years"], (row["dealer_service"] or "unknown")


def warranty_years(conn, brand):
    return warranty_terms(conn, brand)[0]


def service_cost(conn, years):
    """What dealer servicing costs over `years` years, at your quoted rates."""
    if years is None or years <= 0:
        return 0.0
    first = get_float_setting(conn, "service_year1", 0)
    rest = get_float_setting(conn, "service_year_n", 0)
    return first + rest * (years - 1)


def total_cost(conn, listing, price):
    """Landed price plus dealer servicing for as long as you keep it.

    Returns (total, delivered, servicing, years, note). `total` is None when the
    delivered price is not known.
    """
    delivered, _cost, _label = landed(conn, listing["dealer"], price)
    own = int(get_float_setting(conn, "own_years", 5))
    warranty, needs_dealer = warranty_terms(conn, listing["brand"])
    if needs_dealer == "no":
        # nothing forces you to a dealer, so servicing is not a cost of keeping cover
        years, servicing, note = 0, 0.0, "self-service allowed"
    else:
        years = min(own, warranty) if warranty else own
        servicing = service_cost(conn, years)
        note = "" if needs_dealer == "yes" else "dealer requirement unconfirmed"
    if delivered is None:
        return None, None, servicing, years, (note + "; delivery unknown").strip("; ")
    return delivered + servicing, delivered, servicing, years, note


def set_warranty(conn, brand, text):
    _upsert_review(conn, brand, None, {"warranty": text})


def set_corrosion(conn, brand, text):
    _upsert_review(conn, brand, None, {"corrosion": text})


def set_review(conn, brand, verdict, summary, source=None, hp=None):
    _upsert_review(conn, brand, hp,
                   {"verdict": verdict, "summary": summary, "source": source})


def review_for(conn, listing):
    """Most specific review note for a listing: brand+hp first, then brand."""
    brand = (listing["brand"] or "").strip().lower()
    if not brand:
        import re as _re
        words = _re.findall(r"[A-Za-z][\w.-]+", listing["label"] or "")
        brand = words[0].lower() if words else ""
    if not brand:
        return None
    if listing["hp"] is not None:
        row = conn.execute("SELECT * FROM reviews WHERE brand = ? AND hp = ?",
                           (brand, listing["hp"])).fetchone()
        if row:
            return row
    return conn.execute("SELECT * FROM reviews WHERE brand = ? AND hp IS NULL",
                        (brand,)).fetchone()


def all_reviews(conn):
    return conn.execute("SELECT * FROM reviews ORDER BY brand, hp").fetchall()


def wanted(conn, listing):
    """Does this listing meet the saved shopping criteria (HP range)?

    Returns (True, "") or (False, reason). Unknown HP is not assumed to qualify.
    """
    lo = get_setting(conn, "min_hp", "")
    hi = get_setting(conn, "max_hp", "")
    hp = listing["hp"]
    try:
        lo = float(lo) if lo not in (None, "") else None
    except ValueError:
        lo = None
    try:
        hi = float(hi) if hi not in (None, "") else None
    except ValueError:
        hi = None
    want_shaft = (get_setting(conn, "shaft", "") or "").strip().upper()
    if lo is None and hi is None and not want_shaft:
        return True, ""
    if lo is not None or hi is not None:
        if hp is None:
            return False, "HP unknown"
        if lo is not None and hp < lo:
            return False, "under %ghp" % lo
        if hi is not None and hp > hi:
            return False, "over %ghp" % hi
    if want_shaft:
        have = (listing["shaft"] or "").strip().upper()
        # Unknown shaft is not a disqualifier - it is shown with a "?" so you can
        # check on the dealer's page. Only a KNOWN mismatch is filtered out.
        if have and want_shaft not in have.split("/"):
            return False, "%s shaft" % have
    return True, ""


# ------------------------------------------------------------------ alerts ---

def add_alert(conn, listing_id, kind, message, old_price=None, new_price=None) -> None:
    conn.execute(
        "INSERT INTO alerts(listing_id, kind, message, old_price, new_price, created_at)"
        " VALUES (?,?,?,?,?,?)",
        (listing_id, kind, message, old_price, new_price, now_iso()),
    )
    conn.commit()


def recent_alerts(conn, limit: int = 50, unseen_only: bool = False):
    sql = ("SELECT a.*, l.label, l.url FROM alerts a"
           " LEFT JOIN listings l ON l.id = a.listing_id")
    if unseen_only:
        sql += " WHERE a.seen = 0"
    sql += " ORDER BY a.created_at DESC, a.id DESC LIMIT ?"
    return conn.execute(sql, (limit,)).fetchall()


def mark_alerts_seen(conn) -> None:
    conn.execute("UPDATE alerts SET seen = 1 WHERE seen = 0")
    conn.commit()
