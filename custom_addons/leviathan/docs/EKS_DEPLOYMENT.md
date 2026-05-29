# Leviathan EKS Deployment Guide (19.0.2.0.0+)

**Audience:** DevOps / SRE running Odoo on Amazon EKS.

**What changed in 19.0.2.0.0:** RabbitMQ is removed. Batch fan-out now happens
in-process inside the Odoo pod via `boto3 lambda:Invoke(InvocationType='Event')`
with a 250-wide `ThreadPoolExecutor`. Each invoke returns in <1 s; Lambdas run
asynchronously and post back to the existing webhook.

**What changed in 19.0.5.x (drainer flag, behaviour-neutral default):** the
`leviathan_job` table is now a durable PRD queue gated by the System
Parameter `leviathan.prd_queue_enabled` (default `False`). When `True`, an
in-Odoo `ir.cron` drainer claims `state='generating'` rows via
`SELECT ... FOR UPDATE SKIP LOCKED`, fence-protected by `prd_claim_count`.
No deployment change is required to ship this; flipping the flag is live.

**What changed in 2026-05-26 (standalone PRD worker pod, opt-in):** a new
System Parameter `leviathan.prd_execution_mode` (`inprocess` \| `worker`,
default `inprocess`) selects where the drainer loop runs. In `worker`
mode the in-Odoo cron short-circuits and a **standalone Python process**
(`custom_addons/leviathan/worker/run_prd.py`) owns the loop. This is the
production target. See §10 below for the new Deployment spec.

---

## 0. This Release — Pre-Deploy Checklist

These came out of the load-failure post-mortem and are easy to miss. Verify
**before** the standing setup in §1+.

### 0.1 Lambda zip must contain

- [ ] **`--single-process` REMOVED** from every Chromium launch arg in `handler.py`
      and `modules/site_discoverer.py`. It is the #1 cause of all-black screenshots
      and has regressed once already.
- [ ] `template.yaml` → `EphemeralStorage: Size: 4096` (512 MB default → `ENOSPC` on
      browser launch).
- [ ] `template.yaml` → `EventInvokeConfig: MaximumRetryAttempts: 0`. Async (`Event`)
      invokes are auto-retried by AWS on Lambda timeout — without this a single
      timed-out extraction silently re-runs up to 2 more times (3× cost + duplicate
      callbacks).
- [ ] `template.yaml` → `ReservedConcurrentExecutions: 250`.
- [ ] Browser-resilience helpers present: `_relaunch_if_dead`, `_bounded`,
      `_screenshot_with_retry`, and the `playwright-*` `/tmp` cleanup.

```bash
cd leviathan-extraction-lambda
grep -rc single-process handler.py modules/site_discoverer.py        # expect: 0  0
grep -cE 'EphemeralStorage|EventInvokeConfig|ReservedConcurrentExecutions: 250' template.yaml  # expect: 3
python3 -m py_compile handler.py config.py modules/*.py && echo "compile OK"
```

### 0.2 Odoo deploy must include

- [ ] Module version is **`19.0.2.1.0`** — see §10 for what shipped. The
      `19.0.2.0.0` pre-migration (renames `via_rabbitmq`→`via_batch`, seeds config,
      removes the old RabbitMQ server action) runs if the DB is older; the
      `19.0.2.1.0` additions (new fields, `discarded` state) are auto-created on
      `-u leviathan` — no migration file needed.
- [ ] **Confirm prod actually picks up `19.0.2.1.0`.** A prior cycle ran stale code
      (recursive `write()` → infinite recursion). Check the deployed image tag and
      `__manifest__.py` version after rollout.
- [ ] `Settings → Leviathan` has **Lambda Function Name** + **Region** set
      (replaces the old Function-URL field).

### 0.3 `odoo.conf` — log noise (shared multi-project instance)

Quiet per-request HTTP-client logging from every team's modules:

```ini
log_handler = :INFO,httpx:WARNING,botocore:WARNING,boto3:WARNING,urllib3:WARNING,werkzeug:WARNING
```

---

## 1. Capacity Model

