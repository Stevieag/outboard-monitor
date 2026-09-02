"""Walk a dealer site to find outboard product pages (stdlib only).

Prefers sitemap.xml (cheap and complete); falls back to a depth-limited link walk
for sites that publish none.
"""
from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse

import core
import scrape

SITEMAP_PATHS = ("/sitemap.xml", "/sitemap_index.xml", "/sitemap-index.xml",
                 "/sitemap1.xml", "/product-sitemap.xml")

# What marks a page as an outboard MOTOR rather than a part or an instrument.
# "engine" on its own is far too loose - it matches engine mounts, engine oil,
# and marine instruments like the "NASA Clipper AIS Engine".
WANT = re.compile(r"outboard", re.I)
# a title may say "6hp 4-stroke engine" without the word outboard
# many dealers title a motor "Tohatsu 6hp 4-Stroke Short Shaft" with no such word,
# so an HP figure next to engine/motor/stroke/shaft counts too
WANT_TITLE = re.compile(
    r"outboard"
    r"|\b\d{1,3}(?:\.\d)?\s*(?:hp|ps)\b.{0,45}\b(?:engine|motor|stroke|shaft)\b"
    r"|\b(?:engine|motor|stroke|shaft)\b.{0,45}\b\d{1,3}(?:\.\d)?\s*(?:hp|ps)\b"
    r"|\b(?:MFS|DF|BF|FT)\s?\d", re.I)
SKIP_URL = re.compile(r"/(blog|news|about|contact|policies|account|cart|checkout|search|"
                      r"pages/|collections/all|tag/|category/)", re.I)
# non-English locale prefixes on multilingual sites - same products, duplicate URLs
SKIP_LOCALE = re.compile(r"^/(es|de|fr|it|nl|pt|pl|sv|da|no|fi)(/|$)", re.I)
SKIP_TITLE = re.compile(r"\bkit\b|gasket|impeller|anode|filter|\boil\b|\bpump\b|spare|"
                        r"\bparts?\b|cable|propell?er|cover|\btank\b|service|manual|"
                        r"\btool|seal|bearing|piston|carburett?or|thermostat|spark|"
                        r"trailer|battery|charger|clothing|boot|glove|rope|paint|"
                        r"bracket|\block\b|trolley|mount|stabilis|doel.?fin|holder|"
                        r"\bstand\b|\bbag\b|strap|flush|lead|adapter|adaptor|"
                        r"\bhood\b|\bstop\b|lanyard|remote|gauge|hour ?meter", re.I)

# an h1 that is site furniture rather than the product name
JUNK_H1 = re.compile(r"^\s*(save|sale|offer|welcome|shop|menu|basket|free deliver|"
                     r"sign up|newsletter|\d+%)", re.I)


def _locs(xml: str):
    return [m.strip() for m in re.findall(r"<loc>\s*([^<]+?)\s*</loc>", xml)]


def from_sitemap(base: str, limit: int = 20000):
    """All URLs a site advertises, following one level of sitemap index."""
    base = base.rstrip("/")
    for path in SITEMAP_PATHS:
        try:
            xml = scrape.fetch(base + path, respect_robots=False)
        except scrape.FetchError:
            continue
        if "<urlset" not in xml and "<sitemapindex" not in xml:
            continue
        urls = []
        if "<sitemapindex" in xml:
            for child in _locs(xml)[:50]:
                try:
                    sub = scrape.fetch(child, respect_robots=False)
                except scrape.FetchError:
                    continue
                urls.extend(_locs(sub))
                if len(urls) >= limit:
                    break
        else:
            urls = _locs(xml)
        if urls:
            return urls[:limit]
    return []


def from_links(base: str, max_pages: int = 60, depth: int = 2):
    """Breadth-first link walk for sites with no sitemap."""
    base = base.rstrip("/")
    host = urlparse(base).netloc
    seen, out = {base + "/"}, []
    frontier = [(base + "/", 0)]
    fetched = 0
    while frontier and fetched < max_pages:
        url, level = frontier.pop(0)
        try:
            html = scrape.fetch(url, respect_robots=True)
        except scrape.FetchError:
            continue
        fetched += 1
        out.append(url)
        if level >= depth:
            continue
        for a in scrape.select(scrape.parse_html(html), "a"):
            href = a.attrs.get("href", "")
            if not href or href.startswith(("javascript", "mailto", "tel", "#")):
                continue
            full = urljoin(url, href.split("#")[0].split("?")[0])
            if urlparse(full).netloc != host or full in seen:
                continue
            seen.add(full)
            if WANT.search(full) or level == 0:
                frontier.append((full, level + 1))
    return out


def candidate_urls(base: str, extra_pattern: str = None):
    urls = from_sitemap(base)
    how = "sitemap"
    if not urls:
        urls = from_links(base)
        how = "link walk"
    want = re.compile(extra_pattern, re.I) if extra_pattern else WANT
    keep = [u for u in urls
            if want.search(u)
            and not SKIP_URL.search(u)
            and not SKIP_LOCALE.match(urlparse(u).path)]
    return keep, how, len(urls)


def inspect(url: str, lo: float, hi: float):
    """Fetch one page; return a product dict, or None if it is not a motor listing."""
    try:
        html = scrape.fetch(url, respect_robots=True)
    except scrape.FetchError:
        return None
    root = scrape.parse_html(html)
    title = ""
    for node in scrape.select(root, "h1"):
        candidate = re.sub(r"\s+", " ", node.text(140)).strip()
        if candidate and not JUNK_H1.match(candidate):
            title = candidate
            break
    if not title:
        match = re.search(r"<title>(.*?)</title>", html, re.S | re.I)
        if match:
            title = re.sub(r"\s+", " ", match.group(1)).strip()
            # drop a trailing site name: "Honda 5hp Outboard | Gael Force Marine"
            title = re.split(r"\s+[|\u2013-]\s+", title)[0].strip()
    if not title or SKIP_TITLE.search(title):
        return None
    if not WANT_TITLE.search(title):
        return None
    body = scrape.select(root, "body")
    # search the whole page: many sites open with several KB of navigation
    text = body[0].text(40000) if body else ""
    if not re.search(r"outboard|engine", text, re.I):
        return None
    try:
        price, currency, detail = scrape.extract(html, "auto", lo, hi)
    except ValueError:
        return None
    return {"url": url, "title": re.sub(r"\s+", " ", title)[:70],
            "price": price, "currency": currency, "detail": detail}
