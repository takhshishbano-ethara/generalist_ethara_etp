# Vegeta PRD — Kubernetes deployment

PRD generation runs in a **long-lived worker Deployment** (`vegeta-prd-worker`).
An Odoo cron auto-scales the Deployment between `min_replicas` and
`max_replicas` based on queue depth. Each worker pod handles up to
`VEGETA_WORKER_CONCURRENCY` PRD tasks concurrently in an in-process thread
pool. This document explains what must exist on the cluster for that to work.

See `DEVOPS-HANDOFF.md` for the full deployment runbook.

## Components

| File | Purpose |
|------|---------|
| `../Dockerfile.worker` | Multi-stage image for the worker pod (Odoo 19 + vegeta runtime deps). |
| `../worker/run_prd.py` | Pod entrypoint — boots a headless Odoo registry once, then runs a claim loop that pulls PRD jobs from Postgres and processes them concurrently. |

The Kubernetes Deployment and RBAC are **provisioned by DevOps from the
DevOps infra repo** — this addon ships no manifests.

## How it works

1. The `extraction-complete` webhook sets `vegeta.job.state = generating` and
   leaves `job_name` empty.
2. Long-lived **worker pods** (running `python worker/run_prd.py`) claim up to
   10 unclaimed jobs every ~5 s via `SELECT ... FOR UPDATE SKIP LOCKED` on the
   `vegeta_job` table. Each claim stamps `job_name = worker-<host>-<pid>` and
   refreshes `last_heartbeat`.
3. The per-job pipeline (`_run_prd_generation_bg`) runs inside the pod's
   thread pool. Workers heartbeat every 60 s and write the terminal state
   (`done` / `failed`) themselves.
4. The **dispatch cron** (`vegeta.job._cron_dispatch_prd_jobs`, every 1 min,
   advisory-locked) counts active PRD load and patches the Deployment's
   replica count via the Kubernetes API:
   `replicas = clamp(min, ceil(load / VEGETA_WORKER_CONCURRENCY), max)`.
5. The **reconcile cron** (`vegeta.job._cron_reconcile_prd_jobs`, every 1 min,
   advisory-locked) finds records whose `last_heartbeat` is older than 5 min
   (worker pod crashed / hard killed) and clears `job_name` so another worker
   can re-claim them.
6. The **watchdog** (`_cron_watchdog_stuck_jobs`) is a 3 h last-resort
   backstop for when both crons above are down.

When Kubernetes is unavailable (local single-process dev), set
`vegeta.prd_execution_mode = inprocess` — the dispatch cron then submits jobs
to an in-process thread pool inside the Odoo backend (no cluster needed).

## Deploy steps

1. **Provision the Kubernetes Deployment + RBAC** (DevOps, from the infra repo):
   - A namespace `vegeta` (or another namespace — then set the
     `vegeta.k8s_namespace` config parameter to match).
   - A ServiceAccount `vegeta-worker` in that namespace — the worker pods run
     as it. AWS access for Bedrock + S3 is granted via **EKS Pod Identity**
     (associate an IAM role with this ServiceAccount).
   - A **Deployment** `vegeta-prd-worker` running the worker image, starting
     at `min_replicas` (default 1). Pod spec is in `DEVOPS-HANDOFF.md` §3.
   - A Role in the `vegeta` namespace allowing **`get` and `patch` on
     `deployments/scale`** (resource name `vegeta-prd-worker`) in the
     `apps` API group, and a RoleBinding granting it to the Odoo backend's
     ServiceAccount. This is the only K8s API permission the Odoo backend
     needs — the dispatch cron uses it to auto-scale the worker Deployment.
2. **Build & push the worker image** (ECR repo `vegeta-prd-worker`):
   ```sh
   docker build --platform linux/amd64 \
     -f custom_addons/vegeta/Dockerfile.worker \
     -t 426628337772.dkr.ecr.ap-south-1.amazonaws.com/vegeta-prd-worker:latest .
   aws ecr get-login-password --region ap-south-1 | \
     docker login --username AWS --password-stdin \
     426628337772.dkr.ecr.ap-south-1.amazonaws.com
   docker push 426628337772.dkr.ecr.ap-south-1.amazonaws.com/vegeta-prd-worker:latest
   ```
   CI must rebuild this image whenever the vegeta addon changes — the image
   bundles the addon source.

## Configuration parameters (`ir.config_parameter`)

| Key | Default | Purpose |
|-----|---------|---------|
| `vegeta.prd_execution_mode` | `worker` | `worker` (production, auto-scale K8s Deployment) \| `inprocess` (local dev, in-process thread pool). |
| `vegeta.k8s_namespace` | `vegeta` | Namespace of the worker Deployment. |
| `vegeta.worker_deployment_name` | `vegeta-prd-worker` | Deployment patched by the scaler cron. |
| `vegeta.worker_min_replicas` | `1` | Always-on baseline. |
| `vegeta.worker_max_replicas` | `10` | Cost guardrail. |
| `vegeta.worker_target_concurrency` | `100` | Per-pod in-flight capacity (must match worker pod's `VEGETA_WORKER_CONCURRENCY` env var). |
| `vegeta.watchdog_generating_backstop_hours` | `3` | Last-resort watchdog age. |

## Worker pod environment variables

Set in the Deployment manifest. Full table in `DEVOPS-HANDOFF.md` §3.

| Var | Default | Purpose |
|-----|---------|---------|
| `ODOO_DB` | _required_ | Database name. |
| `VEGETA_WORKER_CONCURRENCY` | `100` | In-flight PRD jobs per pod. |
| `VEGETA_WORKER_CLAIM_BATCH` | `10` | Jobs claimed per Postgres tick. |
| `VEGETA_WORKER_POLL_S` | `5` | Idle-poll interval. |
| `VEGETA_WORKER_SHUTDOWN_TIMEOUT_S` | `1800` | Max drain time on SIGTERM. |
| `VEGETA_BEDROCK_MAX_CONCURRENT` | `22` | Per-pod Bedrock semaphore. See `DEVOPS-HANDOFF.md` §4.7 for the sizing formula. |
