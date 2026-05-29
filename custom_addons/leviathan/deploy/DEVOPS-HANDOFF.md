# Leviathan — DevOps Handoff (Pod Architecture, v19.0.6.0.0)

This is the **single source of truth for shipping Leviathan to stage and prod.**
The architecture is the worker-pod + K8s scaler pipeline that replaces the
in-Odoo `ir.cron` drainer with a long-lived Python process (`leviathan-prd-worker`)
sized by queue depth.

It supersedes the older `EKS_DEPLOYMENT.md` notes — that file remains as
background reading; this file is what DevOps follows during a rollout.

---

## TL;DR

```
Odoo backend pod (UI + webhook + cron HTTP) ──► HTTP cron endpoints (POST)
                  │
                  ▼
       K8s CronJob "leviathan-cron-dispatch" (every 1 min)
                  │
                  ▼ patches replicas
       Deployment "leviathan-prd-worker"   ──► claims jobs from Postgres
                                                  via FOR UPDATE SKIP LOCKED
                                                  → Bedrock → S3 → QC → done
```

| Layer | Image | Tag policy | Restart on rollout |
|---|---|---|---|
| Backend Odoo | `etp-be:<gitsha>` | Pin, never `:latest` | Standard Odoo rolling |
| Worker pod | `leviathan-prd-worker:<gitsha>` | Pin, never `:latest` | RollingUpdate; 30-min drain |
| Extraction Lambda | Lambda function alias on AWS | Function version | SAM deploy, alias swap |
| Cron triggers | `curlimages/curl:8.10.1` (pin digest) | Pin to digest | Replaced by next CronJob run |

---

## 1. Prerequisites (one-time per environment)

### 1.1 AWS Secrets Manager

| Secret name | Body | Synced to (K8s Secret) | Used by |
|---|---|---|---|
| `ethara/leviathan/<env>/odoo-config` | Full Odoo `odoo.conf` (`[options]` block with DB creds, `db_maxconn`, `addons_path`) | `leviathan-odoo-config` (mounted as file in worker pod) | Worker pod |
| `ethara/leviathan/<env>/webhook-token` | 64-char random string | `leviathan-webhook-token` (key=`token`) | Worker pod, backend Odoo pod, Lambda env, K8s CronJobs |
| (prod, F-MED-4) `ethara/leviathan/<env>/cron-token` | Separate 64-char random | `leviathan-cron-token` (key=`token`) | K8s CronJobs only; lets you rotate cron auth without touching Lambda |

`ExternalSecret` resources mapping these Secrets into the cluster live in
the platform-team repo. Confirm both `leviathan-odoo-config` and
`leviathan-webhook-token` are present **before** applying anything from
this folder.

### 1.2 IAM (IRSA / EKS Pod Identity)

Attach to ServiceAccount `leviathan-worker` (in namespace `leviathan`):

- `bedrock:InvokeModel`, `bedrock:InvokeModelWithResponseStream` on the
  inference-profile ARN configured in Odoo Settings
  (`leviathan.bedrock_inference_arn`).
- `s3:GetObject`, `s3:PutObject`, `s3:DeleteObject` on the artifact
  bucket (`leviathan.s3_bucket`).

Attach to the Lambda execution role (managed by SAM):

- `s3:GetObject`, `s3:PutObject` on the artifact bucket.
- `logs:CreateLogGroup`, `logs:CreateLogStream`, `logs:PutLogEvents`
  (CloudWatch — covered by the AWS managed `AWSLambdaBasicExecutionRole`).

### 1.3 ECR repos

- `leviathan-prd-worker` (Linux/amd64)
- `leviathan-extraction-lambda` (the lambda image — SAM may push it for you)

### 1.4 RDS

- `db_maxconn` per worker pod set to `LEVIATHAN_PRD_POOL_SIZE × 1.5` slack
  + 4 for the heartbeat aggregator + the log buffer flusher. At
  `LEVIATHAN_PRD_POOL_SIZE=22` (the stage default) that's **≈ 37 slots
  per worker pod**. Cluster total = `worker_max_replicas × 37 + Odoo backend slots`.
  Confirm RDS instance class can serve this.