| Knob | Default | Where set | Effect |
|---|---|---|---|
| Lambda `ReservedConcurrentExecutions` | **250** | `template.yaml` / Lambda console | Hard cap on concurrent extractions |
| `leviathan.batch_concurrency` | 250 | Odoo Settings → Leviathan | Max parallel `lambda:Invoke` calls per batch dispatch |
| `LEVIATHAN_BATCH_FANOUT_SIZE` (env) | 250 | Odoo pod env | Fallback when ICP unset |
| `LEVIATHAN_PRD_POOL_SIZE` (env) | 50 | Odoo pod env | Thread pool for **post-extraction** PRD generation |
| Odoo `workers` | 4–8 | `odoo.conf` | HTTP workers handling concurrent webhook callbacks |
| Odoo `db_maxconn` | **300** | `odoo.conf` | Must exceed total background-thread + HTTP-worker DB usage |

**Rule of thumb:** `batch_concurrency` ≤ `Lambda ReservedConcurrentExecutions`.
If you exceed the Lambda cap, the extra invokes get throttled
(`TooManyRequestsException`) and the affected jobs revert to `not_assigned`
with an error.

**For 250 truly concurrent extractions you need:**
1. Lambda `ReservedConcurrentExecutions = 250` (done in `template.yaml`)
2. AWS account regional Lambda concurrency quota ≥ 250 — *must* request via Service Quotas if the default 1,000 has been carved up by other functions
3. Odoo pod with enough memory to hold 250 thread stacks (~8 MB each = ~2 GB just for fan-out)
4. Webhook handler capacity: 250 concurrent POSTs → at minimum `workers ≥ 8` and `db_maxconn ≥ 300`

---

## 2. IAM (IRSA) Setup

Use **IRSA (IAM Roles for Service Accounts)** instead of static keys.

### 2.1 Create the IAM policy

```bash
cat > leviathan-odoo-policy.json <<'EOF'
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "InvokeExtractionLambda",
      "Effect": "Allow",
      "Action": ["lambda:InvokeFunction"],
      "Resource": "arn:aws:lambda:ap-south-1:<ACCOUNT_ID>:function:leviathan-extraction"
    },
    {
      "Sid": "BedrockInvoke",
      "Effect": "Allow",
      "Action": ["bedrock:InvokeModel", "bedrock:Converse"],
      "Resource": "*"
    },
    {
      "Sid": "S3Artifacts",
      "Effect": "Allow",
      "Action": ["s3:PutObject", "s3:GetObject", "s3:DeleteObject"],
      "Resource": "arn:aws:s3:::production-grtlabs-tag/leviathan/*"
    },
    {
      "Sid": "S3ArtifactsList",
      "Effect": "Allow",
      "Action": ["s3:ListBucket"],
      "Resource": "arn:aws:s3:::production-grtlabs-tag",
      "Condition": {"StringLike": {"s3:prefix": ["leviathan/*"]}}
    }
  ]
}
EOF

aws iam create-policy \
  --policy-name leviathan-odoo-policy \
  --policy-document file://leviathan-odoo-policy.json
```

### 2.2 Bind to the Odoo service account (eksctl)

```bash
eksctl create iamserviceaccount \
  --name etp-be \
  --namespace ethara \
  --cluster ethara-production \
  --attach-policy-arn arn:aws:iam::<ACCOUNT_ID>:policy/leviathan-odoo-policy \
  --approve \
  --override-existing-serviceaccounts \
  --region ap-south-1
```

This annotates the SA with `eks.amazonaws.com/role-arn=<ROLE_ARN>` and the
boto3 client picks it up automatically when **no explicit access keys** are
set in Odoo's `Settings → Leviathan → Lambda Function`.

### 2.3 Confirm IRSA from inside the pod

```bash
kubectl -n ethara exec deploy/etp-be -- env | grep AWS_ROLE_ARN
kubectl -n ethara exec deploy/etp-be -- aws sts get-caller-identity
```

The second command should print the IRSA role ARN, not the EC2 node role.

---

