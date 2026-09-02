"""Local web dashboard for the outboard price monitor (http.server, no deps)."""
from __future__ import annotations

import html as html_mod
import json
import threading
import webbrowser
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import core
import monitor

_check_lock = threading.Lock()
_check_queue = []          # listing ids awaiting a check; None means "all active"
_check_state = {"running": False, "done": 0, "total": 0, "message": ""}


def esc(value) -> str:
    return html_mod.escape("" if value is None else str(value))


CSS = """
:root{--bg:#f6f7f9;--card:#fff;--ink:#14181d;--muted:#697586;--line:#e3e7ec;
--up:#c02b2b;--down:#127a3d;--accent:#0b5cad;--star:#b57d00;}
@media(prefers-color-scheme:dark){:root{--bg:#12161b;--card:#1a2027;--ink:#e8ecf1;
--muted:#93a0b0;--line:#2a333d;--up:#ff7b72;--down:#4ec98a;--accent:#6cb3f5;--star:#e8b93a;}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
header{background:var(--card);border-bottom:1px solid var(--line);padding:14px 22px;
display:flex;align-items:center;gap:16px;flex-wrap:wrap;position:sticky;top:0;z-index:5}
header h1{font-size:17px;margin:0;font-weight:650;letter-spacing:-.01em}
.wrap{max-width:1180px;margin:0 auto;padding:22px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:22px}
.tile{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:13px 15px}
.tile .k{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted)}
.tile .v{font-size:22px;font-weight:640;margin-top:3px;font-variant-numeric:tabular-nums}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;
overflow:hidden;margin-bottom:22px}
.card h2{font-size:13px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);
margin:0;padding:13px 16px;border-bottom:1px solid var(--line)}
table{width:100%;border-collapse:collapse}
th,td{padding:10px 12px;text-align:left;border-bottom:1px solid var(--line);
font-size:13px;vertical-align:middle}
th{font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);
font-weight:600;white-space:nowrap}
tbody tr:last-child td{border-bottom:none}
tbody tr:hover{background:rgba(125,145,170,.07)}
.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.price{font-weight:650}
.down{color:var(--down)}.up{color:var(--up)}
.muted{color:var(--muted)}
.pill{display:inline-block;padding:1px 7px;border-radius:20px;font-size:11px;
border:1px solid var(--line);color:var(--muted);white-space:nowrap}
.pill.hit{background:rgba(181,125,0,.14);color:var(--star);border-color:transparent;font-weight:600}
.pill.err{background:rgba(192,43,43,.13);color:var(--up);border-color:transparent}
.pill.warn{background:rgba(181,125,0,.15);color:var(--star);border-color:transparent}
.pill.good{background:rgba(18,122,61,.14);color:var(--down);border-color:transparent}
.pill.budget{background:rgba(105,117,134,.16);color:var(--muted);border-color:transparent}
.price.big{font-size:15px;font-weight:700}
.btn{background:var(--accent);color:#fff;border:none;border-radius:7px;padding:7px 13px;
font-size:13px;font-weight:550;cursor:pointer;font-family:inherit}
.btn:hover{filter:brightness(1.08)}.btn[disabled]{opacity:.55;cursor:default}
.btn.ghost{background:transparent;color:var(--accent);border:1px solid var(--line)}
form.add{padding:16px;display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:11px}
form.add label{display:block;font-size:11px;text-transform:uppercase;letter-spacing:.05em;
color:var(--muted);margin-bottom:4px}
input,select{width:100%;padding:7px 9px;border:1px solid var(--line);border-radius:7px;
background:var(--bg);color:var(--ink);font:inherit;font-size:13px}
.span2{grid-column:span 2}
.actions{padding:0 16px 16px;display:flex;gap:9px;align-items:center;flex-wrap:wrap}
.flash{padding:11px 16px;border-radius:8px;margin-bottom:18px;font-size:13px;
background:rgba(18,122,61,.12);border:1px solid rgba(18,122,61,.3)}
.flash.bad{background:rgba(192,43,43,.12);border-color:rgba(192,43,43,.32)}
.spark{display:block}
.empty{padding:34px 18px;text-align:center;color:var(--muted)}
.chart-wrap{padding:16px;overflow-x:auto}
details summary{cursor:pointer;color:var(--muted);font-size:12px;padding:0 16px 14px}
code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;
background:var(--bg);padding:1px 5px;border-radius:4px}
"""

JS = """
function run(url){
  document.querySelectorAll('.btn').forEach(function(b){b.disabled=true});
  var s=document.getElementById('status'); if(s){s.textContent='Checking prices...';}
  fetch(url,{method:'POST'}).then(function(){poll()});
  return false;
}
function poll(){
  fetch('/api/status').then(function(r){return r.json()}).then(function(d){
    var s=document.getElementById('status');
    if(d.running){ if(s){s.textContent='Checking '+d.done+' of '+d.total+'...';}
      setTimeout(poll,900);
    } else { location.reload(); }
  }).catch(function(){setTimeout(poll,1500)});
}
window.addEventListener('DOMContentLoaded',function(){
  fetch('/api/status').then(function(r){return r.json()}).then(function(d){
    if(d.running){poll()}});
});
"""


