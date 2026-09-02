#!/usr/bin/env python3
"""Outboard motor price monitor - CLI. Stdlib only, no install step.

  ./monitor.py add "Yamaha F150 XB" https://dealer.example/f150 --brand Yamaha --hp 150
  ./monitor.py check --all
  ./monitor.py serve
"""
from __future__ import annotations

import argparse
import csv
import os
import re
import subprocess
import sys

from urllib.parse import urlparse

import core
import scrape

CURRENCY_GLYPH = {"USD": "$", "AUD": "A$", "NZD": "NZ$", "CAD": "C$",
                  "GBP": "£", "EUR": "€", "ZAR": "R"}


def money(value, currency="USD") -> str:
    if value is None:
        return "-"
    return "%s%s" % (CURRENCY_GLYPH.get(currency, currency + " "), format(round(value), ",d"))


def notify(title: str, message: str) -> None:
    if sys.platform != "darwin":
        return
    try:
        subprocess.run(
            ["osascript", "-e",
             'display notification %s with title %s sound name "Submarine"'
             % (_osa(message), _osa(title))],
            check=False, capture_output=True, timeout=10)
    except Exception:
        pass


def _osa(text: str) -> str:
    return '"%s"' % text.replace("\\", "\\\\").replace('"', '\\"')[:220]


# ------------------------------------------------------------------- checking ---

def check_listing(conn, listing, debug: bool = False) -> dict:
    """Fetch one listing, store the result, raise alerts. Returns a summary dict."""
    lo = core.get_float_setting(conn, "min_plausible", 400)
    hi = core.get_float_setting(conn, "max_plausible", 150000)
    respect = core.get_setting(conn, "respect_robots", "1") == "1"
    previous = core.last_price(conn, listing["id"])

    try:
        html = scrape.fetch(listing["url"], respect_robots=respect)
    except scrape.FetchError as exc:
        core.record_price(conn, listing["id"], None, listing["currency"], exc.kind, str(exc))
        return {"listing": listing, "status": exc.kind, "detail": str(exc), "price": None}

    try:
        price, currency, detail = scrape.extract(html, listing["rule"], lo, hi)
    except ValueError as exc:
        core.record_price(conn, listing["id"], None, listing["currency"], "not_found", str(exc))
        result = {"listing": listing, "status": "not_found", "detail": str(exc), "price": None}
        if debug:
            result["candidates"] = scrape.candidates(html, lo, hi)[:6]
        return result

    # Trust the listing's configured currency over a guess from page text.
    if listing["currency"] and listing["currency"] != "USD":
        currency = listing["currency"]
    core.record_price(conn, listing["id"], price, currency, "ok", detail)

    old = previous["price"] if previous else None
    change = None if old is None else round(price - old, 2)
    _raise_alerts(conn, listing, price, currency, old, previous)
    return {"listing": listing, "status": "ok", "price": price, "currency": currency,
            "detail": detail, "old": old, "change": change}


def _raise_alerts(conn, listing, price, currency, old, previous) -> None:
    label = listing["label"]
    target = listing["target_price"]
    budget = core.get_float_setting(conn, "budget", 0)
    land_now, dcost, dlabel = core.landed(conn, listing["dealer"], price)
    land_old = core.landed(conn, listing["dealer"], old)[0] if old is not None else None
    drop_pct = core.get_float_setting(conn, "drop_alert_pct", 1.0)
    should_notify = core.get_setting(conn, "notify_macos", "1") == "1"
    fired = []

    if old is None:
        core.add_alert(conn, listing["id"], "new",
                       "First price for %s: %s" % (label, money(price, currency)),
                       None, price)
    elif price < old:
        pct = (old - price) / old * 100 if old else 0
        message = "%s dropped %s (%.1f%%) to %s" % (
            label, money(old - price, currency), pct, money(price, currency))
        core.add_alert(conn, listing["id"], "drop", message, old, price)
        if pct >= drop_pct:
            fired.append(("Price drop", message))
    elif price > old:
        core.add_alert(conn, listing["id"], "rise",
                       "%s rose %s to %s" % (label, money(price - old, currency),
                                             money(price, currency)), old, price)

    if target and price <= target and (old is None or old > target):
        message = "%s hit your target: %s (target %s)" % (
            label, money(price, currency), money(target, currency))
        core.add_alert(conn, listing["id"], "target", message, old, price)
        fired.append(("Target price hit", message))

    # Budget: judged on the delivered price, only for motors matching your criteria,
    # and only announced when it newly qualifies.
    ok_spec, _ = core.wanted(conn, listing)
    if ok_spec and budget and land_now is not None and land_now <= budget:
        if land_old is None or land_old > budget:
            where = "collect from %s" % listing["dealer"] if not dcost else \
                "%s delivered" % money(dcost, currency)
            message = "%s is now %s within budget (%s, %s)" % (
                label, money(land_now, currency), where, listing["dealer"])
            core.add_alert(conn, listing["id"], "budget", message, land_old, land_now)
            fired.append(("Under budget", message))

    if should_notify:
        for title, message in fired:
            notify(title, message)


def run_checks(conn, ids=None, debug: bool = False, quiet: bool = False):
    rows = ([core.get_listing(conn, i) for i in ids] if ids
            else core.listings(conn, active_only=True))
    rows = [r for r in rows if r is not None]
    if not rows:
        if not quiet:
            print("No active listings to check. Add one with:  ./monitor.py add ...")
        return []

    results = []
    for index, listing in enumerate(rows, 1):
        result = check_listing(conn, listing, debug=debug)
        results.append(result)
        if quiet:
            continue
        prefix = "[%d/%d] %-38s" % (index, len(rows), listing["label"][:38])
        if result["status"] == "ok":
            change = result["change"]
            if change is None:
                arrow = "  (first check)"
            elif change < 0:
                arrow = "  ▼ %s" % money(-change, result["currency"])
            elif change > 0:
                arrow = "  ▲ %s" % money(change, result["currency"])
            else:
                arrow = "  ="
            target = listing["target_price"]
            flag = "  ★ TARGET" if target and result["price"] <= target else ""
            print("%s %12s%s%s" % (prefix, money(result["price"], result["currency"]),
                                   arrow, flag))
        else:
            print("%s %12s  %s: %s" % (prefix, "-", result["status"], result["detail"][:70]))
            for candidate in result.get("candidates", []):
                print("        candidate %-14s %10s  rule=%s"
                      % (candidate["source"], candidate["price"], candidate["rule"]))

    ok = sum(1 for r in results if r["status"] == "ok")
    if not quiet:
        drops = [r for r in results if r["status"] == "ok" and (r["change"] or 0) < 0]
        print("\n%d/%d checked OK, %d price drop(s)." % (ok, len(results), len(drops)))
    return results