## 3. Deployment YAML

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: etp-be
  namespace: ethara
spec:
  replicas: 2                        # 2 pods absorb webhook bursts better than 1
  strategy:
    type: RollingUpdate
    rollingUpdate:
      maxSurge: 1
      maxUnavailable: 0              # zero downtime on deploy
  template:
    spec:
      serviceAccountName: etp-be     # IRSA-annotated SA
      containers:
      - name: odoo
        image: <REGISTRY>/etp-be:<TAG>
        env:
        - name: LEVIATHAN_WEBHOOK_TOKEN
          valueFrom:
            secretKeyRef:
              name: leviathan-secrets
              key: webhook-token
        - name: LEVIATHAN_BATCH_FANOUT_SIZE
          value: "250"
        - name: LEVIATHAN_PRD_POOL_SIZE
          value: "50"
        resources:
          requests:
            cpu: "2"
            memory: "4Gi"            # >= odoo.conf limit_memory_hard / 2
          limits:
            cpu: "4"
            memory: "8Gi"            # >= odoo.conf limit_memory_hard
        readinessProbe:
          httpGet:
            path: /web/health
            port: 8069
          initialDelaySeconds: 10
          periodSeconds: 5
        livenessProbe:
          httpGet:
            path: /web/health
            port: 8069
          initialDelaySeconds: 60
          periodSeconds: 30
---
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: etp-be-pdb
  namespace: ethara
spec:
  minAvailable: 1
  selector:
    matchLabels:
      app: etp-be
---
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: etp-be-hpa
  namespace: ethara
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: etp-be
  minReplicas: 2
  maxReplicas: 6
  metrics:
  - type: Resource
    resource:
      name: cpu
      target:
        type: Utilization
        averageUtilization: 70
  - type: Resource
    resource:
      name: memory
      target:
        type: Utilization
        averageUtilization: 80
  behavior:
    scaleDown:
      stabilizationWindowSeconds: 600   # don't yo-yo during webhook bursts
```

---

## 4. `odoo.conf` for 250 concurrent extractions

```ini
[options]
workers = 8
max_cron_threads = 2
db_maxconn = 300                    ; must exceed total concurrent DB work
limit_memory_soft = 4294967296      ; 4 GB
limit_memory_hard = 5368709120      ; 5 GB
limit_time_real = 600               ; webhook handler does S3 work
limit_time_real_cron = 1800         ; watchdog cron room
limit_time_cpu = 300
list_db = False
proxy_mode = True
```

**Pod memory limit MUST exceed `limit_memory_hard`** (above we set 8Gi >= 5GiB).
If K8s OOM-kills the pod before Odoo can recycle the worker, the in-flight
fan-out is lost.

---

## 5. PostgreSQL connection sizing

A 250-wide fan-out itself uses ~0 DB connections (boto3 only). But the **return
trip** — 250 concurrent webhook handlers, each writing the job record and
launching the PRD background thread — does. Worst-case at peak burst:

| Source | Connections |
|---|---|
| HTTP workers serving the webhook (one per worker process) | 8–16 |
| Cron threads (watchdog) | 2 |
| `_POOL` PRD-gen threads opening short-lived registry cursors | 50 |
| Headroom | 20–30 |
| **Total** | **~100** |

Set the **Postgres** `max_connections` to **at least 300** (Odoo's
`db_maxconn` is per-process; with 8 workers + 1 cron container that's still
multiple processes opening connections). On Amazon RDS Postgres, use a
`db.t3.large` or larger instance class.

If you hit `FATAL: too many connections`, add **PgBouncer** in transaction-pool
mode in front of Postgres.

---

## 6. Lambda configuration (one-shot)

```bash
# 1. Bump reserved concurrency to 250 (template.yaml does this on next SAM
#    deploy; this is the imperative version if you can't redeploy).
aws lambda put-function-concurrency --region ap-south-1 \
  --function-name leviathan-extraction \
  --reserved-concurrent-executions 250

# 2. Set ephemeral storage to 4 GB (keep)
aws lambda update-function-configuration --region ap-south-1 \
  --function-name leviathan-extraction \
  --ephemeral-storage Size=4096

