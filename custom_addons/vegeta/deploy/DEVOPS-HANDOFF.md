# Vegeta — PRD Generation on Kubernetes (DevOps Handoff)

**Purpose:** explain how the Vegeta addon runs PRD generation as Kubernetes pods, and
list everything the DevOps team must set up on EKS for it to work.

Addon version: `19.0.2.5.0`

---

## 1. What changed and why

Vegeta processes website-analysis tasks through a pipeline:
`Draft → Extracting → Generating PRD → Scoring → Done`.

**v19.0.2.4.0** ran each PRD task in a dedicated Kubernetes Job (one pod per
task). That solved the "Odoo worker recycles mid-run" problem but created two
new ones at scale:

1. Per-task Odoo cold-start (~30–60 s booting the registry) on top of the
   ~13 min generation = wasted capacity.
2. A 500-task batch spawned 500 K8s Jobs, each requesting a node — Karpenter
   churn and large pod-count.

**v19.0.2.5.0 new design:** a **Deployment** of long-lived PRD worker pods,
**auto-scaled by an Odoo cron** that patches the replica count based on
queue depth. Each pod boots Odoo once, then continuously claims pending PRD
tasks from Postgres and runs many concurrently inside a thread pool. Replica
count flexes between `min_replicas` and `max_replicas` (default 1..10) — at
peak load Karpenter provisions extra nodes for the new pods.

---

## 2. How it works (flow)

1. A task reaches the `generating` state in Odoo (set by the extraction-complete
   webhook). `job_name` is empty.
2. A **worker pod** running `python worker/run_prd.py` claims up to 10
   unclaimed jobs every ~5 s via `SELECT ... FOR UPDATE SKIP LOCKED` on the
   `vegeta_job` table — race-safe across pods and across threads inside a pod.
3. On claim, the worker stamps `job_name = worker-<hostname>-<pid>` and starts
   the per-job pipeline (`_run_prd_generation_bg`) inside its thread pool. A
   60-second heartbeat refreshes `last_heartbeat`.
4. When the job finishes (or fails), the worker writes the terminal state to
   Postgres and clears `job_name` (releasing the slot).
5. An Odoo cron — **"Vegeta: PRD Dispatch"** (every 1 min, advisory-locked) —
   counts active PRD load (queued + in-flight) and **patches the worker
   Deployment's replica count** via the Kubernetes API. Range:
   `min_replicas`..`max_replicas` (default 1..10). Karpenter then provisions
   nodes for any new pods.
6. An Odoo cron — **"Vegeta: PRD Reconcile"** (every 1 min) — finds records
   whose `last_heartbeat` is older than 5 minutes (worker pod crashed / hard
   killed) and clears `job_name`, returning them to the queue for re-claim.

---

## 3. PRD worker Deployment specification

PRD generation runs as a Kubernetes **Deployment** (not Jobs). Spec:

| Property | Value |
|---|---|
| Kind | `Deployment` |
| Namespace | `vegeta` |
| Name | `vegeta-prd-worker` (configurable — see §4.7) |
| Replicas | **auto-scaled 1..10** (Odoo dispatch cron patches based on queue depth) |
| ServiceAccount | `vegeta-worker` |
| Image | `<ECR>/vegeta-prd-worker:latest` (`imagePullPolicy: Always`) |
| Node selector | `ethara.ai/node-pool=general-purpose` |
| CPU request / limit | `2` / `4` |
| Memory request / limit | `6Gi` / `8Gi` |
| Security | non-root, `runAsUser: 1000` |
| Pod annotation | `karpenter.sh/do-not-disrupt: "true"` |
| `terminationGracePeriodSeconds` | `1860` (30 min drain + 60 s slack) |
| RollingUpdate strategy | `maxUnavailable: 0`, `maxSurge: 1` |
| Typical per-pod throughput | ~13–15 jobs/min (~110 jobs/h sustained) |

**Scaling math:** the dispatch cron sets
`replicas = clamp(min, ceil(active_load / VEGETA_WORKER_CONCURRENCY), max)`
where `active_load` is the count of jobs in `state='generating'`. At
`VEGETA_WORKER_CONCURRENCY=100` and `max_replicas=10`, the cluster can hold
up to 1000 concurrent in-flight jobs (Bedrock TPM remains the actual ceiling
on throughput).

Pod env vars (set in the Deployment manifest):

