#!/usr/bin/env bash
set -euo pipefail

# =============================================================================
# Skoll-owned hotfix patches for the OpenClaw plugin/SDK tree baked into this
# sandbox image (see ../Dockerfile). Runs at IMAGE BUILD time, as root.
#
# Every patch MUST be:
#   idempotent  - re-running on an already-patched tree is a no-op (sentinel gate)
#   safe        - if the target file/symbol is absent (upstream fixed it, or the
#                 layout changed), skip with a WARN; do NOT fail the build
#   fail-loud   - if the target IS present but cannot be patched (e.g. an
#                 unrecognised module format), exit non-zero so a half-patched
#                 image never ships
#
# To add a patch: append a new self-contained, sentinel-gated block below and
# document its upstream root cause and removal condition (here and in README.md).
# =============================================================================

SDK_FILE="/app/dist/plugin-sdk/provider-web-search.js"
SENTINEL="SKOLL_HOTFIX_SDK_READ_POSITIVE_INTEGER"
log() { echo "[skoll-openclaw-patches] $*"; }
log "starting"

# ---------------------------------------------------------------------------
# Patch 1: restore readPositiveIntegerParam on the web-search provider SDK.
#
# Symptom (web_search tool call inside the pod):
#   {"status":"error","tool":"web_search",
#    "error":"(0 , _providerWebSearch.readPositiveIntegerParam) is not a function"}
#
# Root cause: upstream tag 2026.5.28 ships @openclaw/brave-plugin importing
# readPositiveIntegerParam from the host SDK module
# "openclaw/plugin-sdk/provider-web-search", but that module only exports
# readNumberParam. We restore the missing export ON THE SDK MODULE, which fixes
# every importer without surgery on the minified, hash-named plugin bundle.
#
# Self-contained on purpose: rather than delegate to the module's own
# readNumberParam (imported as `f as readNumberParam` from ../common-*.js, whose
# exact signature and param-reading semantics aren't guaranteed stable across
# upstream builds), the shim re-implements the {max, message} positive-integer
# contract directly. Correct regardless of how the SDK binds/renames internally.
#
# The SDK lives at /app/dist (image layer); the running pod mounts a PVC over
# /home/node/.openclaw but NOT over /app, so this patch is not shadowed and
# needs no PVC/volume action on rollout.
#
# Remove this patch when upstream restores the export or we move off 2026.5.28.
# ---------------------------------------------------------------------------
[ -f "$SDK_FILE" ] || { log "WARN: $SDK_FILE missing; SDK patch skipped"; exit 0; }
grep -q "$SENTINEL" "$SDK_FILE" && { log "already patched; skipping (idempotent)"; exit 0; }
grep -q "\breadPositiveIntegerParam\b" "$SDK_FILE" && { log "SDK already provides readPositiveIntegerParam; nothing to do"; exit 0; }

SDK_FILE="$SDK_FILE" SENTINEL="$SENTINEL" python3 - <<'PY'
import os
import pathlib
import sys

p = pathlib.Path(os.environ["SDK_FILE"])
sentinel = os.environ["SENTINEL"]
src = p.read_text()

is_cjs = ("module.exports" in src) or ("exports." in src) or ("require(" in src)
is_esm = (not is_cjs) and (
    ("export " in src) or ("export{" in src) or ("import " in src)
)
if not (is_esm or is_cjs):
    sys.exit(
        "FATAL: %s is neither recognisably ESM nor CJS; refusing to ship a "
        "half-patched image" % p
    )

fn = (
    "\n// %s\n" % sentinel
    + "// brave-plugin@2026.5.28 imports readPositiveIntegerParam from this SDK\n"
    + "// module, which this build dropped. Self-contained on purpose: re-implement\n"
    + "// the {max, message} contract instead of depending on the module's own\n"
    + "// readNumberParam signature/semantics, which aren't stable across builds.\n"
    + "function readPositiveIntegerParam(params, key, options) {\n"
    + "  options = options || {};\n"
    + "  var max = options.max, message = options.message;\n"
    + "  var raw = (params == null) ? undefined : params[key];\n"
    + "  if (raw === undefined || raw === null || raw === '') return undefined;\n"
    + "  var value = (typeof raw === 'number') ? raw : parseInt(String(raw), 10);\n"
    + "  if (!Number.isInteger(value) || value < 1"
    + " || (typeof max === 'number' && value > max)) {\n"
    + "    throw new Error(message || (key + ' must be a positive integer'"
    + " + (typeof max === 'number' ? (' <= ' + max + '.') : '.')));\n"
    + "  }\n"
    + "  return value;\n"
    + "}\n"
)
src += fn + (
    "export { readPositiveIntegerParam };\n"
    if is_esm
    else "exports.readPositiveIntegerParam = readPositiveIntegerParam;\n"
)
p.write_text(src)
print("[skoll-openclaw-patches] readPositiveIntegerParam restored (%s)"
      % ("ESM" if is_esm else "CJS"))
PY

log "done"