# ------------------------------------------------------------------ commands ---

def cmd_add(conn, args):
    listing_id = core.add_listing(
        conn, args.label, args.url, dealer=args.dealer, brand=args.brand, hp=args.hp,
        shaft=args.shaft, rule=args.rule, currency=args.currency,
        target_price=args.target, notes=args.notes)
    print("Added #%d  %s" % (listing_id, args.label))
    if not args.no_check:
        run_checks(conn, [listing_id], debug=True)


def cmd_list(conn, args):
    rows = core.listings(conn)
    if not rows:
        print("No listings yet.")
        return
    if args.dealer:
        rows = [r for r in rows if (r["dealer"] or "").lower().find(args.dealer.lower()) >= 0]
    if args.brand:
        rows = [r for r in rows if (r["brand"] or "").lower() == args.brand.lower()]
    if args.min_hp is None and not args.all_hp:
        rows = [r for r in rows if core.wanted(conn, r)[0]]   # saved criteria
    if args.min_hp is not None:
        rows = [r for r in rows if r["hp"] and r["hp"] >= args.min_hp]
    if args.max_hp is not None:
        rows = [r for r in rows if r["hp"] and r["hp"] <= args.max_hp]
    if args.tco:
        rows = sorted(rows, key=lambda r: (
            lambda t: (0, t) if t is not None else (1, 0.0))(
                core.total_cost(conn, r,
                    (core.last_price(conn, r["id"]) or {"price": None})["price"])[0]))
    if args.under is not None:
        keep = []
        for r in rows:
            latest_row = core.last_price(conn, r["id"])
            value = latest_row["price"] if latest_row else None
            eff, complete = core.effective_price(conn, r["dealer"], value)
            # Include unquoted-delivery listings too, judged on list price: they may
            # well be cheaper, and hiding them is how you miss the best deal.
            if eff is not None and eff <= args.under:
                keep.append(r)
        rows = keep

    def sort_key(listing):
        latest_row = core.last_price(conn, listing["id"])
        value = latest_row["price"] if latest_row else None
        eff, complete = core.effective_price(conn, listing["dealer"], value)
        if eff is None:
            return (1, 0.0, 0)
        # rank on the comparable figure; a complete price wins any tie
        return (0, eff, 0 if complete else 1)

    rows = sorted(rows, key=sort_key)
    alts = {}
    if args.per_model:
        pairs = core.cheapest_per_model(conn, rows, None)
        rows = [p[0] for p in pairs]
        alts = {p[0]["id"]: p[1] for p in pairs}
    if args.limit:
        rows = rows[:args.limit]
    if not rows:
        print("Nothing matches that filter.")
        return
    city = core.get_setting(conn, "delivery_city")
    if args.tco:
        own = core.get_setting(conn, "own_years")
        print("Cost of ownership over %s years - delivered price plus the dealer servicing"
              " needed to keep cover (year 1 %s, then %s a year). WTY = the brand's"
              " full warranty length.\n"
              % (own, money(core.get_float_setting(conn, "service_year1", 0), "GBP"),
                 money(core.get_float_setting(conn, "service_year_n", 0), "GBP")))
        print("%-4s %-27s %-7s %5s %10s %9s %10s %-6s %s"
              % ("ID", "LABEL", "BRAND", "HP", "MOTOR", "SERVICE", "TOTAL", "WTY", "DEALER"))
        print("-" * 112)
    else:
        print("%-4s %-28s %-7s %5s %11s %5s %-18s %-10s %s"
              % ("ID", "LABEL", "BRAND", "HP", "TO " + city.upper()[:8], "SHAFT", "DEALER",
                 "VERDICT", "ALSO" if args.per_model else ""))
        print("-" * 116)
    for listing in rows:
        latest = core.last_price(conn, listing["id"])
        first = core.history(conn, listing["id"])
        change = ""
        if latest and first and len(first) > 1:
            delta = latest["price"] - first[0]["price"]
            if abs(delta) > 0.005:
                change = "%s%s" % ("+" if delta > 0 else "-",
                                   money(abs(delta), listing["currency"]))
        last = core.last_check(conn, listing["id"])
        when = core.fmt_local(last["checked_at"]) if last else "never"
        if last and last["status"] != "ok":
            when += " (%s)" % last["status"]
        flag = "" if listing["active"] else " [paused]"
        price = latest["price"] if latest else None
        land, dcost, dlabel = core.landed(conn, listing["dealer"], price)
        deliv = money(dcost, listing["currency"]) if dcost is not None else dlabel[:9]
        if args.tco:
            total, delivered, servicing, years, note = core.total_cost(conn, listing, price)
            print("%-4s %-27s %-7s %5s %10s %9s %10s %-5s %s"
                  % (listing["id"], listing["label"][:27], (listing["brand"] or "")[:7],
                     ("%g" % listing["hp"]) if listing["hp"] else "",
                     (money(delivered, "GBP") if delivered is not None
                      else (money(price, "GBP") + "+?" if price else "-")),
                     money(servicing, "GBP") if servicing else "self",
                     money(total, "GBP") if total is not None else "-",
                     # the brand's actual warranty, not the years you happen to pay for
                     ("%gy" % core.warranty_terms(conn, listing["brand"])[0])
                     if core.warranty_terms(conn, listing["brand"])[0] else "?",
                     (listing["dealer"] or "")[:20]))
            continue
        landed_txt = (money(land, listing["currency"]) if land is not None
                      else (money(price, listing["currency"]) + "+?" if price else "-"))
        extra = ""
        if args.per_model and alts.get(listing["id"]):
            extra = "+%d dearer" % alts[listing["id"]]
        rev = core.review_for(conn, listing)
        verdict = (rev["verdict"] if rev else "") or ""
        print("%-4s %-28s %-7s %5s %11s %5s %-18s %-10s %s%s"
              % (listing["id"], listing["label"][:28], (listing["brand"] or "")[:7],
                 ("%g" % listing["hp"]) if listing["hp"] else "",
                 landed_txt, (listing["shaft"] or "?")[:5],
                 (listing["dealer"] or "")[:18], verdict[:10], extra, flag))
        if args.reviews and rev and rev["summary"]:
            import textwrap
            for line in textwrap.wrap(rev["summary"], 92):
                print("     %s" % line)
        if args.links:
            # category-page trackers store the page plus a #fragment we invented
            print("     %s" % listing["url"].split("#")[0])


