#!/usr/bin/env bash
# Walk the REST API as all three roles against a running server with demo data.
#
#   odoo-bin -d <db> -i ethara_project_os --with-demo --stop-after-init
#   odoo-bin -d <db>                       # leave it running
#   ./custom_addons/ethara_project_os/tools/api_smoke.sh [base_url]
#
# It only reads, except for one deliberate 403 attempt, so it is safe to re-run.
set -uo pipefail
BASE="${1:-http://localhost:8069}"

# Pull a nested key out of a JSON response without depending on jq being installed.
jq_get() {
  python3 -c '
import json, sys
value = json.load(sys.stdin)
for key in sys.argv[1:]:
    value = value[int(key)] if key.isdigit() else value[key]
print(value)' "$@" 2>/dev/null
}

token() {
  curl -s -X POST "$BASE/api/v1/auth_token" -H 'Content-Type: application/json' \
       -d "{\"login\":\"$1\",\"password\":\"demo1234\"}" | jq_get data access_token
}

call() { # label  token  method  path  [body]
  local label="$1" tok="$2" method="$3" path="$4" body="${5:-}"
  printf '  %-44s ' "$label"
  local args=(-s -o /tmp/epo_out -w '%{http_code}' -X "$method"
              -H "access-token: $tok" "$BASE/api/project-os$path")
  [ -n "$body" ] && args+=(-H 'Content-Type: application/json' -d "$body")
  local code; code=$(curl "${args[@]}")
  local msg; msg=$(python3 -c "
import json
try:  print((json.load(open('/tmp/epo_out')).get('message') or '')[:58])
except Exception: print('')" 2>/dev/null)
  printf '%s  %s\n' "$code" "$msg"
}

echo "── signing in ──"
PM=$(token gita@demo.ethara)
PL=$(token piyush@demo.ethara)
Tasker=$(token mira@demo.ethara)
for pair in "PM:$PM" "PL:$PL" "Tasker:$Tasker"; do
  name=${pair%%:*}; tok=${pair#*:}
  [ -n "$tok" ] && echo "  $name ok" || { echo "  $name FAILED to sign in"; exit 1; }
done

PROJECT=$(curl -s -H "access-token: $PM" "$BASE/api/project-os/projects" \
          | python3 -c "
import json,sys
for p in json.load(sys.stdin)['data']:
    if p['code'].startswith('DEMO-MM'): print(p['id']); break" 2>/dev/null)
echo "  demo project id: ${PROJECT:-not found}"

echo
echo "── the Tasker's day ──"
call "who am I"                     "$Tasker"  GET  "/me"
call "my onboarding"                "$Tasker"  GET  "/me/onboarding"
call "the form I have to fill"      "$Tasker"  GET  "/me/form?form_type=stagelist"
call "my submission counts"         "$Tasker"  GET  "/counts?form_type=stagelist"
call "the knowledge folder"         "$Tasker"  GET  "/projects/$PROJECT/folders"

echo
echo "── the pod lead's board ──"
call "today's roster"               "$PL"  GET  "/roster"
call "who is still ramping up"      "$PL"  GET  "/onboarding?pending_only=1"
call "my pod's submissions"         "$PL"  GET  "/submissions?limit=5"

echo
echo "── the PM's workbench ──"
call "every project"                "$PM" GET  "/projects"
call "one project in detail"        "$PM" GET  "/projects/$PROJECT"
call "who can I staff"              "$PM" GET  "/projects/$PROJECT/candidates"
call "current allocations"          "$PM" GET  "/allocations"
call "org-wide analytics"           "$PM" GET  "/analytics/overview"

echo
echo "── history, both readings ──"
EMP=$(curl -s -H "access-token: $Tasker" "$BASE/api/project-os/me" | jq_get data employee id)
call "one person's whole history"   "$PM" GET  "/employees/$EMP/history"
call "one project's whole history"  "$PM" GET  "/projects/$PROJECT/history"

echo
echo "── refusals (these SHOULD fail) ──"
call "Tasker creating a project → 403"  "$Tasker"  POST "/projects" '{"name":"Nope"}'
call "Tasker reading candidates → 403"  "$Tasker"  GET  "/projects/$PROJECT/candidates"
call "allocate with no id → 400"    "$PM" POST "/allocations" '{}'
call "limit=abc → 400"              "$PM" GET  "/submissions?limit=abc"
call "no token at all → 401"        ""     GET  "/me"

rm -f /tmp/epo_out
echo
echo "Expected: 200s above, then 403 403 400 400 401."