def page(title: str, body: str) -> bytes:
    return ("""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>%s</title><style>%s</style></head><body>
<header><h1>⚓ Outboard Price Monitor</h1>
<a href="/">Dashboard</a><a href="/alerts">Alerts</a><a href="/delivery">Delivery</a>
<a href="/settings">Settings</a>
<span style="flex:1"></span><span id="status" class="muted"></span>
<button class="btn" onclick="return run('/api/check')">Check all now</button>
</header>%s<script>%s</script></body></html>""" % (esc(title), CSS, body, JS)).encode("utf-8")


# ------------------------------------------------------------------- charting ---

def sparkline(points, width=110, height=26):
    """Tiny inline trend line; points is a list of (datetime, price)."""
    values = [p[1] for p in points]
    if len(values) < 2:
        return '<span class="muted">–</span>'
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1
    step = width / (len(values) - 1)
    coords = ["%.1f,%.1f" % (i * step, height - 3 - (v - lo) / span * (height - 6))
              for i, v in enumerate(values)]
    colour = "var(--down)" if values[-1] < values[0] else (
        "var(--up)" if values[-1] > values[0] else "var(--muted)")
    last = coords[-1].split(",")
    return ('<svg class="spark" width="%d" height="%d" viewBox="0 0 %d %d">'
            '<polyline fill="none" stroke="%s" stroke-width="1.6" stroke-linejoin="round" '
            'points="%s"/><circle cx="%s" cy="%s" r="2.2" fill="%s"/></svg>'
            % (width, height, width, height, colour, " ".join(coords),
               last[0], last[1], colour))


def big_chart(points, currency, width=880, height=280):
    if len(points) < 2:
        return '<div class="empty">Not enough history yet — check again later to build a trend.</div>'
    pad_l, pad_r, pad_t, pad_b = 62, 14, 16, 30
    plot_w, plot_h = width - pad_l - pad_r, height - pad_t - pad_b
    values = [p[1] for p in points]
    times = [p[0] for p in points]
    lo, hi = min(values), max(values)
    if hi == lo:
        lo, hi = lo * 0.98, hi * 1.02
    pad_v = (hi - lo) * 0.12
    lo, hi = lo - pad_v, hi + pad_v
    t0, t1 = times[0], times[-1]
    t_span = (t1 - t0).total_seconds() or 1

    def x(t):
        return pad_l + (t - t0).total_seconds() / t_span * plot_w

    def y(v):
        return pad_t + (hi - v) / (hi - lo) * plot_h

    # Stepped line: a price holds until the next observation.
    path = ["M %.1f %.1f" % (x(times[0]), y(values[0]))]
    for i in range(1, len(points)):
        path.append("L %.1f %.1f" % (x(times[i]), y(values[i - 1])))
        path.append("L %.1f %.1f" % (x(times[i]), y(values[i])))
    line = " ".join(path)
    area = "%s L %.1f %.1f L %.1f %.1f Z" % (line, x(times[-1]), pad_t + plot_h,
                                             pad_l, pad_t + plot_h)

    parts = ['<svg width="%d" height="%d" viewBox="0 0 %d %d" role="img" '
             'aria-label="Price history">' % (width, height, width, height)]
    parts.append('<defs><linearGradient id="g" x1="0" y1="0" x2="0" y2="1">'
                 '<stop offset="0%" stop-color="var(--accent)" stop-opacity=".22"/>'
                 '<stop offset="100%" stop-color="var(--accent)" stop-opacity="0"/>'
                 '</linearGradient></defs>')
    for i in range(5):
        value = hi - (hi - lo) * i / 4
        gy = y(value)
        parts.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="var(--line)"/>'
                     % (pad_l, gy, width - pad_r, gy))
        parts.append('<text x="%d" y="%.1f" font-size="10" fill="var(--muted)" '
                     'text-anchor="end">%s</text>'
                     % (pad_l - 8, gy + 3, esc(monitor.money(value, currency))))
    parts.append('<path d="%s" fill="url(#g)"/>' % area)
    parts.append('<path d="%s" fill="none" stroke="var(--accent)" stroke-width="2" '
                 'stroke-linejoin="round"/>' % line)
    for i, (t, v) in enumerate(points):
        if len(points) <= 60 or i in (0, len(points) - 1):
            parts.append('<circle cx="%.1f" cy="%.1f" r="2.6" fill="var(--accent)">'
                         '<title>%s — %s</title></circle>'
                         % (x(t), y(v), esc(t.astimezone().strftime("%Y-%m-%d %H:%M")),
                            esc(monitor.money(v, currency))))
    for t in (t0, t1):
        parts.append('<text x="%.1f" y="%d" font-size="10" fill="var(--muted)" '
                     'text-anchor="%s">%s</text>'
                     % (x(t), height - 10, "start" if t == t0 else "end",
                        esc(t.astimezone().strftime("%b %d"))))
    parts.append("</svg>")
    return '<div class="chart-wrap">%s</div>' % "".join(parts)


# --------------------------------------------------------------------- pages ---

