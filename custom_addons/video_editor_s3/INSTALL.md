# Installation — video_editor_s3

## 1. System binaries

Install ffmpeg + ffprobe on the Odoo host.

```bash
# macOS (Homebrew)
brew install ffmpeg

# Debian / Ubuntu
sudo apt-get update && sudo apt-get install -y ffmpeg

# RHEL / Fedora
sudo dnf install -y ffmpeg

# Verify
ffmpeg -version
ffprobe -version
```

If Odoo runs from a venv with stripped PATH and `shutil.which("ffmpeg")` returns None,
either symlink the binary into one of the search paths (`/opt/homebrew/bin`,
`/usr/local/bin`, `/opt/local/bin`, `/usr/bin`, `/bin`) or set the absolute path
in Settings → Video Editor S3 → "FFmpeg binary path".

## 2. Python dependencies

Already declared in repo `requirements.txt`:

```
boto3>=1.28.0
requests
```

YouTube ingestion additionally requires `yt-dlp`:

```bash
pip install -U yt-dlp
```

Pin to a known-good release (e.g. `yt-dlp==2024.05.27`) in production; `yt-dlp`
ships frequent updates to track YouTube extractor changes.

If installing into a separate venv:

```bash
pip install boto3>=1.28.0 requests yt-dlp
```

## 3. Install the module

```bash
# From repo root
./odoo-bin -d <db> -i video_editor_s3 --stop-after-init
```

Or from the Apps UI: Apps → Update Apps List → search "Video Editor S3" → Install.

## 4. Configure AWS credentials

Settings → General Settings → "Video Editor S3" section (manager group required):

1. Paste **Access Key**, **Secret Key**, **Bucket**, **Region** (default `ap-south-1`).
2. Optional: change **Export Prefix** (default `video_editor_s3/exports`).
3. Click **Save** then **Test S3 Connection** — runs `head_bucket` and flashes a success
   notification or a clear error.

If you use MinIO / Cloudflare R2 / LocalStack, set the endpoint via env var on the
Odoo process:

```
VIDEO_EDITOR_S3_ENDPOINT=https://minio.internal:9000
```

## 5. Verify install (isolated DB)

This mirrors the workflow stored as
`~/.claude/projects/-Users-user-project-ethara-etp/memory/verify-odoo-module-install.md`:

```bash
# 1. Terminate any backends connected to the verify DB
psql -h localhost -U odoo -d postgres -c \
  "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='video_editor_s3_verify';"

# 2. Drop + recreate
dropdb video_editor_s3_verify 2>/dev/null || true
createdb -O odoo video_editor_s3_verify

# 3. Install + run tests in one shot
./odoo-bin -d video_editor_s3_verify \
  -i base,video_editor_s3 \
  --test-tags video_editor_s3 \
  --stop-after-init \
  --log-level=info

# Expected tail
# > INFO ... odoo.modules.loading: Modules loaded.
# > INFO ... odoo.tests.runner: 0 failed, 0 error(s) of N tests
```

## 6. Open the editor

1. Apps menu → **Video Editor S3** → **Projects** → **New**.
2. Paste an S3 URL into `s3_source_url` (accepts `s3://bucket/key` or
   `https://bucket.s3.<region>.amazonaws.com/key`) and Save.
3. Click **Open Editor**. The OWL editor opens and streams directly from S3 via a
   short-lived presigned URL (the server issues a 302 redirect from
   `/video_editor/stream/<id>/source` to the presigned link, valid ~1 hour).
4. Use the timeline handles to set trim start/end. Use Crop mode to draw a box on
   the stage.
5. Click **Save & Render**. The FFmpeg worker re-reads the source from S3 (with
   `-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5`) and writes the
   edited file into `<media_root>/<project_id>/`.
6. When the project state is `processed`, click **Export to S3**. The output URL
   is written to `output_s3_url` on the project.

## 7. Ingest a YouTube video

1. Apps menu → **Video Editor S3** → **Projects** → **New**.
2. Paste a YouTube URL into `youtube_url` (formats supported:
   `https://www.youtube.com/watch?v=...`, `https://youtu.be/...`,
   `https://www.youtube.com/shorts/...`, `https://www.youtube.com/embed/...`,
   `https://music.youtube.com/watch?v=...`).
3. Click **Ingest from YouTube**. A `youtube_ingest` job is queued and runs in the
   background. Progress text updates every 5% of download.
4. When the job completes, the project's `s3_source_url` is populated and
   YouTube metadata (title, channel, duration, thumbnail) is filled in. The
   project is now ready to edit — click **Open Editor**.
5. Subsequent ingests of the same video reuse the existing S3 object via
   a HEAD probe — no re-download.

The S3 key layout is `<youtube_prefix>/<video_id>.mp4`. Override
`video_editor_s3.youtube_prefix` in Settings if you want a different bucket prefix.

## 8. Configure Bedrock Prompt QC

The module evaluates a project's `prompt` field with an AWS Bedrock model every time
the prompt is created or edited.

1. Enable Anthropic Claude in the AWS Bedrock console for the region you plan to use
   (default: `ap-south-1`). Go to Bedrock → Model access → enable
   `anthropic.claude-3-5-sonnet-20241022-v2:0` (or any other Claude model — set the
   model ID in Settings).
2. Create an IAM principal whose policy grants at minimum:

   ```json
   {
     "Version": "2012-10-17",
     "Statement": [
       {
         "Effect": "Allow",
         "Action": [
           "bedrock:InvokeModel",
           "bedrock:Converse"
         ],
         "Resource": "arn:aws:bedrock:*::foundation-model/anthropic.claude-3-5-sonnet*"
       }
     ]
   }
   ```

   Use a separate principal from the S3 one if you want different blast radii.

3. Settings → General Settings → **Video Editor S3** → **Bedrock Prompt QC**:
   - Paste the **Access Key** and **Secret Key** (both `password='True'`-masked).
   - Override **Region** or **Model ID** if needed.

4. (Optional) Customise the **QC Seed Prompt** under **Prompt QC Seed**. The seed
   text instructs the LLM how to evaluate prompts. Leave blank to use the bundled
   default at `data/qc_seed_prompt.md` (Clarity / Specificity / Coherence /
   Feasibility / Safety, pass requires score ≥ 60 + expert_level ≥ intermediate
   + no policy violations).

5. The LLM must return exactly one fenced ` ```json ` block with `score`,
   `expert_level`, `quality` (`pass`/`fail`), `reason`, `issues` (array). Malformed
   responses cause the job to fail; the raw text is preserved in the job's
   `output_path` field for debugging.

6. Open a project, edit the **Prompt** field on the General Information page, and
   save. A `prompt_qc` job is queued in the background; the form auto-refreshes
   when the job completes and the QC display block appears below the prompt
   showing score, expert level, pass/fail badge, reason, issues, and the evaluated
   prompt snapshot.

## 9. Troubleshooting

- **"FFmpeg binary not found"** — install ffmpeg or override
  `video_editor_s3.ffmpeg_path` in Settings.
- **"Storage write probe failed"** — the configured `media_root` is read-only; the
  module auto-falls-back to `<data_dir>/video_editor_s3_media` and persists the new
  path. Check the log for the chosen directory.
- **Job stuck in `queued`** — the worker pool is full (`max_concurrent_jobs`). Wait
  for an in-flight job to finish or raise the limit in Settings.
- **`status=failed`** — open the job form → "Error" tab for the traceback tail,
  "FFmpeg Command" tab for the exact invocation, and "Log" tab for stderr.