---

## 2. Build + push

### 2.1 Worker image

```bash
cd /path/to/ethara-etp
GITSHA=$(git rev-parse --short HEAD)
ECR=426628337772.dkr.ecr.ap-south-1.amazonaws.com

aws ecr get-login-password --region ap-south-1 \
  | docker login --username AWS --password-stdin "$ECR"

# Build on amd64 (Mac users: --platform linux/amd64 + buildx)
docker buildx build \
  --platform linux/amd64 \
  -f custom_addons/leviathan/Dockerfile.worker \
  -t "$ECR/leviathan-prd-worker:v19.0.6.0.0-$GITSHA" \
  --push \
  .

# (Optionally also tag and push :v19.0.6.0.0 as a moving semver,
# but DO NOT use that tag from the manifest — pin the -$GITSHA tag.)
```

### 2.2 Lambda image

```bash
cd custom_addons/leviathan/leviathan-extraction-lambda
# template.yaml parameterises the webhook URL + token; see §3.
sam build --use-container --parallel
sam deploy --stack-name leviathan-extraction-stage \
  --config-env stage \
  --parameter-overrides \
    WebhookUrl=https://stage-odoo.ethara.example.com/api/v1/leviathan/webhook/extraction-complete \
    WebhookTokenParameter=/ethara/leviathan/stage/webhook-token
```

SAM publishes a new Lambda function version and (with `AutoPublishAlias: live`)
shifts the `live` alias to it. Roll back with
`sam delete --stack-name leviathan-extraction-stage` + redeploy of the
previous git ref.

### 2.3 Image-pin the worker manifest

`deploy/worker-deployment.yaml` ships with `CHANGE-ME-PIN-TAG-OR-DIGEST`
as the image tag — F-CRIT-1 fix. Patch it before apply:

```bash
sed -i.bak \
  "s|leviathan-prd-worker:CHANGE-ME-PIN-TAG-OR-DIGEST|leviathan-prd-worker:v19.0.6.0.0-$GITSHA|" \
  custom_addons/leviathan/deploy/worker-deployment.yaml
rm custom_addons/leviathan/deploy/worker-deployment.yaml.bak
```

Or use Kustomize / Helm — recommended for prod (see §10).

---

## 3. Apply manifests

```bash
# 0. ALWAYS run validate.sh first. Fails on unedited placeholders.
bash custom_addons/leviathan/deploy/validate.sh

# 1. Namespace
kubectl create namespace leviathan || true

# 2. RBAC — edit deploy/rbac.yaml subject + namespace to match your
#    backend ServiceAccount name first.
kubectl apply -f custom_addons/leviathan/deploy/rbac.yaml

# 3. Worker Deployment (pinned image required — see §2.3)
kubectl apply -f custom_addons/leviathan/deploy/worker-deployment.yaml

# 4. NetworkPolicy — tighten ipBlock CIDR to your RDS subnet first.
kubectl apply -f custom_addons/leviathan/deploy/network-policy.yaml

# 5. CronJobs (POST endpoints — F-HIGH-3) — adjust ODOO_URL to your
#    backend Service DNS first.
kubectl apply -f custom_addons/leviathan/deploy/cronjobs.yaml
```

---

## 4. Configure Odoo (System Parameters)

DevOps DOES NOT edit the application config. Set these via Settings →
Technical → Parameters → System Parameters in the UI, or via
`UPDATE/INSERT ir_config_parameter` once during bootstrap.

### 4.1 Required for the pipeline to be active

| Key | Stage default | Prod default | Notes |
|---|---|---|---|
| `leviathan.prd_queue_enabled` | `True` | `True` | Master switch |
| `leviathan.prd_execution_mode` | `worker` | `worker` | Activates the K8s pipeline |
| `leviathan.webhook_token` | <secret> | <secret> | Match `leviathan-webhook-token` |

### 4.2 Bedrock