def dashboard(conn, flash=None, flash_bad=False, under=None, per_model=False,
              show_all=False) -> bytes:
    rows = core.listings(conn)
    total_tracked = len(rows)
    excluded = 0
    if not show_all:
        kept = []
        for listing in rows:
            if core.wanted(conn, listing)[0]:
                kept.append(listing)
            else:
                excluded += 1
        rows = kept

    parts_q = []
    if under is not None:
        parts_q.append("under=%d" % int(under))
    if show_all:
        parts_q.append("all=1")
    base_q = "?" + "&".join(parts_q) if parts_q else "?"

    def sort_key(listing):
        latest_row = core.last_price(conn, listing["id"])
        price_val = latest_row["price"] if latest_row else None
        eff, complete = core.effective_price(conn, listing["dealer"], price_val)
        if eff is None:
            return (1, 0.0, 0)
        return (0, eff, 0 if complete else 1)

    rows = sorted(rows, key=sort_key)
    budget = core.get_float_setting(conn, "budget", 0)
    in_budget = 0
    for listing in rows:
        latest_row = core.last_price(conn, listing["id"])
        value = latest_row["price"] if latest_row else None
        land_val, _, _ = core.landed(conn, listing["dealer"], value)
        if budget and land_val is not None and land_val <= budget \
                and core.wanted(conn, listing)[0]:
            in_budget += 1
    per_model_view = per_model
    if under is not None:
        kept = []
        for listing in rows:
            latest_row = core.last_price(conn, listing["id"])
            value = latest_row["price"] if latest_row else None
            land_val, _, _ = core.landed(conn, listing["dealer"], value)
            eff, _complete = core.effective_price(conn, listing["dealer"], value)
            if eff is not None and eff <= under and core.wanted(conn, listing)[0]:
                kept.append(listing)
        rows = kept
    alt_counts = {}
    if per_model_view:
        pairs = core.cheapest_per_model(conn, rows, None)
        rows = [p[0] for p in pairs]
        alt_counts = {p[0]["id"]: p[1] for p in pairs}
    totals = {}
    drops_30d = 0
    targets_hit = 0
    errors = 0
    body_rows = []
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)

    for listing in rows:
        latest = core.last_price(conn, listing["id"])
        changes = core.price_changes(conn, listing["id"])
        last = core.last_check(conn, listing["id"])
        currency = listing["currency"] or "USD"
        points = [(core.parse_iso(r["checked_at"]), r["price"]) for r in changes]

        if latest:
            bucket = totals.setdefault(currency, [0.0, 0])
            bucket[0] += latest["price"]
            bucket[1] += 1
        first_price = changes[0]["price"] if changes else None
        delta_all = (latest["price"] - first_price) if (latest and first_price) else None
        recent = [p for p in points if p[0] >= cutoff]
        delta_30 = (recent[-1][1] - recent[0][1]) if len(recent) > 1 else None
        if delta_30 and delta_30 < 0:
            drops_30d += 1
        target = listing["target_price"]
        hit = bool(latest and target and latest["price"] <= target)
        if hit:
            targets_hit += 1
        if last and last["status"] != "ok":
            errors += 1

        def signed(value):
            if value is None or abs(value) < 0.005:
                return '<span class="muted">–</span>'
            cls = "down" if value < 0 else "up"
            arrow = "▼" if value < 0 else "▲"
            return '<span class="%s">%s %s</span>' % (
                cls, arrow, esc(monitor.money(abs(value), currency)))

        rev = core.review_for(conn, listing)
        badges = []
        if rev and rev["verdict"]:
            cls = "good" if rev["verdict"] in ("excellent", "very good") else "budget"
            badges.append('<span class="pill %s" title="%s">%s</span>'
                          % (cls, esc(rev["summary"] or ""), esc(rev["verdict"])))
        if alt_counts.get(listing["id"]):
            badges.append('<span class="pill" title="cheapest of %d listings for this model">'
                          '+%d dearer</span>'
                          % (alt_counts[listing["id"]] + 1, alt_counts[listing["id"]]))
        if hit:
            badges.append('<span class="pill hit">★ target</span>')
        if last and last["status"] != "ok":
            badges.append('<span class="pill err" title="%s">%s</span>'
                          % (esc(last["detail"]), esc(last["status"])))
        if not listing["active"]:
            badges.append('<span class="pill">paused</span>')

        spec = " · ".join(filter(None, [
            listing["brand"], ("%ghp" % listing["hp"]) if listing["hp"] else None,
            listing["shaft"], listing["dealer"]]))
        stat = core.stats(conn, listing["id"])

        price_now = latest["price"] if latest else None
        land, dcost, dlabel = core.landed(conn, listing["dealer"], price_now)
        if dcost is None:
            deliv_html = '<span class="pill warn" title="%s">+ delivery ?</span>' % esc(dlabel)
            land_html = ('<span class="muted" title="Delivery unknown, so this is NOT a '
                         'delivered price">%s + ?</span>'
                         % esc(monitor.money(price_now, currency)))
        else:
            deliv_html = esc(monitor.money(dcost, currency)) if dcost else \
                '<span class="pill">free</span>'
            land_html = '<span class="price big">%s</span>' % esc(monitor.money(land, currency))

        body_rows.append(
            "<tr>"
            '<td><a href="/listing?id=%d">%s</a> %s '
            '<a href="%s" target="_blank" rel="noopener" class="muted" '
            'style="font-size:12px" title="open the dealer page">&#8599;</a>'
            '<br><span class="muted" style="font-size:12px">%s</span></td>'
            '<td class="num">%s</td>'
            '<td class="num muted">%s</td><td class="num">%s</td>'
            '<td class="num">%s</td>'
            '<td class="num muted">%s</td>'
            '<td class="num">%s</td>'
            '<td>%s</td>'
            '<td class="num muted" style="font-size:12px">%s</td>'
            "</tr>"
            % (listing["id"], esc(listing["label"]), " ".join(badges),
               esc((listing["url"] or "").split("#")[0]), esc(spec),
               land_html,
               esc(monitor.money(price_now, currency)), deliv_html,
               signed(delta_30),
               esc(monitor.money(stat["lo"], currency)) if stat["n"] else "–",
               esc(monitor.money(target, currency)) if target else '<span class="muted">–</span>',
               sparkline(points),
               esc(core.fmt_local(last["checked_at"])) if last else "never"))

    if totals:
        dominant = max(totals, key=lambda c: totals[c][1])
        total_now, counted = totals[dominant]
        combined = monitor.money(total_now, dominant)
        if len(totals) > 1:
            combined += ' <span class="muted" style="font-size:12px">(%d in %s)</span>' % (
                counted, dominant)
    else:
        combined = "–"

    dealers = sorted({r["dealer"] for r in rows if r["dealer"]})
    known_deliv = sum(1 for d in dealers if core.get_delivery(conn, d) is not None
                      and core.get_delivery(conn, d)["kind"] in ("flat", "free", "threshold"))
    tiles = [
        ("Listings tracked", esc(len(rows))),
        ("Dealers", esc(len(dealers))),
        ("Dropped (30d)", esc(drops_30d)),
        ("At/below target", esc(targets_hit)),
        ("Delivery known", "%s <span class=\"muted\" style=\"font-size:12px\">/ %d</span>"
         % (known_deliv, len(dealers))),
        ("Within budget", '<a href="/?under=%d">%d</a> <span class="muted" '
                          'style="font-size:12px">&le; %s delivered</span>'
         % (budget, in_budget, monitor.money(budget, "GBP")) if budget else esc(in_budget)),
    ]
    tile_html = "".join('<div class="tile"><div class="k">%s</div><div class="v">%s</div></div>'
                        % (esc(k), v) for k, v in tiles)

    table = ('<div class="empty">No listings yet. Paste a dealer product URL below to start '
             'tracking.</div>' if not rows else
             '<table><thead><tr><th>Motor</th>'
             '<th class="num">Delivered to %s</th>'
             '<th class="num">List</th><th class="num">Delivery</th>'
             '<th class="num">30d</th><th class="num">Lowest</th>'
             '<th class="num">Target</th><th>Trend</th><th class="num">Checked</th>'
             '</tr></thead><tbody>%s</tbody></table>'
             % (esc(core.get_setting(conn, "delivery_city")), "".join(body_rows)))

    alerts = core.recent_alerts(conn, limit=6)
    alert_html = "".join(
        '<tr><td class="muted" style="white-space:nowrap">%s</td>'
        '<td><span class="pill">%s</span></td><td>%s</td></tr>'
        % (esc(core.fmt_local(a["created_at"])), esc(a["kind"]), esc(a["message"]))
        for a in alerts)

    lo = core.get_setting(conn, "min_hp", "")
    hi = core.get_setting(conn, "max_hp", "")
    want_shaft = core.get_setting(conn, "shaft", "")
    bits = []
    if lo:
        bits.append("%shp and up" % lo)
    if hi:
        bits.append("up to %shp" % hi)
    if want_shaft:
        bits.append({"S": "short shaft", "L": "long shaft", "XL": "extra-long shaft",
                     "UL": "ultra-long shaft"}.get(want_shaft.upper(), want_shaft + " shaft"))
    if show_all:
        criteria_html = ('<div class="flash">Showing <b>all %d</b> tracked listings, '
                         'ignoring your criteria. <a href="/%s">Apply criteria again</a></div>'
                         % (total_tracked,
                            ("?under=%d" % int(under)) if under is not None else ""))
    elif bits and excluded:
        criteria_html = ('<div class="flash">Filtered to your criteria: <b>%s</b> — '
                         '%d of %d listings hidden. '
                         '<a href="%s">Show everything</a></div>'
                         % (esc(", ".join(bits)), excluded, total_tracked,
                            base_q + ("&" if base_q != "?" else "") + "all=1"))
    else:
        criteria_html = ""

    flash_html = ""
    if flash:
        flash_html = '<div class="flash%s">%s</div>' % (" bad" if flash_bad else "", esc(flash))

    toggle = ('<a href="%s" style="float:right;font-weight:400;text-transform:none;'
              'letter-spacing:0">%s</a>'
              % (base_q + ("" if per_model_view else "&per_model=1"),
                 "show every listing" if per_model_view else "cheapest of each model"))
    heading = ("Within budget — %s delivered to %s or collected"
               % (monitor.money(under, "GBP"), core.get_setting(conn, "delivery_city"))
               if under is not None else "Tracked outboards")
    if per_model_view:
        heading += " · cheapest of each model"
    heading = esc(heading) + toggle
    body = """<div class="wrap">%s%s<div class="grid">%s</div>
<div class="card"><h2>%s</h2>%s</div>
<div class="card"><h2>Add a dealer listing</h2>
<form class="add" method="post" action="/add">
  <div class="span2"><label>Label</label>
    <input name="label" placeholder="Yamaha F150 XB 20&quot;" required></div>
  <div class="span2"><label>Dealer product URL</label>
    <input name="url" type="url" placeholder="https://dealer.example/yamaha-f150" required></div>
  <div><label>Brand</label><input name="brand" placeholder="Yamaha"></div>
  <div><label>HP</label><input name="hp" placeholder="150"></div>
  <div><label>Shaft</label><input name="shaft" placeholder="20&quot;"></div>
  <div><label>Dealer</label><input name="dealer" placeholder="Coastal Marine"></div>
  <div><label>Target price</label><input name="target" placeholder="17500"></div>
  <div><label>Currency</label><select name="currency">%s</select></div>
  <div class="span2"><label>Extraction rule</label>
    <input name="rule" value="auto" placeholder="auto"></div>
  <div style="display:flex;align-items:flex-end"><button class="btn" type="submit">
    Add &amp; check now</button></div>
</form>
<details><summary>Rule syntax — leave as <code>auto</code> unless auto picks the wrong number</summary>
<div style="padding:0 16px 16px" class="muted">
<code>auto</code> tries JSON-LD, then price meta tags, then price-styled elements, then page text.
Other options: <code>css:.price--sale</code>,
<code>attr:meta[property=product:price:amount]@content</code>,
<code>jsonld:lowPrice</code>, <code>regex:Our Price[^0-9]{0,20}([\\d,]+)</code>.<br>
Run <code>./monitor.py probe URL</code> in a terminal to see every candidate and the exact
rule string to paste here.</div></details></div>
%s</div>""" % (
        flash_html, criteria_html, tile_html, heading, table,
        "".join('<option%s>%s</option>' % (" selected" if c == "USD" else "", c)
                for c in ("USD", "AUD", "NZD", "CAD", "GBP", "EUR", "ZAR")),
        ('<div class="card"><h2>Recent alerts</h2><table><tbody>%s</tbody></table>'
         '<div class="actions"><a href="/alerts">All alerts →</a></div></div>' % alert_html)
        if alerts else "")
    return page("Outboard Price Monitor", body)