def cmd_check(conn, args):
    run_checks(conn, args.ids or None, debug=args.debug, quiet=args.quiet)


def cmd_probe(conn, args):
    """Dry-run a URL and print every price candidate plus a suggested rule."""
    lo = core.get_float_setting(conn, "min_plausible", 400)
    hi = core.get_float_setting(conn, "max_plausible", 150000)
    respect = core.get_setting(conn, "respect_robots", "1") == "1"
    print("Fetching %s ..." % args.url)
    try:
        html = scrape.fetch(args.url, respect_robots=respect)
    except scrape.FetchError as exc:
        print("FAILED (%s): %s" % (exc.kind, exc))
        if exc.kind == "blocked":
            print("Tip: the site blocks bots or disallows this path in robots.txt.")
        return
    size = ("%d KB" % (len(html) // 1024)) if len(html) >= 1024 else ("%d bytes" % len(html))
    print("Got %s of HTML.\n" % size)
    options = scrape.candidates(html, lo, hi)
    if not options:
        print("No price candidates in the %s-%s range." % (money(lo), money(hi)))
        print("Try a custom rule, e.g.  --rule 'regex:Our Price[^0-9]{0,20}([\\d,]+)'")
        return
    print("%-18s %12s %5s  %s" % ("SOURCE", "PRICE", "CUR", "RULE"))
    print("-" * 92)
    for candidate in options[:12]:
        print("%-18s %12s %5s  %s" % (candidate["source"], money(candidate["price"]),
                                      candidate["currency"], candidate["rule"]))
        print("%22s└ %s" % ("", candidate["context"][:70]))
    best = options[0]
    print("\nauto would pick: %s (%s)" % (money(best["price"], best["currency"]),
                                          best["source"]))
    print("To pin it explicitly:  --rule '%s'" % best["rule"])


def cmd_history(conn, args):
    listing = core.get_listing(conn, args.id)
    if not listing:
        print("No listing #%s" % args.id)
        return
    print("%s  (%s)\n%s" % (listing["label"], listing["url"], "-" * 60))
    rows = core.price_changes(conn, args.id) if not args.all else core.history(conn, args.id)
    if not rows:
        print("No successful checks yet.")
        return
    previous = None
    for row in rows:
        if previous is None:
            delta = ""
        else:
            step = row["price"] - previous
            delta = "  %s%s" % ("+" if step >= 0 else "-",
                                money(abs(step), row["currency"]))
        print("%-18s %12s%s" % (core.fmt_local(row["checked_at"]),
                                money(row["price"], row["currency"]), delta))
        previous = row["price"]
    summary = core.stats(conn, args.id)
    print("-" * 60)
    print("checks: %d   low: %s   high: %s   avg: %s"
          % (summary["n"], money(summary["lo"], listing["currency"]),
             money(summary["hi"], listing["currency"]),
             money(summary["avg"], listing["currency"])))


def cmd_rm(conn, args):
    listing = core.get_listing(conn, args.id)
    if not listing:
        print("No listing #%s" % args.id)
        return
    core.delete_listing(conn, args.id)
    print("Deleted #%s %s" % (args.id, listing["label"]))


def cmd_edit(conn, args):
    listing = core.get_listing(conn, args.id)
    if not listing:
        print("No listing #%s" % args.id)
        return
    fields = {}
    for name in ("label", "dealer", "brand", "shaft", "url", "rule", "currency", "notes"):
        value = getattr(args, name, None)
        if value is not None:
            fields[name] = value
    if args.hp is not None:
        fields["hp"] = args.hp
    if args.target is not None:
        fields["target_price"] = args.target
    if args.pause:
        fields["active"] = 0
    if args.resume:
        fields["active"] = 1
    if not fields:
        print("Nothing to change.")
        return
    core.update_listing(conn, args.id, **fields)
    print("Updated #%s: %s" % (args.id, ", ".join(sorted(fields))))


def cmd_alerts(conn, args):
    rows = core.recent_alerts(conn, limit=args.limit, unseen_only=args.new)
    if not rows:
        print("No alerts.")
        return
    for alert in rows:
        mark = " " if alert["seen"] else "*"
        print("%s %-17s %-7s %s" % (mark, core.fmt_local(alert["created_at"]),
                                    alert["kind"], alert["message"]))
    if args.mark_seen:
        core.mark_alerts_seen(conn)
        print("\nMarked all alerts as seen.")


def cmd_export(conn, args):
    path = args.out or os.path.join(core.APP_DIR, "prices_export.csv")
    with open(path, "w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["listing_id", "label", "dealer", "brand", "hp", "shaft",
                         "url", "checked_at", "price", "delivery", "landed",
                         "currency", "status"])
        for listing in core.listings(conn):
            for row in core.history(conn, listing["id"], ok_only=False):
                land, dcost, _ = core.landed(conn, listing["dealer"], row["price"])
                writer.writerow([listing["id"], listing["label"], listing["dealer"],
                                 listing["brand"], listing["hp"], listing["shaft"],
                                 listing["url"], row["checked_at"], row["price"],
                                 dcost, land, row["currency"], row["status"]])
    print("Wrote %s" % path)


def cmd_delivery(conn, args):
    """View or set per-dealer delivery terms used for landed-cost comparison."""
    if args.dealer and args.kind:
        core.set_delivery(conn, args.dealer, args.kind, amount=args.amount,
                          free_over=args.free_over, note=args.note, source="manual")
        print("Set %s -> %s" % (args.dealer, args.kind))
        if args.postcode:
            home = core.get_setting(conn, "postcode", "")
            if not home:
                print("  (no postcode of your own set, so distance not calculated)")
            else:
                miles = core.distance_miles(home, args.postcode)
                if miles is None:
                    print("  could not geocode %s - set miles by hand on the "
                          "Delivery tab" % args.postcode)
                else:
                    conn.execute("UPDATE delivery SET miles = ? WHERE dealer = ?",
                                 (miles, args.dealer))
                    conn.commit()
                    cost, label = core.travel_cost(conn, args.dealer)
                    print("  %s is %.0f road miles away%s"
                          % (args.postcode.upper(), miles,
                             (" - about %s to collect" % money(cost, "GBP"))
                             if cost else ""))
        return
    rows = core.all_delivery(conn)
    dealers = sorted({r["dealer"] for r in core.listings(conn) if r["dealer"]})
    known = {r["dealer"]: r for r in rows}
    postcode = core.get_setting(conn, "postcode")
    print("Delivery to %s (%s)\n" % (core.get_setting(conn, "delivery_city"), postcode))
    print("%-30s %-11s %9s %10s  %s" % ("DEALER", "KIND", "AMOUNT", "FREE OVER", "NOTE"))
    print("-" * 104)
    for dealer in dealers:
        row = known.get(dealer)
        if row is None:
            print("%-30s %-11s %9s %10s  %s" % (dealer[:30], "not set", "-", "-",
                                                "run: monitor.py delivery --dealer ... --kind ..."))
        else:
            print("%-30s %-11s %9s %10s  %s"
                  % (dealer[:30], row["kind"],
                     money(row["amount"], "GBP") if row["amount"] is not None else "-",
                     money(row["free_over"], "GBP") if row["free_over"] is not None else "-",
                     (row["note"] or "")[:44]))


# Dealers known to stock outboards and to publish prices a crawl can read.
# These are national online retailers - they ship UK-wide, so they are useful
# wherever you are. Your postcode only decides which are worth COLLECTING from,
# which `delivery --postcode` works out separately.
SEED_DEALERS = [
    ("BoatWorld",               "https://boatworld.co.uk"),
    ("Seamark Nunn",            "https://seamarknunn.com"),
    ("Marine Chandlery",        "https://www.marinechandlery.com"),
    ("Outboard & Marine",       "https://www.outboardandmarine.co.uk"),
    ("Clyde Outboard Services", "https://www.clyde-outboard-services.co.uk"),
    ("Dulas Boats",             "https://dulasboats.co.uk"),
    ("Ash Marine",              "https://www.ashmarine.co.uk"),
    ("Gael Force Marine",       "https://www.gaelforcemarine.co.uk"),
    ("Cambridge Outboards",     "https://www.cambridgeoutboards.co.uk"),
    ("Nestaway Boats",          "https://nestawayboats.com"),
    ("Whitstable Marine",       "https://www.whitstablemarine.co.uk"),
    ("Bill Higham Marine",      "https://www.billhigham.co.uk"),
]


def _listing_facts(title, url):
    """Pull brand, HP and shaft out of a product title. Returns a dict."""
    hp = None
    hp_match = re.search(r"(\d{1,3}(?:\.\d)?)\s*hp\b", title, re.I)
    if hp_match:
        try:
            value = float(hp_match.group(1))
            hp = value if 1 <= value <= 700 else None
        except ValueError:
            hp = None
    brand = next((b for b in ("Yamaha", "Mercury", "Mariner", "Suzuki", "Tohatsu",
                              "Honda", "ePropulsion", "Torqeedo", "Parsun", "Hidea",
                              "Selva", "Minn Kota")
                  if b.lower() in title.lower()), None)
    low = title.lower()
    shaft = ("UL" if "ultra long" in low else "XL" if "extra long" in low
             else "L" if "long shaft" in low
             else "S" if ("short shaft" in low or "standard shaft" in low) else None)
    code = core.model_code(title, url)
    if hp is None:
        # Dealers often title a page just "Suzuki DF6" with no "6hp" anywhere,
        # so fall back to the horsepower encoded in the model code. Only ever a
        # fallback: an explicit "6hp" in the title always wins.
        hp = core.hp_from_code(code, brand)
    return {"hp": hp, "brand": brand, "shaft": shaft, "code": code}


def _crawl_site(conn, site, dealer, pattern=None, max_pages=120, dry_run=False,
                quiet=False):
    """Walk one dealer site, adding every outboard it can price.

    Shared by `crawl` (one site named on the command line) and `populate`
    (every seed dealer in turn). Returns (added, skipped, nomatch).
    """
    import crawl
    lo = core.get_float_setting(conn, "min_plausible", 400)
    hi = core.get_float_setting(conn, "max_plausible", 150000)

    if not quiet:
        print("Discovering pages on %s ..." % site)
    try:
        urls, how, total = crawl.candidate_urls(site, pattern)
    except Exception as exc:
        print("  could not read %s (%s)" % (site, type(exc).__name__))
        return 0, 0, 0
    if not quiet:
        print("  %s: %d urls, %d look like motors" % (how, total, len(urls)))
    if max_pages:
        urls = urls[:max_pages]
    if not urls:
        if not quiet:
            print("Nothing to inspect. Try --pattern to widen the URL filter.")
        return 0, 0, 0

    existing = {r["url"] for r in core.listings(conn)}
    added = skipped = nomatch = 0
    if not quiet:
        print("Inspecting %d pages (about %d min at the polite rate)...\n"
              % (len(urls), max(1, len(urls) * 4 // 60)))
    for index, url in enumerate(urls, 1):
        if url in existing:
            skipped += 1
            continue
        try:
            found = crawl.inspect(url, lo, hi)
        except Exception:
            nomatch += 1
            continue
        if not found:
            nomatch += 1
            continue
        title = found["title"]
        if dry_run:
            if not quiet:
                print("  %9s  %s" % (money(found["price"], found["currency"]), title[:60]))
            added += 1
            continue
        facts = _listing_facts(title, url)
        try:
            core.add_listing(conn, title, url, dealer=dealer, brand=facts["brand"],
                             hp=facts["hp"], shaft=facts["shaft"], rule="auto",
                             currency=found["currency"] or "GBP",
                             notes="found by crawl; listed %s"
                                   % money(found["price"], found["currency"]))
        except Exception:
            skipped += 1
            continue
        if facts["code"]:
            listing_id = conn.execute("SELECT id FROM listings WHERE url = ?",
                                      (url,)).fetchone()["id"]
            core.update_listing(conn, listing_id, model_code=facts["code"])
        existing.add(url)
        added += 1
        if not quiet:
            print("  %9s  %-52s %s" % (money(found["price"], found["currency"]),
                                       title[:52], facts["code"] or ""))
            if index % 25 == 0:
                print("  ... %d/%d inspected" % (index, len(urls)))
    return added, skipped, nomatch


def cmd_crawl(conn, args):
    """Walk a dealer site and add every outboard product page it can price."""
    dealer = args.dealer or urlparse(args.url).netloc.replace("www.", "")
    added, skipped, nomatch = _crawl_site(
        conn, args.url, dealer, pattern=args.pattern, max_pages=args.max,
        dry_run=args.dry_run)
    print("\n%s %d listing(s). %d already tracked, %d pages had no priced motor."
          % ("Would add" if args.dry_run else "Added", added, skipped, nomatch))


def cmd_populate(conn, args):
    """Fill an empty install from the seed dealers, then price what it found."""
    only = {d.strip().lower() for d in (args.only or "").split(",") if d.strip()}
    seeds = [(n, u) for n, u in SEED_DEALERS
             if not only or n.lower() in only or urlparse(u).netloc.replace("www.", "") in only]
    if not seeds:
        print("No seed dealer matched --only. Known dealers:")
        for name, _ in SEED_DEALERS:
            print("   %s" % name)
        return

    postcode = core.get_setting(conn, "postcode", "")
    print("Populating from %d dealer(s)." % len(seeds))
    if postcode:
        print("Your postcode is %s, so collection distances can be worked out "
              "afterwards." % postcode.upper())
    else:
        print("No postcode set - listings will be added, but collection cannot be "
              "costed until you run: ./monitor.py settings postcode \"YOUR POSTCODE\"")
    print("This walks real dealer sites at a polite rate, so it takes a while.\n")

    totals = []
    for number, (name, site) in enumerate(seeds, 1):
        print("-- [%d/%d] %s  (%s)" % (number, len(seeds), name, site))
        added, skipped, nomatch = _crawl_site(
            conn, site, name, pattern=args.pattern, max_pages=args.max,
            dry_run=args.dry_run, quiet=args.quiet)
        totals.append((name, added, skipped))
        print("   %s %d, already had %d, %d pages with no priced motor\n"
              % ("would add" if args.dry_run else "added", added, skipped, nomatch))

    grand = sum(t[1] for t in totals)
    print("=" * 62)
    print("%s %d listing(s) across %d dealer(s):"
          % ("Would add" if args.dry_run else "Added", grand, len(totals)))
    for name, added, skipped in sorted(totals, key=lambda t: -t[1]):
        print("   %-26s %4d new  (%d already tracked)" % (name[:26], added, skipped))
    if args.dry_run:
        print("\nDry run - nothing was saved. Drop --dry-run to keep them.")
        return
    if not grand:
        print("\nNothing new to add - you already track everything these dealers list.")
        return
    print("\nNow run these to finish setting up:")
    print("   ./monitor.py check                     # get a price for each")
    if postcode:
        print('   ./monitor.py delivery --dealer "NAME" --kind free --postcode "THEIRS"')
        print("                                          # per dealer, for collection")
    print("   ./monitor.py serve                     # open the dashboard")


def cmd_compare(conn, args):
    """Price one motor across every dealer, using manufacturer model codes.

    Listings that name an exact code are matched on it; vaguer listings from the
    same brand and HP are shown separately so you can see what they might be.
    """
    import re
    query = args.model.strip().upper().replace(" ", "")
    # DF6 must not swallow DF60: the digits have to end where the query's digits end
    m = re.match(r"^([A-Z]+)(\d+(?:\.\d)?)([A-Z0-9]*)$", query)
    if m:
        matcher = re.compile(r"^%s%s(?![0-9.])%s" % (re.escape(m.group(1)),
                                                     re.escape(m.group(2)),
                                                     re.escape(m.group(3))))
    else:
        matcher = re.compile(r"^%s" % re.escape(query))
    rows = core.listings(conn)
    exact, loose = [], []
    for listing in rows:
        code = (listing["model_code"] or "").upper()
        if code and matcher.match(code):
            exact.append(listing)
    # work out the brand/HP this code implies, then find unnamed listings that match
    brands = {l["brand"] for l in exact if l["brand"]}
    hps = {l["hp"] for l in exact if l["hp"] is not None}
    for listing in rows:
        if listing in exact or listing["model_code"]:
            continue
        if listing["brand"] in brands and listing["hp"] in hps:
            loose.append(listing)

    if not exact and not loose:
        print("Nothing tracked matching %r." % args.model)
        print("Try a code like DF6, MFS6, BF5, F6 - see ./monitor.py list")
        return

    def render(listing):
        latest = core.last_price(conn, listing["id"])
        price = latest["price"] if latest else None
        land, dcost, dlabel = core.landed(conn, listing["dealer"], price)
        currency = listing["currency"] or "GBP"
        if land is not None:
            total = money(land, currency)
            deliv = money(dcost, currency) if dcost else "collect/free"
        else:
            total = (money(price, currency) + "+?") if price else "-"
            deliv = dlabel[:14]
        return (listing, price, land, total, deliv)

    def show(group, heading):
        if not group:
            return
        entries = [render(l) for l in group]
        entries.sort(key=lambda e: (e[2] if e[2] is not None else
                                    (e[1] if e[1] is not None else 1e12)))
        print("\n%s" % heading)
        print("%-4s %-11s %11s %-14s %-6s %-22s %s"
              % ("ID", "CODE", "TOTAL", "DELIVERY", "SHAFT", "DEALER", "LISTED"))
        print("-" * 104)
        for listing, price, land, total, deliv in entries:
            print("%-4s %-11s %11s %-14s %-6s %-22s %s"
                  % (listing["id"], (listing["model_code"] or "-")[:11], total, deliv,
                     (listing["shaft"] or "?")[:6], (listing["dealer"] or "")[:22],
                     money(price, listing["currency"] or "GBP")))
            if args.links:
                print("     %s" % listing["url"].split("#")[0])

    show(exact, "Listings naming this exact model code:")
    show(loose, "Same brand and HP but no model code given - check which variant these are:")
    print()


def cmd_reviews(conn, args):
    if args.brand and args.summary:
        core.set_review(conn, args.brand, args.verdict or "", args.summary,
                        source="entered by hand", hp=args.hp)
        print("Saved note for %s%s" % (args.brand, (" %ghp" % args.hp) if args.hp else ""))
        return
    rows = core.all_reviews(conn)
    if not rows:
        print("No review notes yet.")
        return
    import textwrap
    for row in rows:
        head = row["brand"].title() + (" %ghp" % row["hp"] if row["hp"] else "")
        print("%-16s %-11s %s" % (head, row["verdict"] or "", ""))
        for line in textwrap.wrap(row["summary"] or "", 88):
            print("    %s" % line)
        try:
            warranty = row["warranty"]
        except (IndexError, KeyError):
            warranty = None
        if warranty:
            print("    WARRANTY:")
            for line in textwrap.wrap(warranty, 84):
                print("      %s" % line)
        try:
            corrosion = row["corrosion"]
        except (IndexError, KeyError):
            corrosion = None
        if corrosion:
            print("    SALT CORROSION:")
            for line in textwrap.wrap(corrosion, 84):
                print("      %s" % line)
        if row["source"]:
            print("    source: %s" % row["source"])
        print()


def cmd_serve(conn, args):
    import web
    host = "0.0.0.0" if args.lan else args.host
    web.serve(args.port, host, open_browser=not args.no_open)


DEALER_LOCATORS = [
    ("Tohatsu UK",   "https://tohatsu.co.uk/dealers"),
    ("Suzuki Marine","https://marine.suzuki.co.uk/find-a-dealer/"),
    ("Yamaha Marine","https://www.yamaha-motor.eu/gb/en/dealer-locator/"),
    ("Mercury",      "https://www.mercurymarine.com/en/gb/dealer-locator/"),
    ("Honda Marine", "https://www.honda.co.uk/marine/dealer-search.html"),
]


def cmd_find_dealers(conn, args):
    """Help find dealers near you - the ones a national web search misses."""
    postcode = args.postcode or core.get_setting(conn, "postcode", "")
    if not postcode:
        print("No postcode set. Either pass --postcode, or run:  ./monitor.py setup")
        return
    place = core.geocode(postcode)
    if not place:
        print("Could not look up %r. Check the postcode." % postcode)
        return
    lat, lon, district = place
    print("You are in %s (%s)\n" % (district or "?", postcode.upper()))

    towns = core.nearby_districts(postcode)
    if towns:
        print("Council areas near you, useful as search terms:")
        print("   %s\n" % ", ".join(towns[:12]))

    print("Search these - a plain 'UK outboard dealers' search returns national")
    print("retailers and misses local ones:\n")
    for town in (towns[:6] or [district]):
        print("   outboard dealer %s" % town)
        print("   outboard service %s" % town)
    print()
    print("Then check each manufacturer's own dealer locator, which lists only")
    print("AUTHORISED dealers - the ones whose warranty registration is valid:\n")
    for name, url in DEALER_LOCATORS:
        print("   %-15s %s" % (name, url))
    print("\nSearch each by postcode %s. When you find one, add its listings and" % postcode.upper())
    print("record how far it is so collection is costed properly:\n")
    print('   ./monitor.py delivery --dealer "Their Name" --kind free --postcode "THEIR POSTCODE"')


def _wrap_help(text, width=74, indent="   "):
    """Wrap a setting's help so long explanations stay readable in a terminal."""
    import textwrap
    lines = textwrap.wrap(text, width=width)
    return ("\n" + indent).join(lines)


def cmd_setup(conn, args):
    """Walk through the settings once, on a fresh install."""
    print("Outboard Price Monitor - first-time setup")
    print("Press Enter to keep the current value, or to leave a setting off.\n")
    changed = 0
    for group, key, label, kind, help_text in core.settings_spec_flat():
        if group != getattr(cmd_setup, "_last_group", None):
            print("\n-- %s" % group)
            cmd_setup._last_group = group
        current = core.get_setting(conn, key, "") or ""
        hint = ""
        shown = current or "off"
        if kind == "bool":
            hint = " [yes/no]"
            shown = core.bool_label(current) if current else "yes"
        elif kind.startswith("choice:"):
            hint = " [%s]" % "/".join(c or "blank" for c in kind.split(":", 1)[1].split("|"))
        if help_text:
            print("\n   %s" % _wrap_help(help_text))
        try:
            answer = input("   %s%s [%s]: " % (label, hint, shown)).strip()
        except (EOFError, KeyboardInterrupt):
            print("\n\nStopped. Nothing further changed.")
            return
        if answer == "":
            continue
        if kind == "bool":
            parsed = core.parse_bool(answer)
            if parsed is None:
                print("   (answer yes or no - skipped)")
                continue
            answer = parsed
        elif kind == "number":
            cleaned = answer.replace(",", "").replace("£", "").strip()
            try:
                float(cleaned)
            except ValueError:
                print("   (not a number - skipped)")
                continue
            answer = cleaned
        core.set_setting(conn, key, answer)
        changed += 1
    print("\nSaved %d setting(s)." % changed)
    postcode = core.get_setting(conn, "postcode", "")
    if postcode:
        place = core.geocode(postcode)
        if place:
            print("\nLooked up %s - you are in %s." % (postcode.upper(), place[2]))
            print("Run this to find dealers near you (a national search misses them):")
            print("   ./monitor.py find-dealers")
    if not core.listings(conn):
        print("\nNothing is tracked yet. Add your first listing with:")
        print("   ./monitor.py probe \"https://dealer.example/some-outboard\"")
        print("   ./monitor.py add \"Label\" \"https://dealer.example/some-outboard\" --hp 6")
        print("\nOr open the dashboard and use the form:  ./monitor.py serve")


def cmd_settings(conn, args):
    if args.key and args.value is not None:
        core.set_setting(conn, args.key, args.value)
        print("%s = %s" % (args.key, args.value))
        return
    last_group = None
    for group, key, label, _kind, _help in core.settings_spec_flat():
        if group != last_group:
            print("\n%s" % group)
            last_group = group
        value = core.get_setting(conn, key, "") or ""
        print("  %-18s %-28s %s" % (key, value or "(off)", label))
    extra = [k for k in sorted(core.DEFAULT_SETTINGS)
             if k not in {key for _g, key, _l, _k, _h in core.settings_spec_flat()}]
    if extra:
        print("\nOther")
        for key in extra:
            print("  %-18s %s" % (key, core.get_setting(conn, key) or "(off)"))
    if not core.is_configured(conn):
        print("\nLooks like a fresh install - run:  ./monitor.py setup")


def cmd_schedule(conn, args):
    """Write a launchd job so checks run automatically."""
    label = "com.outboard-monitor.check"
    plist_path = os.path.expanduser("~/Library/LaunchAgents/%s.plist" % label)
    python = sys.executable
    script = os.path.join(core.APP_DIR, "monitor.py")
    log = os.path.join(core.APP_DIR, "check.log")
    if args.at:
        try:
            hh, mm = args.at.split(":")
            hh, mm = int(hh), int(mm)
            assert 0 <= hh <= 23 and 0 <= mm <= 59
        except (ValueError, AssertionError):
            print("--at needs a 24-hour time like 09:00")
            return
        when = ("    <key>StartCalendarInterval</key>\n"
                "    <dict>\n"
                "        <key>Hour</key><integer>%d</integer>\n"
                "        <key>Minute</key><integer>%d</integer>\n"
                "    </dict>" % (hh, mm))
        cadence = "daily at %02d:%02d" % (hh, mm)
    else:
        when = ("    <key>StartInterval</key><integer>%d</integer>"
                % int(args.hours * 3600))
        cadence = "every %g hour(s)" % args.hours
    plist = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>%s</string>
    <key>ProgramArguments</key>
    <array>
        <string>%s</string>
        <string>%s</string>
        <string>check</string>
        <string>--all</string>
    </array>
    <key>WorkingDirectory</key><string>%s</string>
%s
    <key>RunAtLoad</key><false/>
    <key>StandardOutPath</key><string>%s</string>
    <key>StandardErrorPath</key><string>%s</string>
</dict>
</plist>
""" % (label, python, script, core.APP_DIR, when, log, log)

    if args.uninstall:
        subprocess.run(["launchctl", "unload", plist_path], capture_output=True)
        if os.path.exists(plist_path):
            os.remove(plist_path)
        print("Removed scheduled checks.")
        return

    if args.print_only:
        print(plist)
        return

    os.makedirs(os.path.dirname(plist_path), exist_ok=True)
    with open(plist_path, "w") as handle:
        handle.write(plist)
    subprocess.run(["launchctl", "unload", plist_path], capture_output=True)
    result = subprocess.run(["launchctl", "load", plist_path], capture_output=True, text=True)
    if result.returncode == 0:
        print("Scheduled: checking %s.\n  plist: %s\n  log:   %s"
              % (cadence, plist_path, log))
    else:
        print("Wrote %s but launchctl load failed: %s"
              % (plist_path, result.stderr.strip() or result.stdout.strip()))


# ---------------------------------------------------------------------- CLI ---

def build_parser():
    parser = argparse.ArgumentParser(
        prog="monitor.py", description="Monitor new-outboard prices at dealer sites.")
    subs = parser.add_subparsers(dest="command")

    add = subs.add_parser("add", help="track a new dealer listing URL")
    add.add_argument("label", help='e.g. "Yamaha F150 XB 20in"')
    add.add_argument("url")
    add.add_argument("--dealer"), add.add_argument("--brand")
    add.add_argument("--hp", type=float), add.add_argument("--shaft")
    add.add_argument("--rule", default="auto",
                     help="auto | css:SEL | attr:SEL@ATTR | regex:PAT | jsonld:KEY")
    add.add_argument("--currency", default="USD")
    add.add_argument("--target", type=float, help="alert when price falls to this")
    add.add_argument("--notes")
    add.add_argument("--no-check", action="store_true", help="don't fetch immediately")
    add.set_defaults(func=cmd_add)

    listing = subs.add_parser("list",
                              help="listings sorted by delivered price (cheapest first)")
    listing.add_argument("--dealer", help="only this dealer (substring match)")
    listing.add_argument("--brand")
    listing.add_argument("--min-hp", type=float, dest="min_hp")
    listing.add_argument("--max-hp", type=float, dest="max_hp")
    listing.add_argument("--under", type=float,
                         help="only listings whose DELIVERED price is at or below this")
    listing.add_argument("--tco", action="store_true",
                         help="show cost of ownership: motor + dealer servicing")
    listing.add_argument("--reviews", action="store_true",
                         help="print the reputation note under each row")
    listing.add_argument("--links", action="store_true",
                         help="print each listing's URL under its row")
    listing.add_argument("--per-model", action="store_true",
                         help="one row per model - the cheapest delivered")
    listing.add_argument("--all-hp", action="store_true",
                         help="ignore the saved HP criteria and show everything")
    listing.add_argument("--limit", type=int, help="show only the first N")
    listing.set_defaults(func=cmd_list)

    check = subs.add_parser("check", help="fetch current prices")
    check.add_argument("ids", nargs="*", type=int)
    check.add_argument("--all", action="store_true", help="all active listings (default)")
    check.add_argument("--debug", action="store_true", help="show candidates on failure")
    check.add_argument("--quiet", action="store_true")
    check.set_defaults(func=cmd_check)

    probe = subs.add_parser("probe", help="dry-run a URL and suggest an extraction rule")
    probe.add_argument("url")
    probe.set_defaults(func=cmd_probe)

    hist = subs.add_parser("history", help="price history for one listing")
    hist.add_argument("id", type=int)
    hist.add_argument("--all", action="store_true", help="every check, not just changes")
    hist.set_defaults(func=cmd_history)

    edit = subs.add_parser("edit", help="change a listing's fields")
    edit.add_argument("id", type=int)
    for name in ("label", "dealer", "brand", "shaft", "url", "rule", "currency", "notes"):
        edit.add_argument("--" + name)
    edit.add_argument("--hp", type=float), edit.add_argument("--target", type=float)
    edit.add_argument("--pause", action="store_true")
    edit.add_argument("--resume", action="store_true")
    edit.set_defaults(func=cmd_edit)

    remove = subs.add_parser("rm", help="stop tracking and delete history")
    remove.add_argument("id", type=int)
    remove.set_defaults(func=cmd_rm)

    alerts = subs.add_parser("alerts", help="recent price alerts")
    alerts.add_argument("--limit", type=int, default=30)
    alerts.add_argument("--new", action="store_true", help="unseen only")
    alerts.add_argument("--mark-seen", action="store_true")
    alerts.set_defaults(func=cmd_alerts)

    serve = subs.add_parser("serve", help="run the web dashboard")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--host", default="127.0.0.1",
                       help="0.0.0.0 to reach it from other devices on your network")
    serve.add_argument("--lan", action="store_true",
                       help="shorthand for --host 0.0.0.0")
    serve.add_argument("--no-open", action="store_true")
    serve.set_defaults(func=cmd_serve)

    deliv = subs.add_parser("delivery", help="view or set per-dealer delivery costs")
    deliv.add_argument("--dealer")
    deliv.add_argument("--kind", choices=["flat", "free", "threshold", "collect", "quote"])
    deliv.add_argument("--amount", type=float, help="flat fee, or fee below the threshold")
    deliv.add_argument("--free-over", type=float, dest="free_over")
    deliv.add_argument("--note")
    deliv.add_argument("--postcode",
                       help="the dealer's postcode; sets real road distance from yours")
    deliv.set_defaults(func=cmd_delivery)

    cr = subs.add_parser("crawl", help="walk a dealer site and add its outboards")
    cr.add_argument("url", help="site root, e.g. https://dealer.co.uk")
    cr.add_argument("--dealer", help="name to file the listings under")
    cr.add_argument("--max", type=int, default=120, help="most pages to inspect")
    cr.add_argument("--pattern", help="regex the URL must match (default: outboard|engine|motor)")
    cr.add_argument("--dry-run", action="store_true", help="show what it would add")
    cr.set_defaults(func=cmd_crawl)

    pop = subs.add_parser("populate",
                          help="fill an empty install from known outboard dealers")
    pop.add_argument("--max", type=int, default=120,
                     help="most pages to inspect per dealer (default 120)")
    pop.add_argument("--only",
                     help="comma-separated dealer names, instead of all of them")
    pop.add_argument("--pattern", help="regex the URL must match")
    pop.add_argument("--dry-run", action="store_true",
                     help="show what it would add without saving")
    pop.add_argument("--quiet", action="store_true",
                     help="one line per dealer instead of per listing")
    pop.set_defaults(func=cmd_populate)

    comp = subs.add_parser("compare",
                           help="price one model (e.g. DF6) across every dealer")
    comp.add_argument("model")
    comp.add_argument("--links", action="store_true")
    comp.set_defaults(func=cmd_compare)

    revs = subs.add_parser("reviews", help="reputation notes per brand/model")
    revs.add_argument("--brand")
    revs.add_argument("--hp", type=float)
    revs.add_argument("--verdict")
    revs.add_argument("--summary")
    revs.set_defaults(func=cmd_reviews)

    export = subs.add_parser("export", help="dump all price history to CSV")
    export.add_argument("--out")
    export.set_defaults(func=cmd_export)

    settings = subs.add_parser("settings", help="view or change settings")
    settings.add_argument("key", nargs="?"), settings.add_argument("value", nargs="?")
    settings.set_defaults(func=cmd_settings)

    findd = subs.add_parser("find-dealers",
                            help="find dealers near you, including local ones")
    findd.add_argument("--postcode", help="defaults to your saved postcode")
    findd.set_defaults(func=cmd_find_dealers)

    setup = subs.add_parser("setup", help="guided first-time setup")
    setup.set_defaults(func=cmd_setup)

    schedule = subs.add_parser("schedule", help="run checks automatically via launchd")
    schedule.add_argument("--hours", type=float, default=6,
                          help="run every N hours (ignored if --at is given)")
    schedule.add_argument("--at", help="run once a day at this 24-hour time, e.g. 09:00")
    schedule.add_argument("--uninstall", action="store_true")
    schedule.add_argument("--print-only", action="store_true")
    schedule.set_defaults(func=cmd_schedule)

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    conn = core.init_db()
    try:
        args.func(conn, args)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
