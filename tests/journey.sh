#!/bin/zsh
# End-to-end user journey against a FRESH database, as a new user would meet it:
# empty install -> setup -> find dealers -> probe -> add -> delivery -> check ->
# every view -> edit -> schedule -> every web page and form.
#
# Hits real dealer sites, so it takes a couple of minutes and needs a network.
# Uses a throwaway database; your own prices.db is untouched.
#
# The postcode paths (geocoding, distance, delivery-by-postcode) hit the live
# postcodes.io API, so they need REAL postcodes - a made-up one does not resolve.
# The two below are landmarks (Manchester city centre, Buckingham Palace), chosen
# so no contributor's own address ends up in the repo. Override to test your area:
#
#   OUTBOARD_TEST_POSTCODE="..." OUTBOARD_TEST_DEALER_POSTCODE="..." ./tests/journey.sh
#
#   ./tests/journey.sh
set -u
cd "$(dirname "$0")/.."
D=$(mktemp -d)
export OUTBOARD_DB=$D/journey.db
rm -f "$OUTBOARD_DB" "$OUTBOARD_DB-shm" "$OUTBOARD_DB-wal" 2>/dev/null
PASS=0; FAIL=0
# Landmark postcodes, not anyone's home. Must be real: postcodes.io is queried.
PC=${OUTBOARD_TEST_POSTCODE:-M1 1AE}
DEALER_PC=${OUTBOARD_TEST_DEALER_POSTCODE:-SW1A 1AA}
step() { printf "\n\033[1m%s\033[0m\n" "$1"; }
ok()   { PASS=$((PASS+1)); printf "   PASS  %s\n" "$1"; }
bad()  { FAIL=$((FAIL+1)); printf "   FAIL  %s\n" "$1"; }
try()  { if eval "$2" >/dev/null 2>&1; then ok "$1"; else bad "$1"; fi }

step "1. Fresh clone - what does a new user see?"
./monitor.py list 2>&1 | grep -q "No listings yet" && ok "list says nothing tracked" || bad "list on empty db"
./monitor.py settings 2>&1 | grep -q "fresh install" && ok "settings suggests running setup" || bad "no setup hint"

step "2. Guided setup (answering the prompts)"
# One answer per prompt, in the order core.settings_spec_flat() yields them:
# budget min_hp max_hp shaft city postcode travel per_mile road_factor collect
# assume_delivery svc1 svcN years drop notify min_plaus max_plaus
# workers interval robots
printf '1500\n5\n8\nS\nManchester\n%s\n60\n0.30\n1.35\n75\n\n90\n180\n5\n2\n1\n\n\n400\n150000\n4\n4\n1\n' "$PC" | ./monitor.py setup 2>&1 | tail -3
[ "$(./monitor.py settings | grep -c '150000')" -ge 1 ] && ok "answers line up with prompts" || bad "setup answers misaligned"
[ "$(./monitor.py settings | grep -c "$PC")" -ge 1 ] && ok "postcode saved" || bad "postcode not saved"
[ "$(./monitor.py settings | grep -c '1500')" -ge 1 ] && ok "budget saved" || bad "budget not saved"

step "3. Find local dealers"
./monitor.py find-dealers 2>&1 | grep -q "outboard dealer" && ok "suggests local searches" || bad "find-dealers"
./monitor.py find-dealers 2>&1 | grep -qi "tohatsu" && ok "lists manufacturer locators" || bad "no locators"

step "4. Probe a real dealer page before adding"
./monitor.py probe "https://boatworld.co.uk/products/tohatsu-4-stroke-6hp-outboard-engine" 2>&1 | tee $D/j_probe.txt | tail -3
grep -q "auto would pick" $D/j_probe.txt && ok "probe found a price and suggested a rule" || bad "probe"

step "4b. Populate from a seed dealer (what an empty install does)"
try "populate --dry-run" './monitor.py populate --only "Dulas Boats" --max 4 --dry-run'
./monitor.py populate --only "Dulas Boats" --max 4 --quiet >/dev/null 2>&1
[ "$(./monitor.py list --all-hp 2>/dev/null | grep -c '^[0-9]')" -ge 1 ] \
  && ok "populate added listings" || bad "populate added nothing"
[ "$(./monitor.py list --all-hp 2>/dev/null | grep -c 'Dulas')" -ge 1 ] \
  && ok "listings filed under the dealer" || bad "dealer not recorded"

step "5. Add three listings"
try "add Tohatsu 6hp"  './monitor.py add "Tohatsu 6hp short shaft" "https://boatworld.co.uk/products/tohatsu-4-stroke-6hp-outboard-engine" --brand Tohatsu --hp 6 --shaft S --dealer BoatWorld --currency GBP'
try "add Honda 5hp"    './monitor.py add "Honda 5hp short shaft" "https://boatworld.co.uk/products/honda-5hp-4-stroke-short-shaft-outboard-engine" --brand Honda --hp 5 --shaft S --dealer BoatWorld --currency GBP'
try "add Mercury 6hp"  './monitor.py add "Mercury FourStroke 6" "https://dulasboats.co.uk/products/mer-6" --brand Mercury --hp 6 --dealer "Dulas Boats" --currency GBP'
[ "$(./monitor.py list --all-hp 2>/dev/null | grep -c '^[0-9]')" -ge 3 ] && ok "three listings present" || bad "listings missing"

step "6. Set delivery, one by postcode"
try "delivery by postcode" './monitor.py delivery --dealer "BoatWorld" --kind free --postcode "'"$DEALER_PC"'"'
try "delivery flat rate"   './monitor.py delivery --dealer "Dulas Boats" --kind flat --amount 95'
./monitor.py delivery 2>&1 | grep -q "BoatWorld" && ok "delivery table shows dealers" || bad "delivery table"
try "delivery --recompute" './monitor.py delivery --recompute'
try "delivery-scan"        './monitor.py delivery-scan --dealer "BoatWorld"'

