# Vegeta PRD — Kubernetes deployment

PRD generation for each `vegeta.job` runs in its own short-lived Kubernetes
Job (`vegeta-prd-<id>-<uuid>`), ported from the sibling `aurora` addon. This
document explains what must exist on the cluster for that to work.

## Components

| File | Purpose |
|------|---------|
| `../Dockerfile.worker` | Multi-stage image for the worker pod (Odoo 19 + vegeta runtime deps). |
| `../worker/run_prd.py` | Pod entrypoint — boots a headless Odoo registry and runs the existing `_run_prd_generation_bg`. |

The Kubernetes RBAC (and, optionally, Kueue) is **provisioned by DevOps from the
DevOps infra repo** — this addon ships no manifests. See *Deploy steps* below.

## How it works

1. The `extraction-complete` webhook sets `vegeta.job.state = generating` and
   leaves `job_name` empty.
2. The **dispatch cron** (`vegeta.job._cron_dispatch_prd_jobs`, every 1 min,
   advisory-locked) finds those jobs and creates one K8s Job each, then stores
   the Job name on the record.
3. The worker pod runs `python worker/run_prd.py`, which boots Odoo and calls
   `_run_prd_generation_bg`. It heartbeats `last_heartbeat` every 60 s and
   writes the terminal state (`done` / `failed`) itself.
4. The **reconcile cron** (`vegeta.job._cron_reconcile_prd_jobs`, every 1 min,
   advisory-locked) lists Jobs labelled `platform=vegeta` and fails any record
   whose Job vanished or failed (`DeadlineExceeded` / `BackoffLimitExceeded`).
5. The **watchdog** (`_cron_watchdog_stuck_jobs`) is now only a 3 h last-resort
   backstop for when the dispatch/reconcile crons themselves are down.

When Kubernetes is unavailable (local single-process dev), dispatch falls back
to the in-process thread pool — see *Execution mode* below.

## Deploy steps

1. **Provision the Kubernetes RBAC** (DevOps, from the infra repo):
   - A namespace `vegeta` (or another namespace — then set the
     `vegeta.k8s_namespace` config parameter to match).
   - A ServiceAccount `vegeta-worker` in that namespace — the worker pods run
     as it. AWS access for Bedrock + S3 is granted via **EKS Pod Identity**
     (associate an IAM role with this ServiceAccount).
   - A Role allowing **create / list / get / delete** of `jobs` (`batch`),
     `secrets`, and `configmaps` in that namespace, and a RoleBinding granting
     it to the **Odoo backend's own ServiceAccount** (so the Odoo backend can
     create the per-job Jobs/Secrets/ConfigMaps via the Kubernetes API).
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
3. **Point Odoo at the image** (only if not using the hardcoded default): set
   the `vegeta.worker_docker_image` config parameter, or the
   `VEGETA_WORKER_IMAGE` env var, on the Odoo backend.

## Configuration parameters (`ir.config_parameter`)

| Key | Default | Purpose |
|-----|---------|---------|
| `vegeta.prd_execution_mode` | `auto` | `auto` \| `k8s` \| `inprocess` — see below. |
| `vegeta.k8s_namespace` | `vegeta` | Namespace for the PRD Jobs. |
| `vegeta.worker_docker_image` | hardcoded ECR default | Worker image reference. |
| `vegeta.watchdog_generating_backstop_hours` | `3` | Last-resort watchdog age. |
| `vegeta.kueue_queue` | _(empty)_ | Opt-in Kueue `LocalQueue` name. Empty = no Kueue label — see *Kueue (opt-in)* below. |

## Kueue (opt-in)

Kueue admission control is **off by default**. The PRD dispatch code adds the
`kueue.x-k8s.io/queue-name` label to a Job **only** when the config parameter
`vegeta.kueue_queue` is set to a non-empty value.

> **Warning:** setting `vegeta.kueue_queue` without a matching Kueue
> `LocalQueue` in the cluster leaves every PRD Job suspended forever (Kueue
> gates admission on a queue that does not exist).

To enable it: install [Kueue](https://kueue.sigs.k8s.io/) and have DevOps
provision a Kueue `ClusterQueue` + `LocalQueue` (with quotas) in the `vegeta`
namespace, then set `vegeta.kueue_queue` to that `LocalQueue` name. Leave the
parameter empty to run without Kueue.

## Execution mode (local-dev fallback)

`vegeta.prd_execution_mode` controls dispatch:

- `auto` (default) — use Kubernetes when the `kubernetes` package is installed
  **and** a cluster config loads; otherwise fall back to the in-process thread
  pool. This keeps a local single-process Odoo working with no cluster.
- `k8s` — always use Kubernetes; log an error if it is unavailable.
- `inprocess` — always use the in-process thread pool (legacy behaviour).

## Environment variables

| Var | Used by | Purpose |
|-----|---------|---------|
| `VEGETA_WORKER_IMAGE` | Odoo backend | Overrides the worker image. |
| `VEGETA_NAMESPACE` | Odoo backend | Overrides the Jobs namespace. |
| `VEGETA_WEBHOOK_TOKEN` | Odoo backend | Fallback webhook token put into the per-job Secret. |
