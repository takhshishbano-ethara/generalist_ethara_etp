# Instagram Video QC Manager

Production-grade Odoo 19 module that turns the back-office into a complete
Instagram video pipeline: download, edit, prompt, QC, version — all in one
record.

## What's inside

```
instagram_video_qc_manager/
├── __manifest__.py
├── __init__.py
├── models/                  # video.task, version, edit history, processing log
├── services/                # InstagramDownloader, FFmpegProcessor (AbstractModels)
├── controllers/             # HTTP streams + JSON write endpoints used by the OWL editor
├── wizard/                  # QC review + prompt attachment wizards
├── views/                   # form / kanban / list / search / dashboard / menus
├── data/                    # sequences, mail templates, queue_job channels, demo
├── security/                # groups + ACL + record rules
├── static/src/
│   ├── scss/                # editor + dashboard styling
│   └── js/
│       ├── services/        # video_qc RPC service
│       └── video_editor/    # OWL fullscreen editor (component + sub-components)
└── tests/                   # unit tests
```

## Architecture diagram

```
┌──────────────────────────────────────────────────────────────────────────┐
│                            Browser (OWL)                                  │
│                                                                          │
│   VideoEditor (client action)                                            │
│   ├── EditorToolbar    ── tool / aspect / filter sliders                 │
│   ├── EditorTimeline   ── trim handles + cursor                          │
│   ├── EditorPromptPanel── prompt + QC status + history                   │
│   └── video_qc service ── RPC / orm.search / streams                     │
└───────────────────────────────┬──────────────────────────────────────────┘
                                │ HTTP (JSON / range-stream)
                                ▼
┌──────────────────────────────────────────────────────────────────────────┐
│                      Odoo HTTP layer (controllers/main.py)               │
│  GET  /video_qc/version/<id>/source|edited|preview                       │
│  GET  /video_qc/task/<id>/original/<slot>                                │
│  POST /video_qc/task/<id>/new_version                                    │
│  POST /video_qc/version/<id>/save_edit (config, render?)                 │
│  POST /video_qc/version/<id>/save_prompt                                 │
│  POST /video_qc/task/<id>/download                                       │
└───────────────────────────────┬──────────────────────────────────────────┘
                                │ ORM
                                ▼
┌──────────────────────────────────────────────────────────────────────────┐
│                              Models                                       │
│  video.task ──< video.task.version ──< video.task.edit.history           │
│       │                  │                                                │
│       │                  └──< ir.attachment (edited / preview)            │
│       └──< ir.attachment (originals)                                      │
│       └──< video.task.processing.log                                      │
└───────────────────────────────┬──────────────────────────────────────────┘
                                │ self.env.cr.postcommit.add(...)
                                ▼
┌──────────────────────────────────────────────────────────────────────────┐
│                            Services                                       │
│  instagram.downloader  ──► yt-dlp ──► ir.attachment                       │
│  ffmpeg.processor      ──► ffmpeg / ffprobe ──► edited + preview          │
└──────────────────────────────────────────────────────────────────────────┘
```

## Workflow

1. **Draft** — user creates a task and pastes up to two Instagram URLs.
2. **Download** — `action_download_videos` defers a `_job_download_video`
   call per slot to after-commit. yt-dlp pulls the highest-quality MP4 +
   sidecar thumbnail.
3. **Downloaded** — both attachments live on the task; thumbnail is auto-set.
4. **Editing** — user opens the fullscreen OWL editor. Trim/crop/filters
   accumulate as a JSON config. Hitting **Save & Render** defers
   `_job_render` to after-commit; that call invokes FFmpeg, then ffprobe to
   capture duration/resolution, and finally produces a low-bitrate preview.
5. **Prompt** — the prompt panel (or the dedicated wizard) attaches a prompt
   to the version.
6. **QC Pending** — `action_send_to_qc` flips the task and schedules an
   activity for the QC reviewer.
7. **QC** — reviewer opens the QC Review wizard (Approve / Reject / Rework).
   "Rework" auto-creates the next version so editing can resume immediately.
8. **Approved** → **Completed** — terminal state.

Steps 4-7 may repeat arbitrarily; each cycle is captured as a new
`video.task.version` row with its own prompt, QC verdict, edit history and
FFmpeg command.

## Installation

```bash
# 1. System dependencies
sudo apt-get install -y ffmpeg
pip install yt-dlp

# 2. Install the module
./odoo-bin -c odoo.conf -d ethara-dev -i instagram_video_qc_manager --stop-after-init
```

That's it — the module installs and runs on a vanilla Odoo 19 instance, with
only `base`, `mail` and `web` as dependencies.

Downloads and FFmpeg renders are scheduled as **after-commit callbacks**:
the HTTP request returns immediately and the heavy work runs in the same
worker process once the transaction commits. For higher throughput just
raise `--workers` in your odoo.conf.

## Configuration

| Setting                       | Default                          | Notes                                |
|-------------------------------|----------------------------------|--------------------------------------|
| FFmpeg binary                 | `ffmpeg` (on `$PATH`)            | Override via the `PATH` env var      |
| yt-dlp binary                 | `yt-dlp` (on `$PATH`)            | Same                                 |
| Download timeout              | 600 s                            | `instagram_downloader.py`            |
| Render timeout                | 1800 s                           | `ffmpeg_processor.py`                |
| Render preset / CRF           | medium / 22                      | Same                                 |
| Channel concurrency           | uses `queue_job` defaults        | Configure in *Queue Jobs* settings   |

## Security

| Group         | Permissions                                                                 |
|---------------|-----------------------------------------------------------------------------|
| Video User    | Create / read own & assigned tasks, read versions                          |
| Video Editor  | Implies User; full read/write on versions, edit history, processing logs   |
| QC Reviewer   | Implies User; read/write on versions (qc fields), wizard usage             |
| Manager       | Implies Editor + Reviewer; full delete privileges                          |

Record rules limit *Video User* to their own / assigned tasks; Editors,
Reviewers and Managers see everything.

## Running tests

```bash
./odoo-bin -c odoo.conf -d ethara-test -i instagram_video_qc_manager \
    --test-enable --test-tags=video_qc --stop-after-init
```

## Docker

The repo root `Dockerfile` already installs `ffmpeg` and `yt-dlp`. If you start
from a slimmer base, add:

```dockerfile
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir yt-dlp
```

## Optimisation notes

* **Streaming**: HTTP endpoints set `Accept-Ranges: bytes` so the OWL `<video>`
  tag seeks without downloading the full payload.
* **Preview render**: a 480p / CRF 30 copy is generated alongside the edited
  asset so the QC kanban and inline player stay snappy.
* **Filter preview**: the editor uses CSS `filter:` to mirror FFmpeg's `eq`
  filter — *no* in-browser transcoding required.
* **Non-blocking dispatch**: the after-commit callback frees the HTTP
  worker as soon as the transaction commits; add `queue_job` later if you
  need true cross-worker channels.
* **Reproducibility**: every render stores its exact FFmpeg command on the
  version (`ffmpeg_command`).

## Future-ready hooks

* Plug an AI service into `EditorPromptPanel.savePrompt` to populate
  `prompt_response` automatically.
* Swap `instagram.downloader` for a registry-based picker if you add TikTok
  or YouTube support — the controller doesn't care about the source.
* Replace the centred-crop helper with a Cropper.js component when you want
  free-form crop UX; the JSON config schema already accepts arbitrary
  `{x,y,w,h}`.
