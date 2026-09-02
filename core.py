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
    brand      TEXT NOT NULL,
    hp         REAL,             -- NULL = applies to the whole brand
    verdict    TEXT,             -- one-word summary, e.g. "excellent", "budget"
    summary    TEXT,
    source     TEXT,
    updated_at TEXT,
    PRIMARY KEY (brand, hp)
);

-- Delivery terms per dealer, so listings compare on landed cost, not sticker price.
CREATE TABLE IF NOT EXISTS delivery (
    dealer     TEXT PRIMARY KEY,
    kind       TEXT NOT NULL DEFAULT 'quote',   -- flat|free|threshold|collect|quote
    amount     REAL,        -- flat fee, or fee below the threshold
    free_over  REAL,        -- order value at/above which delivery is free
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
    "max_travel_miles": "150", # how far you will drive to collect
    "free_collect": "",        # comma-separated dealers you would collect from anyway
}


# Describes every setting so the CLI, the dashboard and the setup flow all agree
# on what it means, what type it is, and where it belongs.
SETTINGS_SPEC = [
    ("What you are shopping for", [
        ("budget", "Budget", "number",
         "Alert when a matching motor's DELIVERED price falls to or below this."),
        ("min_hp", "Minimum HP", "number", "Ignore anything smaller. Blank for no limit."),
        ("max_hp", "Maximum HP", "number", "Ignore anything larger. Blank for no limit."),
        ("shaft", "Shaft length", "choice:|S|L|XL|UL",
         "S short, L long, XL extra-long, UL ultra-long. Blank accepts any. "
         "Listings that do not state a shaft are always kept."),
    ]),
    ("Where you are", [
        ("delivery_city", "Your town or city", "text",
         "Used in column headings, e.g. 'Delivered to Manchester'."),
        ("postcode", "Your postcode", "text",
         "Used when asking a dealer's own cart what delivery costs."),
    ]),
    ("Collecting in person", [
        ("max_travel_miles", "Furthest you will drive", "number",
         "One-way miles. Dealers further away are treated as delivery-only."),
        ("travel_per_mile", "Running cost per mile", "number",
         "Applied to the ROUND trip, so 0.25 on a 40 mile dealer costs £20."),
        ("free_collect", "Dealers you would collect from anyway", "text",
         "Comma-separated. These cost nothing to collect from - no detour."),
    ]),
    ("Cost of ownership", [
        ("service_year1", "Dealer service, first year", "number",
         "Many warranties require dealer servicing for the whole term."),
        ("service_year_n", "Dealer service, each later year", "number", ""),
        ("own_years", "How long you will keep it", "number",
         "Servicing is only counted for as long as you own it and have cover."),
    ]),
    ("Alerts", [
        ("drop_alert_pct", "Notify on drops of at least", "number", "Percent."),
        ("notify_macos", "Desktop notifications", "choice:1|0", "1 on, 0 off."),
    ]),
    ("Scraping", [
        ("min_plausible", "Ignore prices below", "number",
         "Keeps accessories and finance-per-month figures out."),
        ("max_plausible", "Ignore prices above", "number",
         "Keeps phone numbers and part codes out."),
        ("respect_robots", "Honour robots.txt", "choice:1|0",
         "Leave on unless you have a reason not to."),
    ]),
]


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
ROAD_FACTOR = 1.25


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


def distance_miles(from_postcode, to_postcode):
    """Approximate ROAD miles between two UK postcodes, or None."""
    import math
    a = geocode(from_postcode)
    b = geocode(to_postcode)
    if not a or not b:
        return None
    (lat1, lon1, _), (lat2, lon2, _) = a, b
    radius, rad = 3958.8, math.pi / 180
    h = (math.sin((lat2 - lat1) * rad / 2) ** 2
         + math.cos(lat1 * rad) * math.cos(lat2 * rad)
         * math.sin((lon2 - lon1) * rad / 2) ** 2)
    straight = 2 * radius * math.asin(math.sqrt(h))
    return round(straight * ROAD_FACTOR, 1)


def nearby_districts(postcode, limit=30):
    """Council districts near a postcode - useful as local search terms."""
    import json as _json
    import re as _re
    import scrape as _scrape
    # the outward code is everything except the final three characters ("M1 1AE" -> "M1")
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


def init_db(path: str = None) -> sqlite3.Connection:
    conn = connect(path)
    conn.executescript(SCHEMA)
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