| Key | Stage default | Prod default |
|---|---|---|
| `leviathan.bedrock_inference_arn` | stage ARN | prod ARN |
| `leviathan.bedrock_region` | `ap-south-1` | `ap-south-1` |
| `leviathan.bedrock_max_concurrent` | `22` | `22` (per pod) |
| `leviathan.bedrock_inner_retries` | `2` | `2` |
| `leviathan.bedrock_access_key_id` | (unset → IRSA) | (unset → IRSA) |
| `leviathan.bedrock_secret_access_key` | (unset → IRSA) | (unset → IRSA) |

### 4.3 Lambda + S3

| Key | Stage default | Prod default |
|---|---|---|
| `leviathan.lambda_function_name` | `leviathan-extraction-lambda` | `leviathan-extraction-lambda` |
| `leviathan.lambda_region` | `ap-south-1` | `ap-south-1` |
| `leviathan.lambda_local_url` | (blank) | (blank) |
| `leviathan.s3_bucket` | stage bucket | prod bucket |
| `leviathan.s3_region` | `us-east-1` | `us-east-1` |
| `leviathan.s3_folder` | `leviathan` | `leviathan` |
| `leviathan.s3_cdn_url` | (blank or CF) | (CF distribution) |

### 4.4 K8s scaler (NEW in v19.0.6.0.0)

| Key | Stage default | Prod default |
|---|---|---|
| `leviathan.k8s_namespace` | `leviathan` | `leviathan` |
| `leviathan.worker_deployment_name` | `leviathan-prd-worker` | `leviathan-prd-worker` |
| `leviathan.worker_min_replicas` | `1` | `1` (F-CRIT-2 — never default 0) |
| `leviathan.worker_max_replicas` | `5` | `10` |
| `leviathan.worker_target_concurrency` | `100` | `100` (must match `LEVIATHAN_PRD_POOL_SIZE` env on the pod) |
| `leviathan.worker_scale_down_cooldown_s` | `600` | `600` |

### 4.5 Worker loop (NEW in v19.0.6.0.0 — ICP > env > default)

These are LIVE: change them in Settings and the next worker drainer
tick picks them up. No pod restart needed.

| Key | Stage default | Prod default |
|---|---|---|
| `leviathan.worker_poll_s` | `5` | `5` |
| `leviathan.worker_shutdown_timeout_s` | `1800` | `1800` (must be < `terminationGracePeriodSeconds=1860`) |
| `leviathan.worker_claim_fail_limit` | `5` | `5` |

### 4.6 Watchdog / safety

| Key | Stage default | Prod default |
|---|---|---|
| `leviathan.prd_stale_minutes` | `15` | `15` |
| `leviathan.watchdog_extracting_minutes` | `20` | `20` |
| `leviathan.watchdog_generating_minutes` | `15` | `15` |
| `leviathan.watchdog_auto_retry_max` | `1` | `1` |
| `leviathan.batch_concurrency` | `250` | `250` |
| `leviathan.max_jobs_per_user` | `5` | `10` |
| `leviathan.webhook_max_bytes` | `10485760` | `10485760` |
| `leviathan.log_retention_days` | `90` | `90` (F-MED-2; wire to a daily cron) |

---

## 5. Env vars on the worker pod (minimal surface — F-HIGH-6 sized)

Everything else lives in Settings. This is the complete env set on the
worker pod:

| Env | Source | Why env-only |
|---|---|---|
| `ODOO_DB` | Secret `leviathan-odoo-config` key=`db_name` | Needed before DB is readable |
| `ODOO_CONF` | `/etc/odoo/odoo.conf` (file mount) | Same |
| `LEVIATHAN_ROLE` | hard-coded `worker` | Belt-and-braces against cron firing |
| `LEVIATHAN_WEBHOOK_TOKEN` | Secret `leviathan-webhook-token` key=`token` | Token auth, needed pre-DB |
| `LEVIATHAN_PRD_POOL_SIZE` | Deployment spec (`22`) | Module-import-time; rebuild required to change |
| `LEVIATHAN_BEDROCK_MAX_CONCURRENT` | Deployment spec (`22`) | Module-import-time; rebuild required |
| `AWS_REGION` | Deployment spec | boto3 client init |
| `LEVIATHAN_WORKER_POLL_S` | Deployment spec fallback (`5`) | ICP overrides this live |
| `LEVIATHAN_WORKER_SHUTDOWN_TIMEOUT_S` | Deployment spec fallback (`1800`) | ICP overrides this live |
| `LEVIATHAN_WORKER_CLAIM_FAIL_LIMIT` | Deployment spec fallback (`5`) | ICP overrides this live |