step "7. Check prices"
./monitor.py check 2>&1 | tee $D/j_check.txt | tail -4
grep -qE "[0-9]+/[0-9]+ checked OK" $D/j_check.txt && ok "check ran" || bad "check"
grep -q "0/3" $D/j_check.txt && bad "all checks failed" || ok "prices extracted"

step "8. The views a user actually uses"
try "list"                './monitor.py list'
try "list --under"        './monitor.py list --under 2000'
try "list --per-model"    './monitor.py list --per-model'
try "list --tco"          './monitor.py list --tco'
try "list --links"        './monitor.py list --links'
try "list --reviews"      './monitor.py list --reviews'
try "compare MFS6"        './monitor.py compare MFS6'
try "history"             './monitor.py history 1'
try "alerts"              './monitor.py alerts'
try "reviews"             './monitor.py reviews'
try "export"              './monitor.py export --out '"$D"'/j_export.csv'
[ -s $D/j_export.csv ] && ok "csv written" || bad "csv empty"

step "8b. Backfill horsepower from titles"
try "backfill-hp --dry-run" './monitor.py backfill-hp --dry-run'
try "backfill-hp"          './monitor.py backfill-hp'

step "8c. AI commands degrade politely with no key"
./monitor.py ai-delivery 2>&1 | grep -q "optional" && ok "ai-delivery explains what is missing" || bad "ai-delivery"
./monitor.py ai-dealers 2>&1 | grep -q "optional" && ok "ai-dealers explains what is missing" || bad "ai-dealers"
./monitor.py settings 2>&1 | grep -q "ai_api_key" && ok "ai settings listed" || bad "ai settings missing"

step "9. Edit and remove"
try "edit target"  './monitor.py edit 1 --target 1000'
try "pause"        './monitor.py edit 2 --pause'
try "resume"       './monitor.py edit 2 --resume'

step "10. Scheduling"
./monitor.py schedule --at 09:00 --print-only 2>&1 | grep -q StartCalendarInterval && ok "daily plist generated" || bad "plist"
./monitor.py schedule --hours 6 --print-only 2>&1 | grep -q StartInterval && ok "interval plist generated" || bad "plist interval"

step "10b. Dashboard JavaScript actually parses"
./tests/check_js.py >/dev/null 2>&1 && ok "dashboard JS has no broken strings" \
  || bad "dashboard JS would not run in a browser"

step "11. Web dashboard - every page over HTTP"
./monitor.py serve --port 8899 --no-open > $D/j_web.log 2>&1 &
WEBPID=$!
sleep 4
python3 - <<'PY'
import urllib.request, urllib.parse, urllib.error, sys
B="http://127.0.0.1:8899"; bad=0
def get(p):
    try:
        with urllib.request.urlopen(B+p, timeout=30) as r: return r.status, r.read().decode()
    except urllib.error.HTTPError as e: return e.code, e.read().decode()
    except Exception as e: return 0, str(e)
for p in ("/","/?under=2000","/?under=2000&per_model=1","/?all=1","/delivery","/dealers","/settings","/alerts","/listing?id=1","/api/status"):
    s,h=get(p)
    print("   %s  GET %-30s %s" % ("PASS " if s==200 else "FAIL ", p, s))
    bad += (s!=200)
s,h=get("/nope"); print("   %s  GET %-30s %s (404 expected)" % ("PASS " if s==404 else "FAIL ", "/nope", s)); bad += (s!=404)
# the populate button and its status feed, without starting a real crawl
s,h=get("/")
ok_btn = "Populate from dealers" in h and "function populate()" in h
print("   %s  populate button on dashboard" % ("PASS " if ok_btn else "FAIL ")); bad += (not ok_btn)
s,h=get("/api/status")
ok_api = '"populate"' in h
print("   %s  populate progress in /api/status" % ("PASS " if ok_api else "FAIL ")); bad += (not ok_api)
# the confirmation is built from these, so they must be served
import json as _json
cfg = (_json.loads(h).get("populate") or {}).get("config") or {}
ok_cfg = all(k in cfg for k in ("dealers","workers","interval","pages","minutes"))
print("   %s  populate config for the prompt" % ("PASS " if ok_cfg else "FAIL ")); bad += (not ok_cfg)
for path,data in (("/save-settings",{"budget":"1750","min_hp":"5"}),
                  ("/set-delivery",{"dealer":"BoatWorld","kind":"free","miles":"44"}),
                  ("/set-delivery",{"dealer":"BoatWorld","kind":"free","postcode":"S41 9PZ"}),
                  ("/add",{"label":"Web added Yamaha","url":"https://dulasboats.co.uk/products/yamaha-6hp","brand":"Yamaha","hp":"6","currency":"GBP"})):
    req=urllib.request.Request(B+path, data=urllib.parse.urlencode(data).encode())
    try:
        with urllib.request.urlopen(req, timeout=25) as r: code=r.status
    except urllib.error.HTTPError as e: code=e.code
    print("   %s  POST %-29s %s" % ("PASS " if code in (200,303) else "FAIL ", path, code))
    bad += code not in (200,303)
sys.exit(1 if bad else 0)
PY
if [ $? -eq 0 ]; then ok "all web pages and forms"; else bad "web pages/forms"; fi
kill $WEBPID 2>/dev/null

step "RESULT"
printf "   %d passed, %d failed\n" $PASS $FAIL
echo "JOURNEY DONE"
