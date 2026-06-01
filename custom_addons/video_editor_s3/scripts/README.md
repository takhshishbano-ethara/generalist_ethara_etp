# Crowley Sourcing local YouTube extractor

A standalone HTTP server that downloads a (optionally trimmed) YouTube
video on your local machine with `yt-dlp` and streams the resulting MP4
bytes back as the HTTP response body. Run this on a residential network
(laptop / home box) and point Odoo at it so YouTube does not block the
cloud-IP Odoo backend.

This is the YouTube equivalent of `custom_addons/argus/scripts/local_extractor.py`
(which does the same thing for Instagram).

## What it does

- `GET /health` -> `{ "ok": true, "yt_dlp": "<version>" or false }`
- `POST /download` body `{ "url", "start_seconds", "end_seconds", "tier" }`
  -> response body is MP4 bytes (`Content-Type: video/mp4`), with metadata
  in headers: `X-Video-Id`, `X-Video-Title`, `X-Video-Channel`,
  `X-Video-Duration-Seconds`, `X-Video-Filename`.

Internally the server reuses the same `yt-dlp` primitive that the
server-side ingest uses (`bv*[height=H]+ba/b[height=H]` format spec +
`download_ranges` + `force_keyframes_at_cuts=True`), so a trim that
lands on a non-keyframe does not produce garbled first frames.

## Install

```bash
pip install -U yt-dlp
```

`yt-dlp` needs `ffmpeg` available on `PATH` for the merge step.
On macOS: `brew install ffmpeg`. On Debian/Ubuntu: `apt install ffmpeg`.

## Run

```bash
python local_youtube_extractor.py --host 127.0.0.1 --port 8081
```

Flags:
- `--host` (default `127.0.0.1`) - bind address.
- `--port` (default `8081`) - bind port. Different from the Argus
  extractor's `8080` so they can run side by side.

## Expose to Odoo

Pick one:

### Tailscale (recommended for stable deployments)

Install Tailscale on the extractor host and on the Odoo host, then use
the extractor host's tailnet name:

```
http://<tailnet-name>:8081
```

### cloudflared (no Tailscale needed)

```bash
cloudflared tunnel --url http://localhost:8081
```

cloudflared prints a public `https://<random>.trycloudflare.com` URL.

## Wire it into Odoo

1. Open **Settings > Crowley Sourcing > YouTube Ingest**.
2. Paste the extractor URL (Tailscale or cloudflared) into
   **Local Extractor URL**.
3. Save.

Odoo persists this under the system parameter
`video_editor_s3.local_extractor_url`.

## Use it

On a Crowley Sourcing project:

1. Paste a YouTube URL into **YouTube URL**.
2. (Optional) set **Start Time** and **End Time** in
   `HH:MM:SS:MS` to trim the clip.
3. Click **Download YouTube** in the form header.

A `youtube_local_download` job is queued. It POSTs to your local
extractor, streams the MP4 bytes into the project's
`youtube_local_blob` binary field, uploads the file to S3, sets
**Source S3 URL** on the project, and clears the binary field.

After the upload, click **Copy to Trimmed URL** to mirror
**Source S3 URL** into **Trimmed S3 URL**.

## Security notes

- The extractor has no auth. Bind to `127.0.0.1` and expose via Tailscale
  or cloudflared so the only callers are your own Odoo instance.
- The extractor materialises each download into a per-request tempdir
  and removes it after streaming the bytes back.
