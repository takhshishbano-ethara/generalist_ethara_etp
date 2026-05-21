# Vegeta — PRD Generation on Kubernetes (DevOps Handoff)

**Purpose:** explain how the Vegeta addon runs PRD generation as Kubernetes pods, and
list everything the DevOps team must set up on EKS for it to work.

Addon version: `19.0.2.4.0`

---

## 1. What changed and why

Vegeta processes website-analysis tasks through a pipeline:
`Draft → Extracting → Generating PRD → Scoring → Done`.

Previously the **Generating PRD** step ran inside the Odoo server process as a
background thread. When an Odoo worker/pod was recycled (deploy, OOM, routine
recycle), that thread died mid-work — the task hung and was failed 45 minutes
later with a useless *"Watchdog: generating timed out"* message.

**New design:** each PRD-generation task now runs in its **own dedicated
Kubernetes Job/pod**, independent of the Odoo process. This is the same pattern
the `aurora` addon already uses on this cluster.

---

## 2. How it works (flow)

1. A task reaches the `generating` state in Odoo.
2. An Odoo cron — **"Vegeta: PRD Dispatch"** (every 1 min) — creates **one
   Kubernetes Job per task** in the `vegeta` namespace.
   Job name: `vegeta-prd-<task-id>-<uuid>`.
3. The Job's pod runs the worker image: boots a headless Odoo, generates the PRD
   (calls AWS Bedrock + S3), and writes results **directly into the same
   PostgreSQL database** the Odoo backend uses.
4. An Odoo cron — **"Vegeta: PRD Reconcile"** (every 1 min) — checks each K8s
   Job's status; if a pod crashed or vanished it marks the task `failed` with
   the real reason within ~1–2 min.
5. Pods auto-delete 10 min after finishing.

The Odoo backend creates these Jobs via the in-cluster Kubernetes API — so it
needs RBAC permission (see §4.2).

---

## 3. Worker pod specification

Each task = one pod. Spec (defined in the addon code):

| Property | Value |
|---|---|
| Namespace | `vegeta` |
| ServiceAccount | `vegeta-worker` |
| Image | `<ECR>/vegeta-prd-worker:latest` (`imagePullPolicy: Always`) |
| Node selector | `ethara.ai/node-pool=general-purpose` |
| CPU request | `1` (no CPU limit) |
| Memory request / limit | `2Gi` / `4Gi` |
| Security | non-root, `runAsUser: 1000` |
| Job `backoffLimit` | `0` (no pod-level retry) |
| Job `activeDeadlineSeconds` | `3600` (hard kill after 1 h) |
| Job `ttlSecondsAfterFinished` | `600` (auto-delete 10 min after finish) |
| Pod annotation | `karpenter.sh/do-not-disrupt: "true"` |
| Per-job extras | one Secret + one ConfigMap (auto-deleted with the Job) |
| Typical runtime | ~13 min (up to ~20 min) |

---

## 4. DevOps requirements checklist

### 4.1 Worker container image (ECR)

- Build `custom_addons/vegeta/Dockerfile.worker` —
  **build context = repo root** (the image bundles `src/`,
  `custom_addons/vegeta/`, and its dependency addon `etp_user_roles`).
- Push to an ECR repo named `vegeta-prd-worker`.
- The addon's built-in default image is
  `426628337772.dkr.ecr.ap-south-1.amazonaws.com/vegeta-prd-worker:latest`.
  If you push there, no Odoo config is needed; otherwise set the
  `vegeta.worker_docker_image` system parameter.
- ⚠️ **The image bundles the addon source code.** It MUST be rebuilt and
  re-pushed every time the vegeta addon changes, in lockstep with the Odoo
  backend deploy. A stale worker image = pods running old code = silent bugs.
  **Please set up a CI job for this.**

### 4.2 Kubernetes RBAC

- Apply `custom_addons/vegeta/deploy/vegeta-prd-rbac.yaml`. It creates the
  `vegeta` namespace, the `vegeta-worker` ServiceAccount, and a Role +
  RoleBinding allowing creation of Jobs/Secrets/ConfigMaps in the `vegeta`
  namespace.
- ⚠️ The file has `TODO(devops)` placeholders — fill in **the Odoo backend's
  ServiceAccount name and namespace** (the RoleBinding subject) before applying.
- The Odoo backend pods must run under that ServiceAccount, so Odoo can call the
  Kubernetes API.

### 4.3 Nodes / Karpenter

- The Karpenter NodePool must be able to provision nodes **labeled
  `ethara.ai/node-pool=general-purpose`** — otherwise every worker pod stays
  `Pending` forever.