Per F-HIGH-6 we cap `LEVIATHAN_PRD_POOL_SIZE` at `LEVIATHAN_BEDROCK_MAX_CONCURRENT`
so steady-state is 22 in-flight cursors per pod, not 100.

---

## 6. Verification (per task) — what a HEALTHY task looks like

Stage validation: enqueue 5 tasks via the UI, then walk this checklist
against any one job ID `$JOB`:

```bash
# 1. Lambda extraction
kubectl logs -n leviathan -l app=leviathan-prd-worker --since=15m | grep "job=$JOB"
# OR for real Lambda: CloudWatch logs in /aws/lambda/leviathan-extraction-lambda
# Healthy = "success=True ... prd_prompt=>5000B ... Callback returned 200"

# 2. S3 artifacts
aws s3 ls s3://$BUCKET/leviathan/$JOB/ --recursive | head -30
# Healthy = screenshots/, assets/, raw_data/site_discovery.json all present

# 3. Webhook received
kubectl logs -n ethara -l app=odoo-backend --since=15m | grep "job=LEV-$(printf '%05d' $JOB)] extraction COMPLETE"
# Healthy = state='generating', has_prompt=t, prompt_b > 5000

# 4. Worker claimed it
kubectl logs -n leviathan -l app=leviathan-prd-worker --since=10m | grep "PHASE\|job=$JOB"
# Healthy = PHASE 1 → PHASE 2 → calling Bedrock; prd_claim_count >= 1

# 5. Bedrock + scoring
psql $LEVIATHAN_DB -c "SELECT id,state,LENGTH(prd_markdown) AS prd_b,score,grade FROM leviathan_job WHERE id=$JOB;"
# Healthy = prd_b > 20000, score 0-100 set

# 6. Final S3 + QC
psql $LEVIATHAN_DB -c "SELECT id,state,qc_verdict,prd_s3_key,completed_at FROM leviathan_job WHERE id=$JOB;"
# Healthy = state='done', qc_verdict='shippable' (or 'needs_revision'), prd_s3_key non-null

# 7. No double-spend
psql $LEVIATHAN_DB -c "SELECT id,prd_claim_count,prd_failure_count FROM leviathan_job WHERE id=$JOB;"
# Healthy = prd_claim_count=1 AND prd_failure_count=0
# (claim_count>1 without a kill event → fence over-triggering;
#  prd_failure_count>0 → Bedrock retry → $$ likely doubled — investigate)
```

---

## 7. 100-concurrent burst test on stage

Stage flag-ON validation. **Do this on stage with a real Bedrock account
that has TPM headroom — never on prod first.**