def listing_page(conn, listing_id: int) -> bytes:
    listing = core.get_listing(conn, listing_id)
    if not listing:
        return page("Not found", '<div class="wrap"><div class="card">'
                                 '<div class="empty">No such listing.</div></div></div>')
    currency = listing["currency"] or "USD"
    changes = core.price_changes(conn, listing_id)
    every = core.history(conn, listing_id, ok_only=False)
    points = [(core.parse_iso(r["checked_at"]), r["price"]) for r in changes]
    stat = core.stats(conn, listing_id)
    latest = core.last_price(conn, listing_id)

    history_rows = []
    previous = None
    for row in reversed(every[-120:]):
        if row["status"] != "ok" or row["price"] is None:
            history_rows.append(
                '<tr><td class="muted">%s</td><td class="num">–</td><td class="num"></td>'
                '<td><span class="pill err">%s</span> <span class="muted">%s</span></td></tr>'
                % (esc(core.fmt_local(row["checked_at"])), esc(row["status"]),
                   esc((row["detail"] or "")[:80])))
            continue
        history_rows.append(
            '<tr><td class="muted">%s</td><td class="num price">%s</td>'
            '<td class="num">%s</td><td class="muted" style="font-size:12px">%s</td></tr>'
            % (esc(core.fmt_local(row["checked_at"])),
               esc(monitor.money(row["price"], currency)), "",
               esc((row["detail"] or "")[:80])))

    spec = " · ".join(filter(None, [
        listing["brand"], ("%g hp" % listing["hp"]) if listing["hp"] else None,
        listing["shaft"], listing["dealer"]]))
    tiles = [
        ("Current", monitor.money(latest["price"] if latest else None, currency)),
        ("Lowest seen", monitor.money(stat["lo"], currency) if stat["n"] else "–"),
        ("Highest seen", monitor.money(stat["hi"], currency) if stat["n"] else "–"),
        ("Target", monitor.money(listing["target_price"], currency)
         if listing["target_price"] else "–"),
        ("Price changes", str(max(0, len(changes) - 1))),
    ]
    tile_html = "".join('<div class="tile"><div class="k">%s</div><div class="v">%s</div></div>'
                        % (esc(k), esc(v)) for k, v in tiles)

    body = """<div class="wrap">
<div style="margin-bottom:14px"><a href="/">← All listings</a></div>
<h2 style="margin:0 0 3px;font-size:21px">%s</h2>
<div class="muted" style="margin-bottom:16px">%s · <a href="%s" target="_blank"
rel="noopener">open dealer page ↗</a> · rule <code>%s</code></div>
<div class="grid">%s</div>
<div class="card"><h2>Price history</h2>%s</div>
<div class="card"><h2>Every check</h2><table><thead><tr><th>When</th>
<th class="num">Price</th><th class="num"></th><th>Source</th></tr></thead>
<tbody>%s</tbody></table></div>
<div class="card"><h2>Actions</h2><div class="actions">
<button class="btn" onclick="return run('/api/check?id=%d')">Check this listing now</button>
<form method="post" action="/toggle" style="display:inline"><input type="hidden" name="id"
value="%d"><button class="btn ghost" type="submit">%s</button></form>
<form method="post" action="/delete" style="display:inline"
onsubmit="return confirm('Delete this listing and its price history?')">
<input type="hidden" name="id" value="%d">
<button class="btn ghost" type="submit">Delete</button></form>
</div></div></div>""" % (
        esc(listing["label"]), esc(spec) or "—", esc(listing["url"]), esc(listing["rule"]),
        tile_html, big_chart(points, currency),
        "".join(history_rows) or '<tr><td colspan="4" class="empty">No checks yet.</td></tr>',
        listing_id, listing_id, "Pause" if listing["active"] else "Resume", listing_id)
    return page(listing["label"], body)


