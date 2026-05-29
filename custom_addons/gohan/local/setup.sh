#!/usr/bin/env bash
set -euo pipefail
IFS=$'\n\t'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GOHAN_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PROJECT_ROOT="$(cd "$GOHAN_ROOT/../.." && pwd)"
MANIFESTS="$SCRIPT_DIR/manifests"

CLUSTER_NAME="gohan-local"
NAMESPACE="gohan"
K8S_VERSION="v1.29.0"
CPUS=6
MEMORY="16384"
DISK="80g"

WORKER_IMAGE="gohan-prd-worker:latest"
S3_BUCKET="gohan-local"
AWS_REGION="us-east-1"

PG_PASSWORD="${PG_PASSWORD:-odoo_local_pw}"
AWS_ACCESS_KEY_ID="${AWS_ACCESS_KEY_ID:-minioadmin}"
AWS_SECRET_ACCESS_KEY="${AWS_SECRET_ACCESS_KEY:-minioadmin}"
WEBHOOK_TOKEN="${WEBHOOK_TOKEN:-devsecret}"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log()  { echo -e "${GREEN}[gohan-local]${NC} $*"; }
warn() { echo -e "${YELLOW}[gohan-local]${NC} $*"; }
err()  { echo -e "${RED}[gohan-local]${NC} $*" >&2; }

# Image build platform, derived from host arch. Matching the host arch keeps
# QEMU off the path (it panics on Apple Silicon with 'lfstack.push invalid
# packing') and avoids BuildKit pulling a non-matching platform tag from a
# remote registry where this local-only tag doesn't exist.
detect_platform() {
    case "$(uname -m)" in
        arm64|aarch64) echo "linux/arm64" ;;
        *)             echo "linux/amd64" ;;
    esac
}

