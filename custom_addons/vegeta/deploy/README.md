# Vegeta PRD — Kubernetes deployment

PRD generation for each `vegeta.job` runs in its own short-lived Kubernetes
Job (`vegeta-prd-<id>-<uuid>`), ported from the sibling `aurora` addon. This
directory holds the cluster-side resources that must exist for that to work.

## Components

| File | Purpose |
|------|---------|
| `vegeta-prd-rbac.yaml` | `vegeta` Namespace, `vegeta-worker` ServiceAccount, and the `Role`/`RoleBinding` that let the Odoo backend create/manage per-job Jobs, Secrets and ConfigMaps. |
| `vegeta-kueue-localqueue.yaml` | **Optional** Kueue `ResourceFlavor` + `ClusterQueue` + `LocalQueue`. Apply only if you opt in to Kueue admission control — see *Kueue (opt-in)* below. |
| `../Dockerfile.worker` | Multi-stage image for the worker pod (Odoo 19 + vegeta runtime deps). |
| `../worker/run_prd.py` | Pod entrypoint — boots a headless Odoo registry and runs the existing `_run_prd_generation_bg`. |

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

1. **Resolve the open questions** marked `TODO(devops):` in
   `vegeta-prd-rbac.yaml`:
   - **Q1 — namespace.** This manifest uses a dedicated `vegeta` namespace. To
     run the Jobs in the Odoo backend's own namespace instead, change every
     `namespace:` here and set the `vegeta.k8s_namespace` config parameter.
   - **Odoo ServiceAccount.** Replace `REPLACE_WITH_ODOO_SERVICEACCOUNT` /
     `REPLACE_WITH_ODOO_NAMESPACE` in the `RoleBinding` with the ServiceAccount
     the Odoo backend pods actually run as.
   - **Q4 — Bedrock/S3 auth.** By default the per-job Secret carries the
     Bedrock/S3 access keys read from Odoo settings. To use an IRSA pod role
     instead, uncomment the `eks.amazonaws.com/role-arn` annotation on the
     `vegeta-worker` ServiceAccount.
2. **Build & push the worker image** (Q2 — ECR repo `vegeta-prd-worker`):
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
3. **Apply the RBAC manifest:**
   ```sh
   kubectl apply -f custom_addons/vegeta/deploy/vegeta-prd-rbac.yaml
   ```
4. **Point Odoo at the image** (only if not using the hardcoded default): set
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

