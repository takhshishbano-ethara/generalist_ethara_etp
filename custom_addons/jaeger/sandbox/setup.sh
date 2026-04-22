#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE="docker compose -f ${SCRIPT_DIR}/docker-compose.yml"
DB_NAME="jaeger_dev"
REGISTRY="registry:5000"
IMAGE_TAG="${REGISTRY}/sandbox-odoo:latest"
TIMEOUT=180

red()   { printf "\033[0;31m%s\033[0m\n" "$*"; }
green() { printf "\033[0;32m%s\033[0m\n" "$*"; }
blue()  { printf "\033[0;34m%s\033[0m\n" "$*"; }

blue "=== Jaeger Sandbox Setup ==="

command -v docker >/dev/null 2>&1 || { red "Docker is not installed."; exit 1; }
docker compose version >/dev/null 2>&1 || { red "Docker Compose v2 is not installed."; exit 1; }

blue "1/7  Building and starting services..."
$COMPOSE up -d --build

blue "2/7  Waiting for services to be healthy (timeout: ${TIMEOUT}s)..."
SECONDS=0
while true; do
    if [ "$SECONDS" -ge "$TIMEOUT" ]; then
        red "Timeout waiting for services after ${TIMEOUT}s."
        $COMPOSE ps
        exit 1
    fi

    set +e
    $COMPOSE exec -T db pg_isready -U odoo -d "$DB_NAME" >/dev/null 2>&1
    DB_UP=$?
    curl -sf http://localhost:9000/minio/health/live >/dev/null 2>&1
    MINIO_UP=$?
    set -e

    if [ "$DB_UP" -eq 0 ] && [ "$MINIO_UP" -eq 0 ]; then
        green "  PostgreSQL and MinIO are healthy."
        break
    fi
    printf "."
    sleep 3
done

blue "3/7  Pushing Odoo image to local registry for K3s..."
docker tag sandbox-odoo:latest "localhost:5000/sandbox-odoo:latest"
docker push "localhost:5000/sandbox-odoo:latest" 2>&1 | tail -3
green "  Image pushed to localhost:5000/sandbox-odoo:latest"

blue "4/7  Configuring K3s (namespace, RBAC, configmap)..."
SECONDS=0
while true; do
    if [ "$SECONDS" -ge 60 ]; then
        red "Timeout waiting for K3s."
        exit 1
    fi
    K3S_READY=$($COMPOSE exec -T k3s kubectl get nodes -o name 2>/dev/null | head -1)
    if [ -n "$K3S_READY" ]; then break; fi
    sleep 3
done
$COMPOSE exec -T k3s kubectl apply -f - < "${SCRIPT_DIR}/k3s-manifests.yaml"
green "  K3s namespace 'jaeger' with service account and configmap created."

blue "5/7  Installing Odoo + Jaeger module..."
$COMPOSE exec -T odoo ./odoo-bin \
    -c /etc/odoo/odoo.conf \
    -d "$DB_NAME" \
    -i base,mail,web,jaeger \
    --stop-after-init \
    --no-http

blue "6/7  Configuring Jaeger for K8s dispatch..."
$COMPOSE exec -T odoo ./odoo-bin shell \
    -c /etc/odoo/odoo.conf \
    -d "$DB_NAME" \
    --no-http <<PYEOF
ICP = env["ir.config_parameter"].sudo()
ICP.set_param("jaeger.dispatch_mode", "k8s")
ICP.set_param("jaeger.k8s_job_image", "${IMAGE_TAG}")
ICP.set_param("jaeger.eks_namespace", "jaeger")
ICP.set_param("jaeger.sandbox_mode", "1")
ICP.set_param("jaeger.s3_endpoint", "http://minio:9000")
ICP.set_param("jaeger.output_dir", "/tmp/jaeger_data")
ICP.set_param("jaeger.s3_bucket", "jaeger-local")
ICP.set_param("jaeger.s3_region", "us-east-1")
env.cr.commit()
print("Jaeger configured for K8s dispatch via K3s.")
PYEOF

blue "7/7  Cleaning stale assets and restarting Odoo..."
$COMPOSE exec -T db psql -U odoo -d "$DB_NAME" -c \
    "DELETE FROM ir_attachment WHERE url LIKE '/web/assets/%' OR name LIKE 'web.assets_%';" \
    >/dev/null 2>&1 || true
$COMPOSE restart odoo
sleep 5

echo ""
green "=== Jaeger Sandbox Ready ==="
echo ""
blue  "  Odoo:            http://localhost:8069  (admin / admin)"
blue  "  Odoo (nginx):    http://localhost       (via reverse proxy)"
blue  "  MinIO Console:   http://localhost:9001  (minioadmin / minioadmin)"
blue  "  K3s API:         https://localhost:6443"
echo ""
blue  "  Dispatch mode:   k8s (K3s cluster)"
blue  "  Job image:       ${IMAGE_TAG}"
blue  "  S3 bucket:       jaeger-local (MinIO)"
echo ""
