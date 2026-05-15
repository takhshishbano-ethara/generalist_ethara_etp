# Install / Configure

## 0. Media root

The module writes FFmpeg outputs to a local directory tree rooted at
`<media_root>/<task.id>/<filename>.mp4`.

**Default** (no setup needed for dev installs): the module uses
`<odoo data_dir>/video_qc_media/`. Odoo creates and owns its
`data_dir` on startup (typically `~/.local/share/Odoo/` on Linux/macOS),
so the path is writable out of the box without `sudo`.

**Production override**: point the path at a dedicated dir (e.g. an
NFS / EFS mount that all workers can reach) via the system parameter
`video_qc.media_root`:

```bash
# Settings → Technical → Parameters → System Parameters,
# or via the shell:
./odoo-bin shell -d <db>
>>> env["ir.config_parameter"].sudo().set_param(
...     "video_qc.media_root", "/srv/video_qc_media"
... )
```

For a hardened deploy:

```bash
sudo mkdir -p /srv/video_qc_media
sudo chown odoo:odoo /srv/video_qc_media
sudo chmod 750 /srv/video_qc_media
```

**Self-healing fallback**: if the configured path is unwritable
(e.g. you set `/var/lib/odoo/...` but Odoo runs as your user), the
service auto-falls-back to `<data_dir>/video_qc_media` and rewrites
the system parameter to the working path. A `WARNING video_qc media
root ... is not writable; auto-switched ...` line appears in the
log.

**Backup implications.** These video files live on the **filesystem**, not in
PostgreSQL — `pg_dump` will not include them. Schedule a separate `rsync` /
`borg` / `restic` job over `<media_root>/` if you need offsite copies.

**Multi-worker note.** Each Odoo worker writes to the same shared
`<media_root>` — point the path at a path that all workers (and any
follower replicas) can reach (e.g. an NFS / EFS mount in production).

## 1. System packages

```bash
# Ubuntu/Debian
sudo apt-get update
sudo apt-get install -y ffmpeg

# macOS
brew install ffmpeg

# Python deps (inside your Odoo venv)
pip install yt-dlp
```

Verify:
```bash
ffmpeg -version
ffprobe -version
yt-dlp --version
```

**If Odoo can't find ffmpeg on its PATH** (very common on macOS when
Odoo is launched from a venv that doesn't inherit your shell's PATH),
the processor will *also* search:

* `/opt/homebrew/bin/`  (Apple Silicon Homebrew)
* `/usr/local/bin/`  (Intel Homebrew, generic Linux)
* `/opt/local/bin/`  (MacPorts)
* `/usr/bin/`, `/bin/`  (Linux system packages)

If your install is somewhere else (e.g. `/home/snap/ffmpeg-current/`),
set an absolute path manually:

```
# Settings → Technical → Parameters → System Parameters
video_qc.ffmpeg_path   = /full/path/to/ffmpeg
video_qc.ffprobe_path  = /full/path/to/ffprobe
```

A missing binary is now caught **synchronously** in the Save & Render
RPC, so the OWL editor shows a red notification with the exact install
instructions instead of silently writing `status=error` to the
version row.

## 2. Odoo dependencies

The module depends only on **`base`, `mail`, `web`** — all shipped with
Odoo. Downloads and renders are scheduled via after-commit callbacks, so no
external job framework is needed.

## 3. Install / Upgrade

```bash
# Fresh install
./odoo-bin -c odoo.conf -d <db> -i instagram_video_qc_manager --stop-after-init

# Upgrade from a pre-on-disk-refactor build
./odoo-bin -c odoo.conf -d <db> -u instagram_video_qc_manager --stop-after-init
```

`-i` and `-u` both fire the `post_init_hook` defined in `hooks.py`, which
**migrates legacy ir.attachment-backed renders** to the new on-disk
layout under `<media_root>/<task.id>/`. The migration is:

* **Idempotent** — re-running is safe; already-migrated rows are skipped.
* **Non-destructive** — the original `ir.attachment` rows are NOT
  deleted. After you've verified the move on a representative sample,
  you can purge them by hand. The HTTP streaming controller falls back
  to legacy attachments automatically for any row the migration missed.

Progress is logged every 100 versions (`grep video_qc your-odoo.log`).

## 4. Workers

Use the standard `--workers` flag — every after-commit callback runs in the
same worker that handled the originating request, freed up as soon as the
transaction commits.

```bash
./odoo-bin -c odoo.conf -d <db> --workers=4 --max-cron-threads=1
```

## 5. Kimi K2.5 grammar check (QC wizard)

The QC review wizard offers a **Check Grammar (Kimi K2.5)** button
that sends the version's prompt and the reviewer's suggested
next-prompt to Moonshot AI for grading, then blocks **Approve**
when the score is below a configurable threshold.

Configure via Settings → Technical → Parameters → System Parameters:

| Parameter | Default | Notes |
|---|---|---|
| `video_qc.kimi_api_key` | *(empty — required)* | Your Moonshot AI key |
| `video_qc.kimi_endpoint` | `https://api.moonshot.ai/v1/chat/completions` | Use `https://api.moonshot.cn/...` for the China region |
| `video_qc.kimi_model` | `kimi-k2-0905-preview` | Any K2 / K2.5 / moonshot-v1 variant |
| `video_qc.grammar_score_threshold` | `70` | Approve is blocked below this score (0-100) |

Without `video_qc.kimi_api_key` the **Check Grammar** button raises
a clean `UserError` telling the reviewer how to configure it.
Approve is gated on the grammar check having been run AND on the
score being at-or-above the threshold; Reject / Request Rework
remain available regardless.

The wizard renders two side-by-side panels — one for the version's
existing prompt, one for the reviewer's `Suggested Next Prompt` —
each showing the score badge, summary, issue list, and Kimi's
corrected rewrite.

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
