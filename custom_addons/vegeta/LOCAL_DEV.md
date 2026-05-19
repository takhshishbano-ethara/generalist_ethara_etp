# Vegeta — Local Development

Run the extraction Lambda locally against a real Odoo instance, without deploying to AWS. The same `extraction_service.py` code path works for both modes — local invocation is selected purely by an Odoo system parameter.

## Architecture

```
+--------------+   httpx.post()   +---------------------+   POST callback   +--------------+
| Odoo worker  | ---------------> | vegeta-lambda (RIE) | ----------------> | Odoo webhook |
| (your host)  |   port 9000      |  Playwright+Chromium|  host.docker.     |  /api/v1/    |
+--------------+                  +---------------------+  internal:8069    +--------------+
                                            |
                                            v
                                      +-----------+        bind mount
                                      |   minio   | <----> local/artifacts/
                                      +-----------+        (host filesystem)
```

Selection between local and prod is controlled by **Settings -> Vegeta -> Lambda Local URL**:
- **empty** -> production path: `boto3 lambda:Invoke` against AWS
- **set** -> local path: `httpx.post()` directly to the RIE endpoint

## Prerequisites

- Docker 24+ with Compose v2.17+ (for `additional_contexts`)
- The vegeta source checked out at `/Users/apple/Documents/etp/vegeta/` (parallel to `ethara-etp/`)
- Odoo running on the host at port 8069 with the vegeta addon installed

## One-time setup

```bash
cd custom_addons/vegeta/local
cp .env.example .env
```

Edit `.env` if your vegeta source lives elsewhere (`VEGETA_LAMBDA_PATH`).

## Run

```bash
cd custom_addons/vegeta/local
docker compose up --build
```

First build pulls the Playwright image (~2 GB) and downloads the AWS Lambda RIE binary. Subsequent runs are fast.

Both services start by default:
- `vegeta-lambda` at `http://localhost:9000/2015-03-31/functions/function/invocations`
- `minio` at `http://localhost:9001` (S3 API) and `http://localhost:9090` (console, login with credentials from `.env`)

The lambda is pre-wired to MinIO via `AWS_ENDPOINT_URL=http://minio:9000` — no further env setup needed.

## Artifacts on the host filesystem

MinIO's data dir is bind-mounted to `local/artifacts/`. Anything the lambda uploads to S3 appears immediately on your host:

```
custom_addons/vegeta/local/artifacts/
└── vegeta-local/                       <- bucket name (from S3_BUCKET in .env)
    └── leviathan/                      <- prefix hardcoded in lambda
        └── <job_id>/
            ├── screenshots/
            ├── assets/
            └── raw_data/
                ├── site_discovery.json
                ├── api_signals.json
                ├── business_signals.json
                └── prd_prompt.txt
```

The bucket directory (`vegeta-local/`) is created automatically on first PUT — no console setup required.

## Switching to real AWS S3

Edit `local/.env`:
```
AWS_ENDPOINT_URL=
AWS_ACCESS_KEY_ID=AKIA...
AWS_SECRET_ACCESS_KEY=...
S3_BUCKET=<your-bucket>
S3_REGION=<your-region>
```
Then `docker compose up`. The lambda now writes to real S3; MinIO still runs but is unused.

## Configure Odoo to use the local lambda

1. **Settings -> Vegeta -> Lambda Local URL**:
   ```
   http://localhost:9000/2015-03-31/functions/function/invocations
   ```
2. **Lambda Function Name** can stay empty — local URL bypasses the function-name check.
3. **AWS Credentials** are ignored when Lambda Local URL is set (the container reads its own env).
4. Set the env var **on the Odoo process** (so the webhook handler accepts the lambda's callback):
   ```bash
   export LEVIATHAN_WEBHOOK_TOKEN=devsecret
   python src/odoo-bin --addons-path=src/addons,custom_addons -d ethara_local
   ```
   This must match `LEVIATHAN_WEBHOOK_TOKEN` in `local/.env`.

## Callback URL — host.docker.internal

The lambda container POSTs back to Odoo using the `callback_url` field that Odoo sends in the request payload. Odoo's `_get_webhook_url()` reads the `web.base.url` system parameter — on a local dev box this is typically `http://localhost:8069`.

For the container to reach the host, set:

```
ICP web.base.url = http://host.docker.internal:8069
```

(Settings -> Technical -> Parameters -> System Parameters, or via shell.) The compose file already maps `host.docker.internal` to the host gateway on Linux; on Mac/Windows it works natively.

## Verify

```bash
# Lambda is up
curl http://localhost:9000/2015-03-31/functions/function/invocations \
  -d '{"url": "https://example.com", "job_id": 1, "callback_url": "http://host.docker.internal:8069/api/v1/vegeta/webhook/extraction-complete"}'
```

Container logs show extraction phases; Odoo log shows the inbound webhook. Job transitions extracting -> generating in the UI.

## Switch back to production

Clear **Settings -> Vegeta -> Lambda Local URL**. Set the AWS Lambda function name and credentials (or rely on IRSA). The same code path handles both — no redeploy of the addon needed.

## Stop / clean up

```bash
cd custom_addons/vegeta/local
docker compose down                  # stop containers
rm -rf artifacts/                    # wipe host-side S3 data
docker rmi vegeta-extraction:local   # remove the built image
```
