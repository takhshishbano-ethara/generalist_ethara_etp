#!/usr/bin/env bash
#
# Pre-apply sanity check for the Leviathan K8s manifests.
#
# Catches the most common "I forgot to edit a placeholder" failure
# mode before `kubectl apply` silently succeeds against the wrong
# target. Run as part of your CI / pre-deploy step:
#
#   bash custom_addons/leviathan/deploy/validate.sh
#
# Exits non-zero on the first unedited placeholder so it integrates
# cleanly with `set -e` deploy scripts.
#
# Checks:
#   1. `CHANGE-ME-*` / `<-- CHANGE` placeholders remain (image tag,
#      Service DNS, RBAC subject, network CIDR, etc.)
#   2. `image: ...:latest` on the worker Deployment — must be pinned
#      to a digest or immutable semver (F-CRIT-1)
#   3. PostgreSQL `ipBlock: 10.0.0.0/8` still in network-policy.yaml
#      — overly broad, must be tightened to the real RDS subnet CIDR.
#
# Designed to be cheap (no kube context, no AWS, no jq) so it can run
# from any CI runner. If you need richer validation (kubeconform,
# kube-linter, polaris), wire those into the same pre-deploy step.

set -euo pipefail

cd "$(dirname "$0")"

RED=$'\033[0;31m'
GREEN=$'\033[0;32m'
YELLOW=$'\033[0;33m'
NC=$'\033[0m'

errors=0

fail() {
    echo "${RED}✗${NC} $1" >&2
    errors=$((errors + 1))
}

warn() {
    echo "${YELLOW}!${NC} $1" >&2
}

ok() {
    echo "${GREEN}✓${NC} $1"
}

echo "Leviathan deploy manifest validation"
echo "===================================="

# 1. Unedited placeholders
if grep -lE 'CHANGE-ME|<-- CHANGE|<-- adjust' *.yaml 2>/dev/null; then
    fail "Unedited placeholders above. Edit before applying."
else
    ok "No CHANGE-ME / <-- CHANGE / <-- adjust placeholders."
fi

# 2. Image tag pinning on the worker
if grep -E '^\s+image:.*leviathan-prd-worker:latest' worker-deployment.yaml >/dev/null; then
    fail "worker-deployment.yaml pins image to :latest. Pin to a digest or immutable semver (F-CRIT-1)."
elif grep -E '^\s+image:.*leviathan-prd-worker:CHANGE-ME' worker-deployment.yaml >/dev/null; then
    fail "worker-deployment.yaml still has the CHANGE-ME image placeholder. Replace with actual tag/digest."
elif grep -E '^\s+image:.*leviathan-prd-worker:[^[:space:]]+' worker-deployment.yaml >/dev/null; then
    tag=$(grep -E '^\s+image:.*leviathan-prd-worker:' worker-deployment.yaml | head -1 | awk -F: '{print $NF}' | tr -d ' ')
    if echo "$tag" | grep -qE '^sha256:[0-9a-f]{64}$'; then
        ok "worker image pinned by digest ($tag)."
    elif echo "$tag" | grep -qE '^v?[0-9]+\.[0-9]+\.[0-9]+'; then
        ok "worker image pinned to semver tag ($tag)."
    else
        warn "worker image tagged '$tag' — confirm CI produces immutable tags."
    fi
fi

# 3. NetworkPolicy CIDR sanity
if [[ -f network-policy.yaml ]]; then
    if grep -E 'ipBlock:\s*$' network-policy.yaml >/dev/null; then
        cidr=$(grep -A1 'ipBlock:' network-policy.yaml | grep 'cidr:' | head -1 | awk '{print $2}')
        if [[ "$cidr" == "10.0.0.0/8" ]]; then
            warn "network-policy.yaml egress CIDR is 10.0.0.0/8 — tighten to the actual RDS subnet CIDR for prod."
        fi
    fi
fi

# 4. CronJob ODOO_URL placeholder
if grep -E 'odoo-backend\.ethara:8069' cronjobs.yaml >/dev/null; then
    warn "cronjobs.yaml uses the placeholder Service DNS 'odoo-backend.ethara:8069'. Replace with the real Service DNS."
fi

# 5. RBAC subject placeholder
if grep -E 'etp-backend-etp-be' rbac.yaml >/dev/null; then
    warn "rbac.yaml RoleBinding still references 'etp-backend-etp-be'. Replace with the real backend ServiceAccount name."
fi

echo
if [[ $errors -gt 0 ]]; then
    echo "${RED}validation FAILED${NC} ($errors error(s))"
    exit 1
else
    echo "${GREEN}validation passed${NC} (warnings above are advisory)"
fi