check_prerequisites() {
    local missing=()
    for cmd in minikube kubectl docker; do
        if ! command -v "$cmd" &>/dev/null; then
            missing+=("$cmd")
        fi
    done
    if [[ ${#missing[@]} -gt 0 ]]; then
        err "Missing: ${missing[*]}"
        err "Install via: brew install ${missing[*]}"
        exit 1
    fi

    if ! docker info &>/dev/null; then
        err "Docker daemon not running. Start Docker Desktop first."
        exit 1
    fi
}

start_cluster() {
    if minikube status -p "$CLUSTER_NAME" &>/dev/null; then
        log "Cluster '$CLUSTER_NAME' already running"
        return
    fi

    log "Starting Minikube cluster: $CLUSTER_NAME (${CPUS} CPUs, ${MEMORY}MB RAM)"
    minikube start \
        -p "$CLUSTER_NAME" \
        --kubernetes-version="$K8S_VERSION" \
        --cpus="$CPUS" \
        --memory="$MEMORY" \
        --disk-size="$DISK" \
        --driver=docker \
        --container-runtime=docker \
        --addons=ingress,metrics-server,registry

    log "Cluster started. Context: $CLUSTER_NAME"
}

create_namespace() {
    log "Creating namespace: $NAMESPACE"
    kubectl apply -f "$MANIFESTS/namespace.yaml" --context="$CLUSTER_NAME"
}

create_secrets() {
    log "Creating Secret: gohan-local-env"

    local bedrock_block=""
    if [[ -n "${BEDROCK_ACCESS_KEY_ID:-}" && -n "${BEDROCK_SECRET_ACCESS_KEY:-}" ]]; then
        bedrock_block=$(cat <<EOF
  BEDROCK_ACCESS_KEY_ID: "${BEDROCK_ACCESS_KEY_ID}"
  BEDROCK_SECRET_ACCESS_KEY: "${BEDROCK_SECRET_ACCESS_KEY}"
EOF
)
    fi

    cat <<EOF | kubectl apply --context="$CLUSTER_NAME" -f -
apiVersion: v1
kind: Secret
metadata:
  name: gohan-local-env
  namespace: ${NAMESPACE}
type: Opaque
stringData:
  DB_PASSWORD: "${PG_PASSWORD}"
  AWS_ACCESS_KEY_ID: "${AWS_ACCESS_KEY_ID}"
  AWS_SECRET_ACCESS_KEY: "${AWS_SECRET_ACCESS_KEY}"
  WEBHOOK_TOKEN: "${WEBHOOK_TOKEN}"
${bedrock_block}
EOF
}

deploy_postgres() {
    log "Deploying PostgreSQL"
    kubectl apply -f "$MANIFESTS/postgres.yaml" --context="$CLUSTER_NAME"
    log "Waiting for PostgreSQL to be ready..."
    kubectl wait --for=condition=ready pod -l app=postgres \
        -n "$NAMESPACE" --timeout=120s --context="$CLUSTER_NAME"
}

deploy_minio() {
    log "Deploying MinIO (S3-compatible storage)"
    kubectl apply -f "$MANIFESTS/minio.yaml" --context="$CLUSTER_NAME"
    log "Waiting for MinIO to be ready..."
    kubectl wait --for=condition=ready pod -l app=minio \
        -n "$NAMESPACE" --timeout=120s --context="$CLUSTER_NAME"
}

create_rbac() {
    log "Creating ServiceAccount + RBAC"
    kubectl apply -f "$MANIFESTS/rbac.yaml" --context="$CLUSTER_NAME"
}

build_image() {
    log "Building $WORKER_IMAGE (this may take a few minutes)..."
    eval "$(minikube -p "$CLUSTER_NAME" docker-env)"

    local platform
    platform="$(detect_platform)"
    log "Build platform: $platform"

    # Build context MUST be PROJECT_ROOT: Dockerfile.worker COPYs src/,
    # custom_addons/gohan/, and custom_addons/etp_user_roles/ — all of which
    # live above this script's directory.
    docker build \
        --platform "$platform" \
        -f "$PROJECT_ROOT/custom_addons/gohan/Dockerfile.worker" \
        -t "$WORKER_IMAGE" \
        "$PROJECT_ROOT"

    log "Image built: $WORKER_IMAGE"
}

deploy_odoo() {
    log "Deploying Odoo server"
    kubectl apply -f "$MANIFESTS/odoo.yaml" --context="$CLUSTER_NAME"
    log "Waiting for Odoo to be ready (module install on first boot is slow)..."
    kubectl wait --for=condition=ready pod -l app=odoo \
        -n "$NAMESPACE" --timeout=300s --context="$CLUSTER_NAME"
}

deploy_worker() {
    log "Deploying gohan-worker (replicas=0 baseline; dispatch cron scales on demand)"
    kubectl apply -f "$MANIFESTS/gohan-worker.yaml" --context="$CLUSTER_NAME"
}

auto_configure_odoo() {
    local minikube_ip
    minikube_ip=$(minikube ip -p "$CLUSTER_NAME")
    log "Auto-configuring Odoo ir.config_parameter (minikube IP: $minikube_ip)"

    kubectl exec -i -n "$NAMESPACE" --context="$CLUSTER_NAME" deploy/odoo -- \
        python /opt/odoo/odoo-bin shell -c /etc/odoo/odoo.conf -d odoo --no-http <<PYEOF
env['ir.config_parameter'].sudo().set_param('web.base.url', 'http://${minikube_ip}:30069')
env['ir.config_parameter'].sudo().set_param('web.base.url.freeze', 'True')
env['ir.config_parameter'].sudo().set_param('gohan.lambda_function_name', 'gohan-extractor')
env['ir.config_parameter'].sudo().set_param('gohan.lambda_region', '${AWS_REGION}')
env['ir.config_parameter'].sudo().set_param('gohan.lambda_local_url', '')
env['ir.config_parameter'].sudo().set_param('gohan.extraction_access_key_id', '${AWS_ACCESS_KEY_ID}')
env['ir.config_parameter'].sudo().set_param('gohan.extraction_secret_access_key', '${AWS_SECRET_ACCESS_KEY}')
env['ir.config_parameter'].sudo().set_param('gohan.webhook_token', '${WEBHOOK_TOKEN}')
env['ir.config_parameter'].sudo().set_param('gohan.lambda_api_url', '')
env['ir.config_parameter'].sudo().set_param('gohan.hmac_secret', '')
env['ir.config_parameter'].sudo().set_param('gohan.worker_deployment_name', 'gohan-worker')
env['ir.config_parameter'].sudo().set_param('gohan.worker_min_replicas', '0')
env['ir.config_parameter'].sudo().set_param('gohan.worker_max_replicas', '3')
env['ir.config_parameter'].sudo().set_param('gohan.worker_target_concurrency', '100')
env.cr.commit()
PYEOF
}

print_access_info() {
    local minikube_ip
    minikube_ip=$(minikube ip -p "$CLUSTER_NAME" 2>/dev/null || echo "<pending>")

    log "════════════════════════════════════════════════════════"
    log "  Gohan Local Environment Ready!"
    log "════════════════════════════════════════════════════════"
    log ""
    log "  Odoo UI:          http://${minikube_ip}:30069"
    log "  MinIO Console:    http://${minikube_ip}:30901"
    log "    Login:          ${AWS_ACCESS_KEY_ID} / ${AWS_SECRET_ACCESS_KEY}"
    log ""
    log "  Webhook token:    ${WEBHOOK_TOKEN}"
    log "  S3 bucket:        ${S3_BUCKET}"
    log ""
    log "  Tail logs:"
    log "    $0 logs odoo"
    log "    $0 logs postgres"
    log ""
    warn "  Local rig uses PROD Lambda — auto-configure only sets placeholders."
    warn "  Open Settings → Gohan and override these 5 values manually:"
    warn "    - gohan.lambda_function_name    (prod function)"
    warn "    - gohan.lambda_region           (prod region)"
    warn "    - gohan.extraction_access_key_id"
    warn "    - gohan.extraction_secret_access_key"
    warn "    - gohan.webhook_token           (must match prod Lambda env)"
    warn ""
    warn "  For Lambda → Odoo callbacks, expose Odoo via cloudflared:"
    warn "    Terminal A: kubectl port-forward svc/odoo -n ${NAMESPACE} 8069:8069 --context=${CLUSTER_NAME}"
    warn "    Terminal B: cloudflared tunnel --url http://localhost:8069"
    warn "    Use <cloudflared-url>/api/v1/gohan/webhook/extraction-complete as the"
    warn "    Webhook URL in the prod Lambda env (or its callback config)."
    log "════════════════════════════════════════════════════════"
}

cmd_up() {
    check_prerequisites
    start_cluster
    create_namespace
    create_secrets
    deploy_postgres
    deploy_minio
    create_rbac
    build_image
    deploy_odoo
    deploy_worker
    auto_configure_odoo
    print_access_info
}

cmd_start() {
    check_prerequisites
    log "Starting existing Minikube cluster: $CLUSTER_NAME"
    minikube start -p "$CLUSTER_NAME"

    log "Re-applying manifests"
    kubectl apply -f "$MANIFESTS/namespace.yaml" --context="$CLUSTER_NAME"
    kubectl apply -f "$MANIFESTS/rbac.yaml" --context="$CLUSTER_NAME"
    kubectl apply -f "$MANIFESTS/postgres.yaml" --context="$CLUSTER_NAME"
    kubectl apply -f "$MANIFESTS/minio.yaml" --context="$CLUSTER_NAME"
    kubectl apply -f "$MANIFESTS/odoo.yaml" --context="$CLUSTER_NAME"
    kubectl apply -f "$MANIFESTS/gohan-worker.yaml" --context="$CLUSTER_NAME"

    kubectl wait --for=condition=ready pod -l app=odoo \
        -n "$NAMESPACE" --timeout=300s --context="$CLUSTER_NAME"

    # Minikube IP can shift between restarts, so web.base.url may now point at
    # a stale address. Re-running auto_configure_odoo rewrites it to the
    # current IP — this also clobbers any UI overrides on the 5 prod values,
    # so they must be re-entered manually each `start`.
    auto_configure_odoo
    print_access_info
}

cmd_down() {
    log "Tearing down cluster: $CLUSTER_NAME"
    minikube delete -p "$CLUSTER_NAME" 2>/dev/null || true
    log "Done."
}

cmd_status() {
    kubectl get pods,svc,jobs -n "$NAMESPACE" --context="$CLUSTER_NAME" -o wide
}

cmd_logs() {
    local target="${1:-odoo}"
    case "$target" in
        odoo)
            kubectl logs -f -n "$NAMESPACE" --context="$CLUSTER_NAME" deploy/odoo
            ;;
        worker)
            kubectl logs -f -n "$NAMESPACE" --context="$CLUSTER_NAME" deploy/gohan-worker
            ;;
        postgres)
            kubectl logs -f -n "$NAMESPACE" --context="$CLUSTER_NAME" deploy/postgres
            ;;
        *)
            err "Unknown logs target: $target (valid: odoo, worker, postgres)"
            exit 1
            ;;
    esac
}