# 3. Account-level: confirm regional concurrency quota allows 250+
aws service-quotas get-service-quota \
  --service-code lambda \
  --quota-code L-B99A9384 \
  --region ap-south-1
# If the value is 250 or below, raise it via:
aws service-quotas request-service-quota-increase \
  --service-code lambda \
  --quota-code L-B99A9384 \
  --desired-value 1000 \
  --region ap-south-1
```

---

## 7. Removing the old RabbitMQ consumer

If a `consumer.py` Deployment is still running, **delete it**:

```bash
kubectl -n ethara get deployment -l app=etp-leviathan-consumer
kubectl -n ethara delete deployment etp-leviathan-consumer 2>/dev/null || true
kubectl -n ethara delete configmap leviathan-consumer-env 2>/dev/null || true
kubectl -n ethara delete secret leviathan-consumer-secrets 2>/dev/null || true
```

The RabbitMQ broker itself can be kept running (other apps may use it) or
torn down; the Leviathan module no longer connects to it under any condition.

---

## 8. Smoke test

After deploying both the new Lambda zip and the updated Odoo image:

1. Log into Odoo as an admin.
2. Import 5 URLs via **Configuration → Import URLs**.
3. Select all 5 in the list view → gear menu → **Run Batch (Parallel)**.
4. Notification should appear within 1 second: *"5 task(s) dispatching concurrently"*.
5. Watch CloudWatch Logs for the Lambda — you should see 5 concurrent
   `Direct async invoke for job_id=N` log lines starting within a few
   seconds of each other.
6. Within ~10 min, all 5 jobs should appear as `done` in the Odoo UI.

Then run a full-batch test of 50–100 URLs to confirm the webhook handler
isn't dropping callbacks under burst.

---

## 10. Leviathan PRD worker Deployment (`leviathan-prd-worker`)

Introduced 2026-05-26. Independent of the `etp-be` Deployment in §3.

### 10.1 What it is

A Kubernetes `Deployment` of one or more long-lived pods, each running
`python /opt/odoo/custom_addons/leviathan/worker/run_prd.py`. Each pod
boots a headless Odoo `Registry` once, then runs the PRD claim-loop
(`_prd_queue_fail_poison` → `_prd_queue_recover_stale` →
`_prd_queue_claim_and_dispatch`) every `LEVIATHAN_WORKER_POLL_S` seconds
(default 5 s). Work is dispatched to the in-process `_PRD_POOL`
(`LEVIATHAN_PRD_POOL_SIZE` slots, default 50). On SIGTERM the worker
stops claiming, bounded-drains in-flight futures for
`LEVIATHAN_WORKER_SHUTDOWN_TIMEOUT_S` (default 1800 s), then exits.
Anything still running past the budget is abandoned to SIGKILL — the
row's heartbeat goes stale and the next worker's recovery step
re-claims it. No silent job loss.

### 10.2 Image

`Dockerfile.worker` lives at `custom_addons/leviathan/Dockerfile.worker`.

```bash
docker build --platform linux/amd64 \
  -f custom_addons/leviathan/Dockerfile.worker \
  -t <ACCOUNT_ID>.dkr.ecr.<REGION>.amazonaws.com/leviathan-prd-worker:<NEW_TAG> .

aws ecr get-login-password --region <REGION> | \
  docker login --username AWS --password-stdin \
  <ACCOUNT_ID>.dkr.ecr.<REGION>.amazonaws.com

