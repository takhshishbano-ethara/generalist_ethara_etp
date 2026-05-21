# -*- coding: utf-8 -*-
"""Main task record for an Instagram-video QC engagement.

A *video.task* is the parent container that owns:

* up to two Instagram source URLs (and the downloaded originals);
* an unbounded chain of *video.task.version* records, each one of which
  carries an edited rendition, an optional prompt and the QC verdict that
  was issued against it;
* a per-task edit-history timeline (denormalised across versions) and a
  processing-log audit trail used by the FFmpeg/queue layer.

The state machine is intentionally linear at the task level but the
*version* substream allows arbitrary edit/QC re-cycles - the task only
flips back to ``editing`` if QC marks the latest version as ``rework``.
"""

import logging
import re

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)


INSTAGRAM_URL_RE = re.compile(
    r"^https?://(www\.)?instagram\.com/(reel|p|tv|reels)/[\w\-]+/?",
    re.IGNORECASE,
)


class VideoTask(models.Model):
    _name = "video.task"
    _description = "Instagram Video QC Task"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "create_date desc, id desc"
    _rec_name = "name"

    # ------------------------------------------------------------------
    # Identification
    # ------------------------------------------------------------------
    name = fields.Char(
        string="Reference",
        required=True,
        copy=False,
        readonly=True,
        default=lambda self: _("New"),
        tracking=True,
    )
    description = fields.Text(string="Brief / Description", tracking=True)
    notes = fields.Html(string="Internal Notes")
    active = fields.Boolean(default=True)
    color = fields.Integer(string="Kanban Color")

    # Task-level prompt that lives on the task itself (independent of
    # the per-version ``prompt_text`` on video.task.version, which is the
    # iteration-specific prompt the editor saves alongside each render).
    # Used by the Original Videos tab in the main task form so the user
    # can capture the overall creative brief alongside the two previews.
    prompt_text = fields.Text(
        string="Prompt",
        tracking=True,
        help="Free-form prompt / brief captured at the task level. "
             "The editor still saves a per-version prompt on each version "
             "so iteration history is preserved.",
    )

    assigned_to = fields.Many2one(
        "res.users",
        string="Assigned Editor",
        default=lambda self: self.env.user,
        tracking=True,
    )
    qc_user_id = fields.Many2one(
        "res.users",
        string="QC Reviewer",
        tracking=True,
        domain=[("share", "=", False)],
    )

    # ------------------------------------------------------------------
    # State machine
    # ------------------------------------------------------------------
    state = fields.Selection(
        [
            ("draft", "Draft"),
            ("downloaded", "Downloaded"),
            ("editing", "Editing"),
            ("qc_pending", "QC Pending"),
            ("qc_approved", "QC Approved"),
            ("qc_rejected", "QC Rejected"),
            ("completed", "Completed"),
        ],
        default="draft",
        required=True,
        tracking=True,
        index=True,
    )

    # ------------------------------------------------------------------
    # Source videos (two slots, mirrored)
    # ------------------------------------------------------------------
    original_video_1_url = fields.Char(string="Instagram URL #1", tracking=True)
    original_video_2_url = fields.Char(string="Instagram URL #2", tracking=True)
    original_video_1_attachment = fields.Many2one(
        "ir.attachment",
        string="Original Video #1",
        ondelete="set null",
        domain=[("mimetype", "ilike", "video/")],
    )
    original_video_2_attachment = fields.Many2one(
        "ir.attachment",
        string="Original Video #2",
        ondelete="set null",
        domain=[("mimetype", "ilike", "video/")],
    )
    thumbnail = fields.Image(string="Thumbnail", max_width=1024, max_height=1024)

    download_status_1 = fields.Selection(
        [
            ("not_started", "Not Started"),
            ("queued", "Queued"),
            ("running", "Downloading"),
            ("done", "Downloaded"),
            ("error", "Error"),
        ],
        default="not_started",
        string="Download #1",
    )
    download_status_2 = fields.Selection(
        [
            ("not_started", "Not Started"),
            ("queued", "Queued"),
            ("running", "Downloading"),
            ("done", "Downloaded"),
            ("error", "Error"),
        ],
        default="not_started",
        string="Download #2",
    )
    download_error = fields.Text(string="Last Download Error", readonly=True)

    # ------------------------------------------------------------------
    # Relations - the version stream + audit
    # ------------------------------------------------------------------
    version_ids = fields.One2many(
        "video.task.version",
        "task_id",
        string="Versions",
    )
    # ---- View proxies ------------------------------------------------
    # The form view used to declare ``<field name="version_ids">`` three
    # separate times (Versions tab + Prompts tab + QC Review tab), each
    # with its own inline ``<list>`` arch. The OWL form renderer collapses
    # repeated x2many declarations to a single field state, so the column
    # sets from the other tabs would reference fields that aren't in the
    # active sub-record — which is exactly what made existing records fail
    # to open from list/kanban. These two proxy One2manys give the Prompts
    # and QC tabs their own field identity (same underlying data) so each
    # inline list survives independently.
    prompt_version_ids = fields.One2many(
        "video.task.version",
        "task_id",
        string="Version Prompts",
        help="View-only proxy of version_ids used by the Prompts tab.",
    )
    qc_version_ids = fields.One2many(
        "video.task.version",
        "task_id",
        string="Version QC Records",
        help="View-only proxy of version_ids used by the QC Review tab.",
    )
    # ``video.task.edit.history`` already exposes a *stored related* ``task_id``
    # (related to version_id.task_id) so a plain One2many works without a
    # compute method — the previous compute lacked @api.depends and could leave
    # the field in an inconsistent cache state, which made the form fail to
    # re-open existing records.
    edit_history_ids = fields.One2many(
        "video.task.edit.history",
        "task_id",
        string="Edit History",
    )
    processing_log_ids = fields.One2many(
        "video.task.processing.log",
        "task_id",
        string="Processing Logs",
    )

    # ------------------------------------------------------------------
    # Computed convenience fields
    # ------------------------------------------------------------------
    total_versions_count = fields.Integer(
        string="# Versions",
        compute="_compute_counts",
        store=True,
    )
    qc_count = fields.Integer(
        string="QC Reviews",
        compute="_compute_counts",
        store=True,
    )
    qc_approved_count = fields.Integer(
        string="Approved",
        compute="_compute_counts",
        store=True,
    )
    qc_rejected_count = fields.Integer(
        string="Rejected",
        compute="_compute_counts",
        store=True,
    )
    processing_log_count = fields.Integer(
        compute="_compute_counts",
        store=True,
    )
    latest_version_id = fields.Many2one(
        "video.task.version",
        string="Latest Version",
        compute="_compute_latest_version",
        store=True,
    )
    latest_qc_status = fields.Selection(
        [
            ("none", "No QC yet"),
            ("pending", "Pending"),
            ("approved", "Approved"),
            ("rejected", "Rejected"),
            ("rework", "Rework"),
        ],
        compute="_compute_latest_version",
        store=True,
    )

    # ------------------------------------------------------------------
    # Inline-player helpers
    # ------------------------------------------------------------------
    # These computed Char fields drive the ``widget="video_preview"``
    # field widget shipped in static/src/js/widgets/. They resolve to the
    # streaming URL of the corresponding source attachment, with the
    # latest version's trim window appended as a standard HTML5 media
    # fragment (``#t=start,end``) so the inline player only plays the
    # trimmed segment.
    original_video_1_play_url = fields.Char(
        string="Source #1 Stream URL",
        compute="_compute_play_urls",
    )
    original_video_2_play_url = fields.Char(
        string="Source #2 Stream URL",
        compute="_compute_play_urls",
    )
    latest_prompt_text = fields.Text(
        string="Latest Prompt",
        related="latest_version_id.prompt_text",
        readonly=True,
    )

    # ==================================================================
    # Compute
    # ==================================================================
    @api.depends(
        "original_video_1_attachment",
        "original_video_2_attachment",
        "latest_version_id.trim_1_start",
        "latest_version_id.trim_1_end",
        "latest_version_id.trim_2_start",
        "latest_version_id.trim_2_end",
        "latest_version_id.edited_file_1_path",
        "latest_version_id.edited_file_2_path",
        "latest_version_id.edited_attachment_1_id",
        "latest_version_id.edited_attachment_2_id",
    )
    def _compute_play_urls(self):
        for rec in self:
            ver = rec.latest_version_id
            rec.original_video_1_play_url = rec._slot_play_url(1, ver)
            rec.original_video_2_play_url = rec._slot_play_url(2, ver)

    def _slot_play_url(self, slot, ver):
        """Resolve the play URL for a source slot.

        Resolution order (matches video.task.version._slot_play_url):

        1. New on-disk render (``ver.edited_file_<slot>_path`` populated)
           -> ``/video_qc/version/<ver.id>/edited/<slot>``.
        2. Legacy attachment (``ver.edited_attachment_<slot>_id``) -> same URL.
        3. No render -> source clip + ``#t=start,end`` media fragment.
        """
        self.ensure_one()
        if not self.id:
            return ""
        if ver:
            if slot == 1:
                path_field = ver.edited_file_1_path
                legacy = ver.edited_attachment_1_id
            else:
                path_field = ver.edited_file_2_path
                legacy = ver.edited_attachment_2_id
            if path_field or legacy:
                return f"/video_qc/version/{ver.id}/edited/{slot}"
        # Fallback: stream the source attachment with a media fragment.
        source = self.original_video_1_attachment if slot == 1 else self.original_video_2_attachment
        if not source:
            return ""
        start = 0.0
        end = 0.0
        if ver:
            if slot == 1:
                start = float(ver.trim_1_start or 0.0)
                end = float(ver.trim_1_end or 0.0)
            else:
                start = float(ver.trim_2_start or 0.0)
                end = float(ver.trim_2_end or 0.0)
        frag = ""
        if end and end > start:
            frag = f"#t={start:.3f},{end:.3f}"
        elif start:
            frag = f"#t={start:.3f}"
        return f"/video_qc/task/{self.id}/original/{slot}{frag}"

    @api.depends("version_ids", "version_ids.qc_status")
    def _compute_counts(self):
        for rec in self:
            versions = rec.version_ids
            rec.total_versions_count = len(versions)
            rec.qc_count = len(versions.filtered(lambda v: v.qc_status and v.qc_status != "pending"))
            rec.qc_approved_count = len(versions.filtered(lambda v: v.qc_status == "approved"))
            rec.qc_rejected_count = len(versions.filtered(lambda v: v.qc_status == "rejected"))
            rec.processing_log_count = len(rec.processing_log_ids)

    @api.depends("version_ids", "version_ids.version_no", "version_ids.qc_status", "version_ids.is_latest")
    def _compute_latest_version(self):
        for rec in self:
            latest = rec.version_ids.sorted("version_no", reverse=True)[:1]
            rec.latest_version_id = latest
            rec.latest_qc_status = latest.qc_status or "none" if latest else "none"

    # ==================================================================
    # Constraints
    # ==================================================================
    @api.constrains("original_video_1_url", "original_video_2_url")
    def _check_instagram_urls(self):
        for rec in self:
            for url in (rec.original_video_1_url, rec.original_video_2_url):
                if url and not INSTAGRAM_URL_RE.match(url.strip()):
                    raise ValidationError(
                        _("'%s' does not look like a public Instagram reel/post URL.", url)
                    )

    # ==================================================================
    # ORM overrides
    # ==================================================================
    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get("name") or vals["name"] == _("New"):
                vals["name"] = self.env["ir.sequence"].next_by_code("video.task") or _("New")
        records = super().create(vals_list)
        for rec in records:
            rec.message_subscribe(partner_ids=rec.assigned_to.partner_id.ids)
        return records

    def copy(self, default=None):
        default = dict(default or {})
        default.setdefault("name", _("New"))
        default.setdefault("state", "draft")
        default.setdefault("version_ids", [])
        return super().copy(default)

    def unlink(self):
        """Delete the task row, then wipe its on-disk media directory.

        We snapshot the directories that need cleaning BEFORE
        ``super().unlink()`` (so we still have the task ids) but only
        ``shutil.rmtree`` them AFTER super returns successfully — that
        way a constraint failure or access-rule rejection leaves the
        media in place.  We swallow rmtree errors via ``ignore_errors``
        plus a wrapping try/except: a stale media dir must never block
        a DB delete (operator can purge later via the orphan-cron).
        """
        import os
        import shutil

        storage = self.env["video.qc.media.storage"].sudo()
        dirs_to_purge = []
        for task in self:
            if task.id:
                try:
                    dirs_to_purge.append(storage.task_dir(task))
                except Exception:  # noqa: BLE001 — never block unlink on path math
                    _logger.warning(
                        "Could not resolve media dir for task %s; skipping purge.",
                        task.id,
                    )
        ok = super().unlink()
        if ok:
            for path in dirs_to_purge:
                try:
                    if os.path.isdir(path):
                        shutil.rmtree(path, ignore_errors=True)
                except Exception:  # noqa: BLE001
                    _logger.warning(
                        "Could not remove media dir %s after task delete.", path,
                    )
        return ok

    # ==================================================================
    # Orphan-media housekeeping
    # ==================================================================
    @api.model
    def cron_purge_orphan_media_dirs(self):
        """Walk ``<media_root>/`` and remove subdirs with no matching task.

        Intended to run weekly (disabled by default — see
        ``data/cron.xml``).  Subdir names are integers (task ids);
        anything we can't parse as an int is left alone.  Anything
        whose id no longer exists in ``video.task`` is removed.
        """
        import os
        import shutil

        storage = self.env["video.qc.media.storage"].sudo()
        root = storage.get_media_root()
        if not os.path.isdir(root):
            return 0
        try:
            entries = os.listdir(root)
        except OSError as exc:
            _logger.warning("Could not list media root %s: %s", root, exc)
            return 0
        live_ids = set(self.with_context(active_test=False).search([]).ids)
        removed = 0
        for entry in entries:
            full = os.path.join(root, entry)
            if not os.path.isdir(full):
                continue
            try:
                entry_id = int(entry)
            except ValueError:
                continue  # unrelated dir — leave it alone
            if entry_id in live_ids:
                continue
            try:
                shutil.rmtree(full, ignore_errors=True)
                removed += 1
                _logger.info("Purged orphan media dir %s", full)
            except Exception:  # noqa: BLE001
                _logger.warning("Could not purge orphan media dir %s", full)
        return removed

    # ==================================================================
    # State transitions
    # ==================================================================
    def action_set_draft(self):
        for rec in self:
            rec.state = "draft"
        return True

    def action_mark_downloaded(self):
        for rec in self:
            if not (rec.original_video_1_attachment or rec.original_video_2_attachment):
                raise UserError(_("Cannot mark as downloaded without any original attachment."))
            rec.state = "downloaded"
        return True

    def action_start_editing(self):
        for rec in self:
            if rec.state == "draft":
                raise UserError(_("Download the original videos first."))
            rec.state = "editing"
        return True

    def action_send_to_qc(self):
        for rec in self:
            if not rec.latest_version_id:
                raise UserError(_("There is no version to send to QC."))
            rec.latest_version_id.qc_status = "pending"
            rec.state = "qc_pending"
            # Schedule a QC activity for the reviewer
            if rec.qc_user_id:
                rec.activity_schedule(
                    "mail.mail_activity_data_todo",
                    summary=_("QC review for %s", rec.name),
                    user_id=rec.qc_user_id.id,
                )
        return True

    def action_complete(self):
        for rec in self:
            if rec.latest_qc_status != "approved":
                raise UserError(_("Only approved tasks can be completed."))
            rec.state = "completed"
        return True

    def action_qc_placeholder(self):
        """Stub action used by the dashboard form's "QC" button.

        Intentionally empty — wired up so the button is clickable but no
        side-effect happens yet. Real QC logic will be implemented in a
        follow-up.
        """
        self.ensure_one()
        return True

    # ==================================================================
    # Smart-button actions
    # ==================================================================
    def action_view_versions(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Versions"),
            "res_model": "video.task.version",
            "view_mode": "list,form,kanban",
            "domain": [("task_id", "=", self.id)],
            "context": {"default_task_id": self.id},
        }

    def action_view_qc(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("QC History"),
            "res_model": "video.task.version",
            "view_mode": "list,form",
            "domain": [("task_id", "=", self.id), ("qc_status", "!=", False)],
        }

    def action_view_processing_logs(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Processing Logs"),
            "res_model": "video.task.processing.log",
            "view_mode": "list,form",
            "domain": [("task_id", "=", self.id)],
        }

    def action_open_editor(self):
        """Launch the OWL fullscreen video editor for the latest version."""
        self.ensure_one()
        if not (self.original_video_1_attachment or self.original_video_2_attachment):
            raise UserError(_("Download an original video before opening the editor."))
        return self._open_editor_action(source_kind=None)

    def action_open_editor_slot_1(self):
        """Launch the editor with Original #1 pre-selected as the working source."""
        self.ensure_one()
        if not self.original_video_1_attachment:
            raise UserError(_("Original video #1 is not downloaded yet."))
        return self._open_editor_action(source_kind="original_1")

    def action_open_editor_slot_2(self):
        """Launch the editor with Original #2 pre-selected as the working source."""
        self.ensure_one()
        if not self.original_video_2_attachment:
            raise UserError(_("Original video #2 is not downloaded yet."))
        return self._open_editor_action(source_kind="original_2")

    def _open_editor_action(self, source_kind=None):
        params = {
            "task_id": self.id,
            "version_id": self.latest_version_id.id or False,
        }
        if source_kind:
            params["source_kind"] = source_kind
        return {
            "type": "ir.actions.client",
            "tag": "instagram_video_qc_manager.video_editor",
            "name": _("Video Editor — %s") % self.name,
            "params": params,
        }

    # ==================================================================
    # Download orchestration
    # ==================================================================
    def action_download_videos(self):
        """Schedule downloads as after-commit callbacks.

        The HTTP request returns immediately; the actual yt-dlp call runs
        once the current transaction is committed (same worker process,
        fresh cursor).
        """
        for rec in self:
            if not (rec.original_video_1_url or rec.original_video_2_url):
                raise UserError(_("Provide at least one Instagram URL first."))
            for slot, url in [(1, rec.original_video_1_url), (2, rec.original_video_2_url)]:
                if not url:
                    continue
                rec.write({f"download_status_{slot}": "queued"})
                rec._defer("_job_download_video", args=(slot, url))
        return True

    # ------------------------------------------------------------------
    # After-commit dispatcher
    # ------------------------------------------------------------------
    def _defer(self, method, args=(), kwargs=None):
        """Run *method* after the current transaction commits.

        Re-browses the record in a fresh environment so the callback is
        isolated from the caller's cursor.
        """
        self.ensure_one()
        kwargs = kwargs or {}
        db = self.env.cr.dbname
        uid = self.env.uid
        ctx = dict(self.env.context)
        rec_id = self.id
        model = self._name

        def _run():
            from odoo import api
            from odoo.orm.registry import Registry
            with Registry(db).cursor() as new_cr:
                env = api.Environment(new_cr, uid, ctx)
                rec = env[model].browse(rec_id).exists()
                if rec:
                    try:
                        getattr(rec, method)(*args, **kwargs)
                    except Exception:  # noqa: BLE001
                        _logger.exception("Deferred %s.%s failed", model, method)
                        new_cr.rollback()

        self.env.cr.postcommit.add(_run)
        return True

    def _job_download_video(self, slot, url):
        """Deferred entry point - invoked from the after-commit hook."""
        self.ensure_one()
        self.write({f"download_status_{slot}": "running"})
        try:
            downloader = self.env["instagram.downloader"]
            attachment, thumbnail = downloader.download_to_attachment(self, url, slot=slot)
            vals = {f"download_status_{slot}": "done", f"original_video_{slot}_attachment": attachment.id}
            if thumbnail and not self.thumbnail:
                vals["thumbnail"] = thumbnail
            self.write(vals)
            if all(
                getattr(self, f"download_status_{i}", "not_started") in ("done", "not_started")
                for i in (1, 2)
            ) and (self.original_video_1_attachment or self.original_video_2_attachment):
                self.write({"state": "downloaded"})
            self.env["video.task.processing.log"].sudo().create(
                {
                    "task_id": self.id,
                    "level": "info",
                    "operation": "download",
                    "message": _("Downloaded slot %s from %s") % (slot, url),
                }
            )
        except Exception as exc:  # noqa: BLE001
            _logger.exception("Instagram download failed for task %s slot %s", self.name, slot)
            self.write(
                {
                    f"download_status_{slot}": "error",
                    "download_error": str(exc),
                }
            )
            self.env["video.task.processing.log"].sudo().create(
                {
                    "task_id": self.id,
                    "level": "error",
                    "operation": "download",
                    "message": _("Failed to download %s: %s") % (url, exc),
                }
            )
            raise

    # ==================================================================
    # Version helpers
    # ==================================================================
    def create_new_version(self, vals=None):
        """Create a brand new version based on the latest one (or the original)."""
        self.ensure_one()
        Version = self.env["video.task.version"]
        last = self.latest_version_id
        next_no = (last.version_no if last else 0) + 1
        base_vals = {
            "task_id": self.id,
            "version_no": next_no,
            "original_attachment_id": (
                last.edited_attachment_id.id
                if last and last.edited_attachment_id
                else (self.original_video_1_attachment.id or self.original_video_2_attachment.id)
            ),
        }
        if vals:
            base_vals.update(vals)
        # Demote previous "latest" flags
        self.version_ids.write({"is_latest": False})
        version = Version.create(base_vals)
        version.is_latest = True
        return version
