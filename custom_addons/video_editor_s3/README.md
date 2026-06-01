# Crowley Sourcing

Server-side video editor for Odoo 19 that ingests source video from an Amazon S3 URL, edits it
in a browser-based OWL UI (trim, crop, rotate, resize, mute, filters), processes the result with
FFmpeg in an async worker pool, and uploads the rendered MP4 back to S3.

Companion to the existing `instagram_video_qc_manager` module:

| Module | Source | Pipeline |
|---|---|---|
| `instagram_video_qc_manager` | Instagram URL → instaloader | Local FFmpeg + QC chatter |
| `video_editor_s3` | S3 URL (s3:// or https) | Local FFmpeg + re-upload to S3 |

The two modules share no models. They can be installed together.

## Capabilities

- Load any S3 video URL (s3://bucket/key, virtual-host, path-style, or custom endpoint)
- Ingest video from a YouTube URL: server-side download via `yt-dlp`, upload to S3, dedup
  by video id (skip download if the deterministic S3 key already exists)
- Streams source through Odoo with HTTP Range support (`conditional=True, etag=True`)
- Trim / crop (with overlay anchored to the actual rendered video pixels, letterbox-aware)
- Rotate 0/90/180/270, resize to Original / 1080p / 720p / 480p / 1080×1920 Reel / 1080×1080 Square
- Mute, brightness, contrast, saturation
- Single-track timeline with dimmed cut-away regions and trim handles
- Server-side preview encode (480p veryfast) + final encode (libx264 medium CRF 22, faststart)
- Async job queue (`ThreadPoolExecutor` + `Semaphore`, cooperative cancel)
- Heartbeat + progress text per job; status polled every 1.5s by the editor
- Per-project processing log audit trail
- Export rendered MP4 back to S3 (multipart 50MB threshold, 25MB chunks, 4-way parallel,
  3-attempt retry with exponential backoff)
- Supports up to 5 GB source (configurable via Settings)
- LLM-based prompt QC via AWS Bedrock (Anthropic Claude by default). Score, expert level,
  pass/fail, reason, and issues display next to the project prompt and refresh automatically
  after each save.

## Architecture

```
┌──────────────────────┐          ┌────────────────────────┐
│ OWL editor (browser) │ ──RPC──► │ controllers/main.py    │
└──────────────────────┘          │  /video_editor/load    │
        ▲ stream (Range)          │  /process /export      │
        │                         │  /status /stream       │
        │                         └────────┬───────────────┘
        │                                  ▼
        │                       ┌────────────────────────┐
        │                       │ video.editor.project   │
        │                       │ video.editor.job       │──┐
        │                       └────────────────────────┘  │
        │                                                   │ submit_job_async
        │                                                   ▼
        │                       ┌────────────────────────┐
        │                       │ services/job_executor  │
        │                       │  ThreadPoolExecutor    │
        │                       │  Semaphore (max=2)     │
        │                       │  cancel events         │
        │                       └────────┬───────────────┘
        │                                ▼
        │            ┌─────────────────────────────────────┐
        │            │  s3_storage.download_to_file        │
        │            │  ffmpeg_processor.render            │
        │            │  s3_storage.upload_file             │
        │            └─────────────┬───────────────────────┘
        │                          ▼
        │            ┌──────────────────────────────┐
        └────────────│  <media_root>/<project_id>/  │
                     │   v1_source.mp4              │
                     │   v1_edited.mp4              │
                     │   v1_preview.mp4             │
                     └──────────────────────────────┘
```

## Required runtime dependencies

| Type | Name | Notes |
|---|---|---|
| Python | `boto3 >= 1.28.0` | already in repo `requirements.txt` |
| Python | `requests` | usually already present |
| Python | `yt-dlp` | required for YouTube ingestion (`pip install yt-dlp`) |
| Binary | `ffmpeg` | searched in PATH then `/opt/homebrew/bin`, `/usr/local/bin`, `/opt/local/bin`, `/usr/bin`, `/bin` |
| Binary | `ffprobe` | same search order |

Both binaries can be pinned via system parameters
`video_editor_s3.ffmpeg_path` and `video_editor_s3.ffprobe_path` (Settings → Crowley Sourcing).

## Settings

Settings → General Settings → "Crowley Sourcing" section (manager group only):

| Field | ICP key | Default |
|---|---|---|
| AWS Access Key | `video_editor_s3.aws_access_key` | — |
| AWS Secret Key | `video_editor_s3.aws_secret_key` | — |
| Bucket | `video_editor_s3.aws_bucket` | — |
| Region | `video_editor_s3.aws_region` | `ap-south-1` |
| Export Prefix | `video_editor_s3.export_prefix` | `video_editor_s3/exports` |
| YouTube Prefix | `video_editor_s3.youtube_prefix` | `video_editor_s3/youtube` |
| Max Source Size (MB) | `video_editor_s3.max_source_size_mb` | `5120` |
| Max Concurrent Jobs | `video_editor_s3.max_concurrent_jobs` | `2` |
| FFmpeg binary path | `video_editor_s3.ffmpeg_path` | auto |
| FFprobe binary path | `video_editor_s3.ffprobe_path` | auto |
| Media Root | `video_editor_s3.media_root` | `<data_dir>/video_editor_s3_media` |
| Bedrock Region | `video_editor_s3.bedrock_region` | `ap-south-1` |
| Bedrock Model ID | `video_editor_s3.bedrock_model_id` | `anthropic.claude-3-5-sonnet-20241022-v2:0` |
| Bedrock Access Key | `video_editor_s3.bedrock_access_key` | — |
| Bedrock Secret Key | `video_editor_s3.bedrock_secret_key` | — |
| QC Seed Prompt | `video_editor_s3.qc_seed_prompt` | bundled default (`data/qc_seed_prompt.md`) |
| YouTube Cookies From Browser | `video_editor_s3.yt_cookies_browser` | autodetected — first installed of chrome/firefox/edge/brave/chromium/vivaldi/opera |
| YouTube Cookies File Path | `video_editor_s3.yt_cookies_path` | — |
| YouTube Proxy URL | `video_editor_s3.yt_proxy_url` | — |

Endpoint override (for MinIO / Cloudflare R2 / LocalStack):
set env var `VIDEO_EDITOR_S3_ENDPOINT=https://...` on the Odoo process.

Worker thread / concurrency caps (process-level):
`VIDEO_EDITOR_S3_MAX_WORKERS` (default 2), `VIDEO_EDITOR_S3_MAX_CONCURRENT` (default 2).

## Security groups

- `group_video_editor_s3_user` — read own/assigned projects
- `group_video_editor_s3_editor` — create + write own projects, queue jobs
- `group_video_editor_s3_manager` — full access including unlink and settings

Manager implies editor implies user.

## HTTP API (auth=user)

| Verb | Path | Body / Params | Returns |
|---|---|---|---|
| POST | `/video_editor/load` | `{s3_url, project_id?, name?}` | project payload + `stream_url` |
| POST | `/video_editor/process` | `{project_id, config, preview?}` | job payload |
| POST | `/video_editor/export` | `{project_id, s3_key?}` | job payload |
| POST | `/video_editor/cancel/<job_id>` | — | job payload |
| POST | `/video_editor/ingest_youtube` | `{project_id, youtube_url?}` | job payload |
| GET | `/video_editor/status/<job_id>` | — | job payload |
| GET | `/video_editor/project/<project_id>` | — | project payload + stream URLs |
| GET | `/video_editor/stream/<project_id>/<kind>` | `kind` ∈ {source, edited, preview} | video bytes, Range-aware |

Project payload includes id, name, state, s3_source_url/key, source_metadata, duration_seconds,
resolution, source_size_mb, editing_config, has_source/edited/preview, output_s3_url, active_job_id.

Job payload includes id, project_id, job_type, status, progress_text, ISO timestamps,
duration_ms, output_path, output_s3_url, error_message.

## Files on disk

```
<media_root>/<project_id>/v1_source.mp4
<media_root>/<project_id>/v1_edited.mp4
<media_root>/<project_id>/v1_preview.mp4
```

Path resolution is self-healing: if the admin-configured `media_root` is unwritable, the
storage layer falls back to `<data_dir>/video_editor_s3_media` then `<tmpdir>/odoo_video_editor_s3_media`
and persists the working choice back into `ir.config_parameter`.

A traversal guard (`realpath + startswith(allowed_base + os.sep)`) protects the streaming
controller; any escape attempt converts to a 404.

## Cancellation

`job.action_cancel()` sets a `threading.Event` registered against the job's record id.
The worker checks `_check_cancelled(ev)` at well-defined boundaries (between download chunks,
between FFmpeg invocations, between S3 upload attempts) and translates the event into
`JobCancelled`, which the `_safe_worker` decorator turns into `status='cancelled'` with the
heartbeat preserved.

## YouTube ingestion

Paste a YouTube URL into the `youtube_url` field on a project and click **Ingest from
YouTube**. A `youtube_ingest` job is queued and:

1. Validates the URL (`youtube.com/watch?v=`, `youtu.be/`, `/shorts/`, `/embed/`, `/v/`,
   `music.youtube.com`).
2. Computes a deterministic S3 key `<youtube_prefix>/<video_id>.mp4`.
3. HEAD-probes that key on S3:
   - **Hit (dedup)** — reuses the existing object, writes `s3_source_url`, skips download.
   - **Miss** — downloads with `yt-dlp` to a temp directory, uploads to S3 (multipart
     for >50 MB), then writes `s3_source_url`.
4. Persists YouTube metadata: `youtube_title`, `youtube_channel`,
   `youtube_thumbnail_url`, `youtube_duration_seconds`, `youtube_ingested_at`.
5. Cleans up the tempdir whether the job succeeds, fails, or is cancelled.

Heartbeats every 5% of download progress so the job form's progress text stays live.
Cancel via **Cancel** on the job form — the worker observes the cancel event between
download chunks and at every S3 boundary.

Size cap reuses the existing `video_editor_s3.max_source_size_mb` ICP key — `yt-dlp`
enforces it via its built-in `max_filesize` option.

The `yt-dlp` Python package must be installed on the Odoo host. `ffmpeg` is also
required (already declared) because `yt-dlp` invokes it to mux adaptive video+audio
streams.

### Bot-challenge / "Sign in to confirm you're not a bot"

YouTube increasingly blocks downloads from datacenter or unfamiliar IPs with an
interstitial that yt-dlp surfaces as `Sign in to confirm you're not a bot`. The module
ships with two layers of mitigation:

- **Always-on**: realistic `User-Agent`, `geo_bypass`, retries, and the `ejs:github`
  remote-components solver (needed by yt-dlp ≥ 2026.03 to run YouTube's n-sig JS
  challenge — without it only storyboard images are returned).
- **Cookies-from-browser autodetect**: when the `YouTube Cookies From Browser` setting
  is empty, the downloader probes the Odoo OS user's home directory for an installed
  browser (chrome → firefox → edge → brave → chromium → vivaldi → opera) and re-uses
  its cookies. Safari is intentionally skipped because the macOS sandbox blocks
  non-Apple processes from reading its cookie store. The pick is logged at INFO:
  `yt-dlp cookies-from-browser=chrome (autodetected)`.
- **Cookie cache**: Chromium-family browsers store their cookies encrypted with the OS
  keychain (Chrome Safe Storage), and on macOS the Keychain ACL is not reliably
  persistent for the Python interpreter — "Always Allow" does not stick, so every job
  would otherwise prompt for the keychain password. The downloader extracts the cookies
  once and caches them as a Netscape-format file under
  `<data_dir>/video_editor_s3/yt_cookies_cache.txt` (mode `0o600`, default TTL 24 h).
  Subsequent jobs read the cached file directly without touching the keychain. The
  cache is auto-invalidated whenever a bot-challenge is detected, so the next job
  re-extracts fresh cookies. Delete the file manually to force an immediate refresh.
  Cache events are logged at INFO: `yt-dlp cookies-from-browser=chrome (autodetected,
  cache hit|refreshed)`.

If the bot-challenge still fires, follow yt-dlp's recommended flow ([Extractors wiki
→ Exporting YouTube cookies](https://github.com/yt-dlp/yt-dlp/wiki/Extractors)):

1. Open a **new private / incognito window** in your browser.
2. Sign in to YouTube in that window.
3. Visit `https://www.youtube.com/robots.txt` in the same incognito tab.
4. Export `youtube.com` cookies via the **Get cookies.txt LOCALLY** (Chrome) or
   **cookies.txt** (Firefox) extension to a Netscape-format file.
5. **Close** the incognito window — this freezes the session so YouTube does not
   rotate the cookies behind your back.
6. Upload the file to the Odoo host (e.g. `/var/lib/odoo/yt_cookies.txt`), make it
   readable by the Odoo OS user (`chmod 644`), and set
   **Settings → Crowley Sourcing → YouTube Ingest → Cookies File Path** to the
   absolute path.

This incognito + cookies.txt flow is the **recommended fix for production**. The
autodetect path is a convenience for local development: live-session cookies in a
signed-in browser tab can be rotated by YouTube at any time, which silently breaks
downloads until you re-export.

If your Odoo host's IP is itself blocked, additionally set
**Settings → YouTube Ingest → YouTube Proxy URL** to a residential HTTP / HTTPS / SOCKS5
proxy (e.g. `http://user:pass@host:8080` or `socks5://host:1080`). The proxy applies
only to YouTube traffic, not to S3.

You can also pin a specific browser (or browser profile) with **Cookies From Browser**
— supported values: `chrome`, `firefox`, `edge`, `brave`, `chromium`, `vivaldi`,
`opera`, `safari`, `whale`. Append `:PROFILE_NAME` for a non-default profile, e.g.
`chrome:Profile 1`.

## Prompt QC

Every time the `prompt` field on a project is written (on create or update), the module
queues a `prompt_qc` job that evaluates the prompt with **Moonshot Kimi K2.5** hosted on
AWS Bedrock, and writes the result back to the project. The General Information notebook
page shows the latest score, expert level, pass/fail badge, reason, issues, and the
evaluated prompt snapshot — the form auto-refreshes once the job completes.

The integration calls Bedrock's Converse REST endpoint directly
(`https://bedrock-runtime.{region}.amazonaws.com/model/{model_id}/converse`) with a
long-lived Bedrock API key sent as `Authorization: Bearer ...`. The model identifier
is either a foundation-model ID (e.g. `moonshotai.kimi-k2.5`) or a Bedrock Marketplace
endpoint ARN — both are URL-encoded into the path. This mirrors the pattern used by
`task_forge_core/services/kimi_client.py`, and avoids the boto3 Bedrock-runtime
dependency for the QC path. Retries cover HTTP 408/425/429/5xx and network errors with
exponential backoff (default 3 attempts).

The seed prompt (which tells Kimi K2.5 how to score) is configurable. Upload a `.md` or
`.txt` file (UTF-8, max 100 KB) under **Settings → Crowley Sourcing → Prompt QC Seed**,
or leave it blank to use the bundled default at `data/qc_seed_prompt.md`, which scores
0–100 on Clarity / Specificity / Coherence / Feasibility / Safety, classifies expert
level as novice / intermediate / advanced / expert, and only passes when the score is
≥ 60, the expert level is at least intermediate, and there is no policy violation. The
model is required to return a single fenced ` ```json ` block with `score`,
`expert_level`, `quality` (`pass`/`fail`), `reason`, and `issues` (array). Malformed
responses cause the job to fail; the raw output is stored in `output_path` on the job
for inspection.

Credentials live under the **Kimi K2.5 Prompt QC** block in Settings (separate from the
S3 keys). Configure:

- **Bedrock Region** — e.g. `us-east-1` (also supported: `us-west-2`, `ap-south-1`,
  `ap-northeast-1`, `ap-southeast-2/3/4`, `eu-north-1`, `eu-west-2`, `sa-east-1`).
- **Kimi Model ID / ARN** — defaults to `moonshotai.kimi-k2.5`; paste a Marketplace
  endpoint ARN here if you provisioned Kimi K2.5 via the Bedrock Marketplace.
- **Bedrock API Key** — long-term key from the AWS console, sent as
  `Authorization: Bearer ...`.

## See also

- `INSTALL.md` — install + verify steps
- `docs/DEPLOYMENT.md` — Docker / production rollout