docker push <ACCOUNT_ID>.dkr.ecr.<REGION>.amazonaws.com/leviathan-prd-worker:<NEW_TAG>
```

**Build context = repo root.** The image bundles `src/`,
`custom_addons/leviathan/`, and `custom_addons/etp_user_roles/`.
**CI must rebuild + push this image on every leviathan addon change**,
in lockstep with the etp-be deploy — a stale worker image is silent
bugs.

### 10.3 Deployment spec (reference)

| Property | Value |
|---|---|
| Kind | `Deployment` |
| Namespace | `leviathan` (or `ethara` — set `leviathan.k8s_namespace` to match if you change it) |
| Name | `leviathan-prd-worker` |
| Replicas | **1 baseline** (manually scale with `kubectl scale` until the auto-scaler ships in Phase 2D) |
| ServiceAccount | `leviathan-worker` |
| Image | `<ECR>/leviathan-prd-worker:<TAG>` (`imagePullPolicy: Always`) |
| CPU request / limit | `2` / `4` |
| Memory request / limit | `4Gi` / `6Gi` |
| Security | non-root, `runAsUser: 1000` |
| Pod annotation | `karpenter.sh/do-not-disrupt: "true"` |
| `terminationGracePeriodSeconds` | `1860` (must be > `LEVIATHAN_WORKER_SHUTDOWN_TIMEOUT_S`) |
| RollingUpdate | `maxUnavailable: 0`, `maxSurge: 1` |
| readinessProbe | `exec: command: ["python", "/opt/odoo/custom_addons/leviathan/worker/run_prd.py", "--check"]`, `initialDelaySeconds: 30`, `periodSeconds: 60`, `timeoutSeconds: 30` |

Env vars:

| Env | Value | Purpose |
|---|---|---|
| `ODOO_DB` | _required_ | DB name (same DB as `etp-be`). |
| `ODOO_CONF` | `/etc/odoo/odoo.conf` | Skeleton; real DB creds come from env. |
| `DB_HOST` / `DB_PORT` / `DB_USER` / `DB_PASSWORD` | _Secret_ | RDS creds. |
| `LEVIATHAN_ROLE` | `worker` | Pod-role hint (auto-set by the binary; setting it here is belt-and-suspenders). |
| `LEVIATHAN_PRD_POOL_SIZE` | `50` | In-flight PRD jobs per pod. |
| `LEVIATHAN_WORKER_POLL_S` | `5` | Drainer-tick interval. |
| `LEVIATHAN_WORKER_SHUTDOWN_TIMEOUT_S` | `1800` | Bounded-drain budget on SIGTERM. **Must be < `terminationGracePeriodSeconds`.** |
| `LEVIATHAN_WORKER_CLAIM_FAIL_LIMIT` | `5` | After N consecutive drainer failures the process exits non-zero so K8s replaces the pod with a fresh registry. |

### 10.4 RBAC

* A namespace `leviathan` (or the namespace from `leviathan.k8s_namespace`).
* A ServiceAccount `leviathan-worker` in that namespace. The pod runs
  as this account. AWS access for Bedrock + S3 is granted via
  **EKS Pod Identity** — associate an IAM role with permissions:
  `bedrock:InvokeModel`, `bedrock:Converse`, `s3:GetObject`,
  `s3:PutObject` on the bucket.
* **No K8s API permissions needed** for the worker itself in this phase
  (no auto-scaler yet — see Phase 2D).

### 10.5 Postgres sizing

Each worker pod holds up to `LEVIATHAN_PRD_POOL_SIZE` long-lived
cursors (one per in-flight PRD job) plus a per-job heartbeat-thread
cursor plus registry overhead. Rough budget:

```
per-pod ceiling = 50 (pool) + 50 (heartbeats, peak) + 10 (overhead) = ~110
set db_maxconn = 128 per worker pod (~15% headroom).

cluster total (1 worker + 2 etp-be HTTP pods + admin):
  1 × 128  =  128
  2 ×  64  =  128
  admin/cron =  50
  -------------
           = ~310 — RDS max_connections = 500 is plenty for baseline.
```

**PgBouncer (if present): session pooling mode is REQUIRED.** The
drainer's `pg_try_advisory_lock('leviathan.prd_drainer')` is
session-scoped; transaction pooling silently breaks it and you get
double-drain on every cron tick.

### 10.6 Enable the worker (one-time)

With the Deployment running and `replicas: 1`:

```bash
# 1. Make sure the queue is on (idempotent if already set).
psql -h <RDS> -U <USER> -d <DB> -c \
  "INSERT INTO ir_config_parameter (key, value) \
   VALUES ('leviathan.prd_queue_enabled', 'True') \
   ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;"