- Sizing: ~400 tasks/day, peak ~10–15 concurrent pods (~15 vCPU / 30 GiB).
  Each pod = 1 vCPU + 2 GiB.
- The dispatch cron has **no built-in concurrency cap** — a large burst creates
  many pods at once. Recommend setting a Karpenter NodePool `limits` (CPU
  ceiling) as a cost/capacity guardrail.
- `karpenter.sh/do-not-disrupt: "true"` is set on pods so Karpenter will not
  evict a PRD pod mid-run.

### 4.4 IAM / AWS access

- The EKS **node IAM role** needs ECR pull permission
  (`AmazonEC2ContainerRegistryReadOnly`).
- The **worker pod** needs AWS access for Bedrock + S3. Recommended: **IRSA** —
  attach an IAM role to the `vegeta-worker` ServiceAccount with
  `bedrock:InvokeModel` / `bedrock:Converse` and `s3:GetObject` /
  `s3:PutObject` on the bucket. (Alternative: AWS keys via Odoo system
  parameters, passed through the per-job Secret.)
- ℹ️ **Bedrock and S3 configuration (model ARN, region, credentials, bucket
  name) is set in the Odoo UI** — Settings → Vegeta → system parameters —
  **not by DevOps.** The worker pod reads it from the shared database
  automatically. DevOps only needs to ensure the pod has **network egress** to
  Bedrock and S3 (see §4.5). If using IRSA, leave the credential fields blank
  in the UI and the pod's IAM role is used instead.

### 4.5 Networking

- Worker pods must reach: the **PostgreSQL database** (RDS), **AWS Bedrock**
  (`bedrock-runtime.<region>.amazonaws.com`), and **S3**.
- The RDS security group must allow connections from the worker pods.
- The worker connects to the **same database** as the Odoo backend — that
  shared DB is how results flow back to the UI.

### 4.6 Odoo backend

- Deploy the updated vegeta addon (version `19.0.2.4.0`) and run `-u vegeta` on
  the production DB — this applies the DB migration and creates the two new
  crons (*Vegeta: PRD Dispatch*, *Vegeta: PRD Reconcile*).
- Ensure Odoo runs at least one cron worker (`max_cron_threads >= 1`).
- Do **not** set the `VEGETA_LOCAL_MODE` env var in production (it is a
  local-testing flag only).

### 4.7 System parameters

- No new system parameters are required — defaults handle K8s mode
  (`vegeta.prd_execution_mode=auto` auto-detects the in-cluster environment;
  namespace defaults to `vegeta`; image falls back to the hardcoded ECR
  default).
- Existing parameters (`vegeta.bedrock_*`, `vegeta.s3_*`,
  `vegeta.webhook_token`) are already set in the production DB.

---

## 5. Files in the `deploy/` folder

| File | For production? | Action |
|---|---|---|
| `vegeta-prd-rbac.yaml` | ✅ **Required** | Fill the `TODO(devops)` placeholders, then `kubectl apply`. |
| `DEVOPS-HANDOFF.md` | 📖 This document | Reference only — read it, don't deploy it. |
| `vegeta-kueue-localqueue.yaml` | ⚪ Optional | Apply **only if** you adopt Kueue (see §4.3). Despite the filename, `LocalQueue` is a real Kueue production resource — not a local-testing file. |
| `README.md` | 🧪 Local testing only | The minikube local-testing guide for developer laptops. **Ignore for production** — its instructions are minikube-specific. |
| `minikube-local-test.sh` | 🧪 Local testing only | Script that builds/runs the pipeline on **minikube**. **Ignore for production** — never run it against the cluster. |

**For an EKS production deploy, the only file you act on is `vegeta-prd-rbac.yaml`**
(plus `vegeta-kueue-localqueue.yaml` *if* you use Kueue). **`README.md` and
`minikube-local-test.sh` exist purely for local minikube testing — ignore them.**

---

## 6. Verification after deployment

1. Run a Vegeta task. Within ~1 min a Job `vegeta-prd-<id>-<uuid>` should appear
   in the `vegeta` namespace (`kubectl get jobs -n vegeta`).
2. The pod should run; the task should move `generating → scoring → done`.
3. **Failure test:** kill a pod mid-run
   (`kubectl delete pod -n vegeta <pod>`). The *Vegeta: PRD Reconcile* cron
   should mark the task `failed` with a real reason within ~2 min.

---

## 7. Notes

- Extraction (an earlier pipeline stage) runs on AWS Lambda and is unchanged —
  this document covers only PRD generation.
- This addon mirrors the `aurora` addon's existing K8s-Job pattern; the cluster
  setup is very similar.
- For local testing on minikube, see `deploy/README.md` and
  `deploy/minikube-local-test.sh` (not used in production).
