# Gohan Local K8s (Minikube)

Local Kubernetes development environment for the Gohan Odoo addon. Runs Odoo + Postgres + MinIO inside Minikube.

> **NOTE — Local rig uses prod Lambda.** Gohan has no in-cluster Lambda emulation. Extraction is dispatched to the **real production Lambda** in AWS, and the result is delivered back via a **cloudflared tunnel** to your local Odoo. After every `./setup.sh up` *and* every `./setup.sh start`, you must manually re-enter the 5 production values in **Settings → Gohan** — auto-configure writes placeholders only and the `start` subcommand re-runs auto-configure, clobbering any UI overrides.
>
> The 5 prod values to re-enter every time:
> 1. Lambda Function Name
> 2. Lambda Region
> 3. Extraction Access Key Id
> 4. Extraction Secret Access Key
> 5. Webhook Token *(must match the value baked into the prod Lambda's environment)*

---

## Architecture

| Component | Purpose | Image | Exposed |
|---|---|---|---|
| `odoo` | Odoo server, gohan addon UI, in-process PRD ThreadPoolExecutor | `gohan-prd-worker:latest` | NodePort 30069 |
| `postgres` | Odoo DB | `postgres:16-alpine` | internal only |
| `minio` | S3-compatible storage for extraction artifacts | `minio/minio:latest` | NodePort 30901 (console) |

Gohan has **no worker daemon** and **no autoscaler** — PRD generation runs in Odoo's own thread pool (`batch_concurrency` ICP, default 250).

---

## Prerequisites

```bash
brew install minikube kubectl docker cloudflared
brew install --cask docker
```

Docker Desktop must be running before you start. `cloudflared` is required to expose the local Odoo to the prod Lambda for callbacks.

---

## Quick Start

1. `cd custom_addons/gohan/local`
2. `chmod +x setup.sh`
3. `./setup.sh up` — takes 8-12 min on first run (image build included)
4. Get the Odoo URL: `minikube service odoo -n gohan -p gohan-local --url`
5. Log in as `admin` / `admin`
6. **Re-enter the 5 prod Gohan values** (see top of this README) in **Settings → Gohan**
7. Start a cloudflared tunnel (two terminals — see [Cloudflared Tunnel](#cloudflared-tunnel) below)
8. Configure the Lambda callback URL to `<cloudflared-url>/api/v1/gohan/webhook/extraction-complete` on the prod Lambda side
9. Navigate to **Gohan** menu, create an extraction job, click **Run**

---

## Commands

| Command | Description |
|---|---|
| `./setup.sh up` | Full from-scratch: minikube + build image + deploy stack + auto-configure Odoo |
| `./setup.sh start` | Restart a stopped cluster (skips image rebuild, re-applies manifests, re-runs auto-configure) |
| `./setup.sh down` | `minikube delete -p gohan-local` — destroys everything |
| `./setup.sh status` | `kubectl get pods,svc,jobs -n gohan` |
| `./setup.sh logs [odoo\|postgres]` | Tail logs (default: `odoo`) |
| `./setup.sh build` | Rebuild `gohan-prd-worker:latest` |
| `./setup.sh deploy` | Rebuild image + rollout restart odoo + upgrade gohan module |
| `./setup.sh shell` | Open Odoo Python shell (`odoo-bin shell`) |

---

## Environment Variables (override on `up`)

| Env var | Default | Purpose |
|---|---|---|
| `PG_PASSWORD` | `odoo_local_pw` | Postgres password |
| `AWS_ACCESS_KEY_ID` | `minioadmin` | MinIO root user (also written to ICP as placeholder) |
| `AWS_SECRET_ACCESS_KEY` | `minioadmin` | MinIO root password (also written to ICP as placeholder) |
| `WEBHOOK_TOKEN` | `devsecret` | Webhook token placeholder for ICP. Must be overridden in UI to match prod Lambda. |
| `BEDROCK_ACCESS_KEY_ID` | *(unset)* | Optional: real AWS Bedrock access key, surfaced into the Secret |
| `BEDROCK_SECRET_ACCESS_KEY` | *(unset)* | Optional: real AWS Bedrock secret key |

---

## Auto-Configured ICP Params

After `./setup.sh up`, these appear in **Settings → Technical → System Parameters** (gohan-specific keys are placeholders — **override in Settings → Gohan**):

| Key | Value | Override? |
|---|---|---|
| `web.base.url` | `http://<minikube-ip>:30069` | Optional — use cloudflared URL if you want absolute links to match the tunnel |
| `web.base.url.freeze` | `True` | No |
| `gohan.lambda_function_name` | `gohan-extractor` | **Yes — set to prod function name** |
| `gohan.lambda_region` | `us-east-1` | **Yes — set to prod region** |
| `gohan.lambda_local_url` | *(empty)* | No — empty triggers prod boto3 path |
| `gohan.extraction_access_key_id` | `minioadmin` | **Yes — set to prod IAM access key** |
| `gohan.extraction_secret_access_key` | `minioadmin` | **Yes — set to prod IAM secret key** |
| `gohan.webhook_token` | `devsecret` | **Yes — must match prod Lambda env** |
| `gohan.lambda_api_url` | *(empty)* | No — only set if using API Gateway HMAC path |
| `gohan.hmac_secret` | *(empty)* | No — only set if using API Gateway HMAC path |

---

## Manual UI Fallback

If auto-configuration failed entirely:

1. **Find your Minikube IP:** `minikube ip -p gohan-local` (e.g. `192.168.49.2`)
2. **Log in** as `admin` / `admin`
3. Go to **Settings → Technical → System Parameters → New** and create each row from the table above with prod values (not placeholders)
4. **Restart Odoo** to pick up changes:

```bash
kubectl rollout restart -n gohan deploy/odoo --context=gohan-local
```

---

## Cloudflared Tunnel

The prod Lambda needs a public URL to POST callbacks to. Open two terminals:

```bash
# Terminal A — forward odoo Service to localhost
kubectl port-forward svc/odoo -n gohan 8069:8069 --context=gohan-local

# Terminal B — expose localhost:8069 publicly
cloudflared tunnel --url http://localhost:8069
```

Cloudflared prints a `https://<random>.trycloudflare.com` URL. Configure the prod Lambda to POST callbacks to:

```
https://<random>.trycloudflare.com/api/v1/gohan/webhook/extraction-complete
```

Header for HMAC auth (either is accepted by gohan controllers):

```
X-Gohan-Token: <gohan.webhook_token value>
X-Leviathan-Token: <gohan.webhook_token value>
```

---

## How Extraction Works

```
[Odoo UI] --create job--> [gohan ThreadPoolExecutor in Odoo pod]
                              |
                              v  (boto3 lambda.invoke, async)
                          [PROD AWS Lambda]
                              |   (Playwright extraction)
                              v
                          [PROD S3 bucket]  (artifacts)
                              |
                              v  (HTTP POST callback)
                          [cloudflared tunnel] -> [Odoo /api/v1/gohan/webhook/extraction-complete]
                              |
                              v
                          job status updated, results visible in UI
```

PRD generation runs in-process inside Odoo via `concurrent.futures.ThreadPoolExecutor`. Default concurrency = 250 (set via `gohan.batch_concurrency` ICP). No autoscaler, no worker pods.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Callback never arrives | cloudflared tunnel down, or wrong Webhook URL configured on prod Lambda | Re-check both terminals are running; verify the URL ends in `/api/v1/gohan/webhook/extraction-complete` |
| `403 Forbidden` on callback | `gohan.webhook_token` mismatch between Odoo and prod Lambda env | Re-enter token in Settings → Gohan; restart Odoo |
| Lambda `403` / `AccessDenied` on `lambda:InvokeFunction` | Wrong extraction creds in ICP | Re-enter `gohan.extraction_access_key_id` / `gohan.extraction_secret_access_key` in Settings → Gohan |
| 5 ICP overrides keep resetting | `./setup.sh start` re-runs `auto_configure_odoo` | Re-enter the 5 prod values after every `start`. (This is by design — see top note.) |
| `ImagePullBackOff` for `gohan-prd-worker` | Image built outside Minikube's docker daemon | Always run `eval $(minikube -p gohan-local docker-env)` before `docker build`. `./setup.sh build` handles this automatically. |
| Odoo pod OOMKilled during heavy PRD batch | ThreadPoolExecutor at high concurrency on 4Gi limit | Lower `gohan.batch_concurrency` ICP, or bump `resources.limits.memory` in `manifests/odoo.yaml` |

---

## Cleanup

```bash
./setup.sh down

kubectl delete namespace gohan --context=gohan-local
```
