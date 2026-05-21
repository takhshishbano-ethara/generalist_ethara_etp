#!/usr/bin/env bash
#
# minikube-local-test.sh — local minikube harness for the vegeta PRD
# Kubernetes-Job architecture.
#
# 'setup' automates the WHOLE local test:
#   * cluster side — minikube, the vegeta-prd-worker image, namespace + SA
#   * host side    — odoo.conf db_host, /etc/hosts alias, Postgres config,
#                    the vegeta addon upgrade and the ICP system parameters
#   * then it starts Odoo in the foreground (Ctrl+C to stop).
# It is idempotent (safe to re-run). Only running a test job stays manual.
#
# Usage:
#   custom_addons/vegeta/deploy/minikube-local-test.sh [setup|watch|clean]
#     setup  (default)  cluster + host setup, then start Odoo
#     watch             follow vegeta PRD Jobs in the namespace
#     clean             delete all vegeta PRD Jobs in the namespace
#
# Overridable via env: VEGETA_WORKER_IMAGE, VEGETA_NAMESPACE,
#                      MINIKUBE_CPUS, MINIKUBE_MEMORY
#
set -euo pipefail

IMAGE="${VEGETA_WORKER_IMAGE:-vegeta-prd-worker:local}"
NAMESPACE="${VEGETA_NAMESPACE:-vegeta}"
MINIKUBE_CPUS="${MINIKUBE_CPUS:-4}"
MINIKUBE_MEMORY="${MINIKUBE_MEMORY:-8192}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
DOCKERFILE="custom_addons/vegeta/Dockerfile.worker"

ODOO_CONF="src/odoo.conf"
ODOO_BIN="src/odoo-bin"

PG_DB="ethara_etp"
PG_USER="odoo"
PG_SERVICE="postgresql@18"

cd "$REPO_ROOT"

need() { command -v "$1" >/dev/null 2>&1 || { echo "ERROR: required command '$1' not found in PATH" >&2; exit 1; }; }

host_setup() {
  need psql; need python; need brew

  echo "==> [host 1/4] DB host wiring (odoo.conf, /etc/hosts, Postgres)"

  if grep -qE '^[[:space:]]*db_host[[:space:]]*=[[:space:]]*host\.minikube\.internal' "$ODOO_CONF"; then
    echo "    odoo.conf: db_host already host.minikube.internal — skip"
  else
    sed -i '' 's/^db_host = localhost/db_host = host.minikube.internal/' "$ODOO_CONF"
    if grep -qE '^[[:space:]]*db_host[[:space:]]*=[[:space:]]*host\.minikube\.internal' "$ODOO_CONF"; then
      echo "    odoo.conf: db_host -> host.minikube.internal"
    else
      echo "ERROR: could not set db_host in $ODOO_CONF (no 'db_host = localhost' line)" >&2
      exit 1
    fi
  fi

  if grep -q 'host.minikube.internal' /etc/hosts; then
    echo "    /etc/hosts: host.minikube.internal already present — skip"
  else
    echo "    /etc/hosts: adding host.minikube.internal entry (needs sudo)"
    echo "127.0.0.1   host.minikube.internal" | sudo tee -a /etc/hosts >/dev/null
  fi

  local pgconf pghba pg_changed=0
  pgconf="$(psql -d "$PG_DB" -U "$PG_USER" -h localhost -tAc 'SHOW config_file;')"
  pghba="$(psql -d "$PG_DB" -U "$PG_USER" -h localhost -tAc 'SHOW hba_file;')"

  if grep -qxF "listen_addresses = '*'" "$pgconf"; then
    echo "    postgresql.conf: listen_addresses already '*' — skip"
  else
    echo "listen_addresses = '*'" >> "$pgconf"
    echo "    postgresql.conf: appended listen_addresses = '*'"
    pg_changed=1
  fi

  # 'trust' auth here is intentional and for LOCAL minikube testing ONLY —
  # never use 'trust' on a real or network-reachable Postgres.
  local hba_line
  for hba_line in 'host all all 10.0.0.0/8 trust' 'host all all 192.168.0.0/16 trust'; do
    if grep -qF "$hba_line" "$pghba"; then
      echo "    pg_hba.conf: '$hba_line' already present — skip"
    else
      echo "$hba_line" >> "$pghba"
      echo "    pg_hba.conf: appended '$hba_line'"
      pg_changed=1
    fi
  done

  if [ "$pg_changed" -eq 1 ]; then
    echo "    Postgres config changed — restarting $PG_SERVICE"
    brew services restart "$PG_SERVICE"
    sleep 3
  else
    echo "    Postgres config unchanged — restart not needed"
  fi
  echo "    DB host wiring done"

  echo "==> [host 2/4] upgrading the vegeta addon (-u vegeta --stop-after-init)"
  python "$ODOO_BIN" -c "$ODOO_CONF" -u vegeta --stop-after-init
  echo "    vegeta addon upgrade complete"

  echo "==> [host 3/4] Odoo system parameters"
  python "$ODOO_BIN" shell -c "$ODOO_CONF" <<PYEOF
env['ir.config_parameter'].sudo().set_param('vegeta.worker_docker_image', '$IMAGE')
env['ir.config_parameter'].sudo().set_param('vegeta.prd_execution_mode', 'k8s')
env['ir.config_parameter'].sudo().set_param('vegeta.k8s_namespace', '$NAMESPACE')
env.cr.commit()
PYEOF
  echo "    system parameters set (worker image=$IMAGE, mode=k8s, namespace=$NAMESPACE)"
}