def delivery_page(conn, flash=None) -> bytes:
    dealers = sorted({r["dealer"] for r in core.listings(conn) if r["dealer"]})
    known = {r["dealer"]: r for r in core.all_delivery(conn)}
    postcode = core.get_setting(conn, "postcode")
    city = core.get_setting(conn, "delivery_city")
    rows = []
    for dealer in dealers:
        row = known.get(dealer)
        kind = row["kind"] if row else "quote"
        amount = row["amount"] if row and row["amount"] is not None else ""
        free_over = row["free_over"] if row and row["free_over"] is not None else ""
        note = (row["note"] or "") if row else ""
        count = sum(1 for r in core.listings(conn) if r["dealer"] == dealer)
        opts = "".join('<option value="%s"%s>%s</option>'
                       % (k, " selected" if k == kind else "", k)
                       for k in ("flat", "free", "threshold", "collect", "quote"))
        miles = row["miles"] if row and row["miles"] is not None else ""
        drive, drive_label = core.travel_cost(conn, dealer)
        drive_html = ('<span class="muted" style="font-size:12px">%s%s</span>'
                      % (("collect £%d · " % drive) if drive is not None else "",
                         esc(drive_label)))
        rows.append(
            '<tr><td><b>%s</b><br><span class="muted" style="font-size:12px">%d listings</span></td>'
            '<td><form method="post" action="/set-delivery" style="display:flex;gap:6px;'
            'align-items:center;flex-wrap:wrap">'
            '<input type="hidden" name="dealer" value="%s">'
            '<select name="kind" style="width:auto">%s</select>'
            '<input name="amount" value="%s" placeholder="cost" style="width:90px">'
            '<input name="free_over" value="%s" placeholder="free over" style="width:100px">'
            '<input name="miles" value="%s" placeholder="miles" style="width:80px">'
            '<button class="btn ghost" type="submit">Save</button><br>%s</form></td>'
            '<td class="muted" style="font-size:12px">%s</td></tr>'
            % (esc(dealer), count, esc(dealer), opts, esc(amount), esc(free_over),
               esc(miles), drive_html, esc(note)))
    flash_html = '<div class="flash">%s</div>' % esc(flash) if flash else ""
    body = """<div class="wrap">%s
<h2 style="margin:0 0 4px;font-size:20px">Delivery to %s</h2>
<div class="muted" style="margin-bottom:18px">Landed cost = listed price + delivery.
Where a dealer's own cart quoted a rate for %s it is filled in below; the rest need a
quote from the dealer, and their listings show no landed price until you set one.</div>
<div class="card"><h2>Per-dealer delivery</h2><table><thead><tr><th>Dealer</th>
<th>Terms — kind, cost, free-over, miles from you</th><th>Source</th></tr></thead><tbody>%s</tbody></table></div>
<div class="card"><h2>What the options mean</h2>
<div style="padding:14px 16px" class="muted">
<b>flat</b> — same fee on every order (put it in "cost").<br>
<b>free</b> — no delivery charge.<br>
<b>threshold</b> — "cost" applies below the "free over" order value, free at or above it.<br>
<b>collect</b> — collection only, or delivery arranged separately; no landed price shown.<br>
<b>quote</b> — not known yet; no landed price shown.<br><br>
<b>miles</b> — road distance from you. Collecting is costed at %s a mile for the round
trip, and anything beyond %s miles is treated as too far. Each listing then uses
whichever is cheaper, delivery or driving.</div></div></div>""" % (
        flash_html, esc(city), esc(postcode), "".join(rows),
        esc(monitor.money(core.get_float_setting(conn, "travel_per_mile", 0), "GBP")),
        esc(core.get_setting(conn, "max_travel_miles")))
    return page("Delivery", body)


