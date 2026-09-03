# ⚓ Outboard Price Monitor

Track outboard motor prices across dealer websites, record the history, chart it, and
get alerted when a motor drops or comes within your budget.

Ranks listings on **what a motor actually costs you** — the price plus whichever is
cheaper, delivery or driving there to collect — so a cheap motor with £300 of freight
doesn't beat a dearer one you can pick up.

**Pure Python 3 standard library.** No pip install, no Node, no build step. Runs on
macOS and Linux.

![no dependencies](https://img.shields.io/badge/dependencies-none-brightgreen)
![python](https://img.shields.io/badge/python-3.8%2B-blue)
![license](https://img.shields.io/badge/license-MPL--2.0-blue)

---

## Quick start

```bash
git clone https://github.com/Stevieag/outboard-monitor.git
cd outboard-monitor

# tell it what you are shopping for (or do it in the browser later)
./monitor.py setup

# fill it from dealers known to publish prices - no listings to add by hand
./monitor.py populate

# see what prices a dealer page exposes, and which rule to use
./monitor.py probe "https://dealer.example/yamaha-f6"

# track it
./monitor.py add "Yamaha F6 short shaft" "https://dealer.example/yamaha-f6" \
    --brand Yamaha --hp 6 --shaft S --dealer "Coastal Marine" --currency GBP

# open the dashboard
./monitor.py serve
```

Then set it running by itself:

```bash
./monitor.py schedule --at 09:00        # daily at 9am (macOS launchd)
```

Price drops and motors newly within budget raise a desktop notification.

## Stocking it quickly

Most dealers run Shopify or WooCommerce, and both expose a product feed that lists
every item with its price — far quicker than adding listings by hand:

- Shopify: `https://dealer.example/products.json?limit=250`
- WooCommerce: `https://dealer.example/wp-json/wc/store/v1/products?per_page=100`

For dealers without a feed, `crawl` walks their `sitemap.xml` (falling back to a
link walk) and adds any product page it can price:

```bash
./monitor.py crawl "https://dealer.example" --dealer "Name" --dry-run
```

Always `--dry-run` first. It identifies a motor by the word "outboard", an HP figure
next to engine/motor/stroke/shaft, or a model code like `MFS6`/`DF6`, then rejects
accessories by title. Dealers whose titles carry neither — Mercury's bare "FOURSTROKE 6",
for instance — need `add` or a product feed instead.

## Commands

| Command | What it does |
|---|---|
| `add LABEL URL` | Track a listing (`--brand --hp --shaft --dealer --target --rule --currency`) |
| `list` | Listings by delivered price, cheapest first. `--under`, `--brand`, `--min-hp`, `--max-hp`, `--dealer`, `--per-model`, `--tco`, `--links`, `--reviews`, `--limit` |
| `check [ids…]` | Fetch prices now. `--debug` shows candidates when extraction fails |
| `probe URL` | Dry run a page: every price found, plus the rule to pin the right one |
| `compare MODEL` | Price one model (`DF6`, `MFS6`) across every dealer, via manufacturer model codes |
| `history ID` | Price timeline for one listing |
| `delivery` | View or set per-dealer delivery cost and road miles from you |
| `reviews` | Reputation, warranty terms and corrosion cover per brand/model |
| `alerts` | Drops, rises, target hits and budget hits |
| `serve` | Web dashboard. `--lan` to reach it from other devices |
| `crawl URL` | Walk a dealer's sitemap and add its outboards |
| `export` | Dump all history to CSV |
| `setup` | Guided first-time setup, walks every setting |
| `settings [key value]` | View settings grouped, or change one |
| `schedule --at HH:MM` | Install/remove the launchd job for automatic checks |
| `edit ID` / `rm ID` | Change or delete a listing |

## How prices are found

Each listing has an extraction rule. The default, `auto`, tries in order:

1. **JSON-LD** structured data (`schema.org/Offer`) — what most modern dealer sites emit
2. **Price meta tags** (`product:price:amount`, `og:price:amount`, `itemprop=price`)
3. **Price-styled elements** — anything whose class/id looks like a price, with
   struck-through RRP/"was" prices excluded so you get the *sale* price
4. **Page text** — any currency-marked amount, as a last resort

When `auto` picks the wrong number, `probe` prints every candidate and the exact rule
to pin the right one:

| Rule | Example |
|---|---|
| `css:SELECTOR` | `css:.price--sale`, `css:p.price > ins` |
| `attr:SELECTOR@ATTR` | `attr:meta[property=product:price:amount]@content` |
| `jsonld:KEY` | `jsonld:lowPrice` |
| `regex:PATTERN` | `regex:Our Price[^0-9]{0,20}([\d,]+)` |
| `textregex:PATTERN` | Runs over the page's visible text with whitespace collapsed, so markup between label and price doesn't matter — and lets one category page feed many listings |

Handles US (`£18,499.00`) and European (`14.250,00 €`) number formats, and
GBP/USD/AUD/NZD/CAD/EUR/ZAR.

## Finding dealers near you

A web search for "outboard dealers" returns national online retailers and misses the
local dealer ten miles away — which is often the cheapest, because you can collect and
they want the local trade. `find-dealers` uses your postcode to give you the searches
worth running and the manufacturers' own authorised-dealer locators:

```bash
./monitor.py find-dealers
```

It resolves your postcode through [postcodes.io](https://postcodes.io) (free, no key)
and lists nearby council areas to search by name. Record a dealer's distance by
postcode and collection gets costed properly:

```bash
./monitor.py delivery --dealer "SSI Marine" --kind free --postcode "DEALER POSTCODE"
```

Distances are straight-line × 1.25 to approximate road miles.

## Delivered price, and collecting

Set what each dealer charges to deliver, and how far away they are, on the dashboard's
**Delivery** tab. Each listing is then costed on whichever is cheaper — having it
delivered, or driving to collect.

```bash
./monitor.py settings postcode "YOUR POSTCODE"
./monitor.py settings max_travel_miles 150     # how far you will drive
./monitor.py settings travel_per_mile 0.25     # running cost per mile, round trip
./monitor.py settings free_collect "Local Marine,Other Dealer"
```

Where delivery is unknown a listing shows `£945+?` and sorts below every known
delivered price — it is **never** treated as free.

## Shopping criteria

The app remembers what you are looking for and applies it to the dashboard *and* the
alerts, so you are not pinged about motors you would never buy.

Three ways to set it, all equivalent:

```bash
./monitor.py setup                       # guided, walks every setting
./monitor.py settings                    # show everything, grouped
./monitor.py settings min_hp 4           # set one
```

…or edit them in the browser on the dashboard's **Settings** tab. A fresh install
shows a short welcome page pointing you there.

## Cost of ownership

Manufacturer warranties often require dealer servicing for the whole term, which can
cost more than the difference between two motors. Record the rates and `--tco` shows
the real five-year figure:

```bash
./monitor.py settings service_year1 100
./monitor.py settings service_year_n 200
./monitor.py list --under 2000 --per-model --tco
```

## Settings

| Key | Meaning |
|---|---|
| `budget` | Alert when a matching motor's delivered price falls to/below this |
| `min_hp` / `max_hp` / `shaft` | What you are shopping for |
| `postcode` / `delivery_city` | Where quotes are for |
| `max_travel_miles` / `travel_per_mile` / `free_collect` | Collection costing |
| `service_year1` / `service_year_n` / `own_years` | Cost-of-ownership inputs |
| `drop_alert_pct` | Minimum % drop that triggers a notification |
| `min_plausible` / `max_plausible` | Ignore amounts outside this range |
| `respect_robots` | Honour the site's robots.txt (default on) |
| `notify_macos` | Desktop notifications |

## Being a good citizen

- One request per host every 4 seconds, with a normal browser User-Agent
- `robots.txt` honoured by default
- Keep the check interval at hours, not minutes — you are monitoring a dealer, not
  stress-testing them
- Some retailers render prices in JavaScript or block non-browser requests
  (`blocked` status). `probe` tells you which. Those need a real browser.

## How it fits together

| File | |
|---|---|
| `monitor.py` | CLI, check engine, alerting, scheduling |
| `scrape.py` | Fetching (urllib with a curl fallback), a mini HTML DOM with CSS selectors, price extraction |
| `core.py` | SQLite storage, delivery/travel costing, criteria, reviews |
| `web.py` | Dashboard and inline SVG charts |
| `crawl.py` | Sitemap and link walking |

Data lives in `prices.db` beside the scripts. It is gitignored — your tracked prices
and settings stay local.

## Tests

```bash
./tests/journey.sh
```

Runs the whole user journey against a throwaway database — empty install, guided
setup, dealer discovery, probing a real page, adding listings, delivery by postcode,
a live price check, every list view, editing, scheduling, and every web page and form
over HTTP. 34 checks. It needs a network because it hits real dealer sites, and it
leaves your own `prices.db` alone.

It earned its keep immediately: it caught that columns added to a developer's database
by hand were missing from the schema, so a fresh clone crashed on any page that costed
up collection.

## Licence

Copyright (c) 2026 Stevieag. Mozilla Public License 2.0 — see [LICENSE](LICENSE).

Use it, run it, and build on it freely, including commercially. The one
condition is file-level copyleft: **if you change one of these source files and
give your version to anyone, that file's source has to be published under the
MPL too.** Improvements to the code come back.

It stops there, deliberately. You can add your own files alongside these and
keep them closed, and you can combine this with proprietary code — MPL only
reaches the files it already covers, not your whole project.

Provided as is, with no warranty. It reads prices off third-party websites that
change without notice; check anything before you spend money on it.
