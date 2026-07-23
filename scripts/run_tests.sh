#!/usr/bin/env bash
#
# Canonical test / verification runner for ethara-etp (etp_assessment_pro).
#
# Hermes' verification gate auto-detects scripts/run_tests.sh as the project's
# canonical verify command (agent/coding_context.py detect_project_facts), so
# running it records real green/red verification evidence and stops the gate
# re-firing "unverified" every turn.
#
# It runs the REAL Odoo test suite live against Postgres, wiring up the three
# environment quirks this repo needs (documented inline below). Pass a class or
# tag filter as the first arg to scope the run; omit it for the whole module.
#
# Usage:
#   scripts/run_tests.sh                         # full etp_assessment_pro suite
#   scripts/run_tests.sh TestGeneratorChatterLog # one class
#   scripts/run_tests.sh :TestAssessmentFeatureBatch
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

DB="${ETP_TEST_DB:-etp_ux_test}"
MODULE="etp_assessment_pro"
PG_PORT="${ETP_PG_PORT:-5433}"
# A separate HTTP port from the live dev server (default 8069) so the suite can
# run while a werkzeug instance is up for browser verification -- otherwise Odoo
# aborts with "Port 8069 is in use by another program".
HTTP_PORT="${ETP_TEST_HTTP_PORT:-8071}"
PG_PREFIX="/opt/homebrew/opt/postgresql@16"
PG_DATADIR="/opt/homebrew/var/postgresql@16"

# Optional test-class / tag filter. Bare "Name" -> ":Name" (a class filter);
# an explicit ":Name" or "/tag" is passed through untouched.
FILTER="${1:-}"
if [ -n "$FILTER" ]; then
    case "$FILTER" in
        :*|/*) TAGS="/$MODULE$FILTER" ;;
        *)     TAGS="/$MODULE:$FILTER" ;;
    esac
else
    TAGS="/$MODULE"
fi

# (1) A leaked PYTHONPATH (e.g. the agent's own venv) shadows the project venv's
#     site-packages, so Odoo imports deps from the wrong interpreter and dies
#     with "cannot import name '_imaging' from 'PIL'". Clear it.
unset PYTHONPATH

# (2) macOS postgresql@16 aborts with "postmaster became multithreaded during
#     startup" unless the locale is a real UTF-8 one.
export LC_ALL="${LC_ALL:-en_US.UTF-8}"

# Start Postgres on the repo's non-default port if it is not already listening
# (brew services ignores the -p in odoo.conf; start it manually).
if ! /usr/sbin/lsof -nP -iTCP:"$PG_PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "[run_tests] starting postgresql@16 on port $PG_PORT ..."
    "$PG_PREFIX/bin/pg_ctl" -D "$PG_DATADIR" -o "-p $PG_PORT" -l /tmp/pg.log start
    sleep 2
fi

echo "[run_tests] db=$DB module=$MODULE tags=$TAGS"

# (3) The repo ships an OpenSSL legacy config the bundled Python needs for the
#     Vertex TLS handshake path exercised in some tests.
OPENSSL_CONF="$REPO_ROOT/openssl-legacy.cnf" \
    .venv/bin/python src/odoo-bin \
        -c odoo.conf \
        -d "$DB" \
        -u "$MODULE" \
        --http-port="$HTTP_PORT" \
        --test-enable \
        --test-tags "$TAGS" \
        --stop-after-init \
        --log-level=test
