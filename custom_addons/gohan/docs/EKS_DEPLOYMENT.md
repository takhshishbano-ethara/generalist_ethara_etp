# Gohan EKS Deployment Guide (19.0.2.0.0+)

**Audience:** DevOps / SRE running Odoo on Amazon EKS.

**What changed in 19.0.2.0.0:** RabbitMQ is removed. Batch fan-out now happens
in-process inside the Odoo pod via `boto3 lambda:Invoke(InvocationType='Event')`
with a 250-wide `ThreadPoolExecutor`. Each invoke returns in <1 s; Lambdas run
asynchronously and post back to the existing webhook.

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
cd gohan-extraction-lambda
grep -rc single-process handler.py modules/site_discoverer.py        # expect: 0  0
grep -cE 'EphemeralStorage|EventInvokeConfig|ReservedConcurrentExecutions: 250' template.yaml  # expect: 3
python3 -m py_compile handler.py config.py modules/*.py && echo "compile OK"
```

### 0.2 Odoo deploy must include

- [ ] Module version is **`19.0.2.1.0`** — see §10 for what shipped. The
      `19.0.2.0.0` pre-migration (renames `via_rabbitmq`→`via_batch`, seeds config,
      removes the old RabbitMQ server action) runs if the DB is older; the
      `19.0.2.1.0` additions (new fields, `discarded` state) are auto-created on
      `-u gohan` — no migration file needed.
- [ ] **Confirm prod actually picks up `19.0.2.1.0`.** A prior cycle ran stale code
      (recursive `write()` → infinite recursion). Check the deployed image tag and
      `__manifest__.py` version after rollout.
- [ ] `Settings → Gohan` has **Lambda Function Name** + **Region** set
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
| `gohan.batch_concurrency` | 250 | Odoo Settings → Gohan | Max parallel `lambda:Invoke` calls per batch dispatch |
| `GOHAN_BATCH_FANOUT_SIZE` (env) | 250 | Odoo pod env | Fallback when ICP unset |
| `GOHAN_PRD_POOL_SIZE` (env) | 50 | Odoo pod env | Thread pool for **post-extraction** PRD generation |
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
cat > gohan-odoo-policy.json <<'EOF'
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "InvokeExtractionLambda",
      "Effect": "Allow",
      "Action": ["lambda:InvokeFunction"],
      "Resource": "arn:aws:lambda:ap-south-1:<ACCOUNT_ID>:function:gohan-extraction"
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
      "Resource": "arn:aws:s3:::production-grtlabs-tag/gohan/*"
    },
    {
      "Sid": "S3ArtifactsList",
      "Effect": "Allow",
      "Action": ["s3:ListBucket"],
      "Resource": "arn:aws:s3:::production-grtlabs-tag",
      "Condition": {"StringLike": {"s3:prefix": ["gohan/*"]}}
    }
  ]
}
EOF

aws iam create-policy \
  --policy-name gohan-odoo-policy \
  --policy-document file://gohan-odoo-policy.json
```

### 2.2 Bind to the Odoo service account (eksctl)

```bash
eksctl create iamserviceaccount \
  --name etp-be \
  --namespace ethara \
  --cluster ethara-production \
  --attach-policy-arn arn:aws:iam::<ACCOUNT_ID>:policy/gohan-odoo-policy \
  --approve \
  --override-existing-serviceaccounts \
  --region ap-south-1
```

This annotates the SA with `eks.amazonaws.com/role-arn=<ROLE_ARN>` and the
boto3 client picks it up automatically when **no explicit access keys** are
set in Odoo's `Settings → Gohan → Lambda Function`.

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
        - name: GOHAN_WEBHOOK_TOKEN
          valueFrom:
            secretKeyRef:
              name: gohan-secrets
              key: webhook-token
        - name: GOHAN_BATCH_FANOUT_SIZE
          value: "250"
        - name: GOHAN_PRD_POOL_SIZE
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
  --function-name gohan-extraction \
  --reserved-concurrent-executions 250

# 2. Set ephemeral storage to 4 GB (keep)
aws lambda update-function-configuration --region ap-south-1 \
  --function-name gohan-extraction \
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
kubectl -n ethara get deployment -l app=etp-gohan-consumer
kubectl -n ethara delete deployment etp-gohan-consumer 2>/dev/null || true
kubectl -n ethara delete configmap gohan-consumer-env 2>/dev/null || true
kubectl -n ethara delete secret gohan-consumer-secrets 2>/dev/null || true
```

The RabbitMQ broker itself can be kept running (other apps may use it) or
torn down; the Gohan module no longer connects to it under any condition.

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

## 9. Rollback Plan

The 19.0.1.x branch is preserved on git. To roll back:

1. **Odoo**: redeploy the previous image; the `via_rabbitmq` migration is
   one-way (we drop the old column). If you absolutely must roll back, the
   old column can be re-added with `ALTER TABLE gohan_job ADD COLUMN
   via_rabbitmq BOOLEAN DEFAULT FALSE; UPDATE gohan_job SET via_rabbitmq = via_batch;`
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
- **Log prefixing** — key lifecycle logs prefixed `[gohan][job=<name>]`.

Also already committed to the branch (earlier "error fixes" commit, ships with
this push):
- `extraction_service.py` — SigV4 re-signed per retry attempt + only fast 5xx
  are retried (a slow 5xx = the Lambda ran and timed out; don't re-run it).
- `_run_prd_generation_bg` — generation loop keeps the best PRD even when every
  attempt scores 0, so a run of rejected PRDs no longer crashes on S3 upload.
- `_run_qc_only_bg` — QC errors fail **closed** (`qc_verdict = not_shippable`)
  instead of leaving the verdict blank.

**Migration:** no schema rename — new fields/columns and the `discarded`
selection value are auto-created on `-u gohan`. The version bump
(`19.0.2.0.0` → `19.0.2.1.0`) triggers it automatically on deploy.

### 10.3 Reading the logs on stage (Grafana)

The Odoo instance is shared across projects. To see only Gohan during a
stage test, filter by logger name or the `[gohan]` prefix:

```
{namespace="ethara", container="etp-be"} |= "addons.gohan"        # all Gohan logs
{namespace="ethara", container="etp-be"} |= "[gohan][job="        # per-job lifecycle
{namespace="ethara", container="etp-be"} |~ "gohan.*ERROR|WARNING" # gohan problems only
```

Lambda logs are in **CloudWatch** (`/aws/lambda/<function>`), already `[job=N]`
prefixed — filter `[job=` for one job, or `Callback to` / `success` / `warnings`
for the outcome. The `log_handler` in §0.3 quiets the `httpx`/`boto3` per-request
noise that otherwise drowns the stream.

### 10.4 Out of scope (already built)

Tasker review / edit / regenerate / re-extract flows are the manual-fix path the
product relies on and were left unchanged.