# 2. Hand the drainer to the standalone worker.
psql -h <RDS> -U <USER> -d <DB> -c \
  "INSERT INTO ir_config_parameter (key, value) \
   VALUES ('leviathan.prd_execution_mode', 'worker') \
   ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;"
```

Both changes are **live, no restart needed.** The next Odoo cron tick
short-circuits; the next worker tick (within `LEVIATHAN_WORKER_POLL_S`
seconds) starts claiming. Verify:

```bash
kubectl -n leviathan logs -l app=leviathan-prd-worker --tail=50 | \
  grep -E "registry booted|claimed [0-9]+ job|queue depth="
# Should show the registry boot line and (when there is work) claim/depth lines.

# UI pod must NOT be draining anymore:
kubectl -n ethara logs -l app=etp-be --tail=200 | grep "drainer tick"
# Expect: empty.
```

### 10.7 Rolling deploy

```bash
kubectl -n leviathan set image deployment/leviathan-prd-worker \
  worker=<ECR>/leviathan-prd-worker:<NEW_TAG>
kubectl -n leviathan rollout status deployment/leviathan-prd-worker
```

`maxUnavailable: 0` brings the new pod up before the old pod gets
SIGTERM. Old pod drains in-flight jobs up to
`LEVIATHAN_WORKER_SHUTDOWN_TIMEOUT_S`; anything still running past the
budget is abandoned and re-claimed by the new pod via stale-heartbeat
recovery. No silent job loss.

### 10.8 Roll back to in-process drainer

Live, no redeploy:

```bash
psql -h <RDS> -U <USER> -d <DB> -c \
  "UPDATE ir_config_parameter SET value='inprocess' \
     WHERE key='leviathan.prd_execution_mode';"
kubectl -n leviathan scale deployment/leviathan-prd-worker --replicas=0
```

The in-Odoo cron resumes draining. Reverse with the opposite two
commands.

### 10.9 Settings-parameter propagation to the worker pod (operator gotcha)

When an operator updates a System Parameter via the etp-be UI (e.g.
rotates `leviathan.bedrock_access_key_id`, changes
`leviathan.prd_max_attempts`), the change becomes visible inside the
worker pod on its **next drain tick** (default 5 s, see
`LEVIATHAN_WORKER_POLL_S`). The worker process explicitly clears its
ORM cache at the top of every tick — it cannot rely on Odoo's normal
`bus.bus` cross-process invalidation because the worker runs
`--no-http` and does not start the bus listener.

Practical implication for runbooks:

* **Bedrock token rotation:** save the new value in Settings → click
  Save → within ~5 s the worker is using the new token. **No pod
  restart needed.** Verify by tailing the worker log around the next
  job's PHASE 2; a fresh 200-OK Bedrock call confirms the rotation
  landed.
* **`prd_queue_enabled` toggle:** instantaneous — the worker's tick
  reads the master flag right after the cache clear.
* **`prd_max_attempts` / `batch_max_size`:** same; next-tick.
* **Anything that requires schema change** (a new column, a new
  index) still requires `odoo -u leviathan` on the etp-be pod AND a
  rolling restart of the worker Deployment (image bundles the addon
  source, so the worker pod must pull the new image to see new
  fields).

If an operator complains "I updated a setting but the worker is still
using the old value", the answer is almost always: wait 5 s. If it's
still stale after a full minute, `kubectl rollout restart
deployment/leviathan-prd-worker -n leviathan` is the hammer.

### 10.10 Opt-in image attachment to Bedrock (extraction-richness commit, 2026-05-26)

The pipeline ships with two System Parameters that let an operator
trade reliability for visual grounding on Bedrock LLM calls. Both
default to `False` so this is behaviour-neutral until you flip one.

| Parameter | Default | What it does |
|---|---:|---|
| `leviathan.prd_include_images` | `False` | When `True`, PRD-generation attaches up to `prd_max_images` screenshots to the Bedrock Converse call |
| `leviathan.qc_include_images` | `False` | When `True`, QC alignment-check attaches up to `prd_max_images` screenshots |
| `leviathan.prd_max_images` | `4` | Hard cap on N for both flags above |

**Why both default OFF.** Long-output PRD generation + image content
blocks raises Bedrock's 4xx rejection rate sharply. The Lambda's
`build_prd_prompt` already encodes the visual extraction as text —
attaching images on top is signal-redundant for a well-extracted site
and mainly serves to raise the failure rate. Turn `qc_include_images`
ON first if you turn anything ON — QC is a short-output Bedrock call,
much less prone to image-related 4xx than PRD-gen.

**How operators flip them.** Settings → Leviathan → "Attach
Screenshots to LLM Calls (opt-in)" → toggle the two booleans, set
max-images, Save. Within ~5 s the worker pod sees the new values
(see §10.9). No pod restart required.

**How to back out.** Toggle them OFF in Settings. Next-tick. If a
job is mid-PRD-generation when you toggle, that job runs with the
old setting (config is captured at the start of the Bedrock call);
the NEXT job picks up the new value.

**Telemetry to watch when flipping ON.**

```bash
# 1. Bedrock 4xx rejections (should stay close to current baseline)
kubectl -n leviathan logs -l app=leviathan-prd-worker --since=1h | \
  grep -E "Bedrock API error \[4"

