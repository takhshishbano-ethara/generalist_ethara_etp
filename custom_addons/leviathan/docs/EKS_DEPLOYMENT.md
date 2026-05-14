# Leviathan EKS Deployment Guide (19.0.2.0.0+)

**Audience:** DevOps / SRE running Odoo on Amazon EKS.

**What changed in 19.0.2.0.0:** RabbitMQ is removed. Batch fan-out now happens
in-process inside the Odoo pod via `boto3 lambda:Invoke(InvocationType='Event')`
with a 250-wide `ThreadPoolExecutor`. Each invoke returns in <1 s; Lambdas run
asynchronously and post back to the existing webhook.

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
