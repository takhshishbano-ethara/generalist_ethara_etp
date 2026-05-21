#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KENSEI2_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PROJECT_ROOT="$(cd "$KENSEI2_ROOT/../.." && pwd)"
MANIFESTS="$SCRIPT_DIR/manifests"
CLUSTER_NAME="minikube"
CPUS=4
MEMORY="7680"
DISK="50g"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log()  { echo -e "${GREEN}[kensei2-k8s]${NC} $*"; }
warn() { echo -e "${YELLOW}[kensei2-k8s]${NC} $*"; }
err()  { echo -e "${RED}[kensei2-k8s]${NC} $*" >&2; }

check_prerequisites() {
    local missing=()
    for cmd in minikube kubectl docker; do
        if ! command -v "$cmd" &>/dev/null; then
            missing+=("$cmd")
        fi
    done
    if [[ ${#missing[@]} -gt 0 ]]; then
        err "Missing: ${missing[*]}"
        exit 1
    fi
    if ! docker info &>/dev/null; then
        err "Docker daemon not running. Start Docker Desktop first."
        exit 1
    fi
}

start_cluster() {
    if minikube status -p "$CLUSTER_NAME" 2>/dev/null | grep -q "Running"; then
        log "Cluster '$CLUSTER_NAME' already running"
        return
    fi

    log "Starting Minikube: ${CPUS} CPUs, ${MEMORY}MB RAM, ${DISK} disk"
    minikube start \
        -p "$CLUSTER_NAME" \
        --cpus="$CPUS" \
        --memory="$MEMORY" \
        --disk-size="$DISK" \
        --driver=docker

    log "Cluster started."
}

create_namespace_and_rbac() {
    log "Creating kensei2 namespace + RBAC"
    kubectl apply -f "$MANIFESTS/namespace.yaml"
    kubectl apply -f "$MANIFESTS/rbac.yaml"
}

create_secrets() {
    log "Creating kensei2-env secret from .env"

    local env_file="$PROJECT_ROOT/.env"
    if [[ ! -f "$env_file" ]]; then
        err ".env file not found at $env_file"
        exit 1
    fi

    source_env_var() {
        grep "^$1=" "$env_file" 2>/dev/null | head -1 | cut -d= -f2-
    }

    local aws_bearer; aws_bearer=$(source_env_var AWS_BEARER_TOKEN_BEDROCK)
    local kensei2_aws_bearer; kensei2_aws_bearer=$(source_env_var KENSEI2_AWS_BEARER_TOKEN)
    local openai_key; openai_key=$(source_env_var KENSEI2_OPENAI_API_KEY)
    local litellm_key; litellm_key=$(source_env_var KENSEI2_LITELLM_MASTER_KEY)
    local litellm_db_pw; litellm_db_pw=$(source_env_var KENSEI2_LITELLM_DB_PASSWORD)
    local llama_key; llama_key=$(source_env_var KENSEI2_LLAMA_API_KEY)
    local moonshot_key; moonshot_key=$(source_env_var KENSEI2_MOONSHOT_API_KEY)

    kubectl apply -f - <<EOF
apiVersion: v1
kind: Secret
metadata:
  name: kensei2-env
  namespace: kensei2
type: Opaque
stringData:
  AWS_BEARER_TOKEN_BEDROCK: "${aws_bearer}"
  KENSEI2_AWS_BEARER_TOKEN: "${kensei2_aws_bearer}"
  KENSEI2_OPENAI_API_KEY: "${openai_key}"
  KENSEI2_LITELLM_MASTER_KEY: "${litellm_key}"
  KENSEI2_LITELLM_DB_PASSWORD: "${litellm_db_pw}"
  KENSEI2_LLAMA_API_KEY: "${llama_key}"
  KENSEI2_MOONSHOT_API_KEY: "${moonshot_key}"
EOF
    log "Secret created."
}

deploy_postgres() {
    log "Deploying PostgreSQL (Odoo DB)"
    kubectl apply -f "$MANIFESTS/postgres.yaml"
    log "Waiting for PostgreSQL..."
    kubectl wait --for=condition=ready pod -l app=postgres \
        -n kensei2 --timeout=120s
}

build_odoo_image() {
    log "Building kensei2-odoo:local image inside minikube (this may take several minutes)..."
    eval "$(minikube -p "$CLUSTER_NAME" docker-env)"

    local host_arch
    host_arch="$(uname -m)"
    local platform="linux/amd64"
    case "$host_arch" in
        arm64|aarch64) platform="linux/arm64" ;;
    esac

    docker build \
        --platform "$platform" \
        -f "$SCRIPT_DIR/Dockerfile.kensei2-odoo" \
        -t kensei2-odoo:local \
        "$PROJECT_ROOT"

    log "Odoo image built: kensei2-odoo:local"
}

load_sandbox_images() {
    log "Loading sandbox images into minikube..."
    eval "$(minikube -p "$CLUSTER_NAME" docker-env)"

    local images=(
        "ghcr.io/openclaw/openclaw:latest"
        "ghcr.io/berriai/litellm:main-stable"
        "postgres:16"
        "nginx:alpine"
    )

    for img in "${images[@]}"; do
        if docker image inspect "$img" &>/dev/null; then
            log "  ✓ $img already in minikube"
        else
            log "  Loading $img from host docker..."
            eval "$(minikube -p "$CLUSTER_NAME" docker-env --unset)"
            if docker image inspect "$img" &>/dev/null; then
                minikube -p "$CLUSTER_NAME" image load "$img"
                log "  ✓ $img loaded"
            else
                warn "  ✗ $img not found locally. Pulling into minikube..."
                eval "$(minikube -p "$CLUSTER_NAME" docker-env)"
                docker pull "$img" || warn "  Failed to pull $img"
            fi
            eval "$(minikube -p "$CLUSTER_NAME" docker-env)"
        fi
    done
    log "Sandbox images ready."
}

deploy_odoo() {
    log "Deploying Odoo"
    kubectl apply -f "$MANIFESTS/odoo.yaml"
    log "Waiting for Odoo to be ready (may take 2-5 minutes for init)..."
    kubectl wait --for=condition=ready pod -l app=odoo \
        -n kensei2 --timeout=600s
}

configure_kensei2_k8s_mode() {
    log "Configuring Kensei2 for K8s mode via Odoo XML-RPC..."

    local odoo_url
    odoo_url="$(minikube service odoo -n kensei2 -p "$CLUSTER_NAME" --url 2>/dev/null | head -1)"

    if [[ -z "$odoo_url" || "$odoo_url" == *"pending"* ]]; then
        warn "Could not get Odoo URL automatically."
        warn "Run manually after Odoo is up:"
        warn "  minikube service odoo -n kensei2 --url"
        return
    fi

    log "Odoo URL: $odoo_url"
    log "Setting config parameters via XML-RPC..."

    python3 - "$odoo_url" <<'PYEOF'
import sys, xmlrpc.client
url = sys.argv[1]
db = "kensei2_test"
uid = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/common").authenticate(db, "admin", "admin", {})
models = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/object")

params = {
    "kensei2.deployment_mode": "k8s",
    "kensei2.openclaw_image": "ghcr.io/openclaw/openclaw:latest",
    "kensei2.litellm_image": "ghcr.io/berriai/litellm:main-stable",
    "kensei2.postgres_image": "postgres:16",
    "kensei2.nginx_image": "nginx:alpine",
    "kensei2.ws_router_host": "",
}
for key, val in params.items():
    models.execute_kw(db, uid, "admin", "ir.config_parameter", "set_param", [key, val])
    print(f"  Set {key} = {val}")
print("Done.")
PYEOF
}

print_access_info() {
    local odoo_url
    odoo_url=$(minikube service odoo -n kensei2 -p "$CLUSTER_NAME" --url 2>/dev/null | head -1 || echo "pending")

    log "════════════════════════════════════════════════════════"
    log "  Kensei2 K8s Local Environment Ready!"
    log "════════════════════════════════════════════════════════"
    log ""
    log "  Odoo URL:  $odoo_url"
    log "  DB:        kensei2_test"
    log "  Login:     admin / admin"
    log ""
    log "  Open Odoo in browser:"
    log "    minikube service odoo -n kensei2"
    log ""
    log "  Tail Odoo logs:"
    log "    kubectl logs -f -n kensei2 deploy/odoo"
    log ""
    log "  Watch sandbox pods:"
    log "    kubectl get pods -n kensei2 -w"
    log ""
    log "  Kensei2 config: K8s mode, skip S3, skip node selector"
    log "  Batch size: configurable in task UI (default 8 per model)"
    log "  Tip: Set batch_size=1 for resource-constrained testing"
    log "════════════════════════════════════════════════════════"
}

cmd_up() {
    check_prerequisites
    start_cluster
    create_namespace_and_rbac
    create_secrets
    deploy_postgres
    build_odoo_image
    load_sandbox_images
    deploy_odoo
    configure_kensei2_k8s_mode
    print_access_info
}

cmd_down() {
    log "Tearing down all kensei2 resources"
    kubectl delete namespace kensei2 --ignore-not-found 2>/dev/null || true
    log "Done. Cluster still running. Use 'minikube delete' to remove cluster."
}

cmd_status() {
    kubectl get pods,svc,deploy,configmaps,secrets -n kensei2 -o wide 2>/dev/null || warn "Namespace kensei2 not found"
}

cmd_logs() {
    kubectl logs -f -n kensei2 deploy/odoo
}

cmd_pods() {
    kubectl get pods -n kensei2 -w
}

cmd_rebuild() {
    check_prerequisites
    build_odoo_image
    log "Restarting Odoo deployment..."
    kubectl rollout restart deploy/odoo -n kensei2
    kubectl rollout status deploy/odoo -n kensei2 --timeout=300s
    log "Rebuild complete."
}

case "${1:-help}" in
    up)      cmd_up ;;
    down)    cmd_down ;;
    status)  cmd_status ;;
    logs)    cmd_logs ;;
    pods)    cmd_pods ;;
    rebuild) cmd_rebuild ;;
    *)
        echo "Usage: $0 {up|down|status|logs|pods|rebuild}"
        echo ""
        echo "  up       Start minikube + deploy full Kensei2 stack"
        echo "  down     Delete kensei2 namespace (keeps cluster)"
        echo "  status   Show all resources in kensei2 namespace"
        echo "  logs     Tail Odoo server logs"
        echo "  pods     Watch pods in kensei2 namespace"
        echo "  rebuild  Rebuild Odoo image + restart pod"
        exit 1
        ;;
esac
