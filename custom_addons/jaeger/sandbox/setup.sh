#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE="docker compose -f ${SCRIPT_DIR}/docker-compose.yml"
DB_NAME="jaeger_dev"
TIMEOUT=180

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
ICP.set_param("jaeger.dispatch_mode", "local")
ICP.set_param("jaeger.output_dir", "/tmp/jaeger_data")
ICP.set_param("jaeger.s3_bucket", "jaeger-local")
ICP.set_param("jaeger.s3_region", "us-east-1")
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
blue  "  Dispatch mode:  local (background thread)"
echo ""
green "=== K8s Dispatch Mode ==="
echo ""
echo "  To switch to K8s dispatch (uses the local K3s cluster):"
echo "    1. Go to Settings > Jaeger > Pipeline Dispatch Mode > Kubernetes"
echo "    2. Set K8s Job Docker Image to the image tag built by docker compose"
echo "    3. The K3s kubeconfig is mounted at /etc/rancher/k3s/k3s.yaml"
echo "    4. Note: _create_scrape_k8s_job() calls load_incluster_config()."
echo "       For K3s sandbox, you may need to switch to load_kube_config()."
echo ""