```bash
# 1. Make sure stage settings match prod (esp. min_replicas, max_replicas).
#    Bump max temporarily if needed:
psql $LEVIATHAN_DB -c \
  "UPDATE ir_config_parameter SET value='10' WHERE key='leviathan.worker_max_replicas';"

# 2. Inject 100 test rows directly into the queue.
psql $LEVIATHAN_DB <<'SQL'
INSERT INTO leviathan_job (
    url, state, auto_continue, cancel_requested,
    via_batch, prd_claim_count, prd_failure_count, heartbeat_failure_count,
    name, create_uid, write_uid, create_date, write_date
)
SELECT
    'https://example.com/test-' || i,
    'generating', true, false,
    true, 0, 0, 0,
    'LEV-T' || lpad(i::text, 5, '0'),
    1, 1, now(), now()
FROM generate_series(1, 100) i;
SQL

# 3. Watch the scaler patch replicas up.
watch -n 5 \
  "kubectl -n leviathan get deploy leviathan-prd-worker -o jsonpath='{.spec.replicas}'; echo"

# Expected within ~60s: spec.replicas = ceil(100 / 22) = 5

# 4. Watch claim progress.
watch -n 10 "psql $LEVIATHAN_DB -tAc \
  \"SELECT state, count(*) FROM leviathan_job \
     WHERE name LIKE 'LEV-T%' GROUP BY state ORDER BY count DESC\""

# 5. Final report
psql $LEVIATHAN_DB -c "
SELECT state, qc_verdict, count(*),
       avg(EXTRACT(EPOCH FROM (completed_at - started_processing_at)))::int AS avg_wall_s,
       avg(score)::numeric(5,1) AS avg_score,
       sum(CASE WHEN prd_failure_count > 0 THEN 1 ELSE 0 END) AS jobs_with_retries
FROM leviathan_job WHERE name LIKE 'LEV-T%'
GROUP BY state, qc_verdict ORDER BY count(*) DESC;"

# 6. Scale-down verification — clean the test rows, watch replicas drop
#    after the cooldown (default 600s).
psql $LEVIATHAN_DB -c "DELETE FROM leviathan_job WHERE name LIKE 'LEV-T%';"
sleep 700
kubectl -n leviathan get deploy leviathan-prd-worker -o jsonpath='{.spec.replicas}'; echo
# Expected: 1 (the min_replicas default)
```

Healthy 100-concurrent looks like:

- Scaler tick patches `spec.replicas` from 1 → 5 within 60–90s.
- All 100 jobs reach `state='done'` within ~10 minutes.
- `prd_failure_count = 0` for ≥ 98 of 100 (1–2% Bedrock 5xx retries is normal).
- No `prd_claim_count > 1` rows (the fence never re-fired).
- Bedrock invocation count ≈ 100 in CloudWatch (NOT 200 — that would mean double-spend).
- `qc_verdict='shippable'` for the majority; `needs_revision` for the rest.

---

## 8. Roll-back paths

The architecture has THREE redundant rollback paths in increasing order
of intrusiveness:

1. **Suspend the K8s CronJobs** — fastest. Stops the scaler from
   patching replicas; existing pods finish their work.
   ```bash
   kubectl -n leviathan patch cronjob leviathan-cron-dispatch \
     -p '{"spec":{"suspend":true}}'
   kubectl -n leviathan patch cronjob leviathan-cron-watchdog \
     -p '{"spec":{"suspend":true}}'
   ```

2. **Switch to inprocess mode** — bypasses the K8s pipeline entirely.
   The Odoo backend pod runs the drainer in-process. Use this if you
   suspect the bug is worker-pod-specific.
   ```sql
   UPDATE ir_config_parameter SET value='inprocess'
   WHERE key='leviathan.prd_execution_mode';
   ```
   Plus re-activate the in-Odoo cron via the UI (`data/cron.xml` records
   are shipped with `active=False`).

3. **Disable the queue entirely** — emergency only. Workers idle, no
   new claims, existing in-flight jobs continue but new jobs get stuck
   in `state='generating' AND started_processing_at IS NULL` until you
   flip the flag back.
   ```sql
   UPDATE ir_config_parameter SET value='False'
   WHERE key='leviathan.prd_queue_enabled';
   ```

---

## 9. Observability & runbook hooks

### 9.1 Where logs land

| Source | Goes to | View via |
|---|---|---|
| Worker pod stdout | kubelet → CloudWatch (FluentBit) | `kubectl logs -l app=leviathan-prd-worker` |
| Backend Odoo stdout | Existing Odoo logging | Existing dashboard |
| Lambda extraction | CloudWatch `/aws/lambda/leviathan-extraction-lambda` | CloudWatch console |
| Per-job log table (`leviathan_job_log`) | Postgres, fed by buffered handler (F-HIGH-1) | Odoo UI Logs tab on each job form |

### 9.2 Log handler drop reports

Watch for `[leviathan-log-handler] dropped N log lines` in worker
stdout. >100/min sustained means `leviathan_job_log` is contended or
the buffer (`LEVIATHAN_LOG_BUFFER_MAX=5000`) is too small for the
log rate. Tune up; or investigate the lock contention.