To enable it: install [Kueue](https://kueue.sigs.k8s.io/), apply
`vegeta-kueue-localqueue.yaml` (tune its quotas first), then set
`vegeta.kueue_queue` to the `LocalQueue` name (`vegeta-prd` in the sample).
Leave the parameter empty to run without Kueue.

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
| `VEGETA_LOCAL_MODE` | Odoo backend | When set, drops the node selector and uses `IfNotPresent` image pull (local kind/minikube). |
| `VEGETA_WORKER_IMAGE` | Odoo backend | Overrides the worker image. |
| `VEGETA_NAMESPACE` | Odoo backend | Overrides the Jobs namespace. |
| `VEGETA_WEBHOOK_TOKEN` | Odoo backend | Fallback webhook token put into the per-job Secret. |

## Local testing on minikube

Run the PRD Kubernetes-Job pipeline end-to-end against a local
[minikube](https://minikube.sigs.k8s.io/) cluster, with Odoo running as a host
process. `_load_k8s_config()` tries `load_incluster_config()` first and falls
back to `load_kube_config()`, so a host Odoo automatically picks up the minikube
context from `~/.kube/config` — no in-cluster config needed.

All commands assume the repository root as the working directory:

```sh
cd "/Users/apple/Desktop/ankit bhai/ethara-etp"
```

### 1. Start minikube

A PRD worker pod requests `cpu: 1` / `memory: 2Gi` (memory limit `4Gi`), so give
the VM headroom:

```sh
minikube start --cpus=4 --memory=8192
```

### 2. Build the worker image into minikube

`Dockerfile.worker` expects the **repository root** as its build context — it
`COPY`s `src/` (Odoo core) to `/opt/odoo/` and `custom_addons/vegeta/` to
`/opt/odoo/custom_addons/vegeta/`. Build it straight into minikube's runtime so
no registry or push is needed:

```sh
minikube image build -t vegeta-prd-worker:local -f custom_addons/vegeta/Dockerfile.worker .
```

Alternative — build with host Docker, then load it into minikube:

```sh
docker build -t vegeta-prd-worker:local -f custom_addons/vegeta/Dockerfile.worker .
minikube image load vegeta-prd-worker:local
```

> **Apple Silicon:** omit the `--platform linux/amd64` flag the ECR build uses —
> the minikube node is arm64, and an amd64 image fails at runtime with
> `exec format error`. Build for the node's native architecture.

Confirm the image is present inside the cluster:

```sh
minikube image ls | grep vegeta-prd-worker
```

### 3. Export the local-mode env var on the Odoo process

Before starting Odoo, export:

```sh
export VEGETA_LOCAL_MODE=1
```

`NODE_SELECTOR` and `IMAGE_PULL_POLICY` in `models/vegeta_job.py` are evaluated
**at import time** from `VEGETA_LOCAL_MODE`. With it set:

- `NODE_SELECTOR` becomes `{}` — without it the Job pod carries
  `ethara.ai/node-pool: general-purpose`, a label no minikube node has, so the
  pod stays **Pending** forever.
- `IMAGE_PULL_POLICY` becomes `IfNotPresent` instead of `Always` — so K8s uses
  the locally-built image instead of trying to pull it from ECR.

Because these are import-time constants, the variable must be set **before** the
Odoo process starts; changing it later has no effect until a restart.

### 4. Set the ICP system parameters

Set these in **Settings > Technical > Parameters > System Parameters**, or from
an Odoo shell:

| Key | Value | Why |
|-----|-------|-----|
| `vegeta.worker_docker_image` | `vegeta-prd-worker:local` | Must match the tag built in step 2. |
| `vegeta.prd_execution_mode` | `k8s` | Forces K8s dispatch. (`auto` also works once the kubeconfig loads; `k8s` makes the test deterministic and logs an error instead of silently falling back.) |
| `vegeta.k8s_namespace` | `vegeta` | Namespace for the Jobs. This is also the default — set it explicitly to be sure. |
| `vegeta.kueue_queue` | _(leave empty / unset)_ | A queue name with no matching Kueue `LocalQueue` suspends every Job forever. Empty = no Kueue label. |

From an Odoo shell:

```sh
./src/odoo-bin shell -c <odoo.conf> -d <your-db>
```
```python
ICP = env['ir.config_parameter'].sudo()
ICP.set_param('vegeta.worker_docker_image', 'vegeta-prd-worker:local')
ICP.set_param('vegeta.prd_execution_mode', 'k8s')
ICP.set_param('vegeta.k8s_namespace', 'vegeta')
env.cr.commit()
```

### 5. Create the namespace and worker ServiceAccount

The Job pod runs as the `vegeta-worker` ServiceAccount in the `vegeta`
namespace. Apply the RBAC manifest:

```sh
kubectl apply -f custom_addons/vegeta/deploy/vegeta-prd-rbac.yaml
```

Or create just the two objects the local test actually needs:

```sh
kubectl create namespace vegeta
kubectl create serviceaccount vegeta-worker -n vegeta
```

> The `Role` / `RoleBinding` in `vegeta-prd-rbac.yaml` are **not needed** on
> minikube: your host kubeconfig is cluster-admin, so the Odoo dispatcher's API
> calls are already authorized, and the worker pod itself never calls the K8s
> API. The `RoleBinding`'s `REPLACE_WITH_ODOO_*` placeholders can stay as-is —
> they bind a non-existent ServiceAccount and are simply inert here.

### 6. Point the Job pod at host Postgres — the #1 gotcha

`_create_prd_job` copies the Job's DB env **directly from the host Odoo process
config** (`odoo.tools.config`):

- `DB_HOST` ← `odoo_config["db_host"]`
- `DB_PORT` ← `odoo_config["db_port"]` (defaults to `5432`)
- `DB_USER` ← `odoo_config["db_user"]`
- `DB_PASSWORD` ← `odoo_config["db_password"]` (carried into the pod via a per-job Secret)
- `ODOO_DB` ← the current database name

So whatever `db_host` the host Odoo runs with is exactly what the pod tries to
connect to. `localhost` / `127.0.0.1` is meaningless inside the pod. minikube
exposes the host machine as `host.minikube.internal`, so:

1. Set `db_host` to `host.minikube.internal` in the `odoo.conf` used to launch
   Odoo (or pass `--db_host host.minikube.internal`). Also set `db_user`,
   `db_password`, and `db_port` to your local Postgres values.
2. The **host** Odoo process reads that same value, so add a host alias to keep
   it resolvable locally. On macOS, add to `/etc/hosts` (needs `sudo`):
   ```
   127.0.0.1   host.minikube.internal
   ```
   The host Odoo then reaches Postgres via `127.0.0.1`, while the pod resolves
   the same name to the host via minikube's injected cluster entry.
3. Postgres must accept the connection coming from the pod:
   - In `postgresql.conf`: `listen_addresses = '*'` (not just `localhost`).
   - In `pg_hba.conf`: allow the minikube source range, e.g.
     ```
     host  all  all  10.0.0.0/8       scram-sha-256
     host  all  all  192.168.0.0/16   scram-sha-256
     ```
     Use the auth method your install already uses (`scram-sha-256` or `md5`).
     Reload Postgres afterwards (`pg_ctl reload`, or
     `brew services restart postgresql`).

If the pod's source IP is rejected, the pod log shows
`no pg_hba.conf entry for host "X.X.X.X"` — add `host all all X.X.X.X/32 ...`
for that exact address.

### 7. Start Odoo with the module upgrade

Launch (or restart) Odoo with `-u vegeta` so the new migration and the PRD
dispatch / reconcile crons are applied — in the same shell where
`VEGETA_LOCAL_MODE` was exported (step 3):

```sh
./src/odoo-bin -c <odoo.conf> -d <your-db> -u vegeta
```

### 8. Run a job and watch it

Trigger a PRD job from the Odoo UI — a record reaching `state = generating` with
an empty `job_name` is what the dispatch cron picks up. The dispatch cron runs
every minute; watch the cluster:

```sh
kubectl get jobs -n vegeta -w
kubectl get pods -n vegeta
kubectl logs -n vegeta -l vegeta-job-id=<id> -f
```

A healthy run: the Job appears as `vegeta-prd-<id>-<uuid>`, its pod goes
`ContainerCreating → Running → Completed`, the pod log shows the worker booting
the Odoo registry and heartbeating, and the `vegeta.job` record ends in `done`.
Finished Jobs self-delete 600 s later via `ttlSecondsAfterFinished`.

### 9. Test failure recovery

Kill a pod mid-run and confirm the reconcile cron (every 1 min) fails the record
cleanly:

```sh
kubectl get pods -n vegeta
kubectl delete pod -n vegeta <pod-name>
```

The Job has `backoffLimit: 0`, so the deleted pod is not retried — the Job goes
`Failed`, and within ~2 minutes the reconcile cron flips the `vegeta.job` to
`failed` with a concrete `error_message` (e.g. `BackoffLimitExceeded`). Watch
the record's state in the Odoo UI, or:

```sh
kubectl get jobs -n vegeta -w
```

### Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| Pod stuck **Pending** | `NODE_SELECTOR` still set — `VEGETA_LOCAL_MODE` was not exported before Odoo started | Export `VEGETA_LOCAL_MODE=1`, restart Odoo. Verify with `kubectl get pod -n vegeta <pod> -o jsonpath='{.spec.nodeSelector}'` (should be empty). |
| **ImagePullBackOff** / `ErrImagePull` | Image not in minikube, tag mismatch, or pull policy `Always` | Re-run step 2; check `minikube image ls \| grep vegeta`; confirm `vegeta.worker_docker_image` equals the built tag; confirm `VEGETA_LOCAL_MODE` is set (→ `IfNotPresent`). |
| Pod **Error** / **CrashLoopBackOff**, DB errors in `kubectl logs` | Pod cannot reach host Postgres — wrong `db_host`, `listen_addresses`, or `pg_hba.conf` | See step 6: `db_host = host.minikube.internal`, `listen_addresses = '*'`, add the pod source range to `pg_hba.conf`. |
| No Job and no pod ever created | Dispatch fell back to the in-process pool | Check the Odoo log for `no K8s config`; ensure `vegeta.prd_execution_mode = k8s` and the kubeconfig loads. |