| Env var | Value | Purpose |
|---|---|---|
| `ODOO_DB` | _required_ | Database name. Workers connect to the same DB as the Odoo backend. |
| `ODOO_CONF` | `/etc/odoo/odoo.conf` | Bundled in the image. |
| `DB_HOST` / `DB_PORT` / `DB_USER` / `DB_PASSWORD` | _from Secret_ | Override booted odoo.conf DB credentials. |
| `VEGETA_WORKER_CONCURRENCY` | `100` | In-flight PRD jobs per pod. |
| `VEGETA_WORKER_CLAIM_BATCH` | `10` | Jobs claimed per Postgres tick. |
| `VEGETA_WORKER_POLL_S` | `5` | Idle-poll interval when queue is empty. |
| `VEGETA_WORKER_SHUTDOWN_TIMEOUT_S` | `1800` | Max drain time on SIGTERM. Must be < `terminationGracePeriodSeconds`. |
| `VEGETA_BEDROCK_MAX_CONCURRENT` | **`22`** | Per-pod Bedrock semaphore. **Raised from default 5.** Sized so `value × max_replicas ≈ Bedrock TPM ceiling` (~220 concurrent calls). See §4.7 for the formula. |

Cluster capacity at max scale: 10 pods × 100 workers = **1000 in-flight jobs**,
of which ~220 can actively call Bedrock at any moment
(`VEGETA_BEDROCK_MAX_CONCURRENT=22` × 10 pods ≈ TPM ceiling). Bedrock TPM
quota (2,000,000 TPM) caps sustained throughput at ~40 jobs/min cluster-wide;
a 500-task burst drains in ~12 min once pods are running. At low load the
cron scales down to `min_replicas` (default 1) to minimise idle cost.

---

## 4. DevOps requirements checklist

### 4.1 Worker container image (ECR)

- Build `custom_addons/vegeta/Dockerfile.worker` —
  **build context = repo root** (the image bundles `src/`,
  `custom_addons/vegeta/`, and its dependency addon `etp_user_roles`).
- Push to an ECR repo named `vegeta-prd-worker`.
- The addon's built-in default image is
  `426628337772.dkr.ecr.ap-south-1.amazonaws.com/vegeta-prd-worker:latest`.
- ⚠️ **The image bundles the addon source code.** It MUST be rebuilt and
  re-pushed every time the vegeta addon changes, in lockstep with the Odoo
  backend deploy. A stale worker image = pods running old code = silent bugs.
  **Please set up a CI job for this.**
- On image roll: the Deployment's rolling-update brings up new pods one at a
  time with `maxUnavailable: 0`, and old pods drain in-flight jobs (up to
  `terminationGracePeriodSeconds = 1860`) before exiting. No tasks are lost.

### 4.2 Kubernetes RBAC

**Simpler than v19.0.2.4.0** — no more per-task Jobs/Secrets/ConfigMaps to
create. Provision:

- A namespace `vegeta`.
- A ServiceAccount `vegeta-worker` in the `vegeta` namespace — PRD worker
  pods run as this account.
- A Role in the `vegeta` namespace allowing **`get` and `patch` on
  `deployments/scale`** in the `apps` API group (resource name restricted
  to `vegeta-prd-worker`, principle of least privilege).
- A RoleBinding granting that Role to **the Odoo backend's own
  ServiceAccount**. The Odoo dispatch cron uses this permission to scale the
  worker Deployment up/down. **This is the ONLY K8s API permission the Odoo
  backend needs** (no Job/Secret/ConfigMap create perms anymore).

The worker ServiceAccount only needs **EKS Pod Identity** binding to an IAM
role with Bedrock + S3 access (§4.4). It does not need any Kubernetes API
permissions itself.

### 4.3 Nodes / Karpenter

- The Karpenter NodePool must provision nodes **labeled
  `ethara.ai/node-pool=general-purpose`** — otherwise pods stay `Pending`.
- Per-replica footprint: 2 vCPU + 6 GiB requests. At baseline
  `min_replicas=1` ≈ 2 vCPU / 6 GiB. At peak `max_replicas=10` ≈ 20 vCPU /
  60 GiB. Karpenter packs onto 1–3 general-purpose nodes depending on scale.
- Set a Karpenter NodePool `limits` (CPU ceiling) corresponding to
  `max_replicas × 2 vCPU` as a cost guardrail.
- `karpenter.sh/do-not-disrupt: "true"` is set on pods so Karpenter will not
  evict a worker mid-run.
- No per-task Karpenter churn anymore — pods are long-lived, nodes are stable.

