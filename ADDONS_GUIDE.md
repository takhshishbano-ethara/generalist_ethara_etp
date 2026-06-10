# Ethara Addons Guide — `crowley` and `video_editor_s3`

End-to-end reference for the two Odoo 19 video-pipeline addons that live in
[`custom_addons/`](./custom_addons/):

| Addon | Role in the pipeline |
|---|---|
| [`crowley`](./custom_addons/crowley) | **Generation** — turn a text prompt into an MP4 via OpenRouter Seedance 2.0, store the result on S3. |
| [`video_editor_s3`](./custom_addons/video_editor_s3) | **Sourcing** — ingest video from S3 / YouTube, trim & crop in a browser editor, FFmpeg-render, score with an LLM, re-upload to S3. |

The two addons are independent (no shared models), but they share a workflow:
`video_editor_s3` produces *real-world* training rows; `crowley` produces *AI-generated*
rows. Both write into the same S3 bucket layout consumed by downstream training jobs.

Repo conventions assumed throughout:

- Working tree: `/Users/apple/Desktop/ethara-etp/ethara-etp`
- Odoo 19 source: [`src/`](./src) (Odoo source tree)
- Addons paths: `src/addons`, `custom_addons`
- Venv: [`.venv/`](./.venv) (Python 3.12)
- Postgres 18 on `localhost:5432`, role `apple`
- DB: `ethara_dev`
- Config: [`odoo.conf`](./odoo.conf) (no http port collisions: `8071`)

---

## Table of contents

