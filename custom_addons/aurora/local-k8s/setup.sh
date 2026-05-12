#!/usr/bin/env bash
set -euo pipefail

##############################################################################
# Aurora Local Development Environment — Minikube
#
# Prerequisites (install before running):
#   brew install minikube kubectl helm
#   brew install --cask docker   (Docker Desktop for building images)
#
# Usage:
#   ./custom_addons/aurora/local-k8s/setup.sh up      # Start everything
#   ./custom_addons/aurora/local-k8s/setup.sh down    # Tear down everything
#   ./custom_addons/aurora/local-k8s/setup.sh status  # Show pod/service status
#   ./custom_addons/aurora/local-k8s/setup.sh logs    # Tail Odoo logs
#   ./custom_addons/aurora/local-k8s/setup.sh build   # Rebuild worker image only
##############################################################################

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AURORA_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PROJECT_ROOT="$(cd "$AURORA_ROOT/../.." && pwd)"
MANIFESTS="$SCRIPT_DIR/manifests"
CLUSTER_NAME="aurora-local"
K8S_VERSION="v1.29.0"
CPUS=6
MEMORY="16384"
DISK="80g"

# Configurable via environment
GITHUB_TOKEN="${GITHUB_TOKEN:-}"
AURORA_ENCRYPTION_KEY="${AURORA_ENCRYPTION_KEY:-$(openssl rand -base64 32)}"
MINIO_ACCESS_KEY="${MINIO_ACCESS_KEY:-minioadmin}"
MINIO_SECRET_KEY="${MINIO_SECRET_KEY:-minioadmin}"
PG_PASSWORD="${PG_PASSWORD:-odoo_local_pw}"
PG_USER="${PG_USER:-odoo}"
PG_DB="${PG_DB:-odoo}"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log()  { echo -e "${GREEN}[aurora-local]${NC} $*"; }
warn() { echo -e "${YELLOW}[aurora-local]${NC} $*"; }
err()  { echo -e "${RED}[aurora-local]${NC} $*" >&2; }

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
        --addons=ingress,metrics-server,registry \
        --insecure-registry="10.0.0.0/24"

    log "Cluster started. Context set to minikube/$CLUSTER_NAME"
}

create_namespace() {
    log "Creating aurora namespace with PSA privileged"
    kubectl apply -f "$MANIFESTS/namespace.yaml" --context="$CLUSTER_NAME"
}

deploy_postgres() {
    log "Deploying PostgreSQL"
    kubectl apply -f "$MANIFESTS/postgres.yaml" --context="$CLUSTER_NAME"
    log "Waiting for PostgreSQL to be ready..."
    kubectl wait --for=condition=ready pod -l app=postgres \
        -n aurora --timeout=120s --context="$CLUSTER_NAME"
}

deploy_minio() {
    log "Deploying MinIO (S3-compatible storage)"
    kubectl apply -f "$MANIFESTS/minio.yaml" --context="$CLUSTER_NAME"
    log "Waiting for MinIO to be ready..."
    kubectl wait --for=condition=ready pod -l app=minio \
        -n aurora --timeout=120s --context="$CLUSTER_NAME"

    log "Creating S3 bucket 'production-grtlabs-tag'"
    kubectl exec -n aurora deploy/minio --context="$CLUSTER_NAME" -- \
        mc alias set local http://localhost:9000 "$MINIO_ACCESS_KEY" "$MINIO_SECRET_KEY" 2>/dev/null || true
    kubectl exec -n aurora deploy/minio --context="$CLUSTER_NAME" -- \
        mc mb local/production-grtlabs-tag --ignore-existing 2>/dev/null || true
}

create_rbac() {
    log "Creating ServiceAccount + RBAC"
    kubectl apply -f "$MANIFESTS/rbac.yaml" --context="$CLUSTER_NAME"
}

create_secrets() {
    log "Creating K8s secrets"

    if [[ -z "$GITHUB_TOKEN" ]]; then
        warn "GITHUB_TOKEN not set. Pipeline won't be able to fetch PRs."
        warn "Set it: export GITHUB_TOKEN=ghp_xxx && ./custom_addons/aurora/local-k8s/setup.sh up"
    fi

    cat <<EOF | kubectl apply --context="$CLUSTER_NAME" -f -
apiVersion: v1
kind: Secret
metadata:
  name: aurora-local-env
  namespace: aurora
type: Opaque
stringData:
  AURORA_ENCRYPTION_KEY: "$AURORA_ENCRYPTION_KEY"
  AURORA_S3_ACCESS_KEY: "$MINIO_ACCESS_KEY"
  AURORA_S3_SECRET_KEY: "$MINIO_SECRET_KEY"
  DB_PASSWORD: "$PG_PASSWORD"
  GITHUB_TOKEN: "${GITHUB_TOKEN:-placeholder}"
EOF
}

build_worker_image() {
    log "Building aurora-worker image (this may take a few minutes)..."
    eval "$(minikube -p "$CLUSTER_NAME" docker-env)"

    docker build \
        --platform linux/amd64 \
        -f "$PROJECT_ROOT/custom_addons/aurora/Dockerfile.worker" \
        -t aurora-worker:local \
        "$PROJECT_ROOT"

    log "Worker image built: aurora-worker:local"
}

build_odoo_image() {
    log "Building aurora-odoo image..."
    eval "$(minikube -p "$CLUSTER_NAME" docker-env)"

    docker build \
        --platform linux/amd64 \
        -f "$SCRIPT_DIR/Dockerfile.local-odoo" \
        -t aurora-odoo:local \
        "$PROJECT_ROOT"

    log "Odoo image built: aurora-odoo:local"
}

