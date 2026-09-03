"""Fetching and price extraction for dealer listing pages (stdlib only).

Extraction rules supported per listing:
  auto              - try JSON-LD, then meta tags, then price-ish elements, then text
  css:SELECTOR      - text of the first element matching a small CSS subset
  attr:SELECTOR@ATTR- an attribute of the first matching element
  regex:PATTERN     - first capture group (or whole match) of a regex over the HTML
  textregex:PATTERN - same, but over the page's visible text with whitespace collapsed
                      (use when markup sits between the label and the price)
  jsonld:KEY        - a specific key inside any JSON-LD block (e.g. jsonld:lowPrice)
"""
from __future__ import annotations

import gzip
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import zlib
from html.parser import HTMLParser
from urllib import robotparser
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse, urljoin
from urllib.request import Request, urlopen

USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "close",
}
MIN_HOST_INTERVAL = 4.0   # seconds between requests to the same host (default)
_min_host_interval = [MIN_HOST_INTERVAL]   # live value, settable from settings


def set_min_host_interval(seconds):
    """Override the polite rate, from the user's settings. Never below 0.5s."""
    try:
        value = float(seconds)
    except (TypeError, ValueError):
        return
    _min_host_interval[0] = max(0.5, value)
TIMEOUT = 25

# Some retailers reject urllib on headers/TLS fingerprint alone. curl, with a cookie
# jar and a full browser header set, gets through many of them.
BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
              "(KHTML, like Gecko) Version/17.4 Safari/605.1.15")
COOKIE_DIR = os.path.join(tempfile.gettempdir(), "outboard-monitor-cookies")
CURL = shutil.which("curl")

CURRENCY_SYMBOLS = {"$": "USD", "£": "GBP", "€": "EUR", "¥": "JPY", "R": "ZAR"}
CURRENCY_CODES = ("AUD", "NZD", "CAD", "USD", "GBP", "EUR", "ZAR")

_last_hit = {}
_robots_cache = {}


class FetchError(Exception):
    """Raised when a page cannot be retrieved. .kind classifies the failure."""

    def __init__(self, message, kind="error"):
        super().__init__(message)
        self.kind = kind


# ------------------------------------------------------------------ fetching ---

def _throttle(host: str) -> None:
    last = _last_hit.get(host)
    if last is not None:
        wait = _min_host_interval[0] - (time.time() - last)
        if wait > 0:
            time.sleep(wait)
    _last_hit[host] = time.time()


def robots_allows(url: str) -> bool:
    """True if robots.txt permits us; also True when robots.txt is unreachable."""
    parts = urlparse(url)
    base = "%s://%s" % (parts.scheme, parts.netloc)
    parser = _robots_cache.get(base)
    if parser is None:
        parser = robotparser.RobotFileParser()
        parser.set_url(urljoin(base, "/robots.txt"))
        try:
            _throttle(parts.netloc)
            req = Request(urljoin(base, "/robots.txt"), headers={"User-Agent": USER_AGENT})
            with urlopen(req, timeout=10) as resp:
                parser.parse(_decode(resp.read(), resp.headers).splitlines())
        except Exception:
            parser = None  # unreachable robots.txt -> do not block the check
        _robots_cache[base] = parser
    if parser is None:
        return True
    try:
        return parser.can_fetch(USER_AGENT, url)
    except Exception:
        return True


def _decode(raw: bytes, headers) -> str:
    encoding = (headers.get("Content-Encoding") or "").lower()
    if "gzip" in encoding:
        try:
            raw = gzip.decompress(raw)
        except OSError:
            pass
    elif "deflate" in encoding:
        try:
            raw = zlib.decompress(raw, -zlib.MAX_WBITS)
        except zlib.error:
            try:
                raw = zlib.decompress(raw)
            except zlib.error:
                pass
    charset = None
    ctype = headers.get("Content-Type") or ""
    match = re.search(r"charset=([\w\-]+)", ctype, re.I)
    if match:
        charset = match.group(1)
    if not charset:
        head = raw[:4096].decode("latin-1", "ignore")
        match = re.search(r'charset=["\']?([\w\-]+)', head, re.I)
        if match:
            charset = match.group(1)
    for enc in (charset, "utf-8", "latin-1"):
        if not enc:
            continue
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


