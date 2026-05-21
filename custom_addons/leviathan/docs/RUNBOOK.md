# Leviathan 19.0.2.0.0 Deployment Runbook

> **Audience:** DevOps / SRE executing the upgrade from 19.0.1.x to 19.0.2.0.0.
> **Estimated duration:** 45 min hands-on + AWS quota approval (hours, can run in parallel).
> **Downtime:** None if executed in order. ~30 s of degraded latency during pod roll.
> **Companion docs:** [EKS_DEPLOYMENT.md](./EKS_DEPLOYMENT.md) (architecture deep-dive), `PRODUCTION_FIXES_AND_DEVOPS.md §9` (change list).

---

## What you're shipping

| What | Before | After |
|---|---|---|
| Lambda invocation | Synchronous HTTP via Function URL (blocking) | Async `lambda:Invoke(Event)` via boto3 (returns <1 s) |
| Batch orchestration | RabbitMQ + `consumer.py` pod | In-Odoo `ThreadPoolExecutor(250)` |
| Lambda concurrency | 10 | **250** |
| External deps required | RabbitMQ broker + consumer pod | None (Lambda + S3 + Bedrock only) |

**Backward compatibility:** the new Lambda image still accepts Function URL POSTs, so it can be deployed BEFORE Odoo without downtime.

---

## Environment reference (fill in if blank)

| Var | Value |
|---|---|
| `<ACCOUNT_ID>` | _your AWS account ID_ |
| `<REGION>` | `ap-south-1` |
| `<CLUSTER>` | `ethara-production` |
| `<NAMESPACE>` | `ethara` |
| `<DEPLOYMENT>` | `etp-be` |
| `<LAMBDA_FN>` | `leviathan-extraction` |
| `<ECR_REPO>` | `<ACCOUNT_ID>.dkr.ecr.<REGION>.amazonaws.com/leviathan-extraction` |
| `<DB_NAME>` | _your Odoo prod database name_ |
| `<S3_BUCKET>` | `production-grtlabs-tag` |
| `<NEW_TAG>` | `19.0.2.0.0` |

---

## Pre-flight checklist (do these in parallel, all asynchronous)

### PF-1. Verify AWS Lambda regional quota allows 250+ concurrent

```bash
aws service-quotas get-service-quota \
  --service-code lambda \
  --quota-code L-B99A9384 \
  --region <REGION> \
  --query 'Quota.Value' --output text
```

| Expected | Action |
|---|---|
| `1000.0` (default) | OK, proceed |
| `< 250.0` | Raise it — can take **24–48 h**: |

```bash
aws service-quotas request-service-quota-increase \
  --service-code lambda \
  --quota-code L-B99A9384 \
  --desired-value 1000 \
  --region <REGION>
```

### PF-2. Confirm `LEVIATHAN_WEBHOOK_TOKEN` matches between Lambda and Odoo

```bash
aws lambda get-function-configuration --region <REGION> \
  --function-name <LAMBDA_FN> \
  --query 'Environment.Variables.LEVIATHAN_WEBHOOK_TOKEN' --output text
```

Compare to Odoo pod env:

```bash
kubectl -n <NAMESPACE> exec deploy/<DEPLOYMENT> -- \
  printenv LEVIATHAN_WEBHOOK_TOKEN
```

**Must be byte-identical.** If they don't match, webhooks land with 401 and jobs hang in `extracting` until the watchdog kills them.

### PF-3. Inventory the current RabbitMQ consumer pod (for teardown later)

```bash
kubectl -n <NAMESPACE> get deployment -l app=etp-leviathan-consumer
kubectl -n <NAMESPACE> get configmap | grep -i leviathan
kubectl -n <NAMESPACE> get secret | grep -i leviathan
```

Note the names of anything found. You'll delete them in step 6.

### PF-4. Tag the rollback point

```bash
cd ethara-etp
git tag pre-leviathan-19.0.2.0.0 origin/stage
git push origin pre-leviathan-19.0.2.0.0

# Note current Lambda image digest for fast rollback
aws lambda get-function --region <REGION> --function-name <LAMBDA_FN> \
  --query 'Code.ImageUri' --output text > /tmp/lambda-prev-image-uri.txt
cat /tmp/lambda-prev-image-uri.txt
```

---

## Deployment sequence

> **Steps 1, 2 are AWS-only and zero-risk** (the existing Odoo keeps working with the old Lambda or the new one).
> **Steps 3, 4, 5 are the actual cutover.**
> **Step 6 is cleanup.**
> **Step 7 is verification.**

