# -*- coding: utf-8 -*-
"""A single edited rendition of a video.task, plus its prompt and QC verdict."""

import json
import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class VideoTaskVersion(models.Model):
    _name = "video.task.version"
    _description = "Video Task Version"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "task_id, version_no desc"
    _rec_name = "display_name"

    task_id = fields.Many2one(
        "video.task",
        string="Task",
        ondelete="cascade",
        required=True,
        index=True,
    )
    version_no = fields.Integer(string="Version #", required=True, default=1)
    display_name = fields.Char(compute="_compute_display_name", store=True)
    is_latest = fields.Boolean(string="Latest", default=False, index=True)

    # ------------------------------------------------------------------
    # Attachments
    # ------------------------------------------------------------------
    original_attachment_id = fields.Many2one(
        "ir.attachment",
        string="Source Attachment",
        ondelete="restrict",
    )
    edited_attachment_id = fields.Many2one(
        "ir.attachment",
        string="Edited Output",
        ondelete="set null",
    )
    preview_attachment_id = fields.Many2one(
        "ir.attachment",
        string="Preview Render",
        ondelete="set null",
    )

    # ------------------------------------------------------------------
    # Editing configuration
    # ------------------------------------------------------------------
    # Legacy single-trim fields — kept around so existing controllers,
    # views, and serialized editing_json from older versions still work.
    # On save these are mirrored to slot 1.
    trim_start = fields.Float(string="Trim Start (s)", default=0.0)
    trim_end = fields.Float(string="Trim End (s)", default=0.0)
    crop_data_json = fields.Char(string="Crop (JSON)")

    # ---- Per-slot fields ---------------------------------------------
    # A single version now owns a trim window AND an edited output for
    # BOTH source slots, with one shared prompt.  This matches the QC
    # workflow: each "version" is one iteration that produces a trimmed
    # rendition for source #1 and a trimmed rendition for source #2,
    # rejected as a unit by QC and superseded by the next version.
    trim_1_start = fields.Float(string="Source #1 Trim Start (s)", default=0.0)
    trim_1_end = fields.Float(string="Source #1 Trim End (s)", default=0.0)
    trim_2_start = fields.Float(string="Source #2 Trim Start (s)", default=0.0)
    trim_2_end = fields.Float(string="Source #2 Trim End (s)", default=0.0)
    crop_1_data_json = fields.Char(string="Source #1 Crop (JSON)")
    crop_2_data_json = fields.Char(string="Source #2 Crop (JSON)")
    edited_attachment_1_id = fields.Many2one(
        "ir.attachment",
        string="Trimmed Source #1",
        ondelete="set null",
        help="FFmpeg-rendered trimmed clip for source slot #1.",
    )
    edited_attachment_2_id = fields.Many2one(
        "ir.attachment",
        string="Trimmed Source #2",
        ondelete="set null",
        help="FFmpeg-rendered trimmed clip for source slot #2.",
    )

    resolution = fields.Char(string="Resolution")
    duration = fields.Float(string="Duration (s)")
    edit_notes = fields.Text(string="Editing Notes")
    editing_json = fields.Text(
        string="Editing Config (JSON)",
        help="Free-form JSON capturing every operation that was applied. "
             "Modern shape: ``{slot_1: {...}, slot_2: {...}, ...shared}``; "
             "the legacy flat shape ``{trim, crop, ...}`` is still "
             "accepted and treated as slot 1.",
    )
    ffmpeg_command = fields.Text(
        string="Last FFmpeg Command",
        readonly=True,
        help="Stored verbatim for reproducibility.",
    )

    # ------------------------------------------------------------------
    # Authorship / lifecycle
    # ------------------------------------------------------------------
    created_by = fields.Many2one(
        "res.users",
        string="Editor",
        default=lambda self: self.env.user,
        readonly=True,
    )
    created_on = fields.Datetime(default=fields.Datetime.now, readonly=True)
    status = fields.Selection(
        [
            ("draft", "Draft"),
            ("processing", "Processing"),
            ("rendered", "Rendered"),
            ("error", "Error"),
        ],
        default="draft",
        tracking=True,
    )
    processing_error = fields.Text(readonly=True)

    # ------------------------------------------------------------------
    # Prompt + QC
    # ------------------------------------------------------------------
    prompt_text = fields.Text(string="Prompt")
    prompt_response = fields.Text(string="AI Response / Notes")

    qc_status = fields.Selection(
        [
            ("pending", "Pending"),
            ("approved", "Approved"),
            ("rejected", "Rejected"),
            ("rework", "Rework"),
        ],
        string="QC Status",
        tracking=True,
    )
    qc_comment = fields.Text(string="QC Comment")
    qc_user = fields.Many2one("res.users", string="QC Reviewer")
    qc_date = fields.Datetime(string="QC Date")

    edit_history_ids = fields.One2many(
        "video.task.edit.history",
        "version_id",
        string="Edit Actions",
    )

    # ------------------------------------------------------------------
    # Inline-player URL (powers the video_preview widget on this model's
    # form view + on the dashboard task form).
    #
    # If the version has already been rendered we return the streaming
    # endpoint for the *edited* attachment — that file already contains
    # the trim (and crop, and any filters), so no media fragment is
    # appended.  Otherwise we fall back to the source attachment with a
    # ``#t=start,end`` HTML5 media fragment so the user can still
    # preview what the trimmed clip will look like before they hit
    # Save & Render in the editor.
    # ------------------------------------------------------------------
    edited_play_url = fields.Char(
        string="Play URL",
        compute="_compute_edited_play_url",
    )
    # Per-slot streaming URLs.  Each plays the rendered slot attachment
    # if it exists; otherwise falls back to the task's source attachment
    # for that slot with a ``#t=start,end`` media fragment so the user
    # can still preview the *intended* trim before Save & Render.
    edited_1_play_url = fields.Char(
        string="Slot #1 Play URL",
        compute="_compute_slot_play_urls",
    )
    edited_2_play_url = fields.Char(
        string="Slot #2 Play URL",
        compute="_compute_slot_play_urls",
    )

    @api.depends(
        "edited_attachment_id",
        "original_attachment_id",
        "trim_start",
        "trim_end",
    )
    def _compute_edited_play_url(self):
        for rec in self:
            if not rec.id:
                rec.edited_play_url = ""
                continue
            if rec.edited_attachment_id:
                rec.edited_play_url = f"/video_qc/version/{rec.id}/edited"
                continue
            if rec.original_attachment_id:
                frag = ""
                start = float(rec.trim_start or 0.0)
                end = float(rec.trim_end or 0.0)
                if end and end > start:
                    frag = f"#t={start:.3f},{end:.3f}"
                elif start:
                    frag = f"#t={start:.3f}"
                rec.edited_play_url = f"/video_qc/version/{rec.id}/source{frag}"
                continue
            rec.edited_play_url = ""

    @api.depends(
        "edited_attachment_1_id",
        "edited_attachment_2_id",
        "trim_1_start", "trim_1_end",
        "trim_2_start", "trim_2_end",
        "task_id.original_video_1_attachment",
        "task_id.original_video_2_attachment",
    )
    def _compute_slot_play_urls(self):
        for rec in self:
            rec.edited_1_play_url = rec._slot_play_url(1)
            rec.edited_2_play_url = rec._slot_play_url(2)

    def _slot_play_url(self, slot):
        self.ensure_one()
        if not self.id:
            return ""
        edited = self.edited_attachment_1_id if slot == 1 else self.edited_attachment_2_id
        if edited:
            return f"/video_qc/version/{self.id}/edited/{slot}"
        # Fall back to the task's source attachment with a media
        # fragment so the user still sees their intended trim window.
        task = self.task_id
        if slot == 1:
            src = task.original_video_1_attachment
            start = float(self.trim_1_start or 0.0)
            end = float(self.trim_1_end or 0.0)
        else:
            src = task.original_video_2_attachment
            start = float(self.trim_2_start or 0.0)
            end = float(self.trim_2_end or 0.0)
        if not src:
            return ""
        frag = ""
        if end and end > start:
            frag = f"#t={start:.3f},{end:.3f}"
        elif start:
            frag = f"#t={start:.3f}"
        return f"/video_qc/task/{task.id}/original/{slot}{frag}"

    # ==================================================================
    # Compute / constraints
    # ==================================================================
    @api.depends("task_id.name", "version_no")
    def _compute_display_name(self):
        for rec in self:
            rec.display_name = f"{rec.task_id.name or '-'} / v{rec.version_no}"

    _sql_constraints = [
        (
            "uniq_task_version",
            "UNIQUE(task_id, version_no)",
            "Version number must be unique within a task.",
        )
    ]

    # ==================================================================
    # ORM
    # ==================================================================
    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        for rec in records:
            rec.message_post(body=_("Version v%s created.") % rec.version_no)
        return records

    # ==================================================================
    # Editing entry-points
    # ==================================================================
    def write_editing_config(self, config):
        """Persist a structured editing configuration coming from the
        OWL editor.

        Two shapes are accepted:

        * **Per-slot (modern)** — ``{"slot_1": {trim, crop, ...},
          "slot_2": {trim, crop, ...}, ...shared}``.  Each slot's trim
          and crop are mirrored onto dedicated columns so list/kanban
          views can read them cheaply.
        * **Flat (legacy)** — ``{"trim": {...}, "crop": {...}, ...}``,
          treated as slot 1 only.

        The full payload is always stored verbatim in ``editing_json``
        so the FFmpeg processor can rebuild the exact command later.
        """
        self.ensure_one()
        if not isinstance(config, dict):
            raise UserError(_("Editing configuration must be a dictionary."))
        self.editing_json = json.dumps(config)

        s1 = config.get("slot_1")
        s2 = config.get("slot_2")
        # Legacy fallback: top-level trim/crop = slot 1.
        if s1 is None and "trim" in config:
            s1 = {"trim": config.get("trim"), "crop": config.get("crop")}

        if s1 is not None:
            trim_1 = (s1 or {}).get("trim") or {}
            self.trim_1_start = trim_1.get("start", 0.0) or 0.0
            self.trim_1_end = trim_1.get("end", 0.0) or 0.0
            crop_1 = (s1 or {}).get("crop")
            self.crop_1_data_json = json.dumps(crop_1) if crop_1 else False
            # Legacy mirror so existing list views keep showing the
            # primary trim window.
            self.trim_start = self.trim_1_start
            self.trim_end = self.trim_1_end
            if crop_1:
                self.crop_data_json = json.dumps(crop_1)

        if s2 is not None:
            trim_2 = (s2 or {}).get("trim") or {}
            self.trim_2_start = trim_2.get("start", 0.0) or 0.0
            self.trim_2_end = trim_2.get("end", 0.0) or 0.0
            crop_2 = (s2 or {}).get("crop")
            self.crop_2_data_json = json.dumps(crop_2) if crop_2 else False

        return True

    def action_render(self):
        """Schedule the FFmpeg render as an after-commit callback.

        The old guard raised ``UserError("This version is already being
        rendered.")`` if ``status == 'processing'``.  In practice that
        wedged the editor whenever the previous post-commit job had
        crashed silently (leaving the row stuck on ``processing``) or
        when the user legitimately wanted to retry a still-running
        render after tweaking the config.

        Every Save & Render now treats the click as a fresh attempt:
        the row is reset to ``processing`` with any previous error
        cleared, and a brand-new deferred job is enqueued.  The job
        opens its own cursor and tempdir, so overlapping schedules
        don't collide — the last writer wins, which matches user
        intent.
        """
        for rec in self:
            rec.write({
                "status": "processing",
                "processing_error": False,
            })
            rec._defer_render()
        return True

    def _defer_render(self):
        """Run ``_job_render`` once the current transaction commits."""
        self.ensure_one()
        db = self.env.cr.dbname
        uid = self.env.uid
        ctx = dict(self.env.context)
        rec_id = self.id

        def _run():
            # NOTE: ``from odoo import registry`` was removed in Odoo 19;
            # the canonical location is now ``odoo.orm.registry.Registry``
            # (matching the pattern used in ``video_task.py``).
            from odoo import api
            from odoo.orm.registry import Registry
            with Registry(db).cursor() as new_cr:
                env = api.Environment(new_cr, uid, ctx)
                rec = env["video.task.version"].browse(rec_id).exists()
                if rec:
                    try:
                        rec._job_render()
                    except Exception:  # noqa: BLE001
                        _logger.exception("Deferred render failed for version %s", rec_id)
                        new_cr.rollback()

        self.env.cr.postcommit.add(_run)
        return True

    @staticmethod
    def _slot_should_render(slot_cfg):
        """Decide whether a slot's config warrants invoking FFmpeg.

        We render a slot only when the user has DONE SOMETHING to it:

        * a meaningful trim window (``end > start``), or
        * an explicit crop rectangle (``x/y/w/h`` all set).

        Otherwise we skip the slot.  The previous behaviour was to
        always run FFmpeg on every slot whose source attachment
        existed, which silently produced a full-source re-encode for
        any slot the user hadn't touched — that's why "saving a
        trimmed video then playing it back showed the full clip":
        the untouched slot's `edited_attachment_*_id` was the full
        re-encoded source.

        Skipped slots leave `edited_attachment_*_id` empty, so the
        slot's play URL falls back to the source-with-media-fragment
        view — same content the user would have seen pre-render.
        """
        if not slot_cfg:
            return False
        trim = slot_cfg.get("trim") or {}
        start = float(trim.get("start") or 0)
        end = float(trim.get("end") or 0)
        if end > start:
            return True
        crop = slot_cfg.get("crop")
        if crop and all(k in (crop or {}) for k in ("x", "y", "w", "h")):
            return True
        return False

    def _job_render(self):
        """Render ONLY the slots the user has actually touched.

        For each slot whose source attachment exists AND whose config
        has a meaningful trim or crop, FFmpeg is invoked with that
        slot's per-slot config and the result is stored on the
        corresponding ``edited_attachment_<n>_id``.  The legacy
        ``edited_attachment_id`` mirrors slot #1's output (or slot
        #2's if only that exists) for backwards compat with existing
        controllers and views.
        """
        self.ensure_one()
        try:
            config = json.loads(self.editing_json or "{}")
            # Build per-slot configs, falling back to a flat legacy
            # payload (= slot 1).
            slot_1_cfg = config.get("slot_1")
            slot_2_cfg = config.get("slot_2")
            if slot_1_cfg is None and ("trim" in config or "crop" in config):
                slot_1_cfg = {
                    "trim": config.get("trim"),
                    "crop": config.get("crop"),
                }
            # Inherit "shared" filters from the top-level payload so the
            # user doesn't have to duplicate brightness/contrast/rotate
            # per slot.
            shared = {
                k: v for k, v in config.items()
                if k not in ("slot_1", "slot_2", "trim", "crop")
            }
            if slot_1_cfg:
                slot_1_cfg = {**shared, **slot_1_cfg}
            if slot_2_cfg:
                slot_2_cfg = {**shared, **slot_2_cfg}

            processor = self.env["ffmpeg.processor"]
            task = self.task_id
            vals = {"status": "rendered"}
            last_cmd = False
            last_probe = None

            render_slot_1 = (
                task.original_video_1_attachment
                and self._slot_should_render(slot_1_cfg)
            )
            render_slot_2 = (
                task.original_video_2_attachment
                and self._slot_should_render(slot_2_cfg)
            )

            if render_slot_1:
                edited, cmd, probe = processor.render_for_attachment(
                    self, task.original_video_1_attachment, slot_1_cfg, slot=1,
                )
                vals["edited_attachment_1_id"] = edited.id
                last_cmd, last_probe = cmd, probe
            else:
                # Clear any stale render from a previous iteration so the
                # form's preview correctly falls back to the source view.
                vals["edited_attachment_1_id"] = False

            if render_slot_2:
                edited, cmd, probe = processor.render_for_attachment(
                    self, task.original_video_2_attachment, slot_2_cfg, slot=2,
                )
                vals["edited_attachment_2_id"] = edited.id
                last_cmd, last_probe = cmd, probe
            else:
                vals["edited_attachment_2_id"] = False

            # Back-compat: ``edited_attachment_id`` and ``ffmpeg_command``
            # still point at *something* for downstream code.
            primary = vals.get("edited_attachment_1_id") or vals.get("edited_attachment_2_id")
            vals["edited_attachment_id"] = primary or False
            if last_cmd:
                vals["ffmpeg_command"] = last_cmd
            if last_probe:
                vals["duration"] = last_probe.get("duration") or self.duration
                vals["resolution"] = last_probe.get("resolution") or self.resolution

            if not render_slot_1 and not render_slot_2:
                raise UserError(_(
                    "Nothing to render: trim window is empty and no crop is "
                    "set on either slot. Drag the bottom strip's trim handles "
                    "(or draw a crop rectangle) on the slot you want to render."
                ))

            self.write(vals)
            self.task_id.message_post(
                body=_("Version v%s rendered (slot 1: %s, slot 2: %s).") % (
                    self.version_no,
                    "ok" if render_slot_1 else "skipped (no trim/crop)",
                    "ok" if render_slot_2 else "skipped (no trim/crop)",
                )
            )
        except Exception as exc:  # noqa: BLE001
            _logger.exception("Render failed for %s", self.display_name)
            self.write({"status": "error", "processing_error": str(exc)})
            self.env["video.task.processing.log"].sudo().create(
                {
                    "task_id": self.task_id.id,
                    "version_id": self.id,
                    "level": "error",
                    "operation": "render",
                    "message": str(exc),
                }
            )
            raise

    # ==================================================================
    # QC entry-points
    # ==================================================================
    def action_qc_approve(self, comment=None):
        for rec in self:
            rec._record_qc("approved", comment)
            rec.task_id.state = "qc_approved"
        return True

    def action_qc_reject(self, comment=None):
        for rec in self:
            rec._record_qc("rejected", comment)
            rec.task_id.state = "qc_rejected"
        return True

    def action_qc_rework(self, comment=None):
        """Mark for rework and flip the parent task back into editing."""
        for rec in self:
            rec._record_qc("rework", comment)
            rec.task_id.state = "editing"
        return True

    def _record_qc(self, status, comment=None):
        self.ensure_one()
        self.write(
            {
                "qc_status": status,
                "qc_comment": comment or self.qc_comment,
                "qc_user": self.env.user.id,
                "qc_date": fields.Datetime.now(),
            }
        )
        body = _("QC <b>%s</b>", status.upper())
        if comment:
            body += f"<br/><i>{comment}</i>"
        self.message_post(body=body)
        self.task_id.message_post(body=_("Version v%s — QC %s") % (self.version_no, status.upper()))
