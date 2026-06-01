# Deployment — video_editor_s3

## Docker

The repo root already has a `Dockerfile`. Append this stanza so the container ships with
ffmpeg + ffprobe and the Python deps:

```dockerfile
# --- video_editor_s3 dependencies ---
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# requirements.txt already pins boto3>=1.28.0 and requests
# If isolating: RUN pip install --no-cache-dir boto3>=1.28.0 requests
```

Verify inside the container:

```bash
docker run --rm <image> bash -c "ffmpeg -version | head -1 && ffprobe -version | head -1"
```

## docker-compose snippet

```yaml
services:
  odoo:
    image: ethara/odoo:19-video_editor_s3
    environment:
      VIDEO_EDITOR_S3_MAX_WORKERS: "2"
      VIDEO_EDITOR_S3_MAX_CONCURRENT: "2"
      VIDEO_EDITOR_S3_ENDPOINT: ""   # set only for MinIO/R2/LocalStack
    volumes:
      - odoo-data:/var/lib/odoo
      - video-media:/var/lib/odoo/filestore/video_editor_s3_media
    deploy:
      resources:
        limits:
          memory: 4G       # ffmpeg 5GB inputs prefer headroom
volumes:
  odoo-data:
  video-media:
```

`<data_dir>` defaults to `/var/lib/odoo`, so `video_editor_s3_media/` lands inside it and
survives container restarts via the named volume.

## Storage sizing

Per project, three encoded copies live on disk:
`v1_source.mp4` (download), `v1_preview.mp4` (480p), `v1_edited.mp4` (final). Budget
roughly **3× the largest source** per project. Cleanup on `project.unlink()` removes the
whole `<media_root>/<project_id>/` tree.

## Worker pool sizing

Two knobs interact:

| Knob | Purpose | Default |
|---|---|---|
| `VIDEO_EDITOR_S3_MAX_WORKERS` (env) | Thread pool size — how many jobs can run concurrently per Odoo process | 2 |
| `VIDEO_EDITOR_S3_MAX_CONCURRENT` (env) | Semaphore — admission control before `executor.submit` | 2 |
| `video_editor_s3.max_concurrent_jobs` (ICP) | Read by `S3SettingsResolver.get_max_concurrent_jobs()` | 2 |

FFmpeg is CPU-bound; pin both env vars to roughly `ceil(vCPU / 2)` and leave headroom for
the rest of Odoo. For a 4-vCPU instance, `2` is the right value. For a 16-vCPU video
box, `4–6` is reasonable.

## S3 credentials

Three deployment patterns:

1. **Static credentials in Settings** — simplest, paste into Settings UI. Stored in
   `ir.config_parameter` (encrypted at rest only if the Odoo DB is encrypted).
2. **IAM role on EC2 / ECS / EKS** — leave access/secret blank in Settings; boto3 picks
   up the instance role automatically. Bucket + region must still be set.
3. **Custom endpoint (MinIO / R2 / LocalStack)** — set `VIDEO_EDITOR_S3_ENDPOINT` env var
   on the Odoo process. The `_get_client` builder threads it through.

The required IAM policy on the export bucket:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:HeadObject"],
      "Resource": "arn:aws:s3:::<source-bucket>/*"
    },
    {
      "Effect": "Allow",
      "Action": ["s3:PutObject", "s3:HeadBucket"],
      "Resource": [
        "arn:aws:s3:::<export-bucket>",
        "arn:aws:s3:::<export-bucket>/*"
      ]
    }
  ]
}
```

Source and export buckets can be the same.

## Long worker timeouts (5 GB videos)

Bump the Odoo HTTP and cron worker timeouts:

```ini
[options]
limit_time_real = 7200
limit_time_real_cron = 7200
limit_memory_hard = 4294967296   ; 4 GB
limit_memory_soft = 3221225472   ; 3 GB
```

The FFmpeg subprocess inside the worker already uses an 1800s timeout (`_FFMPEG_TIMEOUT`)
per pass; the cron / HTTP limits only matter for the controller responses (job *submission*
is fast — the long work happens off the request thread).

## Reverse proxy

Nginx in front of Odoo needs Range pass-through (default-on) and large body size:

```nginx
location /video_editor/ {
    proxy_pass http://odoo_upstream;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_request_buffering off;
    proxy_buffering off;
    client_max_body_size 0;       # accept arbitrary upload size
    proxy_read_timeout 7200s;
    proxy_send_timeout 7200s;
}
```

## Monitoring

Each `video.editor.job` row carries:

- `status` (queued/running/done/failed/cancelled)
- `last_heartbeat` (datetime) — the executor refreshes this between chunks
- `progress_text` (char) — short string ("downloading 35%", "encoding final pass")
- `duration_ms` (computed)
- `log` (text, capped at 2 MB, ring-buffered via `RIGHT(...)`)
- `ffmpeg_command` (full argv)

A stuck job is one with `status=running` AND `last_heartbeat < now() - 120s`
(`_HEARTBEAT_STALE_SECONDS`). The cron file (`data/cron.xml`) can be wired up to flip
those to `failed` automatically.

## Smoke test in production

```bash
# 1. Test S3 connectivity
./odoo-bin shell -d <db> <<'PY'
cfg = env['video.editor.s3.settings'].get_s3_config()
from odoo.addons.video_editor_s3.services import s3_storage
print(s3_storage.validate_credentials(cfg))
PY

# 2. Submit a tiny round trip
./odoo-bin shell -d <db> <<'PY'
p = env['video.editor.project'].create({
    'name': 'Smoke test',
    's3_source_url': 's3://<bucket>/<small-mp4-key>',
})
p.action_download_source()
env.cr.commit()
print('Project', p.id, 'job kicked')
PY
```

Watch the job advance via Apps → Crowley Sourcing → Jobs.