### Step 1 — Build & push the new Lambda image

**What:** Build the v19.0.2.0.0 Lambda image and push to ECR.
**Why:** New handler accepts both old (Function URL) and new (direct async) invocation modes — safe to deploy first.

```bash
cd leviathan-extraction-lambda

# Build
docker build -t leviathan-extraction:<NEW_TAG> .

# Tag for ECR
docker tag leviathan-extraction:<NEW_TAG> <ECR_REPO>:<NEW_TAG>
docker tag leviathan-extraction:<NEW_TAG> <ECR_REPO>:latest

# Login + push
aws ecr get-login-password --region <REGION> | \
  docker login --username AWS --password-stdin <ACCOUNT_ID>.dkr.ecr.<REGION>.amazonaws.com
docker push <ECR_REPO>:<NEW_TAG>
docker push <ECR_REPO>:latest
```

**Verify:**

```bash
aws ecr describe-images --region <REGION> \
  --repository-name leviathan-extraction \
  --image-ids imageTag=<NEW_TAG> \
  --query 'imageDetails[0].imageDigest' --output text
# Should print a sha256:... digest
```

**Rollback:** N/A (we haven't deployed it yet).

---

### Step 2 — Update Lambda to the new image, set concurrency 250

**What:** Point the live Lambda at the new image and bump reserved concurrency to 250.
**Why:** Old Odoo keeps working (backward compat); the moment Odoo upgrades, capacity is already in place.

```bash
# Update the function code
aws lambda update-function-code --region <REGION> \
  --function-name <LAMBDA_FN> \
  --image-uri <ECR_REPO>:<NEW_TAG>

# Wait for the update to finish
aws lambda wait function-updated --region <REGION> --function-name <LAMBDA_FN>

# Bump concurrency
aws lambda put-function-concurrency --region <REGION> \
  --function-name <LAMBDA_FN> \
  --reserved-concurrent-executions 250

# Keep /tmp at 4 GB (explicit; template.yaml ships this too)
aws lambda update-function-configuration --region <REGION> \
  --function-name <LAMBDA_FN> \
  --ephemeral-storage Size=4096
```

**Verify:**

```bash
aws lambda get-function-configuration --region <REGION> --function-name <LAMBDA_FN> \
  --query '{Image:Code.ImageUri,Storage:EphemeralStorage.Size,LastUpdate:LastUpdateStatus}'
# Expect:
#   Image:   .../leviathan-extraction:<NEW_TAG>
#   Storage: 4096
#   LastUpdate: Successful

aws lambda get-function-concurrency --region <REGION> --function-name <LAMBDA_FN>
# Expect: ReservedConcurrentExecutions: 250

# Sanity-check the health endpoint (Function URL still works)
curl -s "$(aws lambda get-function-url-config --region <REGION> --function-name <LAMBDA_FN> --query FunctionUrl --output text)health" \
  --aws-sigv4 "aws:amz:<REGION>:lambda" \
  --user "$(aws configure get aws_access_key_id):$(aws configure get aws_secret_access_key)"
# Expect: {"status":"ok","version":"2.1.0"}
```

**Rollback:**

```bash
aws lambda update-function-code --region <REGION> \
  --function-name <LAMBDA_FN> \
  --image-uri "$(cat /tmp/lambda-prev-image-uri.txt)"
aws lambda put-function-concurrency --region <REGION> \
  --function-name <LAMBDA_FN> --reserved-concurrent-executions 10
```

---

### Step 3 — Grant Odoo pod permission to invoke Lambda (IRSA — recommended)

**What:** Attach an IAM policy that allows `lambda:InvokeFunction` to the Odoo pod's service account.
**Why:** Before this, the Odoo pod cannot call `lambda:Invoke` — batch dispatch will fail with `AccessDeniedException`.

```bash
# 3a. Create policy
cat > /tmp/leviathan-odoo-policy.json <<EOF
{
  "Version": "2012-10-17",
  "Statement": [{
    "Sid": "InvokeExtractionLambda",
    "Effect": "Allow",
    "Action": ["lambda:InvokeFunction"],
    "Resource": "arn:aws:lambda:<REGION>:<ACCOUNT_ID>:function:<LAMBDA_FN>"
  }]
}
EOF

aws iam create-policy \
  --policy-name leviathan-odoo-invoke-policy \
  --policy-document file:///tmp/leviathan-odoo-policy.json \
  --query 'Policy.Arn' --output text
# Note the returned ARN for the next command.

# 3b. Bind via IRSA
eksctl create iamserviceaccount \
  --name <DEPLOYMENT> \
  --namespace <NAMESPACE> \
  --cluster <CLUSTER> \
  --attach-policy-arn arn:aws:iam::<ACCOUNT_ID>:policy/leviathan-odoo-invoke-policy \
  --approve \
  --override-existing-serviceaccounts \
  --region <REGION>
```

**Verify:**

```bash
# Restart the deployment so pods pick up the SA annotation
kubectl -n <NAMESPACE> rollout restart deployment <DEPLOYMENT>
kubectl -n <NAMESPACE> rollout status deployment <DEPLOYMENT>

# Confirm the pod gets the IRSA-issued credentials
kubectl -n <NAMESPACE> exec deploy/<DEPLOYMENT> -- env | grep AWS_ROLE_ARN
# Expect a non-empty role ARN ending in "leviathan-odoo-..."

# Confirm STS works from inside the pod
kubectl -n <NAMESPACE> exec deploy/<DEPLOYMENT> -- aws sts get-caller-identity
# .Arn should be the IRSA role, NOT the EC2 node role.
```

**Alternative (NOT preferred):** if IRSA is impossible in your environment, set explicit IAM keys via Odoo Settings:
**Settings → Leviathan → Extraction AWS Credentials**. The IAM user attached to those keys must have `lambda:InvokeFunction` on the Lambda ARN.

**Rollback:**

```bash
eksctl delete iamserviceaccount --name <DEPLOYMENT> --namespace <NAMESPACE> --cluster <CLUSTER> --region <REGION>
aws iam delete-policy --policy-arn arn:aws:iam::<ACCOUNT_ID>:policy/leviathan-odoo-invoke-policy
```

---

### Step 4 — Increase Odoo resource limits and DB connections

**What:** Raise `odoo.conf` memory + worker count, K8s pod memory, and Postgres `max_connections`.
**Why:** 250 concurrent webhook callbacks land back at Odoo simultaneously. Old config (4 workers / 2.3 GB / 64 conns) drops callbacks.

#### 4a. `odoo.conf`

Edit `/opt/ethara/app/odoo.conf` (via ConfigMap or image rebuild):

```ini
[options]
workers = 8
max_cron_threads = 2
db_maxconn = 300
limit_memory_soft = 4294967296
limit_memory_hard = 5368709120
limit_time_real = 600
limit_time_real_cron = 1800
limit_time_cpu = 300
proxy_mode = True
```

#### 4b. K8s pod resources

```bash
kubectl -n <NAMESPACE> edit deployment <DEPLOYMENT>
```

Ensure the Odoo container has:

```yaml
resources:
  requests:
    cpu: "2"
    memory: "4Gi"
  limits:
    cpu: "4"
    memory: "8Gi"
```

> **CRITICAL:** `limits.memory` MUST exceed `limit_memory_hard` (5 GiB), or K8s will OOM-kill the pod before Odoo can recycle the worker gracefully.

#### 4c. Postgres

```sql
-- via RDS parameter group (preferred) or directly on self-managed PG:
ALTER SYSTEM SET max_connections = 300;
-- Then reboot the DB.
```

Or for RDS:

```bash
aws rds modify-db-parameter-group --region <REGION> \
  --db-parameter-group-name <PG_NAME> \
  --parameters "ParameterName=max_connections,ParameterValue=300,ApplyMethod=pending-reboot"
# Reboot the DB instance during the next maintenance window.
```

**Verify:**

```bash
kubectl -n <NAMESPACE> rollout status deployment <DEPLOYMENT>
kubectl -n <NAMESPACE> exec deploy/<DEPLOYMENT> -- \
  grep -E '^(workers|db_maxconn|limit_memory)' /opt/ethara/app/odoo.conf
# Expect the new values.

# Postgres
psql -h <PG_HOST> -U <USER> -d <DB_NAME> -c "SHOW max_connections;"
# Expect 300
```

**Rollback:** revert ConfigMap / pod resource edits, rollout restart.

---

### Step 5 — Deploy the new Odoo image and run the addon upgrade

**What:** Roll the Odoo deployment to the v19.0.2.0.0 image, then upgrade the `leviathan` addon. The addon's pre-migration script auto-handles DB changes.
**Why:** This is the actual cutover from RabbitMQ to direct Lambda invocation.

```bash
# 5a. Roll the deployment to the new image
kubectl -n <NAMESPACE> set image deployment/<DEPLOYMENT> \
  odoo=<REGISTRY>/etp-be:<NEW_TAG>

kubectl -n <NAMESPACE> rollout status deployment <DEPLOYMENT> --timeout=10m
```

```bash
# 5b. Run the addon upgrade — triggers migrations/19.0.2.0.0/pre-migration.py
kubectl -n <NAMESPACE> exec deploy/<DEPLOYMENT> -- \
  python3 /opt/odoo/odoo-bin -c /opt/ethara/app/odoo.conf \
  -d <DB_NAME> -u leviathan --stop-after-init
```

```bash
# 5c. Roll the pods one more time so they pick up the post-upgrade state cleanly
kubectl -n <NAMESPACE> rollout restart deployment <DEPLOYMENT>
kubectl -n <NAMESPACE> rollout status deployment <DEPLOYMENT> --timeout=10m
```

```bash
# 5d. Set the ONE setting the migration cannot auto-populate
#    (the Function URL doesn't contain the function name)
#    Replace <FUNCTION_ARN> with the actual Lambda ARN.
kubectl -n <NAMESPACE> exec deploy/<DEPLOYMENT> -- \
  python3 /opt/odoo/odoo-bin shell -c /opt/ethara/app/odoo.conf -d <DB_NAME> <<'EOF'
self.env['ir.config_parameter'].sudo().set_param(
    'leviathan.lambda_function_name',
    'arn:aws:lambda:<REGION>:<ACCOUNT_ID>:function:<LAMBDA_FN>',
)
self.env.cr.commit()
print('Lambda function name set')
EOF
```

Or via UI: **Settings → Leviathan → Extraction Lambda → Lambda Function** → paste the ARN → Save.

**Verify:**

```bash
# 5e. Confirm migration ran
kubectl -n <NAMESPACE> exec deploy/<DEPLOYMENT> -- \
  psql -h <PG_HOST> -U <USER> -d <DB_NAME> -c \
  "SELECT column_name FROM information_schema.columns
     WHERE table_name='leviathan_job' AND column_name IN ('via_rabbitmq', 'via_batch');"
# Expect: only via_batch (NOT via_rabbitmq)

# 5f. Confirm the new config seeded
kubectl -n <NAMESPACE> exec deploy/<DEPLOYMENT> -- \
  psql -h <PG_HOST> -U <USER> -d <DB_NAME> -c \
  "SELECT key, value FROM ir_config_parameter WHERE key LIKE 'leviathan.%' ORDER BY key;"
# Expect rows for: batch_concurrency, lambda_function_name, lambda_region,
#                  watchdog_extracting_minutes, watchdog_generating_minutes

# 5g. Confirm the stale server action was cleaned up
kubectl -n <NAMESPACE> exec deploy/<DEPLOYMENT> -- \
  psql -h <PG_HOST> -U <USER> -d <DB_NAME> -c \
  "SELECT name FROM ir_model_data
    WHERE module='leviathan' AND name LIKE '%rabbitmq%';"
# Expect: 0 rows.
```

**Rollback:**

```bash
# Roll back image
kubectl -n <NAMESPACE> set image deployment/<DEPLOYMENT> odoo=<REGISTRY>/etp-be:<PREVIOUS_TAG>
kubectl -n <NAMESPACE> rollout status deployment <DEPLOYMENT>

# Restore via_rabbitmq column (one-way migration)
psql -h <PG_HOST> -U <USER> -d <DB_NAME> <<'EOF'
ALTER TABLE leviathan_job ADD COLUMN via_rabbitmq BOOLEAN DEFAULT FALSE;
UPDATE leviathan_job SET via_rabbitmq = via_batch;
ALTER TABLE leviathan_job DROP COLUMN via_batch;
EOF

# Re-deploy the consumer.py Deployment from your git history.
```

---

### Step 6 — Tear down the old RabbitMQ consumer

**What:** Delete the K8s Deployment that was running `consumer.py` (if any).
**Why:** It's now dead code; running it wastes resources. The RabbitMQ broker itself stays — other addons (berserker, talos, skoll, kensei) still use it.

```bash
# Use the names you inventoried in PF-3
kubectl -n <NAMESPACE> delete deployment etp-leviathan-consumer 2>/dev/null || echo "no consumer deployment"
kubectl -n <NAMESPACE> delete configmap leviathan-consumer-env 2>/dev/null || echo "no consumer configmap"
kubectl -n <NAMESPACE> delete secret leviathan-consumer-secrets 2>/dev/null || echo "no consumer secret"
```

**Verify:**

```bash
kubectl -n <NAMESPACE> get all -l app=etp-leviathan-consumer
# Expect: "No resources found"
```

**Rollback:** redeploy from your old K8s manifest — the broker is still running so it'll reconnect immediately.

---

### Step 7 — Smoke test

#### 7a. One-record dispatch

1. Open Odoo, log in as an admin user.
2. Go to **Leviathan → All Tasks**.
3. Click **New** and create a task with URL `https://example.com`.
4. Click **Run Pipeline**.
5. Within ~10 minutes the task should reach `done`.

While it's running:

```bash
# Follow the Lambda logs
aws logs tail /aws/lambda/<LAMBDA_FN> --region <REGION> --follow --since 5m
# Expect:
#   "Direct async invoke for job_id=N, url=https://example.com"
#   ... phases ...
#   "Callback to https://<your-odoo>/api/v1/leviathan/webhook/extraction-complete returned 200"
```

#### 7b. Batch dispatch (the actual 250-concurrent test)

1. **Leviathan → Configuration → Import URLs** → upload a CSV with 5 URLs.
2. **Leviathan → All Tasks** → filter "Not Assigned" → select all 5.
3. **Gear menu → Run Batch (Parallel)**.
4. Notification appears within 1 s: *"5 task(s) dispatching concurrently (max parallel: 250)."*
5. Watch the Lambda logs:

```bash
aws logs tail /aws/lambda/<LAMBDA_FN> --region <REGION> --follow --since 1m \
  | grep "Direct async invoke"
# Expect 5 different job_ids within seconds of each other.
```

6. Within ~10 minutes all 5 jobs should reach `done`.

#### 7c. Stress test (do this only after 7a, 7b pass)

Import 50–100 URLs, batch-run them, and watch:

```bash
# Lambda concurrency over time
aws cloudwatch get-metric-statistics --region <REGION> \
  --namespace AWS/Lambda \
  --metric-name ConcurrentExecutions \
  --dimensions Name=FunctionName,Value=<LAMBDA_FN> \
  --start-time $(date -u -v -1H '+%Y-%m-%dT%H:%M:%SZ') \
  --end-time   $(date -u            '+%Y-%m-%dT%H:%M:%SZ') \
  --period 60 --statistics Maximum

# Lambda throttles (should stay 0)
aws cloudwatch get-metric-statistics --region <REGION> \
  --namespace AWS/Lambda \
  --metric-name Throttles \
  --dimensions Name=FunctionName,Value=<LAMBDA_FN> \
  --start-time $(date -u -v -1H '+%Y-%m-%dT%H:%M:%SZ') \
  --end-time   $(date -u            '+%Y-%m-%dT%H:%M:%SZ') \
  --period 60 --statistics Sum

# Odoo pod memory
kubectl -n <NAMESPACE> top pod -l app=<DEPLOYMENT>

# Postgres connection count
psql -h <PG_HOST> -U <USER> -d <DB_NAME> -c \
  "SELECT count(*) FROM pg_stat_activity WHERE datname='<DB_NAME>';"
```

---

## Post-deployment watch list (first 24 h)

| Metric | Where | Healthy | If unhealthy |
|---|---|---|---|
| Lambda `ConcurrentExecutions` (peak) | CloudWatch | Spikes to ≤250 on batch, idle otherwise | Investigate batch size logic |
| Lambda `Throttles` | CloudWatch | 0 sustained | Bump account quota; reduce `leviathan.batch_concurrency` |
| Lambda `Errors` rate | CloudWatch | < 1% | Pull function logs for failing job_ids |
| Odoo pod memory | `kubectl top` | < `limits.memory` (8 GiB) | Bump K8s `limits.memory`, investigate thread leak |
| Postgres conn count | `pg_stat_activity` | < 250 sustained | Bump `max_connections`, consider PgBouncer |
| Jobs stuck in `extracting` | Odoo search | Drain in < 60 min | Check Lambda CloudWatch for that job_id |
| Odoo log: `Lambda invoke ClientError` | log aggregator | None sustained | Almost always IAM — re-check step 3 |
| Odoo log: `Watchdog: stuck in extracting` | log aggregator | Rare (< 1% of jobs) | Inspect that job's Lambda log |

---

## Common failures and fixes

| Symptom | Likely cause | Fix |
|---|---|---|
| Batch fails with `AccessDeniedException` | IRSA not bound to pod | Re-run step 3, restart deployment |
| Batch fails with `ResourceNotFoundException` | Wrong function name in settings | Set `leviathan.lambda_function_name` to full ARN |
| Batch fails with `TooManyRequestsException` | Selected count > reserved concurrency | Reduce batch size OR raise concurrency |
| Lambda logs show no "Direct async invoke" but show "POST /api/v1/extract" | Odoo is still using old code path | Confirm the addon upgrade ran (step 5b) |
| Lambdas finish but jobs stay in `extracting` | Webhook auth failing | Confirm `LEVIATHAN_WEBHOOK_TOKEN` matches (PF-2) |
| Lambdas finish but some webhooks lost | Odoo overwhelmed | Bump Odoo `workers`, K8s replicas, or DB conn pool (step 4) |
| OOM-killed Odoo pods | Pod `limits.memory` ≤ `limit_memory_hard` | Bump K8s pod `limits.memory` to ≥ 8 GiB |

---

## Full rollback procedure (if needed)

> Use only if multiple smoke tests fail and root cause is the upgrade itself.

```bash
# 1. Roll Odoo back to the pre-upgrade image
kubectl -n <NAMESPACE> set image deployment/<DEPLOYMENT> odoo=<REGISTRY>/etp-be:<PREVIOUS_TAG>
kubectl -n <NAMESPACE> rollout status deployment <DEPLOYMENT>

# 2. Restore via_rabbitmq column
psql -h <PG_HOST> -U <USER> -d <DB_NAME> <<EOF
BEGIN;
ALTER TABLE leviathan_job ADD COLUMN via_rabbitmq BOOLEAN DEFAULT FALSE;
UPDATE leviathan_job SET via_rabbitmq = via_batch;
ALTER TABLE leviathan_job DROP COLUMN via_batch;
COMMIT;
EOF

# 3. Re-deploy the consumer.py Deployment from git history
kubectl apply -f /path/to/old/etp-leviathan-consumer.yaml

# 4. Optional: roll Lambda back to the previous image
aws lambda update-function-code --region <REGION> --function-name <LAMBDA_FN> \
  --image-uri "$(cat /tmp/lambda-prev-image-uri.txt)"
aws lambda put-function-concurrency --region <REGION> --function-name <LAMBDA_FN> \
  --reserved-concurrent-executions 10

# 5. Run the addon downgrade (you may need to manually revert the manifest version first)
kubectl -n <NAMESPACE> exec deploy/<DEPLOYMENT> -- \
  python3 /opt/odoo/odoo-bin -c /opt/ethara/app/odoo.conf \
  -d <DB_NAME> -u leviathan --stop-after-init
```

---

## Sign-off checklist

Before declaring the deployment complete, confirm:

- [ ] PF-1: AWS Lambda regional quota ≥ 250
- [ ] PF-2: Webhook token matches between Lambda and Odoo
- [ ] PF-3: Old consumer deployment inventoried
- [ ] PF-4: Rollback tag pushed; previous Lambda image URI saved
- [ ] Step 1: New Lambda image pushed to ECR
- [ ] Step 2: Lambda using new image; concurrency = 250; /health returns version 2.1.0
- [ ] Step 3: IRSA role bound; pod can call `sts get-caller-identity` with the IRSA role
- [ ] Step 4: `odoo.conf` updated; K8s pod memory ≥ 8 GiB; Postgres `max_connections` ≥ 300
- [ ] Step 5: Odoo on new image; migration ran (no `via_rabbitmq` column); `leviathan.lambda_function_name` set
- [ ] Step 6: Old consumer deployment + configmap + secret deleted
- [ ] Step 7a: Single-record smoke test passed
- [ ] Step 7b: 5-record batch smoke test passed (all 5 reach `done`)
- [ ] Step 7c: Stress test passed (50+ records, no throttles, no OOM)
- [ ] Watch list metrics green for 24 h

Once all boxes are ticked, send the all-clear and update the runbook with any deviations encountered for next time.

---

**Last updated:** v19.0.2.0.0 deployment, 2026-05-14.
**Owner:** Backend / Platform team.
**Escalation:** if step 7 fails AND step 8 rollback also fails → page the on-call engineer; do NOT leave the system in a partially-migrated state overnight.