def fetch_curl(url: str) -> str:
    """Fetch via curl with a persistent per-host cookie jar and browser headers."""
    if not CURL:
        raise FetchError("curl not available", "error")
    os.makedirs(COOKIE_DIR, exist_ok=True)
    host = urlparse(url).netloc.replace(":", "_")
    jar = os.path.join(COOKIE_DIR, "%s.txt" % host)
    cmd = [CURL, "-sS", "-L", "--compressed", "--max-time", str(TIMEOUT),
           "-w", "\n__HTTP_STATUS__%{http_code}", "-c", jar, "-b", jar,
           "-H", "User-Agent: " + BROWSER_UA,
           "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
           "-H", "Accept-Language: en-GB,en;q=0.9",
           "-H", "Sec-Fetch-Dest: document", "-H", "Sec-Fetch-Mode: navigate",
           "-H", "Sec-Fetch-Site: none", "-H", "Sec-Fetch-User: ?1",
           "-H", "Upgrade-Insecure-Requests: 1", url]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=TIMEOUT + 15)
    except subprocess.TimeoutExpired:
        raise FetchError("curl timed out", "http_error")
    body = proc.stdout.decode("utf-8", "replace")
    status = None
    marker = body.rfind("__HTTP_STATUS__")
    if marker != -1:
        status = body[marker + len("__HTTP_STATUS__"):].strip()
        body = body[:marker].rstrip("\n")
    if proc.returncode != 0 and not body:
        raise FetchError("curl failed: %s" % proc.stderr.decode("utf-8", "replace")[:120],
                         "http_error")
    if status and not status.startswith("2"):
        kind = "blocked" if status in ("401", "403", "429") else "http_error"
        raise FetchError("HTTP %s (curl)" % status, kind)
    return body


def fetch(url: str, respect_robots: bool = True) -> str:
    parts = urlparse(url)
    if parts.scheme not in ("http", "https"):
        raise FetchError("unsupported URL scheme: %s" % parts.scheme, "error")
    if respect_robots and not robots_allows(url):
        raise FetchError("blocked by robots.txt", "blocked")
    _throttle(parts.netloc)
    try:
        with urlopen(Request(url, headers=HEADERS), timeout=TIMEOUT) as resp:
            return _decode(resp.read(), resp.headers)
    except HTTPError as exc:
        kind = "blocked" if exc.code in (401, 403, 429) else "http_error"
        first = FetchError("HTTP %s %s" % (exc.code, exc.reason), kind)
    except URLError as exc:
        first = FetchError("network error: %s" % exc.reason, "http_error")
    except Exception as exc:  # socket timeouts, bad SSL, malformed responses
        first = FetchError("%s: %s" % (type(exc).__name__, exc), "error")

    # urllib was refused - retry through curl, which many sites accept.
    if CURL:
        try:
            return fetch_curl(url)
        except FetchError:
            pass
    raise first


# ----------------------------------------------------------- tiny DOM + CSS ---

VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
             "meta", "param", "source", "track", "wbr"}
SKIP_TEXT_TAGS = {"script", "style", "noscript", "template", "svg"}


class Node:
    __slots__ = ("tag", "attrs", "children", "parent", "texts")

    def __init__(self, tag, attrs=None, parent=None):
        self.tag = tag
        self.attrs = attrs or {}
        self.children = []
        self.parent = parent
        self.texts = []

    def classes(self):
        return (self.attrs.get("class") or "").split()

    def has_descendant(self, tags) -> bool:
        return any(n.tag in tags for n in self.iter() if n is not self)

    def text(self, limit: int = 4000, skip=()) -> str:
        out = []

        def walk(node):
            if node.tag in SKIP_TEXT_TAGS or (node is not self and node.tag in skip):
                return
            for item in node.texts:
                out.append(item)
            for child in node.children:
                walk(child)

        walk(self)
        return re.sub(r"\s+", " ", " ".join(out)).strip()[:limit]

    def iter(self):
        yield self
        for child in self.children:
            for node in child.iter():
                yield node


