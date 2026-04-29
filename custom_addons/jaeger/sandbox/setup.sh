#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE="docker compose -f ${SCRIPT_DIR}/docker-compose.yml"
DB_NAME="jaeger_dev"
TIMEOUT=180
WORKER_IMAGE="jaeger-scrape:latest"
K8S_NAMESPACE="jaeger"

red()   { printf "\033[0;31m%s\033[0m\n" "$*"; }
green() { printf "\033[0;32m%s\033[0m\n" "$*"; }
blue()  { printf "\033[0;34m%s\033[0m\n" "$*"; }

blue "=== Jaeger Sandbox Setup ==="

command -v docker >/dev/null 2>&1 || { red "Docker is not installed."; exit 1; }
docker compose version >/dev/null 2>&1 || { red "Docker Compose v2 is not installed."; exit 1; }

blue "Building and starting services..."
$COMPOSE up -d --build

blue "Waiting for services to be healthy (timeout: ${TIMEOUT}s)..."
SECONDS=0
while true; do
    if [ "$SECONDS" -ge "$TIMEOUT" ]; then
        red "Timeout waiting for services."
        $COMPOSE ps
        exit 1
    fi

    DB_UP=$($COMPOSE exec -T db pg_isready -U odoo -d "$DB_NAME" 2>/dev/null && echo "yes" || echo "no")
    MINIO_UP=$(curl -sf http://localhost:9000/minio/health/live >/dev/null 2>&1 && echo "yes" || echo "no")

    if [ "$DB_UP" = "yes" ] && [ "$MINIO_UP" = "yes" ]; then
        green "PostgreSQL and MinIO are healthy."
        break
    fi

    printf "."
    sleep 3
done

blue "Creating K8s namespace '${K8S_NAMESPACE}'..."
$COMPOSE exec -T k3s kubectl create namespace "$K8S_NAMESPACE" 2>/dev/null || true

blue "Importing worker image into K3s..."
docker save "$WORKER_IMAGE" | $COMPOSE exec -T k3s ctr images import -

blue "Initializing Odoo database and installing Jaeger module..."
$COMPOSE exec -T odoo ./odoo-bin \
    -c /etc/odoo/odoo.conf \
    -d "$DB_NAME" \
    -i base,mail,web,jaeger \
    --stop-after-init \
    --no-http

blue "Configuring Jaeger settings..."
$COMPOSE exec -T odoo ./odoo-bin shell \
    -c /etc/odoo/odoo.conf \
    -d "$DB_NAME" \
    --no-http <<'PYEOF'
ICP = env["ir.config_parameter"].sudo()
ICP.set_param("jaeger.dispatch_mode", "k8s")
ICP.set_param("jaeger.output_dir", "/tmp/jaeger_data")
ICP.set_param("jaeger.s3_bucket", "jaeger-local")
ICP.set_param("jaeger.s3_region", "us-east-1")
ICP.set_param("jaeger.s3_prefix", "jaeger/phase1")
ICP.set_param("jaeger.sandbox_mode", "1")
ICP.set_param("jaeger.s3_endpoint", "http://minio:9000")
ICP.set_param("jaeger.s3_access_key", "minioadmin")
ICP.set_param("jaeger.s3_secret_key", "minioadmin")
ICP.set_param("jaeger.eks_namespace", "jaeger")
ICP.set_param("jaeger.scrape_image", "jaeger-scrape:latest")
ICP.set_param("web.base.url", "http://odoo:8069")
env.cr.commit()
print("Jaeger settings configured.")
PYEOF

echo ""
green "=== Jaeger Sandbox Ready ==="
echo ""
blue  "  Odoo:           http://localhost:8069  (admin / admin)"
blue  "  Odoo (nginx):   http://localhost       (via reverse proxy)"
blue  "  MinIO Console:  http://localhost:9001  (minioadmin / minioadmin)"
blue  "  K3s API:        https://localhost:6443"
echo ""
blue  "  S3 bucket:      jaeger-local (on MinIO)"
blue  "  S3 endpoint:    http://minio:9000"
blue  "  K8s namespace:  ${K8S_NAMESPACE}"
blue  "  Worker image:   ${WORKER_IMAGE} (loaded into K3s)"
blue  "  Webhook token:  sandbox-webhook-secret"
blue  "  web.base.url:   http://odoo:8069"
echo ""
blue "  Verify K8s:  docker compose exec k3s kubectl -n jaeger get jobs"
echo ""