def settings_page(conn, flash=None) -> bytes:
    groups = []
    for group, items in core.SETTINGS_SPEC:
        rows = []
        for key, label, kind, help_text in items:
            value = core.get_setting(conn, key, "") or ""
            if kind.startswith("choice:"):
                choices = kind.split(":", 1)[1].split("|")
                field = ('<select name="%s">%s</select>'
                         % (key, "".join('<option value="%s"%s>%s</option>'
                                         % (esc(c), " selected" if str(value) == c else "",
                                            esc(c) if c else "any")
                                         for c in choices)))
            else:
                field = ('<input name="%s" value="%s" inputmode="%s">'
                         % (key, esc(value), "decimal" if kind == "number" else "text"))
            rows.append('<tr><td style="width:230px"><label for="%s">%s</label><br>'
                        '<span class="muted" style="font-size:12px">%s</span></td>'
                        '<td style="width:220px">%s</td>'
                        '<td class="muted" style="font-size:12px">%s</td></tr>'
                        % (esc(key), esc(label), esc(key), field, esc(help_text)))
        groups.append('<div class="card"><h2>%s</h2><table><tbody>%s</tbody></table></div>'
                      % (esc(group), "".join(rows)))
    flash_html = '<div class="flash">%s</div>' % esc(flash) if flash else ""
    body = """<div class="wrap">%s
<h2 style="margin:0 0 4px;font-size:20px">Settings</h2>
<div class="muted" style="margin-bottom:18px">Leave anything blank to switch it off.
These drive both what the dashboard shows and what you get alerted about.</div>
<form method="post" action="/save-settings">%s
<div class="card"><div class="actions">
<button class="btn" type="submit">Save settings</button>
<a href="/" class="muted" style="margin-left:8px">Back to dashboard</a>
</div></div></form></div>""" % (flash_html, "".join(groups))
    return page("Settings", body)