cmd_build() {
    check_prerequisites
    build_image
}

cmd_deploy() {
    check_prerequisites
    build_image
    log "Rolling out deploy/odoo"
    kubectl rollout restart deploy/odoo -n "$NAMESPACE" --context="$CLUSTER_NAME"
    kubectl rollout status  deploy/odoo -n "$NAMESPACE" --context="$CLUSTER_NAME" --timeout=300s
    log "Upgrading gohan module"
    kubectl exec -n "$NAMESPACE" --context="$CLUSTER_NAME" deploy/odoo -- \
        python /opt/odoo/odoo-bin -c /etc/odoo/odoo.conf -u gohan --stop-after-init
    log "Deploy complete."
}

cmd_shell() {
    kubectl exec -it -n "$NAMESPACE" --context="$CLUSTER_NAME" deploy/odoo -- \
        python /opt/odoo/odoo-bin shell -c /etc/odoo/odoo.conf -d odoo --no-http
}

usage() {
    cat <<EOF
Usage: $0 <command> [args]

  up              Full from-scratch setup (cluster, image, manifests, config)
  start           Reuse image; start existing cluster, apply manifests, reconfigure Odoo
  down            Delete the Minikube cluster entirely
  status          Show pods/services/jobs in the $NAMESPACE namespace
  logs [target]   Tail logs. Targets: odoo (default), worker, postgres
  build           Rebuild the Odoo image
  deploy          Rebuild image, rollout odoo, upgrade gohan module
  shell           Open an Odoo shell inside the odoo pod
EOF
}

case "${1:-help}" in
    up)      cmd_up ;;
    start)   cmd_start ;;
    down)    cmd_down ;;
    status)  cmd_status ;;
    logs)    shift || true; cmd_logs "${1:-odoo}" ;;
    build)   cmd_build ;;
    deploy)  cmd_deploy ;;
    shell)   cmd_shell ;;
    *)
        usage
        exit 1
        ;;
esac
