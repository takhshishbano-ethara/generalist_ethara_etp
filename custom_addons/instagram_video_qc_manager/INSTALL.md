# Install / Configure

## 1. System packages

```bash
# Ubuntu/Debian
sudo apt-get update
sudo apt-get install -y ffmpeg

# Python deps (inside your Odoo venv)
pip install yt-dlp
```

Verify:
```bash
ffmpeg -version
ffprobe -version
yt-dlp --version
```

## 2. Odoo dependencies

The module depends only on **`base`, `mail`, `web`** — all shipped with
Odoo. Downloads and renders are scheduled via after-commit callbacks, so no
external job framework is needed.

## 3. Install

```bash
./odoo-bin -c odoo.conf -d <db> -i instagram_video_qc_manager --stop-after-init
```

## 4. Workers

Use the standard `--workers` flag — every after-commit callback runs in the
same worker that handled the originating request, freed up as soon as the
transaction commits.

```bash
./odoo-bin -c odoo.conf -d <db> --workers=4 --max-cron-threads=1
```

## 6. Permissions

Assign users to one of:

* **Video User** — create tasks
* **Video Editor** — edit videos
* **QC Reviewer** — approve/reject
* **Manager** — full control

## 7. Smoke test

1. Create a task and paste an Instagram reel URL.
2. Click **Download Videos** — the task should flip to `Queued → Running → Downloaded`.
3. Click **Open Editor** — the fullscreen OWL editor opens.
4. Drag the trim handles, press **Save & Render**, wait a few seconds and
   reload — the *Edited Preview* tab shows the rendered file.
5. Click **Send to QC** → the QC kanban shows the version.
6. Open the QC Review wizard, choose *Rework* with a comment → a new version
   is auto-created in editing.