def setup_page(conn) -> bytes:
    """Shown on a fresh install, before anything is tracked."""
    body = """<div class="wrap">
<div class="card"><h2>Welcome</h2>
<div style="padding:16px 18px;line-height:1.6">
<p style="margin-top:0">Nothing is being tracked yet. Two things to do:</p>
<p><b>1. Tell it what you are looking for</b> — your town, how far you would drive to
collect, your budget and the size of motor you want. That drives both the dashboard and
the alerts, so it is worth setting first.</p>
<p><a class="btn" href="/settings">Open settings</a></p>
<p style="margin-top:22px"><b>2. Add some listings.</b> Paste a dealer's product URL in
the form on the dashboard, or stock it in bulk from a terminal:</p>
<pre style="background:var(--bg);padding:12px;border-radius:8px;overflow-x:auto"><code>./monitor.py probe "https://dealer.example/yamaha-f6"
./monitor.py add "Yamaha F6" "https://dealer.example/yamaha-f6" --brand Yamaha --hp 6
./monitor.py crawl "https://dealer.example" --dry-run</code></pre>
<p><a href="/">Go to the dashboard</a></p>
</div></div></div>"""
    return page("Welcome", body)


def alerts_page(conn) -> bytes:
    rows = core.recent_alerts(conn, limit=200)
    if not rows:
        body = ('<div class="wrap"><div class="card"><div class="empty">'
                'No alerts yet.</div></div></div>')
        return page("Alerts", body)
    trs = "".join(
        '<tr><td class="muted" style="white-space:nowrap">%s</td>'
        '<td><span class="pill%s">%s</span></td><td>%s</td>'
        '<td>%s</td></tr>'
        % (esc(core.fmt_local(a["created_at"])),
           " hit" if a["kind"] == "target" else "", esc(a["kind"]), esc(a["message"]),
           ('<a href="/listing?id=%d">view</a>' % a["listing_id"]) if a["listing_id"] else "")
        for a in rows)
    body = ('<div class="wrap"><div class="card"><h2>All alerts</h2>'
            '<table><thead><tr><th>When</th><th>Kind</th><th>What happened</th><th></th>'
            '</tr></thead><tbody>%s</tbody></table></div></div>' % trs)
    return page("Alerts", body)


# ------------------------------------------------------------------- handler ---

def _background_check(ids=None):
    """Queue a check. Requests made while a sweep runs are appended, not dropped."""
    def work():
        conn = core.init_db()
        try:
            while True:
                with _check_lock:
                    if not _check_queue:
                        _check_state["running"] = False
                        return
                    batch = list(_check_queue)
                    del _check_queue[:]
                wanted = None if None in batch else batch
                rows = ([core.get_listing(conn, i) for i in wanted] if wanted
                        else core.listings(conn, active_only=True))
                rows = [r for r in rows if r is not None]
                _check_state.update(done=0, total=len(rows))
                for listing in rows:
                    try:
                        monitor.check_listing(conn, listing)
                    except Exception as exc:  # never let one bad page kill the sweep
                        _check_state["message"] = str(exc)
                    _check_state["done"] += 1
        finally:
            _check_state["running"] = False
            conn.close()

    with _check_lock:
        _check_queue.extend(ids if ids else [None])
        if _check_state["running"]:
            return
        _check_state.update(running=True, done=0, total=len(_check_queue))
    threading.Thread(target=work, daemon=True).start()