# 2. "attached N screenshot block(s)" lines confirm images are flowing
kubectl -n leviathan logs -l app=leviathan-prd-worker --since=1h | \
  grep "attached.*screenshot block"

# 3. PRD failure_count and poison-cap rate — should not spike
psql -h <RDS> -U <USER> -d <DB> -c \
  "SELECT date_trunc('hour', completed_at) AS hour, \
          count(*) FILTER (WHERE state='failed' AND error_message LIKE '%poison cap%') AS poisoned, \
          count(*) FILTER (WHERE state='done') AS done \
     FROM leviathan_job \
    WHERE completed_at > NOW() - INTERVAL '6 hours' \
    GROUP BY 1 ORDER BY 1;"
```

If poison-cap rate jumps after flipping a flag ON, flip it back —
it's not worth more visual signal at the cost of more failed jobs.

---

## 9. Rollback Plan

The 19.0.1.x branch is preserved on git. To roll back:

1. **Odoo**: redeploy the previous image; the `via_rabbitmq` migration is
   one-way (we drop the old column). If you absolutely must roll back, the
   old column can be re-added with `ALTER TABLE leviathan_job ADD COLUMN
   via_rabbitmq BOOLEAN DEFAULT FALSE; UPDATE leviathan_job SET via_rabbitmq = via_batch;`
   then revert the manifest to 19.0.1.x.
2. **Lambda**: redeploy the previous zip — the new handler is backward
   compatible with Function URL invocations, so no Lambda rollback is
   strictly required.
3. **Bring back the RabbitMQ consumer**: re-apply its Deployment YAML and
   restore `services/rabbitmq_service.py` from git history.

---

## 10. Shipped in 19.0.2.1.0 (Lambda + Odoo)

**Guiding principle (product):** the deliverable is the **PRD**. Screenshots and
assets are *inputs* — the tasker supplies/fixes them manually at review time, so
missing/blank assets are **not** a pipeline failure. A job is `failed` (red)
**only** when (1) extraction produced nothing usable for a PRD, or (2) PRD
generation failed. Everything else is a **successful extraction** → proceeds to
PRD gen, surfaced as a non-red "Partial extraction" warning banner.

Tested locally: module upgrades clean, all views load, simulated state-transition
suite passes (skip-re-extraction, discard, transparency fields).

### 10.1 Lambda — `handler.py` (needs a new zip)

- **Extraction success redefined** — `_finalize_and_callback`: `success` is now
  based on a usable `prd_prompt`, **not** screenshot count. 0/low screenshots →
  `partial: true` + a `warnings` list, never a hard failure.
- **`phase_log` + `extraction_summary`** added to the callback payload — per-job
  transparency on what every phase did and what was captured.
- **`status: "started"` ping** — POSTed to the Odoo webhook at Phase 1 so the
  watchdog measures real progress, not time-in-state.
- All browser-resilience work retained (`_relaunch_if_dead`, `_bounded`,
  `_screenshot_with_retry`, `--single-process` removed, `/tmp` self-cleanup).

### 10.2 Odoo — `19.0.2.1.0` (GitHub; auto-migrates on update)

- **Failure semantics** — webhook + `_run_prd_generation_bg` only set `failed`
  on the two real conditions above; a `success:true` callback (incl. `partial`,
  incl. 0 screenshots) **always** proceeds to `generating`. Partial/warnings go
  to a new `extraction_warnings` field (yellow banner), never `error_message`.
- **Skip re-extraction** — a job that already has a `prd_prompt` is "extracted":
  **Batch Run** and **Retry** send it straight to `generating`. Only jobs
  *without* a `prd_prompt` go through the Lambda fan-out. The explicit
  "Re-extract" wizard still forces a fresh extraction.
- **`discarded` terminal state** — for tasks that are genuinely unusable
  (site unsuitable / nothing extracted). The **Discard** button shows on
  *Failed* tasks and *Done* tasks with a **NOT SHIPPABLE** QC verdict ("not
  shippable and below"). Discarding **keeps the tasker assigned** — the task
  stays "theirs". A discarded task is not a dead end: the **Assign** button
  reopens it as a Draft (keeping its tasker, or the clicker if it had none).
  **Cancel** is unchanged and separate: it shows only on the
  running stages (extracting / generating / scoring) and returns the task to
  Draft, signalling background threads to stop.
- **Robust self-recovering pool** — all background work goes through
  `_submit_bg()`: logs pool saturation, wraps every callable so a crash is
  logged not lost, and runs inline if the pool is gone. Watchdog is the backstop.
- **Full transparency** — new `lambda_callback_json` (raw Lambda payload) and
  `llm_trace_json` (every PRD-gen attempt + QC request/response) fields,
  populated as the pipeline runs — captured for audit (no dedicated UI tab).
- **Watchdog 'started' ping** — webhook handles `status:"started"`, updating
  `last_heartbeat` so queued-but-not-started jobs aren't killed.
- **Log prefixing** — key lifecycle logs prefixed `[leviathan][job=<name>]`.

Also already committed to the branch (earlier "error fixes" commit, ships with
this push):
- `extraction_service.py` — SigV4 re-signed per retry attempt + only fast 5xx
  are retried (a slow 5xx = the Lambda ran and timed out; don't re-run it).
- `_run_prd_generation_bg` — generation loop keeps the best PRD even when every
  attempt scores 0, so a run of rejected PRDs no longer crashes on S3 upload.
- `_run_qc_only_bg` — QC errors fail **closed** (`qc_verdict = not_shippable`)
  instead of leaving the verdict blank.

**Migration:** no schema rename — new fields/columns and the `discarded`
selection value are auto-created on `-u leviathan`. The version bump
(`19.0.2.0.0` → `19.0.2.1.0`) triggers it automatically on deploy.

### 10.3 Reading the logs on stage (Grafana)

The Odoo instance is shared across projects. To see only Leviathan during a
stage test, filter by logger name or the `[leviathan]` prefix:

```
{namespace="ethara", container="etp-be"} |= "addons.leviathan"        # all Leviathan logs
{namespace="ethara", container="etp-be"} |= "[leviathan][job="        # per-job lifecycle
{namespace="ethara", container="etp-be"} |~ "leviathan.*ERROR|WARNING" # leviathan problems only
```

Lambda logs are in **CloudWatch** (`/aws/lambda/<function>`), already `[job=N]`
prefixed — filter `[job=` for one job, or `Callback to` / `success` / `warnings`
for the outcome. The `log_handler` in §0.3 quiets the `httpx`/`boto3` per-request
noise that otherwise drowns the stream.

### 10.4 Out of scope (already built)

Tasker review / edit / regenerate / re-extract flows are the manual-fix path the
product relies on and were left unchanged.