### 9.3 Useful queries

```sql
-- Stuck jobs by state, oldest first
SELECT id, name, state, started_processing_at, last_heartbeat,
       prd_claim_count, prd_failure_count
FROM leviathan_job
WHERE state IN ('extracting','generating','scoring','qc_running')
  AND COALESCE(started_processing_at, create_date) <
      now() - interval '15 minutes'
ORDER BY started_processing_at NULLS FIRST;

-- Per-day throughput + cost proxy
SELECT date(completed_at) AS day,
       count(*) FILTER (WHERE state='done') AS done,
       count(*) FILTER (WHERE state='failed') AS failed,
       avg(score) AS avg_score,
       count(*) FILTER (WHERE prd_failure_count > 0) AS jobs_with_retries
FROM leviathan_job
WHERE completed_at > now() - interval '30 days'
GROUP BY 1 ORDER BY 1 DESC;
```

---

## 10. Production hardening (post-stage)

These are not blockers for stage flag-ON but are required before
flagging the worker pipeline on for **prod** traffic:

- [ ] **Kustomize overlays** for stage/prod under `deploy/overlays/`,
      eliminating the validate.sh placeholders entirely.
- [ ] **Separate cron token** (F-MED-4) — different secret for K8s
      CronJobs vs Lambda callbacks. Rotation runbook.
- [ ] **Pin curl image to digest** in `cronjobs.yaml` (F-MED-7).
- [ ] **Tighten egress CIDR** in `network-policy.yaml` to RDS subnet
      (F-LOW-9).
- [ ] **topologySpreadConstraints + podAntiAffinity** on the worker
      Deployment for HA across AZs (F-LOW-7).
- [ ] **PriorityClass** for the worker (don't get evicted under node
      pressure mid-PRD).
- [ ] **Log retention cron** (F-MED-2) — ship the
      `_cron_gc_old_logs` cleanup so `leviathan_job_log` doesn't grow
      unboundedly.

---

## 11. Quick-reference — pod build + push + apply (stage)

```bash
# from repo root
export GITSHA=$(git rev-parse --short HEAD)
export ECR=426628337772.dkr.ecr.ap-south-1.amazonaws.com
export TAG="v19.0.6.0.0-$GITSHA"

# Auth
aws ecr get-login-password --region ap-south-1 \
  | docker login --username AWS --password-stdin "$ECR"

# Build worker (amd64)
docker buildx build --platform linux/amd64 \
  -f custom_addons/leviathan/Dockerfile.worker \
  -t "$ECR/leviathan-prd-worker:$TAG" \
  --push .

# Build + deploy lambda
cd custom_addons/leviathan/leviathan-extraction-lambda
sam build --use-container
sam deploy --config-env stage \
  --parameter-overrides "WebhookUrl=https://stage.../webhook/extraction-complete \
                         WebhookTokenParameter=/ethara/leviathan/stage/webhook-token"
cd -

# Patch + validate manifests
sed -i.bak \
  "s|leviathan-prd-worker:CHANGE-ME-PIN-TAG-OR-DIGEST|leviathan-prd-worker:$TAG|" \
  custom_addons/leviathan/deploy/worker-deployment.yaml
rm custom_addons/leviathan/deploy/worker-deployment.yaml.bak
bash custom_addons/leviathan/deploy/validate.sh

# Apply
kubectl create namespace leviathan || true
kubectl apply -f custom_addons/leviathan/deploy/rbac.yaml
kubectl apply -f custom_addons/leviathan/deploy/worker-deployment.yaml
kubectl apply -f custom_addons/leviathan/deploy/network-policy.yaml
kubectl apply -f custom_addons/leviathan/deploy/cronjobs.yaml

# Watch rollout
kubectl -n leviathan rollout status deploy/leviathan-prd-worker --timeout=5m
kubectl -n leviathan get pods,deploy,cronjob

# Tail logs
kubectl -n leviathan logs -f -l app=leviathan-prd-worker --tail=100
```
