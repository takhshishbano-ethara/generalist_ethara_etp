# Crowley AI Video Generation

Production-grade Odoo 19 module for AI video generation via
**ByteDance Seedance 2.0** (routed through **OpenRouter**), with **S3** storage
and in-Odoo **HTML5 video playback**.

**Self-contained**: no external Odoo dependencies beyond `base`, `mail`, `web`.
No `ffmpeg`, no `yt-dlp`, no `queue_job` — purely HTTP + boto3.

## Default generation target

* **Resolution**: 720p
* **Duration**: 8 seconds (range 4–15; recommended 8–10 for cinematic shots)
* **Aspect ratio**: 16:9
* **Audio**: enabled (free on Seedance 2.0)
* **FPS**: fixed 24 fps — Seedance 2.0 via OpenRouter renders at 24 fps and
  the API does not expose an `fps` parameter. If you need 30 fps you must
  switch the upstream route (e.g. fal.ai, Volcengine direct) — out of scope
  for v1.

## Bit-Perfect (No Re-Encode) Pipeline

H.264 (the codec Seedance returns) is mathematically lossy — no provider
exposes a lossless option. What this module **does** guarantee is that the
MP4 file leaving OpenRouter and the MP4 file the browser plays in Odoo are
**byte-for-byte identical**:

1. The bytes streaming back from OpenRouter are SHA-256-hashed inline.
2. The same bytes are uploaded to S3 via multipart (no transcoding).
3. The form view presents a presigned S3 GET URL to an HTML5 `<video>` tag.
4. The browser decodes the original H.264 stream directly — zero playback
   re-encoding.

The SHA-256 of the source bytes is persisted on every job for audit.

> **We do not market this as "lossless video".** H.264 is lossy by codec
> design. We market the **pipeline** as bit-perfect — and that promise is
> verifiable on every job.

## Architecture

```
User -> crowley.ai.vid.gen.job -> OpenRouter (submit + webhook + poll fallback)
                                      |
                              stream MP4 + SHA-256
                                      v
                              S3 (private bucket, multipart)
                                      |
                              presigned GET URL (5-min TTL)
                                      v
                  widget="video_preview" -> <video> -> direct S3 range requests
```

## Dependencies

| Type | Item |
|---|---|
| Odoo | `base`, `mail`, `web` (no other modules required) |
| Python | `requests`, `boto3`, `botocore` (all already in repo `requirements.txt`) |
| External | AWS S3 bucket (private, CORS configured per `docs/s3-bucket-setup.md`); OpenRouter account with API key |

## Install

```bash
python src/odoo-bin -c src/odoo.conf -d ethara-dev -i crowley_ai_vid_gen --stop-after-init
```

## Configure (Settings -> Crowley AI Vid Gen)

| Setting | Notes |
|---|---|
| OpenRouter API key | `sk-or-v1-...` |
| OpenRouter webhook secret | Shared HMAC secret for incoming callback verification |
| S3 region / bucket / access key / secret | AWS credentials (or use IAM role) |
| S3 endpoint URL | Empty for AWS; set for MinIO override |
| Presigned URL TTL | Default 300s |
| Poll interval | Default 60s (fallback when webhook missed) |

### About password fields (important)

All secret fields (`OpenRouter API Key`, `Webhook Secret`, `S3 Access Key`,
`S3 Secret Key`) use Odoo's `password="True"` widget. **They appear blank on
every reload** even when a value is stored — this is standard Odoo behaviour
for password fields. The module preserves the previously saved value
automatically when you save the settings form with the field left blank;
you only need to retype a secret when you want to **rotate** it. If you
accidentally clear a secret by typing into it then deleting the text, that
empty value WILL be saved.

## Run tests

```bash
python src/odoo-bin -c src/odoo.conf -d ethara-test -i crowley_ai_vid_gen \
    --test-enable --test-tags=crowley_ai_vid_gen --stop-after-init
```

## Security model

| Group | Permissions |
|---|---|
| Crowley AI Vid Gen / User | Read own jobs |
| Crowley AI Vid Gen / Creator | CRUD on own jobs |
| Crowley AI Vid Gen / Manager | CRUD + unlink on all jobs in company; sees S3 keys |

Record rules confine Users to their own jobs and Managers to their company.

## Source

- Module path: `custom_addons/crowley_ai_vid_gen/`
- Plan: `.sisyphus/plans/seedance-odoo-module.md`
- OpenRouter Seedance 2.0 reference: https://openrouter.ai/bytedance/seedance-2.0