class DOM(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("#document")
        self._stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = Node(tag, {k: (v or "") for k, v in attrs}, self._stack[-1])
        self._stack[-1].children.append(node)
        if tag not in VOID_TAGS:
            self._stack.append(node)

    def handle_startendtag(self, tag, attrs):
        node = Node(tag, {k: (v or "") for k, v in attrs}, self._stack[-1])
        self._stack[-1].children.append(node)

    def handle_endtag(self, tag):
        for index in range(len(self._stack) - 1, 0, -1):
            if self._stack[index].tag == tag:
                del self._stack[index:]
                return

    def handle_data(self, data):
        if data.strip():
            self._stack[-1].texts.append(data)


def parse_html(html: str) -> Node:
    dom = DOM()
    try:
        dom.feed(html)
    except Exception:
        pass  # keep whatever parsed cleanly
    return dom.root


_COMPOUND = re.compile(r"""
    (?P<tag>^[a-zA-Z][\w-]*|\*)
  | \.(?P<cls>[\w-]+)
  | \#(?P<id>[\w-]+)
  | \[(?P<attr>[\w:-]+)(?:(?P<op>[~^$*]?=)["']?(?P<val>[^\]"']*)["']?)?\]
""", re.X)


def _match_compound(node: Node, compound: str) -> bool:
    if node.tag.startswith("#"):
        return False
    position = 0
    matched_any = False
    while position < len(compound):
        match = _COMPOUND.match(compound, position)
        if not match:
            return False
        position = match.end()
        matched_any = True
        if match.group("tag"):
            if match.group("tag") != "*" and node.tag != match.group("tag").lower():
                return False
        elif match.group("cls"):
            if match.group("cls") not in node.classes():
                return False
        elif match.group("id"):
            if node.attrs.get("id") != match.group("id"):
                return False
        elif match.group("attr"):
            name = match.group("attr").lower()
            if name not in node.attrs:
                return False
            if match.group("op"):
                have, want = node.attrs[name], match.group("val")
                op = match.group("op")
                if op == "=" and have != want:
                    return False
                if op == "~=" and want not in have.split():
                    return False
                if op == "^=" and not have.startswith(want):
                    return False
                if op == "$=" and not have.endswith(want):
                    return False
                if op == "*=" and want not in have:
                    return False
    return matched_any


def select(root: Node, selector: str):
    """Supports tag/.class/#id/[attr=val], descendant and '>' combinators, commas."""
    results, seen = [], set()
    for alternative in selector.split(","):
        chain = [part for part in re.split(r"\s*(>)\s*|\s+", alternative.strip()) if part]
        if not chain:
            continue
        for node in root.iter():
            if node.tag.startswith("#"):
                continue
            if _matches_chain(node, chain) and id(node) not in seen:
                seen.add(id(node))
                results.append(node)
    return results


def _matches_chain(node: Node, chain) -> bool:
    if not _match_compound(node, chain[-1]):
        return False
    index = len(chain) - 2
    current = node.parent
    while index >= 0:
        token = chain[index]
        if token == ">":
            compound = chain[index - 1]
            if current is None or not _match_compound(current, compound):
                return False
            current = current.parent
            index -= 2
            continue
        found = False
        while current is not None:
            if _match_compound(current, token):
                current = current.parent
                found = True
                break
            current = current.parent
        if not found:
            return False
        index -= 1
    return True


# ------------------------------------------------------- price number parsing ---

STRUCK_TAGS = ("del", "s", "strike")

_AMOUNT = re.compile(
    r"\d{1,3}(?:[,\u202f\s]\d{3})+(?:\.\d{1,2})?"   # 18,499.00  /  18 499
    r"|\d{1,3}(?:\.\d{3})+(?:,\d{1,2})?"              # 14.250,00  (European)
    r"|\d+(?:[.,]\d{1,2})?"                           # 18499.00 / 18499,00
)


def detect_currency(text: str, default: str = "USD") -> str:
    for code in CURRENCY_CODES:
        if re.search(r"\b%s\b" % code, text):
            return code
    for symbol, code in CURRENCY_SYMBOLS.items():
        if symbol in text and symbol != "R":
            return code
    return default


def parse_amount(text: str, lo: float = 0, hi: float = 1e12):
    """First plausible money amount in a string, or None."""
    if not text:
        return None
    cleaned = text.replace("\u00a0", " ")
    for match in _AMOUNT.finditer(cleaned):
        raw = match.group(0)
        if re.match(r"^\d{1,3}(?:\.\d{3})+(?:,\d{1,2})?$", raw):
            raw = raw.replace(".", "").replace(",", ".")   # 14.250,00 -> 14250.00
        elif re.match(r"^\d+,\d{1,2}$", raw):
            raw = raw.replace(",", ".")                    # 14250,00 -> 14250.00
        else:
            raw = re.sub(r"[,\s\u202f]", "", raw)          # 18,499.00 -> 18499.00
        try:
            value = float(raw)
        except ValueError:
            continue
        if lo <= value <= hi:
            return value
    return None


def _ancestors(node):
    current = node.parent
    while current is not None:
        yield current
        current = current.parent


def _iter_json(obj):
    if isinstance(obj, dict):
        yield obj
        for value in obj.values():
            for item in _iter_json(value):
                yield item
    elif isinstance(obj, list):
        for value in obj:
            for item in _iter_json(value):
                yield item


def jsonld_blocks(root: Node):
    blocks = []
    for node in select(root, 'script[type=application/ld+json]'):
        raw = " ".join(node.texts).strip()
        if not raw:
            continue
        try:
            blocks.append(json.loads(raw))
        except ValueError:
            # some sites emit several concatenated objects
            for chunk in re.findall(r"\{.*?\}(?=\s*[\{\[]|\s*$)", raw, re.S):
                try:
                    blocks.append(json.loads(chunk))
                except ValueError:
                    pass
    return blocks


PRICE_KEYS = ("price", "lowPrice", "highPrice", "salePrice", "offerPrice")


def candidates(html: str, lo: float, hi: float):
    """All plausible prices found on the page, best-evidence first.

    Each candidate: {'price', 'currency', 'source', 'rule', 'context'}
    """
    root = parse_html(html)
    found = []

    def push(price, currency, source, rule, context):
        if price is None or not (lo <= price <= hi):
            return
        found.append({"price": round(price, 2), "currency": currency, "source": source,
                      "rule": rule, "context": (context or "")[:160]})

    # 1. JSON-LD structured data - the most reliable signal when present.
    for block in jsonld_blocks(root):
        for obj in _iter_json(block):
            currency = obj.get("priceCurrency") or obj.get("currency")
            for key in PRICE_KEYS:
                if key in obj and not isinstance(obj[key], (dict, list)):
                    value = parse_amount(str(obj[key]), lo, hi)
                    push(value, currency or "USD", "json-ld", "jsonld:%s" % key,
                         "%s=%s" % (key, obj[key]))

    # 2. Meta tags used by most e-commerce platforms.
    meta_specs = [
        ('meta[property=product:price:amount]', "content", 'meta product:price'),
        ('meta[property=og:price:amount]', "content", 'meta og:price'),
        ('meta[itemprop=price]', "content", 'meta itemprop price'),
        ('meta[name=twitter:data1]', "content", 'meta twitter:data1'),
    ]
    for selector, attr, source in meta_specs:
        for node in select(root, selector):
            raw = node.attrs.get(attr, "")
            push(parse_amount(raw, lo, hi), detect_currency(raw), source,
                 "attr:%s@%s" % (selector, attr), raw)

    # 3. Microdata / price-ish elements.
    for node in select(root, "[itemprop=price]"):
        raw = node.attrs.get("content") or node.text(200)
        push(parse_amount(raw, lo, hi), detect_currency(raw), "itemprop=price",
             "css:[itemprop=price]", raw)

    price_words = re.compile(r"price|amount|cost|money|pricing", re.I)
    strike_words = re.compile(r"was|rrp|list|msrp|compare|strike|old|regular|original", re.I)
    for node in root.iter():
        if node.tag.startswith("#") or node.tag in SKIP_TEXT_TAGS:
            continue
        marker = " ".join([node.attrs.get("class", ""), node.attrs.get("id", ""),
                           node.attrs.get("data-testid", "")])
        if not marker.strip() or not price_words.search(marker):
            continue
        struck = node.has_descendant(STRUCK_TAGS)
        text = node.text(200, skip=STRUCK_TAGS if struck else ())
        if not text or len(text) > 120:
            continue
        value = parse_amount(text, lo, hi)
        if value is None:
            continue
        is_was = strike_words.search(marker) or node.tag in STRUCK_TAGS \
            or any(a.tag in STRUCK_TAGS for a in _ancestors(node))
        label = "was-price" if is_was else "price-element"
        hook = next((c for c in node.classes() if price_words.search(c)), None)
        rule = "css:%s.%s" % (node.tag, hook) if hook else "css:%s" % node.tag
        push(value, detect_currency(text), label, rule, text)

    # 4. Last resort: any currency-prefixed amount in the visible body text.
    body = select(root, "body")
    body_text = body[0].text(30000) if body else re.sub(r"<[^>]+>", " ", html)[:30000]
    money = re.compile(
        r"(?:AUD|NZD|CAD|USD|GBP|EUR|\$|£|€)\s?(?P<pre>[\d][\d,.\s\u202f]{2,14})"
        r"|(?P<post>[\d][\d,.\s\u202f]{2,14})\s?(?:€|£|EUR|AUD|USD)")
    for match in money.finditer(body_text):
        raw = match.group("pre") or match.group("post") or ""
        value = parse_amount(raw, lo, hi)
        start = max(0, match.start() - 45)
        push(value, detect_currency(match.group(0)), "body-text", "auto",
             body_text[start:match.end() + 25])

    priority = {"json-ld": 0, "meta product:price": 1, "meta og:price": 1,
                "meta itemprop price": 2, "meta twitter:data1": 4, "itemprop=price": 2,
                "price-element": 3, "was-price": 6, "body-text": 7}
    found.sort(key=lambda c: priority.get(c["source"], 9))

    deduped, seen = [], set()
    for candidate in found:
        key = (candidate["price"], candidate["source"])
        if key not in seen:
            seen.add(key)
            deduped.append(candidate)
    return deduped


def extract(html: str, rule: str = "auto", lo: float = 400, hi: float = 150000):
    """Apply a listing's extraction rule. Returns (price, currency, detail).

    Raises ValueError when the rule finds nothing usable.
    """
    rule = (rule or "auto").strip()

    if rule == "auto":
        options = candidates(html, lo, hi)
        if not options:
            raise ValueError("no price found on page")
        best = options[0]
        return best["price"], best["currency"], "%s | %s" % (best["source"], best["context"])

    if rule.startswith("regex:"):
        pattern = rule[len("regex:"):]
        match = re.search(pattern, html, re.I | re.S)
        if not match:
            raise ValueError("regex matched nothing")
        text = match.group(1) if match.groups() else match.group(0)
        value = parse_amount(text, lo, hi)
        if value is None:
            raise ValueError("regex matched %r but no amount in range" % text[:60])
        return value, detect_currency(text), "regex | %s" % text[:120]

    if rule.startswith("textregex:"):
        pattern = rule[len("textregex:"):]
        body = select(parse_html(html), "body")
        text = body[0].text(200000) if body else re.sub(r"<[^>]+>", " ", html)
        match = re.search(pattern, text, re.I)
        if not match:
            raise ValueError("textregex matched nothing")
        found = match.group(1) if match.groups() else match.group(0)
        value = parse_amount(found, lo, hi)
        if value is None:
            raise ValueError("textregex matched %r but no amount in range" % found[:60])
        return value, detect_currency(found), "textregex | %s" % found[:120]

    if rule.startswith("jsonld:"):
        key = rule[len("jsonld:"):]
        root = parse_html(html)
        for block in jsonld_blocks(root):
            for obj in _iter_json(block):
                if key in obj and not isinstance(obj[key], (dict, list)):
                    value = parse_amount(str(obj[key]), lo, hi)
                    if value is not None:
                        currency = obj.get("priceCurrency") or obj.get("currency") or "USD"
                        return value, currency, "jsonld:%s | %s" % (key, obj[key])
        raise ValueError("no JSON-LD key %r with an in-range amount" % key)

    if rule.startswith("attr:"):
        spec = rule[len("attr:"):]
        if "@" not in spec:
            raise ValueError("attr rule needs SELECTOR@ATTRIBUTE")
        selector, attr = spec.rsplit("@", 1)
        for node in select(parse_html(html), selector.strip()):
            raw = node.attrs.get(attr.strip().lower(), "")
            value = parse_amount(raw, lo, hi)
            if value is not None:
                return value, detect_currency(raw), "attr | %s" % raw[:120]
        raise ValueError("selector %r had no attribute %r with an amount" % (selector, attr))

    if rule.startswith("css:"):
        selector = rule[len("css:"):].strip()
        for node in select(parse_html(html), selector):
            text = node.attrs.get("content") or node.text(200)
            value = parse_amount(text, lo, hi)
            if value is not None:
                return value, detect_currency(text), "css | %s" % text[:120]
        raise ValueError("selector %r matched no in-range amount" % selector)

    raise ValueError("unknown rule %r" % rule)