pull_dind_images() {
    log "Pre-pulling DinD sidecar images into Minikube (required for Phase 2 evaluation)..."
    eval "$(minikube -p "$CLUSTER_NAME" docker-env)"

    local arch
    arch="$(docker info --format '{{.Architecture}}')"
    if [ "$arch" = "aarch64" ] || [ "$arch" = "arm64" ]; then
        docker pull --platform linux/arm64 docker:27-dind
        docker pull --platform linux/arm64 tonistiigi/binfmt:latest
    else
        docker pull docker:27-dind
        docker pull tonistiigi/binfmt:latest
    fi

    log "DinD images pulled: docker:27-dind, tonistiigi/binfmt:latest (arch=$arch)"
}

deploy_odoo() {
    log "Deploying Odoo server"
    kubectl apply -f "$MANIFESTS/odoo.yaml" --context="$CLUSTER_NAME"
    log "Waiting for Odoo to be ready..."
    kubectl wait --for=condition=ready pod -l app=odoo \
        -n aurora --timeout=300s --context="$CLUSTER_NAME"
}

print_access_info() {
    local odoo_url
    odoo_url=$(minikube service odoo -n aurora -p "$CLUSTER_NAME" --url 2>/dev/null || echo "pending")

    log "════════════════════════════════════════════════════════"
    log "  Aurora Local Environment Ready!"
    log "════════════════════════════════════════════════════════"
    log ""
    log "  Odoo:    $odoo_url"
    log "  MinIO:   $(minikube service minio-console -n aurora -p "$CLUSTER_NAME" --url 2>/dev/null || echo 'pending')"
    log ""
    log "  DB Host (from inside cluster): postgres.aurora.svc.cluster.local"
    log "  S3 Endpoint (from inside cluster): http://minio.aurora.svc.cluster.local:9000"
    log ""
    log "  Worker image: aurora-worker:local (loaded in Minikube)"
    log ""
    log "  To access Odoo from browser:"
    log "    minikube service odoo -n aurora -p $CLUSTER_NAME"
    log ""
    log "  To tail logs:"
    log "    kubectl logs -f -n aurora deploy/odoo --context=$CLUSTER_NAME"
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
    build_worker_image
    build_odoo_image
    pull_dind_images
    deploy_odoo
    print_access_info
}

cmd_down() {
    log "Tearing down cluster: $CLUSTER_NAME"
    minikube delete -p "$CLUSTER_NAME" 2>/dev/null || true
    log "Done."
}

cmd_status() {
    kubectl get pods,svc,jobs -n aurora --context="$CLUSTER_NAME" -o wide
}

cmd_logs() {
    kubectl logs -f -n aurora deploy/odoo --context="$CLUSTER_NAME"
}

cmd_build() {
    check_prerequisites
    build_worker_image
}

cmd_deploy() {
    check_prerequisites
    build_worker_image
    build_odoo_image
    log "Restarting Odoo deployment..."
    kubectl rollout restart deploy/odoo -n aurora --context="$CLUSTER_NAME"
    kubectl rollout status deploy/odoo -n aurora --context="$CLUSTER_NAME" --timeout=120s
    log "Upgrading Aurora module..."
    kubectl exec deploy/odoo -n aurora --context="$CLUSTER_NAME" -- \
        python /opt/odoo/odoo-bin -c /etc/odoo/odoo.conf -u aurora --stop-after-init
    log "Deploy complete. Odoo is ready."
}

cmd_run() {
    local org="${2:-colinhacks}"
    local repo="${3:-zod}"
    local lang="${4:-typescript}"

    local odoo_port
    odoo_port=$(kubectl get svc odoo -n aurora --context="$CLUSTER_NAME" \
        -o jsonpath='{.spec.ports[0].nodePort}' 2>/dev/null || echo "30069")
    local odoo_ip
    odoo_ip=$(minikube ip -p "$CLUSTER_NAME" 2>/dev/null || echo "127.0.0.1")
    local odoo_url="http://${odoo_ip}:${odoo_port}"

    log "Triggering pipeline: $org/$repo (lang=$lang)"
    log "Odoo URL: $odoo_url"

    python3 "$SCRIPT_DIR/trigger_pipeline.py" \
        --org "$org" \
        --repo "$repo" \
        --lang "$lang" \
        --url "$odoo_url"
}

case "${1:-help}" in
    up)     cmd_up ;;
    down)   cmd_down ;;
    status) cmd_status ;;
    logs)   cmd_logs ;;
    build)  cmd_build ;;
    deploy) cmd_deploy ;;
    run)    cmd_run "$@" ;;
    *)
        echo "Usage: $0 {up|down|status|logs|build|deploy|run}"
        echo ""
        echo "  up      Start Minikube + deploy full Aurora stack"
        echo "  down    Delete the Minikube cluster entirely"
        echo "  status  Show all pods/services/jobs in aurora namespace"
        echo "  logs    Tail Odoo server logs"
        echo "  build   Rebuild aurora-worker image only"
        echo "  deploy  Rebuild worker + odoo images, restart pod, upgrade module"
        echo "  run     Trigger pipeline (default: colinhacks/zod)"
        echo "          ./custom_addons/aurora/local-k8s/setup.sh run [org] [repo] [lang]"
        exit 1
        ;;
esac