setup() {
  need minikube; need kubectl; need docker

  echo "==> [1/3] minikube"
  if minikube status >/dev/null 2>&1; then
    echo "    already running"
  else
    minikube start --cpus="$MINIKUBE_CPUS" --memory="$MINIKUBE_MEMORY"
  fi

  echo "==> [2/3] building worker image '$IMAGE' into minikube (context: $REPO_ROOT)"
  minikube image build -t "$IMAGE" -f "$DOCKERFILE" .

  echo "==> [3/3] namespace + ServiceAccount"
  kubectl create namespace "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f -
  kubectl create serviceaccount vegeta-worker -n "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f -

  host_setup

  echo "==> [host 4/4] starting Odoo"
  cat <<EOF

================ automated setup complete — Odoo is about to start =============
 Odoo runs in the FOREGROUND in this terminal — press Ctrl+C to stop it.
 Once it is up it is reachable at:  http://localhost:8069

 One manual step remains — the test. Run it from a SEPARATE terminal:

 * Trigger a PRD job from the Odoo UI (a record reaching state=generating
   with an empty job_name is what the dispatch cron picks up), then watch it
   and verify failure recovery:
        $0 watch                                 # follow PRD Jobs
        kubectl logs   -n $NAMESPACE -l vegeta-job-id=<id> -f
        kubectl delete pod -n $NAMESPACE <pod>   # job -> failed within ~2 min
        $0 clean                                 # delete all vegeta PRD Jobs
================================================================================
EOF

  export VEGETA_LOCAL_MODE=1
  exec python "$ODOO_BIN" -c "$ODOO_CONF"
}

watch() {
  need kubectl
  echo "Watching vegeta PRD Jobs in namespace '$NAMESPACE' (Ctrl-C to stop) ..."
  kubectl get jobs -n "$NAMESPACE" -l platform=vegeta -w
}

clean() {
  need kubectl
  echo "Deleting all vegeta PRD Jobs in namespace '$NAMESPACE' ..."
  kubectl delete jobs -n "$NAMESPACE" -l platform=vegeta --ignore-not-found
}

case "${1:-setup}" in
  setup) setup ;;
  watch) watch ;;
  clean) clean ;;
  -h|--help|help) sed -n '2,20p' "${BASH_SOURCE[0]}" ;;
  *) echo "usage: $0 [setup|watch|clean]" >&2; exit 1 ;;
esac
