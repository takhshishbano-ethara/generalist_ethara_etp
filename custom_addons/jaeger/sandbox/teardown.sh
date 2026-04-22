#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE="docker compose -f ${SCRIPT_DIR}/docker-compose.yml"

red()   { printf "\033[0;31m%s\033[0m\n" "$*"; }
green() { printf "\033[0;32m%s\033[0m\n" "$*"; }
blue()  { printf "\033[0;34m%s\033[0m\n" "$*"; }

blue "=== Jaeger Sandbox Teardown ==="

$COMPOSE down

if [ "${1:-}" = "--clean" ]; then
    blue "Removing volumes (pg-data, minio-data, k3s-data, k3s-kubeconfig)..."
    $COMPOSE down -v
    green "Volumes removed."
else
    green "Containers stopped. Volumes preserved."
    echo "  Run with --clean to also remove volumes."
fi
