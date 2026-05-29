# Leviathan — Local E2E (worker pod + extraction lambda + MinIO)

End-to-end developer rig for the new worker-pod architecture. Mirrors the
production pod topology: your **host Odoo** is the UI tier, the
**leviathan-worker** container is the worker tier, the
**leviathan-lambda** container stands in for AWS Lambda, and **MinIO**
stands in for S3.

```
   host                                   docker (leviathan-local network)
   ─────                                  ──────────────────────────────────
   Odoo (odoo-bin) :8069  ──webhook──┐    ┌──── leviathan-worker
   Postgres        :5432  ◄──drain───┼────┤      (run_prd.py)
                                     │    │
                                     │    ├──── leviathan-lambda (RIE)
                                     └────┤      :9000
                                          │
                                          └──── minio
                                                :9001 / :9090 (console)
```

The point of this rig is that the **worker pod boundary is exercised**.
A single-process `python odoo-bin` setup tests the in-process drainer
path; this compose stack tests the standalone worker process you ship
to production.

---

## Prerequisites

- Docker 24+ with Compose v2.17+ (needed for `additional_contexts`)
- Host Odoo 19 + Postgres running, with the `leviathan` addon installed
- Postgres on host accepting connections from `host.docker.internal`
  (default on macOS / Docker Desktop; on Linux the compose file already
  maps the gateway). Confirm `listen_addresses = '*'` and
  `pg_hba.conf` allows the docker bridge subnet.

## One-time setup

```bash
cd custom_addons/leviathan/local
cp .env.example .env
# Edit .env: at minimum set DB_PASSWORD to match your host Postgres.
```

Make sure the host Odoo process is launched with the **same**
`LEVIATHAN_WEBHOOK_TOKEN` you put in `.env`:

```bash
export LEVIATHAN_WEBHOOK_TOKEN=devsecret
python src/odoo-bin --addons-path=src/addons,custom_addons -d leviathan_local
```

## Bring up

```bash
cd custom_addons/leviathan/local
docker compose up --build         # first build pulls Playwright (~2GB)
```

Three containers start:
- `leviathan-lambda` at `http://localhost:9000/2015-03-31/functions/function/invocations`
- `minio` at `http://localhost:9001` (S3 API) and `http://localhost:9090` (console)
- `leviathan-worker` — claim loop log to stdout (`docker compose logs -f leviathan-worker`)

### One-time: pre-create the MinIO bucket

MinIO does **not** auto-create buckets on first PUT to a sub-path. The
lambda will upload extracted assets to a `leviathan/` prefix inside the
bucket named by `S3_BUCKET` (default `leviathan-local`), and the first
upload to a missing bucket logs `NoSuchBucket` warnings until the
bucket exists. Create it once:

```bash
# Browser: http://localhost:9090 → login (minio / miniosecret) → Create bucket
# OR via the MinIO client inside the container:
docker exec leviathan-minio mc alias set local http://localhost:9000 minio miniosecret
docker exec leviathan-minio mc mb local/leviathan-local 2>/dev/null || true
```

The **final PRD** upload goes through the worker's S3 service against
*real* AWS S3 (the bucket from `leviathan.s3_bucket`), so it is
unaffected by this MinIO quirk. Only the lambda's optional asset
uploads warn.

## Configure Odoo (one-time)

In Odoo, go to **Settings → Technical → Parameters → System Parameters**
and set:

| Key | Value |
|---|---|
| `web.base.url` | `http://host.docker.internal:8069` |
| `leviathan.prd_queue_enabled` | `True` |
| `leviathan.prd_execution_mode` | `worker` |
| `leviathan.lambda_local_url` | `http://localhost:9000/2015-03-31/functions/function/invocations` |
| `leviathan.s3_bucket` | `leviathan-local` |
| `leviathan.s3_endpoint_url` | `http://localhost:9000` *(only if your Odoo uses MinIO directly; otherwise leave blank)* |

`prd_execution_mode = worker` makes the in-Odoo `_cron_prd_queue_drainer`
a no-op so the standalone worker process is the only drainer running
cluster-wide. Leave it on `inprocess` (or unset) if you want the old
Odoo-cron-driven behaviour.

## E2E verification checklist

Run these in order. Each one is its own commit-blocker if it fails.

- [ ] **Migration clean** — `python src/odoo-bin -u leviathan -d
      leviathan_local --stop-after-init` exits 0, no ERROR in log.
- [ ] **Worker boots** — `docker compose logs leviathan-worker | grep
      "registry booted"` shows a line like
      `Odoo registry booted for db=leviathan_local` within ~30 s.