### 4.4 IAM / AWS access

- The EKS **node IAM role** needs ECR pull permission
  (`AmazonEC2ContainerRegistryReadOnly`).
- The **worker pod and Odoo backend pod** get AWS access via **EKS Pod
  Identity** — DevOps associates an IAM role with the `vegeta-worker`
  ServiceAccount (worker) and one with the Odoo backend's ServiceAccount
  (for the batch-fanout Lambda invokes). Worker permissions:
  `bedrock:InvokeModel` / `bedrock:Converse`, `s3:GetObject` /
  `s3:PutObject` on the bucket. Odoo backend permissions:
  `lambda:InvokeFunction` on the extraction Lambda ARN.
- ℹ️ **All AWS service configuration is input through the Odoo UI** —
  Settings → Vegeta → system parameters — **not by DevOps.** This includes:
  - **Bedrock**: model inference profile ARN, region
  - **S3**: bucket name, region, optional CDN URL
  - **Lambda**: extraction function ARN, region, reserved-concurrency target
  The worker pods (and the Odoo backend's batch-fanout for Lambda invokes)
  read these from the shared database. The Bedrock / S3 / Lambda
  **access-key fields in the UI are left blank** — Pod Identity covers
  boto3 for all three services through the `vegeta-worker` ServiceAccount's
  IAM role (which needs `bedrock:InvokeModel` / `bedrock:Converse`,
  `s3:GetObject` / `s3:PutObject`, and `lambda:InvokeFunction` on the
  extraction Lambda ARN).

### 4.5 Networking

- Worker pods must reach: the **PostgreSQL database** (RDS), **AWS Bedrock**
  (`bedrock-runtime.<region>.amazonaws.com`), and **S3**.
- The RDS security group must allow connections from the worker pods.
- The worker connects to the **same database** as the Odoo backend — that
  shared DB is how results flow back to the UI.

### 4.6 Odoo backend & Postgres tuning

- Deploy the updated vegeta addon (version `19.0.2.5.0`) and run
  `odoo -u vegeta` on the production DB — this applies the migration
  (`migrations/19.0.2.5.0/post-migration.py`) which clears `job_name` on any
  in-flight jobs leftover from v19.0.2.4.0 so the new workers can pick them up.
- Ensure Odoo runs at least one cron worker (`max_cron_threads >= 1`) for the
  reconcile cron.
- **Postgres tuning** (REQUIRED — worker pool holds many concurrent connections).
  Connection math at **max scale** (`max_replicas=10`, not `min_replicas=1`):
  ```
  Per worker pod connection ceiling:
    100 worker threads × 1 long-held cursor each   = 100
    + 100 per-job heartbeat threads × 1 cursor      = 100  (peak; short-lived)
    + boto3 / registry overhead                     =  ~20
                                                    -----
                                                    ~220
  Set db_maxconn = 250 per worker pod (gives 13% headroom).

  Cluster total at max scale:
    10 worker pods × 250                            = 2500
    + 3 Odoo HTTP backend pods × 64                 =  192
    + admin / cron / Odoo internal sessions         =  ~50
                                                    -----
                                                    ~2750
  Set Postgres max_connections = 3000 (10% safety margin).
  ```
  - `idle_in_transaction_session_timeout = '5min'` recommended (catches stuck
    worker cursors).
- **Worker pod `db_maxconn`**: `250` per pod (sizes for 100 concurrent jobs
  + 100 heartbeat threads + overhead).
- **Odoo backend `db_maxconn`**: `64` per Odoo HTTP worker (default; HTTP
  requests are short — no parking cursors).
- ⚠️ **Critical**: `max_connections` MUST be sized for `max_replicas`, not
  `min_replicas`. A scale-up burst that exhausts the Postgres pool causes
  heartbeat threads to silently fail, which trips the reconcile cron's
  stale-heartbeat detection while jobs are still running — triggering
  double-Bedrock-spend on the same task. See known-issue #2 below.
- ⚠️ **PgBouncer** (if present): MUST be in **session pooling mode**, not
  transaction pooling. The reconcile cron's `pg_try_advisory_lock` requires
  session pooling — transaction pooling silently breaks the lock.

### 4.7 System parameters & Bedrock quota sizing

Set via Odoo Settings → Technical → System Parameters
(`ir.config_parameter`) on the Odoo backend:

| Key | Default | Purpose |
|---|---|---|
| `vegeta.prd_execution_mode` | `worker` | `worker` (production: scale Deployment) \| `inprocess` (local single-process dev: use thread pool). |
| `vegeta.k8s_namespace` | `vegeta` | Namespace of the worker Deployment. |
| `vegeta.worker_deployment_name` | `vegeta-prd-worker` | Name of the Deployment the dispatch cron scales. |
| `vegeta.worker_min_replicas` | `1` | Always-on baseline (avoids cold-start on the first task of a burst). |
| `vegeta.worker_max_replicas` | `10` | Cost/capacity guardrail. 10 × 100 = 1000 in-flight ceiling. |
| `vegeta.worker_target_concurrency` | `100` | Per-pod in-flight capacity. **MUST match the worker pod's `VEGETA_WORKER_CONCURRENCY` env var** — they describe the same number from two sides. |

**`VEGETA_BEDROCK_MAX_CONCURRENT` per pod sizing is critical.** The vegeta
shared `bedrock_service.py` defaults this to `5` (safe floor for AWS's
default 5 RPS quota). With your **TPM = 2,000,000** quota:

```
2,000,000 TPM ÷ 60 = 33,333 tokens/sec budget
Each in-flight call consumes ~150 tokens/sec while streaming
Max concurrent Bedrock calls cluster-wide = 33,333 ÷ 150 = ~220
```

Because the worker Deployment **auto-scales 1..10**, the per-pod Bedrock
semaphore can't simply equal `220 / current_replicas` (the env var is fixed
at pod boot). The safe choice is to size for the MAX scale:

