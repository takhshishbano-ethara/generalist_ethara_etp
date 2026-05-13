# HTTP API

All endpoints require an authenticated Odoo session (`auth="user"`) and
respect the module's record rules.

## Streaming

| Method | URL                                            | Returns                            |
|--------|------------------------------------------------|------------------------------------|
| GET    | `/video_qc/version/<id>/source`                | Source (original) attachment bytes |
| GET    | `/video_qc/version/<id>/edited`                | Edited render bytes                |
| GET    | `/video_qc/version/<id>/preview`               | Low-bitrate preview render bytes   |
| GET    | `/video_qc/task/<id>/original/<slot>`          | Task original (slot 1 or 2)        |

All stream responses set:
* `Content-Type: video/mp4` (or the attachment's mimetype)
* `Accept-Ranges: bytes`
* `Cache-Control: private, max-age=60`

## Write endpoints (JSON-RPC)

### `POST /video_qc/task/<id>/new_version`

Creates the next `video.task.version` for the task and marks it latest.
Optional body: `{ "edit_notes": "..." }`.

Returns: `{ "version_id": int, "version_no": int }`

### `POST /video_qc/version/<id>/save_edit`

Body:
```json
{
    "config": {
        "trim":       {"start": 0.0, "end": 12.5},
        "crop":       {"x": 100, "y": 50, "w": 720, "h": 1280, "aspect": "9:16"},
        "rotate":     90,
        "resize":     {"w": 1080, "h": 1920},
        "mute":       false,
        "brightness": 0.05,
        "contrast":   1.1,
        "saturation": 1.0
    },
    "render": true
}
```

Persists the configuration on the version (mirrored onto `trim_start`,
`trim_end`, `crop_data_json`). When `render` is true, enqueues a
`_job_render` and immediately returns; the version moves to `processing`
and finally `rendered` (or `error`).

Returns: `{ "ok": true, "version_id": int, "status": "draft|processing|rendered|error" }`

### `POST /video_qc/version/<id>/save_prompt`

Body: `{ "prompt_text": "...", "prompt_response": "..." }`

### `POST /video_qc/task/<id>/download`

Enqueues yt-dlp downloads for any populated URL slot.

## Python service models

```python
# Anywhere in server code
self.env["instagram.downloader"].download_to_attachment(task, url, slot=1)
self.env["ffmpeg.processor"].render_version(version, config_dict)
```

Both are `AbstractModel` services so they can be unit-tested with
`TransactionCase` and overridden via standard Odoo inheritance.

## Configuration via system parameters

The module ships with no `ir.config_parameter` knobs by default — the
FFmpeg/yt-dlp behaviour is set via the binary's own command-line flags
inside `services/`. Override by inheriting the service and replacing the
`_build_command` / `_invoke_yt_dlp` methods.