1. [Install & setup](#1-install--setup)
2. [`crowley` — code & flow reference](#2-crowley--code--flow-reference)
3. [`video_editor_s3` — code & flow reference](#3-video_editor_s3--code--flow-reference)
4. [Operational notes](#4-operational-notes)

---

## 1. Install & setup

### 1.1 Prerequisites

| Component | Required version | How to install |
|---|---|---|
| macOS / Linux | — | — |
| Python | 3.12 | Already in `.venv` |
| PostgreSQL | ≥ 14 (18 is what's running) | `brew services start postgresql@18` |
| FFmpeg + FFprobe | any modern build | `brew install ffmpeg` (already at `/opt/homebrew/bin/ffmpeg`) |
| AWS S3 bucket | any | required for both addons |
| OpenRouter API key | — | required by `crowley` (Seedance) and `video_editor_s3` (LLM QC) |
| `yt-dlp` (optional) | latest | needed only for the local YouTube ingest path (the prod path uses an EC2 service) |

### 1.2 One-shot install

```bash
# from repo root
export PYTHONPATH=$(pwd)/src
export PATH="/opt/homebrew/opt/postgresql@18/bin:$PATH"

# 1. (one time) create the DB
createdb -h localhost ethara_dev

# 2. install the 3 modules (s3_connector is a dep of crowley)
./.venv/bin/python src/odoo-bin \
  -c odoo.conf \
  -d ethara_dev \
  -i s3_connector,crowley,video_editor_s3 \
  --stop-after-init \
  --no-http
```

The `odoo.conf` at the repo root already points to both addon paths:

```ini
addons_path = ./src/addons,./custom_addons
data_dir    = ./.odoo-data
db_name     = ethara_dev
http_port   = 8071
workers     = 0          ; threaded mode; background ThreadPools used inside addons
```

### 1.3 Start Odoo for normal use

```bash
export PYTHONPATH=$(pwd)/src
./.venv/bin/python src/odoo-bin -c odoo.conf
# Open http://localhost:8071  (login: admin / admin on a fresh DB)
```

### 1.4 First-time configuration

After login, an admin must wire credentials before either addon does useful work.

#### S3 connector (used by `crowley`)

`Settings → Technical → AWS S3 Connector`:

1. Create one `s3.connector` record. The **Name** field holds the **bucket name**
   (this is the `s3.connector.name` quirk — verified in
   [`custom_addons/s3_connector/models/s3_connector.py`](./custom_addons/s3_connector/models/s3_connector.py)).
2. Fill `aws_access_key_id`, `aws_secret_access_key`, `region_name`, optional CDN URL.

#### Crowley settings

`Settings → Crowley`:

| Field | ICP key | Notes |
|---|---|---|
| OpenRouter API key | `crowley.openrouter_api_key` | Fernet-encrypted at rest |
| S3 connector | `crowley.s3_connector_id` | Many2one to the connector above |
| Webhook secret | `crowley.webhook_secret` | Optional — turns on `/crowley/webhook` |

Set the env var **`CROWLEY_ENCRYPTION_KEY`** before going to production:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# put the value into the Odoo process env (systemd, docker compose, …)
```

If the var is absent the addon generates a key and stores it in
`ir.config_parameter` — convenient for dev, fatal for prod (the key sits next to
the secret it's supposed to protect).

#### Crowley Sourcing settings (`video_editor_s3`)

`Settings → General Settings → Crowley Sourcing` (or `Settings → Crowley Sourcing`).
The two credential blocks are kept separate so blast radius is small:

- **S3** — `video_editor_s3.aws_bucket`, `aws_region`, `s3_access_key`, `s3_secret_key`,
  `export_prefix`, `youtube_prefix`, `media_root`, `ffmpeg_path`, `ffprobe_path`.
  Click **Test S3 Connection** to run `head_bucket` and verify.
- **Lambda render pipeline** (optional) — `use_lambda`, `lambda_function_name`,
  `lambda_region`, `lambda_callback_base_url`, `lambda_webhook_token`. When enabled,
  render jobs are dispatched to the SAM-deployed Lambda under
  [`video_editor_s3/video-pipeline-lambda/`](./custom_addons/video_editor_s3/video-pipeline-lambda)
  instead of running locally.
- **YouTube EC2 service** — `youtube_ec2_url`, `youtube_ec2_callback_base_url`.
  Required because the in-process `yt-dlp` path is now a fallback; production
  uses an external EC2 FastAPI extractor (callback to
  `/video_editor_s3/callback/youtube_ec2`).
- **LLM QC** — `openrouter_api_key`, `llm_qc_model_id`
  (default `openrouter/google/gemini-3.1-pro-preview`).
  Optional `qc_seed_prompt` upload (`.md`/`.txt`, ≤ 100 KB) to override the bundled
  [`data/llm_qc_seed.md`](./custom_addons/video_editor_s3/data/llm_qc_seed.md).
- **Caps** — `trim_min_seconds` (8), `trim_max_seconds` (16),
  `prompt_max_words` (150), `max_source_size_mb` (5120),
  `max_concurrent_jobs` (2).

### 1.5 Smoke-test the install

```bash
# tests live next to each addon
./.venv/bin/python src/odoo-bin -c odoo.conf -d ethara_dev \
  --test-enable --test-tags crowley,video_editor_s3 \
  -u crowley,video_editor_s3 --stop-after-init
```

---

## 2. `crowley` — code & flow reference

> Generate videos from text prompts via OpenRouter Seedance 2.0, track them through a
> state machine, and persist the MP4 to S3.

Module manifest: [`custom_addons/crowley/__manifest__.py`](./custom_addons/crowley/__manifest__.py)
(version `19.0.1.5.0`, depends on `base, web, mail, bus, s3_connector`).

### 2.1 Directory layout

```
custom_addons/crowley/
├── __manifest__.py
├── README.md                                  ← user-facing intro
├── data/
│   ├── crowley_sequence.xml                   ← CRW/00000N sequence
│   ├── crowley_category_sequences.xml         ← per-category dataset sequences
│   └── ir_cron.xml                            ← every-minute poll cron
├── security/
│   ├── crowley_security.xml                   ← groups + record rules
│   └── ir.model.access.csv                    ← model ACLs
├── models/
│   ├── credential_manager.py                  ← Fernet wrappers
│   ├── crowley_generation.py                  ← Job (one creative goal)
│   ├── crowley_attempt.py                     ← Attempt (1..3 per job)
│   ├── crowley_s3_storage.py                  ← boto3 presigned URLs + SHA-256
│   ├── crowley_webhook_verifier.py            ← HMAC verification AbstractModel
│   ├── ir_attachment.py                       ← attachment cross-links
│   └── res_config_settings.py                 ← Settings panel
├── services/                                  ← pure Python (no `odoo.` imports)
│   ├── openrouter_client.py                   ← Seedance HTTP client
│   ├── s3_publisher.py                        ← download + upload pipeline
│   └── cost.py                                ← Seedance token math
├── controllers/
│   └── webhook.py                             ← /crowley/webhook (POST)
├── views/                                     ← form, list, kanban, settings
├── wizard/                                    ← review wizard + CSV import/export
├── static/src/components/
│   ├── live_status/                           ← OWL widget on bus.bus
│   └── video_player/                          ← inline HTML5 player
├── migrations/19.0.1.{1,3,5}.0/               ← schema migrations
└── tests/                                     ← Odoo unit + integration tests
```

### 2.2 Data model

```
crowley.generation  (1)  ───  (1..3)  crowley.attempt
       │                                   │
       │ proxies (state, cost, video_url)  │
       ▼                                   ▼
res.users (owner)             ir.attachment (the actual MP4 reference)
                                        │
                                        ▼
                              s3.connector + boto3 (presigned URLs)
```

#### `crowley.generation` — the *job*

One row per creative goal owned by one user. Lives in
[`models/crowley_generation.py`](./custom_addons/crowley/models/crowley_generation.py).

Key fields:

| Field | Role |
|---|---|
| `name` | `CRW/000NN` allocated from `ir.sequence` |
| `user_id`, `company_id` | ownership |
| `prompt`, `original_prompt`, `negative_prompt` | text inputs for the next attempt |
| `duration`, `resolution`, `aspect_ratio`, `seed`, `generate_audio` | Seedance knobs |
| `category` (selection of 19 slugs) | required before generate; locks once an attempt is `done` |
| `sub_category`, `topic`, `style`, `priority`, `complexity`, `language`, `contains_dialogue`, `speaker_count` | annotation fields |
| `attempt_ids` / `active_attempt_id` | One2many; the active attempt drives proxy fields |
| `state` (`draft → queued → submitting → processing → downloading → done|failed|cancelled`) | computed proxy of `active_attempt_id.state`, stored for list views |
| `review_state` (`pending/approved/rejected`) | aggregated from attempts |
| `cost_usd` / `cost_usd_estimate` / `cost_usd_delta` | actual vs estimated cost |
| `attempts_used` / `attempts_remaining` / `attempts_label` | `MAX_ATTEMPTS = 3` |
| `allow_duplicate` | manager-only override for the duplicate-prompt check |
| `ui_retry_pending` | UX flag while inputs are unlocked for a retry |

Key actions (all return an Odoo client action so the form auto-reloads):

- `action_generate()` — spawns attempt #1 and `_defer("_run_submit")`.
- `action_start_retry()` / `action_discard_retry()` / `action_submit_retry()`
  — three-button retry flow that requires at least one input field to change.
- `action_cancel()` — best-effort cancel of the active in-flight attempt
  (sets state to `cancelled` and calls `openrouter_client.cancel_job`).
- `action_reconcile()` — force-runs `_run_poll` synchronously on the active
  attempt, used after a worker restart leaves a record orphaned.
- `action_open_video()` / `action_download()` — open or download the MP4.

Duplicate-prompt prevention (v1.5.0):

- `_check_duplicate_prompts()` normalises the prompt (`strip + lowercase + collapse
  whitespace`) and searches all `crowley.attempt` rows with state in
  `(queued, submitting, processing, downloading, done)` across **all** users
  (uses `sudo()` so the dataset is org-wide unique).
- Fires from `_validate_can_submit` and from `@api.onchange` so taskers see a soft
  warning before clicking Generate.
- The post-migration script
  [`migrations/19.0.1.5.0/post-migration.py`](./custom_addons/crowley/migrations/19.0.1.5.0/post-migration.py)
  creates two **partial unique indexes** on `prompt_normalized` and
  `original_prompt_normalized` as a TOCTOU guard (if pre-existing duplicates
  exist, it logs a warning and skips index creation).

#### `crowley.attempt` — the *unit of work*

One row per OpenRouter submission. Lives in
[`models/crowley_attempt.py`](./custom_addons/crowley/models/crowley_attempt.py).

State machine (the canonical source of truth):

```
draft ─┬─► queued ─► submitting ─► processing ─► downloading ─► done
       │       │           │           │             │
       │       └───────────┼───────────┼─────────────┴──► failed
       │                   │           │
       │                   └───────────┴────────────────► cancelled
       └─► failed (validation)
```

`_ALLOWED_TRANSITIONS` is checked inside `write()` so any illegal transition
raises `ValidationError`.

Pipeline methods (postcommit-deferred via `_defer`):

| Method | Trigger | Does |
|---|---|---|
| `_run_submit` | `action_generate` / `action_submit_retry` | `POST /api/v1/videos` via `openrouter_client.submit_video`, move to `processing`. |
| `_run_poll` | `_cron_poll_openrouter` (every minute) and `action_reconcile` | `GET /api/v1/videos/{id}`; map OpenRouter status to internal state. |
| `_handle_completion` | called from poll OR webhook | Compare-and-set UPDATE so only one path advances to `downloading`. |
| `_run_download` | postcommit after `_handle_completion` | Stream MP4 → temp file → `crowley.s3.storage.upload_with_integrity` → create `ir.attachment` → state `done`. |
| `_handle_webhook_event` | `/crowley/webhook` callback | Same effect as `_run_poll`, idempotency-guarded by row lock + `webhook_idempotency_key`. |
| `_cron_poll_openrouter` | `ir.cron` every minute | Three watchdogs (stuck-submitting, stuck-downloading, poll-exceeded) + active poll batch, capped at 50 records / 120s wall-clock. |
| `_fail(code, msg)` | any pipeline failure | Single failure path; writes diagnostics, posts to chatter, pushes to `bus.bus`. |

The `_defer()` helper does **dual-cursor** failure recording: the deferred
callback runs on a fresh cursor, and if it raises we open a SECOND cursor to
write the failure — otherwise the failure write itself would be rolled back
and the attempt would be invisibly stuck. This is the production-critical
pattern; do not refactor it casually.

`video_play_url` is a **non-stored compute** so every read regenerates a
fresh ~5-minute presigned URL. The stored `video_s3_url` is the public CDN
fallback (set via `s3.connector.cdn_url`) and is kept only for backwards
compatibility.

Dataset naming (v1.2):

- Each attempt allocates a per-category sequence at successful S3 upload
  (`ir.sequence` with code `crowley.attempt.<category>`).
- Canonical filename: `T2AV_<category>_<NNNNNN>.mp4` (stored in `video_file`).
- S3 key layout: `T2AV/<category>/T2AV_<category>_<NNNNNN>.mp4`.
- Sequence is held under a row lock during the upload; if S3 upload fails
  after allocation, the number is consumed (gap left). Dataset consumers
  must filter on `video_file IS NOT NULL`, not on contiguous sequences.

#### `crowley.s3.storage` — boto3 wrapper

[`models/crowley_s3_storage.py`](./custom_addons/crowley/models/crowley_s3_storage.py)
is an `AbstractModel` that:

- Maintains a thread-safe module-level boto3 client cache keyed by
  `(db_name, connector_id, secret_fingerprint, region)` so rotating the
  connector's secret auto-invalidates the cache.
- Uses `TransferConfig(multipart_threshold=8 MB, chunksize=16 MB, concurrency=4)`
  for uploads.
- Forces `endpoint_url=https://s3.<region>.amazonaws.com` for non-`us-east-1`
  regions to avoid 307 redirects that HTML5 `<video>` refuses to follow.
- Exposes `presigned_get_url`, `verify_object_sha256`, `upload_with_integrity`.

### 2.3 Services layer (pure Python)

[`services/openrouter_client.py`](./custom_addons/crowley/services/openrouter_client.py)
— no Odoo imports, safe from background threads. Submits and polls
`bytedance/seedance-2.0`. Retries 3× with `(1, 3, 7)` second backoff on
401/403/429/5xx/timeouts. Custom exceptions:
`OpenRouterAuthError`, `OpenRouterRateLimitError`, `OpenRouterValidationError`,
`OpenRouterAPIError`, `OpenRouterTimeoutError`. Honours `Retry-After` headers.

[`services/s3_publisher.py`](./custom_addons/crowley/services/s3_publisher.py)
— glue between `openrouter_client` and `crowley.s3.storage`. Streams the MP4
to a `NamedTemporaryFile` (8 MB chunks, 1 GB cap, SHA-256 inline), uploads via
multipart, optionally re-reads to verify SHA-256 (`verify=True` → trades S3
egress for byte-identity proof).

[`services/cost.py`](./custom_addons/crowley/services/cost.py)
— Seedance pricing formula:
`tokens = (W * H * fps * duration) / 1024`, `usd = tokens * 7 / 1_000_000`,
`fps = 24`. `pixels_for(resolution, aspect_ratio)` returns even integers
(encoder-friendly). Used by both `cost_usd_estimate` on the job and the
attempt's `cost_usd` (the latter prefers the API-reported value when present).

### 2.4 Security model

[`security/crowley_security.xml`](./custom_addons/crowley/security/crowley_security.xml)
defines two groups:

- `crowley.group_crowley_user` — can CRUD own `crowley.generation` records
  (record rule by `user_id`). Cannot delete.
- `crowley.group_crowley_manager` (implies user) — full access, can override
  `allow_duplicate`, can approve/reject attempts via the review wizard, can
  unlink.

Field-level ACL: `crowley.generation.allow_duplicate` carries
`groups="crowley.group_crowley_manager"` so taskers can't see or edit it.

### 2.5 OpenRouter webhook

[`controllers/webhook.py`](./custom_addons/crowley/controllers/webhook.py) at
`POST /crowley/webhook`. Returns 503 unless `crowley.webhook_secret` is set
(inactive by default). On a request:

1. Reads raw body + `X-OpenRouter-Signature` + `X-OpenRouter-Idempotency-Key`.
2. HMAC-verifies via `crowley.webhook.verifier` (tolerance from
   `crowley.webhook_signature_tolerance`, default 300 s).
3. Looks up the attempt by `openrouter_job_id`.
4. Row-locks the attempt (`SELECT ... FOR UPDATE`) and skips if the
   idempotency key already matches.
5. Calls `attempt._handle_webhook_event(event_type, data)`.

### 2.6 Background plumbing

- `data/ir_cron.xml` — `ir.cron` calling `crowley.attempt._cron_poll_openrouter`
  every minute. Disable in tests with `nocron=True`.
- `bus.bus` channel `crowley.generation` + type `crowley.generation.update`
  feeds the OWL `<CrowleyLiveStatus/>` widget in
  [`static/src/components/live_status/`](./custom_addons/crowley/static/src/components/live_status).
- `CROWLEY_POOL_SIZE` env var controls a worker thread pool (default 6) used
  by the deferred submit / download tasks.

### 2.7 Lifecycle, end to end

```
User opens form  ──►  fills prompt + category  ──►  Generate
                                                      │
                                                      ▼
                                          _validate_can_submit
                                          _check_duplicate_prompts
                                          _spawn_attempt(state=queued)
                                          _defer("_run_submit")
                                                      │
                                                      ▼ postcommit
                                          POST /videos (openrouter_client)
                                          state=processing
                                                      │
                          ┌───────────────────────────┼───────────────────────────┐
                          ▼                                                       ▼
                  ir.cron every 60s                                     /crowley/webhook
                  _run_poll → poll_status                              _handle_webhook_event
                          │                                                       │
                          └───────────────────────────┬───────────────────────────┘
                                                      ▼
                            _handle_completion (compare-and-set to downloading)
                                                      │ _defer("_run_download")
                                                      ▼
                              s3_publisher.persist_video_to_s3
                              ir.sequence.next_by_code("crowley.attempt.<cat>")
                              ir.attachment.create
                              state=done
                                                      │
                                                      ▼
                        _push_bus  ──►  OWL CrowleyLiveStatus refreshes form
```

---

## 3. `video_editor_s3` — code & flow reference

> Ingest video from S3 or YouTube, edit in a browser editor, FFmpeg-render
> asynchronously, score with an LLM, re-upload to S3.

Module manifest:
[`custom_addons/video_editor_s3/__manifest__.py`](./custom_addons/video_editor_s3/__manifest__.py)
(version `19.0.1.0.35`, depends on `base, mail, web`). Note: the display name
in the manifest is **"Crowley Sourcing"** — same brand, different module.

### 3.1 Directory layout

```
custom_addons/video_editor_s3/
├── __manifest__.py
├── hooks.py                                   ← post_init_hook
├── README.md / INSTALL.md / docs/
├── data/
│   ├── sequences.xml                          ← Project/Job sequences
│   ├── cron.xml                               ← reap stale jobs
│   ├── llm_qc_seed.md                         ← bundled QC prompt
│   ├── video_editor_category_data.xml         ← 19 categories
│   └── video_editor_sub_category_data.xml     ← ~100 sub-categories
├── security/
│   ├── security.xml                           ← 3 groups + record rules
│   └── ir.model.access.csv
├── models/
│   ├── video_editor_project.py                ← Project (1167 lines, the centerpiece)
│   ├── video_editor_job.py                    ← Job (the worker-side runner)
│   ├── video_editor_processing_log.py         ← Per-job audit log
│   ├── video_editor_category.py / sub_category.py
│   └── res_config_settings.py
├── services/                                  ← pure Python (mostly)
│   ├── job_executor.py                        ← ThreadPoolExecutor + Semaphore
│   ├── ffmpeg_processor.py                    ← AbstractModel wrapping ffmpeg/ffprobe
│   ├── s3_storage.py                          ← parse, presign, upload
│   ├── media_storage.py                       ← AbstractModel; self-healing media_root
│   ├── youtube_downloader.py                  ← yt-dlp glue (fallback path)
│   ├── youtube_ec2_client.py                  ← EC2 service POST /download
│   ├── lambda_invoker.py                      ← async InvokeAsync for render
│   └── llm_qc.py                              ← OpenRouter multimodal QC
├── controllers/
│   ├── main.py                                ← /video_editor/* HTTP API
│   ├── lambda_callbacks.py                    ← /video_editor_s3/callback/render
│   └── youtube_ec2_callbacks.py               ← /video_editor_s3/callback/youtube_ec2
├── views/                                     ← project form, job list, menus, settings
├── wizards/                                   ← Force-pass LLM QC wizard
├── static/src/
│   ├── scss/editor.scss
│   └── js/
│       ├── services/                          ← OWL services
│       ├── fields/video_url_preview/          ← custom field widget
│       └── video_editor/                      ← the fullscreen OWL editor
├── video-pipeline-lambda/                     ← SAM template + Lambda handler
├── migrations/19.0.1.0.{1,2,3,4,5,10,20,24}/
└── tests/
```

### 3.2 Data model

#### `video.editor.project`

The user-facing record. Lives in
[`models/video_editor_project.py`](./custom_addons/video_editor_s3/models/video_editor_project.py).
~1170 lines, packed with workflow.

State machine:
`draft → processing → processed → exporting → exported`, plus terminal
`error`. The project also carries a `review_status` (`pending/approved/rejected`)
that mirrors the manager workflow.

Significant field groups:

- **Source intake**
  - `s3_source_url` (s3://, virtual-host, or path-style) → `s3_source_key` (compute).
  - `youtube_url` → `youtube_video_id` (compute via `youtube_downloader.parse_youtube_url`).
    Supported forms: `youtube.com/watch?v=`, `youtu.be/`, `/shorts/`, `/embed/`,
    `/v/`, `music.youtube.com`.
  - `youtube_start_time` / `youtube_end_time` in `HH:MM:SS:MS` (helpers
    `_parse_hhmmssms_to_seconds` / `_seconds_to_hhmmssms`).
  - `@api.onchange("youtube_url")` parses `?t=`/`?start=` from the pasted URL.
  - YouTube metadata: `youtube_title`, `channel`, `thumbnail_url`,
    `duration_seconds`, `resolution`, `fps`, `tier`, `ingested_at`.
  - `youtube_local_blob` — durable DB-attached copy used to bridge the gap
    between the local extractor download and the S3 upload (survives a worker
    restart). Cleared once the file lands in S3.
- **Probed source metadata** — `source_metadata` (JSON) holds the ffprobe output.
  Stored computes: `duration_seconds`, `resolution`, `source_fps`, `source_size_mb`.
- **Editing**
  - `editing_config` (JSON) holds trim/crop/rotate/resize/filters.
  - `edited_file_path` / `preview_file_path` are relative paths under the media root.
  - `edited_blob` / `edited_blob_filename` — the same durable-DB-blob trick for the
    render output so export can run even after the local file is GC'd.
  - `trim_start_seconds`, `trim_end_seconds`, `trim_duration_seconds`,
    `edited_resolution`, `edited_fps` are written after a successful render.
- **Output** — `output_s3_url` → `output_s3_key`, `output_metadata` (probed again).
- **LLM QC** — `prompt`, `llm_qc_result` (`pass/fail/flag`),
  `llm_failure_reason`, `llm_fixed_prompt`, `llm_evaluated_at`, `llm_qc_cost_usd`,
  plus the `llm_qc_force_passed*` audit quartet that records manual overrides
  (force-pass requires a reason and posts to the chatter).
- **Categorisation** — twin (legacy + new) M2O / Selection pairs:
  `category` (selection of 19) + `category_id` (M2O to `video.editor.category`),
  same for `sub_category` / `sub_category_id`. The `_onchange_category` clears
  `sub_category` if it no longer matches the new category.
- **Workflow** — `state`, `assigned_to`, `review_status`, `review_decided_by`,
  `review_decided_at`, `review_notes`. `action_approve` / `action_reject` are
  manager-only (raise `AccessError` otherwise).
- **Jobs** — `job_ids` (One2many to `video.editor.job`), `active_job_id`
  (computed from queued/running jobs), `processing_log_ids`.

Actions (all post a chatter note + show a non-sticky notification):

| Action | What it does |
|---|---|
| `action_render(config)` | Validates trim duration (`_check_trim_duration_in_range`), state → `processing`, `_kick_job("render", config=config)`. |
| `action_preview(config)` | Same but kicks a `preview` job (cheaper encode for the browser). |
| `action_export(s3_key=None)` | Requires `edited_file_path`, state → `exporting`, `_kick_job("export")`. |
| `action_ingest_youtube()` | Builds the YouTube job config and `_kick_job("youtube_ingest", config=cfg)`. |
| `action_copy_source_to_trimmed_url()` | Useful shortcut when the source is already the desired output. |
| `action_open_editor()` | Returns a client action `video_editor_s3.video_editor` that boots the OWL editor with `params.project_id`. |
| `action_run_llm_qc()` | Validates prompt + category + sub-category + topic + style + output URL, then `_kick_job("llm_qc")`. |
| `action_force_pass_llm_qc()` | Opens the force-pass wizard if no reason in context, else writes the override and posts to chatter. |
| `action_apply_fixed_prompt()` | Copies `llm_fixed_prompt` into `prompt` and prompts the user to re-run QC. |
| `action_clear_llm_qc()` | Wipes the QC verdict (including a force-pass) so the project can be re-evaluated. |
| `action_approve` / `action_reject` | Manager-only, state → `exported`, sets `review_status`. |
| `action_submit_for_processing()` | Requires LLM QC pass (or force-pass); state → `processed`. |

Auto-triggers (transparent helpers):

- `_maybe_auto_ingest_youtube` — on a fresh project with a YouTube URL and no
  active job, kicks a `youtube_ingest` automatically.
- `_maybe_probe_s3_source` / `maybe_probe_output_s3` — fires an `s3_probe` job
  whenever `s3_source_url` / `output_s3_url` changes and the metadata is empty.
- `_maybe_run_llm_qc` — re-runs QC when the prompt changes (skipped if a
  `prompt_qc` job is already queued/running).

Project deletion cleans the media folder (`shutil.rmtree(project_dir)`) and
relies on the `s3_source_url` constraints to refuse malformed input.

#### `video.editor.job`

The worker-side runner. Lives in
[`models/video_editor_job.py`](./custom_addons/video_editor_s3/models/video_editor_job.py).

| Field | Purpose |
|---|---|
| `job_type` | `render`, `preview`, `export`, `youtube_ingest`, `llm_qc`, `s3_probe`. |
| `status` | `queued → running → done|failed|cancelled`. |
| `config_json` | Per-job dict (trim/crop config, YouTube URL+times, target='source'/'output'). |
| `progress_text`, `last_heartbeat` | Updated every ~10 s by the worker. |
| `error_message`, `log` | Plain text; `log` is right-truncated at 2 MB. |
| `ffmpeg_command` | The exact invocation, for debugging. |
| `lambda_request_id`, `last_lambda_log_ts` | Set when the render is dispatched to Lambda. |
| `started_at`, `finished_at`, `duration_ms` | Lifecycle. |

`_submit_async()` is called from a `postcommit` callback set inside
`project._kick_job` so the database transaction commits before the thread
starts. The thread acquires a `Semaphore(MAX_CONCURRENT)` ticket; if the
ticket can't be acquired, `_submit_async` raises a UserError so the caller
sees "too many concurrent jobs" instead of silently dropping the work.

`action_cancel()` flips the status to `cancelled` and signals
`job_executor.request_cancel(job_id)`, which sets a `threading.Event`
that worker tasks poll at safe cancellation points (between download chunks,
between FFmpeg invocations, between S3 multipart attempts).

`action_retry()` requires `failed|cancelled` and re-queues the same job row
(resets `error_message`, `started_at`, `finished_at`, `progress_text`).

`_cron_reap_stale_jobs` (called from `data/cron.xml`) finds jobs in `running`
whose `last_heartbeat` is older than 10 hours (`_HEARTBEAT_STALE_SECONDS`)
and marks them failed — this is the safety net for crashed worker threads.

#### `video.editor.processing.log`

A child One2many under the project, populated by `_log_yt(...)` calls during
ingestion / QC / lambda dispatch. Levels: `info | warning | error`. Includes
`operation`, `message` (right-truncated at 8000), `duration_ms`. Surfaces in
the project's "Processing Log" tab.

#### `video.editor.category` / `video.editor.sub.category`

Master-detail M2O for categorisation. Bootstrapped from
[`data/video_editor_category_data.xml`](./custom_addons/video_editor_s3/data/video_editor_category_data.xml)
and
[`data/video_editor_sub_category_data.xml`](./custom_addons/video_editor_s3/data/video_editor_sub_category_data.xml).
Coexists with the legacy `Selection` fields (`category`, `sub_category`) on
the project — both are kept so the migration path is incremental.

### 3.3 Services layer

#### `services/job_executor.py`

The threading core. Module-level globals:

```python
_executor = ThreadPoolExecutor(max_workers=100, thread_name_prefix="video_editor_s3")
_semaphore = threading.Semaphore(100)
_cancel_events: dict[int, threading.Event]  # keyed by job id
_HEARTBEAT_STALE_SECONDS = 36000  # 10 h
```

Tuneable via env vars `VIDEO_EDITOR_S3_MAX_WORKERS` /
`VIDEO_EDITOR_S3_MAX_CONCURRENT`. The Semaphore caps in-flight FFmpeg / S3
work even though the executor pool is generous (FFmpeg is CPU-heavy enough
that 2–4 concurrent is the practical limit).

`_safe_worker(fn)` is a decorator wrapping every job entrypoint. It catches:

- `LambdaDispatched` / `Ec2Dispatched` — sentinel exceptions raised by
  `_run_render` / `_run_youtube_ingest` when the work is handed off to an
  external service. The wrapper simply logs and returns — the **callback
  controller** (`controllers/lambda_callbacks.py` or `youtube_ec2_callbacks.py`)
  is responsible for setting the final job status.
- `JobCancelled` — marks the job `cancelled` (without an error message).
- Any other Exception — marks the job `failed` with the truncated traceback.

`_check_cancelled(event)` is called at every safe pause boundary in the
worker code (`_run_render`, `_run_export`, `_run_youtube_ingest`,
`_run_llm_qc`, `_run_s3_probe`).

`_notify_job_completion` opens a SUPERUSER cursor and pushes a
`simple_notification` to the job's creator via `user._bus_send`, with
type-specific messages (`Render complete: <project name>`, etc.).

#### `services/ffmpeg_processor.py` (AbstractModel)

Wraps `ffmpeg` / `ffprobe`. Auto-resolves binary paths from `shutil.which`
then walks a known list of macOS / Linux paths
(`/opt/homebrew/bin`, `/usr/local/bin`, etc.). Admin overrides:
ICP keys `video_editor_s3.ffmpeg_path` / `ffprobe_path`.

`probe(src)` runs `ffprobe -v error -print_format json -show_format -show_streams`
and extracts `duration`, `width`, `height`, `fps`, `codec`, `size_bytes`,
`resolution`.

`render(job, src, dst, config, preview)` builds an `ffmpeg` invocation from
the JSON `config` (trim, crop, rotate, resize, mute, brightness/contrast/saturation),
adds `-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5` so it survives
S3 read flakes, and reports progress through `_heartbeat` updates by parsing
the `pipe:1` progress stream. Preset selection:

- `preview=True` → `libx264 veryfast`, 480p target.
- `preview=False` → `libx264 medium CRF 22 -movflags +faststart` for the final render.

#### `services/s3_storage.py`

Pure helpers (no Odoo imports) for:

- `parse_s3_url(url)` — accepts `s3://bucket/key`, virtual-host
  `https://bucket.s3.<region>.amazonaws.com/key`, and path-style
  `https://s3.<region>.amazonaws.com/bucket/key`.
- `is_configured(cfg)` — true when bucket/access/secret are all present.
- `validate_credentials(cfg)` — runs `head_bucket` (Settings `Test S3 Connection`).
- `download_to_file(cfg, key, dst, cancel_event)` — chunked download with
  cancellation checks.
- `upload_file(cfg, src, key)` — 50 MB multipart threshold, 25 MB parts,
  4-way parallel, 3-attempt retry with exponential backoff. Returns
  `https://<bucket>.s3.<region>.amazonaws.com/<key>` (or a custom-endpoint URL).
- `generate_presigned_url(cfg, key, expires_in)` — SigV4 presigned GET.

`VIDEO_EDITOR_S3_ENDPOINT` env var lets you point at MinIO / R2 / LocalStack.

#### `services/media_storage.py`

`video.editor.s3.media.storage` AbstractModel. Resolves a writable media
root by trying, in order:

1. `ir.config_parameter` `video_editor_s3.media_root`.
2. `<data_dir>/video_editor_s3_media`.
3. `<tmpdir>/odoo_video_editor_s3_media`.

The first writable location wins and is persisted back into the ICP. The
streaming controller uses `realpath + startswith(allowed_base + os.sep)` so a
traversal attempt (`..`) downgrades to a 404. `project_dir(project)` returns
`<media_root>/<project_id>/`. `path_for(project, kind, version)` returns
`v<n>_<kind>.mp4`.

#### `services/youtube_downloader.py` (fallback path)

The in-process yt-dlp wrapper. Builds a yt-dlp config with:

- Realistic User-Agent.
- `geo_bypass`, retries.
- `ejs:github` remote-components solver for `yt-dlp ≥ 2026.03` (without it
  YouTube returns storyboard images instead of the video).
- **Cookies autodetect** — probes `~/Library/Application Support/...` for
  Chrome → Firefox → Edge → Brave → Chromium → Vivaldi → Opera (Safari skipped
  on macOS because of the sandbox). Logs the chosen browser at INFO.
- **Cookie cache** — extracts cookies once and caches them as a Netscape file
  at `<data_dir>/video_editor_s3/yt_cookies_cache.txt` (`0o600`, 24 h TTL) so
  every subsequent download skips the macOS Keychain ACL prompt.
- Cache auto-invalidates when a bot-challenge is detected (next job
  re-extracts).
- Optional `video_editor_s3.yt_proxy_url` for residential proxies (applies
  only to YouTube traffic).

`parse_youtube_url(url)` returns `(video_id, normalized_url)` or `(None, None)`
for malformed input.

#### `services/youtube_ec2_client.py` (production path)

`submit_youtube_job(base_url, payload)` does a `POST {base_url}/download`
with `{tasker_id, yt_url, start_time, end_time}`. The EC2 service is expected
to perform the download + S3 upload, then call back to
`/video_editor_s3/callback/youtube_ec2` on Odoo with the result.

#### `services/lambda_invoker.py`

`invoke_async(function_name, region, payload, access_key, secret_key)` —
boto3 Lambda InvokeAsync (`InvocationType=Event`). Returns the AWS request id.
The Lambda is implemented under
[`video-pipeline-lambda/`](./custom_addons/video_editor_s3/video-pipeline-lambda)
(`handler.py`, `template.yaml`, `samconfig.toml`) and posts results back to
`/video_editor_s3/callback/render`. Render dispatch path:

```python
out_key = f"{export_prefix}/render_{ts}_{job_id}.mp4"
payload = {
    "op": "render", "job_id": job_id,
    "source_url": presigned_source, "s3_bucket": bucket, "s3_key": out_key,
    "config": editing_config,
    "callback_url": f"{lambda_callback_base}/video_editor_s3/callback/render",
}
lambda_invoker.invoke_async(...)
raise job_executor.LambdaDispatched()    # marks job as in-flight, no failure
```

#### `services/llm_qc.py`

The OpenRouter multimodal QC reviewer. `evaluate_llm_qc(...)` accepts the
row metadata + the rendered video bytes + the configured seed prompt, calls
the model (default `openrouter/google/gemini-3.1-pro-preview`) with both
text and a `video_url` content part, and parses a fenced ` ```json ` block
with `qc_result` (`PASS|FAIL|FLAG`), `failure_reason`, `fixed_prompt`,
`cost_usd`. 3-attempt retry covers 408/425/429/5xx and network errors with
exponential backoff. The raw response goes into `job.output_path` if
parsing fails.

The seed prompt is loaded from
`ir.config_parameter` `video_editor_s3.llm_qc_seed_file` (base64) or, when
absent, from the bundled
[`data/llm_qc_seed.md`](./custom_addons/video_editor_s3/data/llm_qc_seed.md)
which scores Clarity / Specificity / Coherence / Feasibility / Safety on 0–100,
and requires score ≥ 60 + expert level ≥ intermediate + no policy violations
to pass.

### 3.4 HTTP API

All routes under [`controllers/main.py`](./custom_addons/video_editor_s3/controllers/main.py)
use `type="http"`, `auth="user"`, `csrf=False`, JSON in/out:

| Verb | Path | Body | Returns |
|---|---|---|---|
| POST | `/video_editor/load` | `{s3_url, project_id?, name?}` | project payload + `stream_url` |
| POST | `/video_editor/process` | `{project_id, config, preview?}` | job payload |
| POST | `/video_editor/export` | `{project_id, s3_key?}` | job payload |
| POST | `/video_editor/cancel/<job_id>` | — | job payload |
| POST | `/video_editor/ingest_youtube` | `{project_id, youtube_url?}` | job payload |
| GET | `/video_editor/status/<job_id>` | — | job payload |
| GET | `/video_editor/project/<project_id>` | — | project payload + stream URLs |
| GET | `/video_editor/stream/<project_id>/<kind>` | kind ∈ `source/edited/preview` | video bytes |
| GET | `/video_editor_s3/llm_qc_seed/download` | — | the seed prompt file |

`stream/source` issues a `302` redirect to a fresh ~1 h presigned S3 URL.
`stream/edited` and `stream/preview` use Werkzeug's `send_file(conditional=True,
etag=True)` so HTML5 `<video>` can do HTTP Range requests for scrubbing.

Callback controllers (auth=`public`, HMAC-verified):

- `POST /video_editor_s3/callback/render` — Lambda result (success / failure /
  log lines). Verified against `video_editor_s3.lambda_webhook_token`.
- `POST /video_editor_s3/callback/youtube_ec2` — EC2 result. Updates
  `s3_source_url`, YouTube metadata, marks the ingest job `done`.

### 3.5 Security

[`security/security.xml`](./custom_addons/video_editor_s3/security/security.xml)
defines:

- `group_video_editor_s3_user` — read own/assigned projects.
- `group_video_editor_s3_editor` (implies user) — create + write own projects,
  queue jobs.
- `group_video_editor_s3_manager` (implies editor) — full access, can approve /
  reject and configure settings.

### 3.6 Frontend (OWL)

The browser-side editor lives under
[`static/src/js/video_editor/`](./custom_addons/video_editor_s3/static/src/js/video_editor).
It is a client action (`video_editor_s3.video_editor`) wired up in
[`views/video_editor_action.xml`](./custom_addons/video_editor_s3/views/video_editor_action.xml).

High-level flow:

1. Mount with `params.project_id`.
2. `GET /video_editor/project/<id>` to populate state.
3. `<video>` stream from `/video_editor/stream/<id>/source` (Range-aware).
4. User interacts with the timeline + crop overlay; state lives in an OWL
   `state` object and is serialised into the `editing_config` JSON.
5. On **Save & Render**: `POST /video_editor/process` with the config, poll
   `GET /video_editor/status/<job_id>` every 1.5 s until `done|failed|cancelled`.
6. Preview re-encode is the same dance with `preview: true` in the body.
7. On **Export**: `POST /video_editor/export` which kicks the export job;
   project `output_s3_url` populates when the job is done.

Field widgets:

- [`static/src/js/fields/video_url_preview/`](./custom_addons/video_editor_s3/static/src/js/fields/video_url_preview)
  — small inline player + thumbnail used on the project form.

### 3.7 Lifecycle, end to end

```
A. YouTube ingest path                          B. S3 source path
─────────────────────────                        ────────────────
paste youtube_url + start/end                    paste s3_source_url
        │                                                │
        ▼                                                ▼
action_ingest_youtube                            _maybe_probe_s3_source
_kick_job("youtube_ingest")                      _kick_job("s3_probe")
        │                                                │
        │ (EC2 path)                                     │
        ▼                                                ▼
youtube_ec2_client.submit_youtube_job             ffmpeg_processor.probe
raise Ec2Dispatched                              source_metadata written
        │                                                │
        ▼                                                │
EC2 service → callback                                   │
/callback/youtube_ec2                                    │
sets s3_source_url + metadata                            │
        │                                                │
        └────────────────────────┬───────────────────────┘
                                 ▼
                        action_open_editor
                                 ▼
                OWL editor streams /video_editor/stream/<id>/source
                user trims/crops/...
                                 ▼
                  POST /video_editor/process (config)
                                 ▼
                action_render → _kick_job("render")
                                 ▼
                ┌─────────────────────────────┐
                │ if use_lambda:              │
                │   lambda_invoker.invoke_async  │
                │   raise LambdaDispatched       │
                │   ── callback /callback/render │
                │ else (local):                  │
                │   ffmpeg_processor.render      │
                │   writes <media_root>/<id>/    │
                │      v1_edited.mp4             │
                └─────────────────────────────┘
                                 ▼
                  project state=processed
                  prompt + run_llm_qc
                                 ▼
                _kick_job("llm_qc")
                downloads trimmed video from S3
                calls OpenRouter multimodal model
                writes llm_qc_result / failure_reason / fixed_prompt
                                 ▼
                action_export → _kick_job("export")
                s3_storage.upload_file (multipart)
                project.output_s3_url set
                                 ▼
                          manager review
                          action_approve / action_reject
                          state=exported
```

---

## 4. Operational notes

### 4.1 Filesystem layout at runtime

```
.odoo-data/                           ← Odoo data_dir
├── filestore/ethara_dev/             ← ir.attachment binary store
├── sessions/
└── video_editor_s3/
    ├── yt_cookies_cache.txt          ← 0o600, 24 h TTL
    └── yt_cookies_cache_meta.json
.odoo-data/video_editor_s3_media/<project_id>/
    ├── v1_source.mp4                 ← used by older flows (most paths stream
    │                                   from S3 directly via presigned URL)
    ├── v1_edited.mp4                 ← final FFmpeg output
    └── v1_preview.mp4                ← preview encode for scrubbing
```

### 4.2 Cron schedule

| Cron | Module | Cadence | Purpose |
|---|---|---|---|
| `crowley.attempt._cron_poll_openrouter` | crowley | 60 s | Poll active attempts; rescue stuck rows. |
| `video.editor.job._cron_reap_stale_jobs` | video_editor_s3 | configured in `data/cron.xml` | Mark jobs failed if `last_heartbeat` older than 10 h. |

### 4.3 Tests

```bash
# crowley
./.venv/bin/python src/odoo-bin -c odoo.conf -d ethara_dev \
  --test-enable --test-tags crowley -u crowley --stop-after-init

# video_editor_s3
./.venv/bin/python src/odoo-bin -c odoo.conf -d ethara_dev \
  --test-enable --test-tags video_editor_s3 -u video_editor_s3 --stop-after-init

# lint
ruff check custom_addons/crowley/ custom_addons/video_editor_s3/
```

### 4.4 Common failure modes & rescue

| Symptom | Likely cause | Rescue |
|---|---|---|
| Crowley attempt stuck in `submitting`/`processing` | Worker crashed before completion | `_cron_poll_openrouter` rescues after 5 min; manual via **Reconcile** action on the form. |
| Crowley attempt stuck in `downloading` | S3 upload hung | Same cron rescues after 30 min. |
| `video.editor.job` stuck in `running` | Thread died without commit | `_cron_reap_stale_jobs` after 10 h, or `action_cancel` then `action_retry`. |
| Edited file disappeared, export fails | Local media root was wiped (ephemeral disk) | The project's `edited_blob` is restored from the DB on export; if also missing, re-render. |
| YouTube ingest fails with "Sign in to confirm you're not a bot" | YouTube extractor challenge | Set `video_editor_s3.yt_cookies_path` to a Netscape cookies file exported from an incognito session, or configure `yt_proxy_url`. |
| LLM QC marked malformed | Model returned non-JSON | Inspect `job.output_path` (the raw text is preserved) and re-run. |
| Crowley duplicate-prompt error | Another user already generated this prompt | Manager toggles `allow_duplicate` on the generation form. |

### 4.5 Where to look next

- [`custom_addons/crowley/README.md`](./custom_addons/crowley/README.md) — user-facing intro + duplicate-prompt deep dive.
- [`custom_addons/video_editor_s3/README.md`](./custom_addons/video_editor_s3/README.md) — feature matrix + architecture diagram.
- [`custom_addons/video_editor_s3/INSTALL.md`](./custom_addons/video_editor_s3/INSTALL.md) — step-by-step install + verify flow.
- [`custom_addons/video_editor_s3/docs/DEPLOYMENT.md`](./custom_addons/video_editor_s3/docs/DEPLOYMENT.md) — Docker / production rollout.
- [`custom_addons/video_editor_s3/video-pipeline-lambda/README.md`](./custom_addons/video_editor_s3/video-pipeline-lambda/README.md) — SAM-deployed render Lambda.