```
VEGETA_BEDROCK_MAX_CONCURRENT = ceil(220 / max_replicas)
                              = ceil(220 / 10) = 22
```

At 10 replicas, the cluster sits at the TPM ceiling. At 1 replica, only 22
concurrent Bedrock calls happen — TPM is under-utilized but the queue is
also small so it doesn't matter. **Recommended: 22.** If TPM quota or
`max_replicas` change, recompute.

Existing parameters (`vegeta.bedrock_*`, `vegeta.s3_*`,
`vegeta.webhook_token`) are unchanged from previous versions.

---

## 5. Files in the `deploy/` folder

This folder is **documentation only** — it ships no Kubernetes manifests.

| File | Purpose |
|---|---|
| `DEVOPS-HANDOFF.md` | This document — the deployment handoff. |
| `README.md` | The full Kubernetes deployment reference — components, deploy steps, config parameters. Read alongside this document. |

DevOps provisions all Kubernetes resources (Deployment, RBAC) from the DevOps
infra repo — this addon does not create them.

---

## 6. Verification after deployment

1. **Pods running:** `kubectl get pods -n vegeta -l app=vegeta-prd-worker`
   should show `min_replicas` pod(s) `Running` at idle (default 1). Logs
   should include
   `Worker booting: db=... concurrency=100 ... label=worker-<host>-<pid>`.
2. **Auto-scale:** create N tasks > 100. Within ~1 min the Odoo dispatch cron
   should log `worker scaler: vegeta/vegeta-prd-worker 1 -> M replicas
   (load=N, per_pod=100, range=1..10)` and `kubectl get deployment -n vegeta
   vegeta-prd-worker` should show `M` replicas.
2. **End-to-end task:** create a Vegeta task and trigger extraction. Within
   ~5 s of reaching `generating`, a worker should log
   `claimed N job(s): [id]` and the task should move
   `generating → scoring → done` over ~13 min.
3. **Failure test:** `kubectl delete pod -n vegeta <pod-name>`. The pod's
   in-flight jobs drain over up to 30 min (SIGTERM → SHUTDOWN_TIMEOUT). The
   Deployment immediately starts a replacement pod. Any job left with a stale
   heartbeat past 5 min is re-queued by the reconcile cron and picked up by
   another worker.
4. **Throughput:** create 100 tasks in a batch. They should drain in ~3 min
   given the 40 jobs/min sustained throughput at TPM=2M.

---

## 7. Notes

- Extraction (an earlier pipeline stage) runs on AWS Lambda and is unchanged —
  this document covers only PRD generation.
- The original Kubernetes-Job-per-task code paths (`_create_prd_job`,
  `_cleanup_prd_k8s_resources`, `_vegeta_namespace`, `K8S_AVAILABLE` block)
  remain in the codebase as dead code in this version — they have no callers
  but were left in place to keep the v19.0.2.4.0 → v19.0.2.5.0 patch minimal.
  A follow-up cleanup PR should delete them.