- [ ] **Smoke** — `docker compose run --rm leviathan-worker python
      /opt/odoo/custom_addons/leviathan/worker/run_prd.py --check`
      exits 0 (`--check OK ...`).
- [ ] **Single task (flag OFF, control)** — temporarily set
      `leviathan.prd_queue_enabled=False`, create 1 task, click **Run
      All**, watch it finish via the legacy in-process pool. Reverts
      to flag ON afterwards.
- [ ] **Single task (flag ON, worker mode)** — create 1 task, click
      **Run All**. Within `LEVIATHAN_WORKER_POLL_S` seconds (default
      3) the worker logs `claimed 1 job(s)` and the form's
      `pipeline_status` advances through "PRD worker assigned" →
      "Generating PRD (Bedrock)" → ... → "Done". Bedrock requires
      real AWS creds in Odoo settings — there's no MinIO/RIE
      equivalent.
- [ ] **UI pod does NOT drain** — `grep -i "drainer tick" $(host
      Odoo log file)` should be empty (the cron short-circuits on
      `prd_execution_mode=worker`).
- [ ] **Crash recovery** — during PHASE 2 of a job, `docker kill
      leviathan-worker`. Restart with `docker compose up -d
      leviathan-worker`. Within `leviathan.prd_stale_minutes` (set
      this to `1` in System Parameters for fast local testing, then
      restore to `15`) the `_prd_queue_recover_stale` step re-claims
      the row, the worker re-runs PHASE 2, the job reaches `done`.
      No `failed` state. **This is the test that proves the fence
      works.**
- [ ] **Batch** — import 10 URLs, **Run Batch**. Worker logs repeated
      `claimed N` lines until queue drains. All 10 reach `done`.
- [ ] **Batch guard** — try Run Batch on 501 rows → clean
      `UserError`, no jobs dispatched.

## Switching back to the in-process drainer

Set **System Parameters → `leviathan.prd_execution_mode = inprocess`**.
`docker compose stop leviathan-worker`. The Odoo `ir.cron` resumes
draining. Useful when you need to test changes to the drainer logic
itself without rebuilding the worker image.

## Switching the lambda to real AWS

Clear `leviathan.lambda_local_url`. Set the real Lambda function name
and AWS creds (or IRSA). The same `services/extraction_service.py`
code handles both modes — no addon redeploy needed.

## Artifacts on the host filesystem

MinIO's data dir is bind-mounted to `./artifacts/`. Everything the
lambda uploads is on your host immediately:

```
custom_addons/leviathan/local/artifacts/
└── leviathan-local/                <- bucket
    └── leviathan/<job_id>/
        ├── screenshots/
        ├── assets/
        └── raw_data/
            ├── site_discovery.json
            └── prd_prompt.txt
```

## Tear down

```bash
docker compose down                 # stop containers, keep volumes
rm -rf artifacts/                   # wipe MinIO data
docker rmi leviathan-prd-worker:local leviathan-extraction:local
```

## First successful run on this rig (2026-05-26)

For reference — what a green E2E looks like end-to-end on a developer
machine:

| Stage | Where | Time | Outcome |
|---|---|---:|---|
| Click Retry on a `failed` task | Host Odoo UI | t=0 | producer writes `state='generating'`, `auto_continue=True` |
| Lambda extraction (`gladia.io`) | `leviathan-lambda` (RIE) | ~276 s | `success=True partial=True prd_prompt=14772B` |
| Webhook callback | `host → host.docker.internal:8069` | <100 ms | `state='generating'` (via fence) |
| Worker claim | `leviathan-worker` | <3 s after state change | `drainer tick: claimed=1 free=5 in_flight=0 pool=5 depth=0` |
| Bedrock PRD call | `leviathan-worker` → AWS | 202 s | 30,502 chars / 4,220 words / 8,269 output tokens |
| Score | in-worker | <1 s | 81/100 grade B |
| S3 upload (final PRD) | `leviathan-worker` → AWS S3 | ~1 s | `s3://<bucket>/leviathan/<job>/final_prd.md` |
| Bedrock QC call | `leviathan-worker` → AWS | 8 s | `verdict=shippable` |
| Phase 4 final write | in-worker | <100 ms | `state='done'` |
| **Total worker time** | | **211 s** | |

If your numbers are wildly different (e.g. PHASE 2 < 5 s or > 10 min),
check `LEVIATHAN_BEDROCK_MAX_CONCURRENT`, your Bedrock TPM headroom,
and network egress from the worker container.