class Handler(BaseHTTPRequestHandler):
    server_version = "OutboardMonitor/1.0"

    def log_message(self, fmt, *args):
        pass  # keep the terminal clean

    def _send(self, payload, status=200, ctype="text/html; charset=utf-8"):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _redirect(self, location="/"):
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        parts = urlparse(self.path)
        query = parse_qs(parts.query)
        conn = core.init_db()
        try:
            if parts.path == "/" and not core.is_configured(conn):
                self._send(setup_page(conn))
            elif parts.path == "/setup":
                self._send(setup_page(conn))
            elif parts.path == "/settings":
                self._send(settings_page(conn, flash=(query.get("msg") or [None])[0]))
            elif parts.path == "/":
                cap = None
                if query.get("under"):
                    try:
                        cap = float(query["under"][0])
                    except ValueError:
                        cap = None
                self._send(dashboard(conn, flash=(query.get("msg") or [None])[0],
                                     flash_bad=bool(query.get("bad")), under=cap,
                                     per_model=bool(query.get("per_model")),
                                     show_all=bool(query.get("all"))))
            elif parts.path == "/listing":
                self._send(listing_page(conn, int((query.get("id") or ["0"])[0])))
            elif parts.path == "/alerts":
                core.mark_alerts_seen(conn)
                self._send(alerts_page(conn))
            elif parts.path == "/delivery":
                self._send(delivery_page(conn, flash=(query.get("msg") or [None])[0]))
            elif parts.path == "/api/status":
                self._send(json.dumps(_check_state).encode(), ctype="application/json")
            elif parts.path == "/favicon.ico":
                self._send(b"", status=404)
            else:
                self._send(page("Not found", '<div class="wrap"><div class="card">'
                                             '<div class="empty">Not found.</div>'
                                             '</div></div>'), status=404)
        except Exception as exc:
            self._send(page("Error", '<div class="wrap"><div class="flash bad">%s</div></div>'
                            % esc(exc)), status=500)
        finally:
            conn.close()

    def do_POST(self):
        parts = urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        form = parse_qs(self.rfile.read(length).decode("utf-8", "replace")) if length else {}
        query = parse_qs(parts.query)

        def field(name, default=None):
            value = (form.get(name) or [""])[0].strip()
            return value or default

        conn = core.init_db()
        try:
            if parts.path == "/add":
                url, label = field("url"), field("label")
                if not url or not label:
                    self._redirect("/?bad=1&msg=Label+and+URL+are+required")
                    return
                try:
                    hp = float(field("hp")) if field("hp") else None
                except ValueError:
                    hp = None
                try:
                    target = float(str(field("target", "")).replace(",", "").replace("$", "")) \
                        if field("target") else None
                except ValueError:
                    target = None
                try:
                    listing_id = core.add_listing(
                        conn, label, url, dealer=field("dealer"), brand=field("brand"),
                        hp=hp, shaft=field("shaft"), rule=field("rule", "auto"),
                        currency=field("currency", "USD"), target_price=target)
                except Exception as exc:
                    message = ("That URL is already tracked." if "UNIQUE" in str(exc)
                               else str(exc))
                    self._redirect("/?bad=1&msg=" + message.replace(" ", "+")[:120])
                    return
                _background_check([listing_id])
                self._redirect("/?msg=Added+%s+-+fetching+price..."
                               % label.replace(" ", "+")[:60])
            elif parts.path == "/api/check":
                ids = [int(i) for i in query.get("id", [])] or None
                _background_check(ids)
                self._send(json.dumps({"started": True}).encode(),
                           ctype="application/json")
            elif parts.path == "/save-settings":
                saved = 0
                for _group, key, _label, kind, _help in core.settings_spec_flat():
                    if key not in form:
                        continue
                    raw = (form.get(key) or [""])[0].strip()
                    if kind == "number" and raw:
                        raw = raw.replace(",", "").replace("£", "").strip()
                        try:
                            float(raw)
                        except ValueError:
                            continue          # ignore junk rather than storing it
                    core.set_setting(conn, key, raw)
                    saved += 1
                self._redirect("/settings?msg=Saved+%d+settings" % saved)
            elif parts.path == "/set-delivery":
                dealer = field("dealer")
                kind = field("kind", "quote")
                def num(name):
                    raw = (field(name) or "").replace(",", "").replace("£", "")
                    try:
                        return float(raw) if raw else None
                    except ValueError:
                        return None
                if dealer:
                    core.set_delivery(conn, dealer, kind, amount=num("amount"),
                                      free_over=num("free_over"),
                                      note="set by hand", source="manual")
                    miles = num("miles")
                    if miles is not None:
                        conn.execute("UPDATE delivery SET miles = ? WHERE dealer = ?",
                                     (miles, dealer))
                        conn.commit()
                self._redirect("/delivery?msg=Saved+" + dealer.replace(" ", "+")[:40])
            elif parts.path == "/delete":
                core.delete_listing(conn, int(field("id", "0")))
                self._redirect("/?msg=Listing+deleted")
            elif parts.path == "/toggle":
                listing_id = int(field("id", "0"))
                listing = core.get_listing(conn, listing_id)
                if listing:
                    core.update_listing(conn, listing_id,
                                        active=0 if listing["active"] else 1)
                self._redirect("/listing?id=%d" % listing_id)
            else:
                self._send(b"Not found", status=404, ctype="text/plain")
        except Exception as exc:
            self._send(page("Error", '<div class="wrap"><div class="flash bad">%s</div></div>'
                            % esc(exc)), status=500)
        finally:
            conn.close()


def lan_address():
    """This machine's address on the local network, or None."""
    import socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("192.168.1.1", 1))     # no packets sent; just picks the route
        return sock.getsockname()[0]
    except OSError:
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return None
    finally:
        sock.close()


def serve(port: int = 8765, host: str = "127.0.0.1", open_browser: bool = True) -> None:
    core.init_db()
    httpd = ThreadingHTTPServer((host, port), Handler)
    url = "http://127.0.0.1:%d/" % port
    print("Outboard Price Monitor running at %s" % url)
    if host in ("0.0.0.0", "::"):
        lan = lan_address()
        if lan:
            print("On your home network:      http://%s:%d/" % (lan, port))
            print("  (any device on the same wifi - phone, tablet, another Mac)")
        print("  Anyone on your network can view AND edit this - no password.")
    print("Press Ctrl+C to stop.")
    if open_browser:
        threading.Timer(0.7, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        httpd.server_close()
