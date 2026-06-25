# -*- coding: utf-8 -*-
import base64
import mimetypes
import os

from odoo import api, fields, models, _
from odoo.exceptions import ValidationError


class MmTaskerMedia(models.Model):
    _name = 'mm.tasker.media'
    _description = 'MM Tasker Media Asset (image / pdf / video)'
    _order = 'task_id, sequence, id'

    task_id = fields.Many2one(
        'mm.tasker.task',
        string='Task',
        required=True,
        ondelete='cascade',
        index=True,
    )
    sequence = fields.Integer(default=10)

    name = fields.Char(string='Name', required=True)
    file = fields.Binary(string='File', required=True, attachment=True)
    filename = fields.Char(string='Filename')
    # Stored computes so file_size / mime_type are always correct
    # regardless of upload path. The previous onchange-only approach
    # missed: kanban/widget uploads that don't fire the onchange,
    # server-side .create() calls, attachment-storage round-trips that
    # surface the binary asynchronously. Media QC reads file_size and
    # used to fail with "empty file" on perfectly valid uploads.
    mime_type = fields.Char(
        string='MIME Type',
        compute='_compute_mime_type',
        store=True,
        readonly=False,  # let onchange / manual override take precedence
    )
    file_size = fields.Integer(
        string='Size (bytes)',
        compute='_compute_file_size',
        store=True,
    )

    kind = fields.Selection(
        [
            ('image', 'Image'),
            ('pdf', 'PDF'),
            ('video', 'Video'),
            ('other', 'Other'),
        ],
        string='Kind',
        compute='_compute_kind',
        store=True,
        readonly=False,
    )

    image_preview = fields.Binary(
        string='Preview',
        compute='_compute_image_preview',
        readonly=True,
    )

    notes = fields.Text(string='Notes')

    _IMAGE_EXT = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp', '.tif', '.tiff'}
    _PDF_EXT = {'.pdf'}
    _VIDEO_EXT = {'.mp4', '.mov', '.avi', '.mkv', '.webm', '.m4v'}

    @api.depends('file')
    def _compute_file_size(self):
        """Source of truth for file_size — runs on every save."""
        for rec in self:
            if not rec.file:
                rec.file_size = 0
                continue
            try:
                rec.file_size = len(base64.b64decode(rec.file))
            except Exception:
                # Some upload paths leave `file` as raw bytes rather than
                # base64; len() of bytes is the byte size directly.
                rec.file_size = len(rec.file) if isinstance(rec.file, (bytes, bytearray)) else 0

    @api.depends('filename')
    def _compute_mime_type(self):
        for rec in self:
            if rec.filename:
                guessed, _enc = mimetypes.guess_type(rec.filename)
                if guessed:
                    rec.mime_type = guessed
                    continue
            # Preserve any manual override — only blank when there's
            # nothing to guess from and no prior value.
            if not rec.mime_type:
                rec.mime_type = False

    @api.depends('filename', 'mime_type')
    def _compute_kind(self):
        for rec in self:
            rec.kind = rec._guess_kind()

    def _guess_kind(self):
        ext = os.path.splitext((self.filename or self.name or '').lower())[1]
        if ext in self._IMAGE_EXT:
            return 'image'
        if ext in self._PDF_EXT:
            return 'pdf'
        if ext in self._VIDEO_EXT:
            return 'video'
        mt = (self.mime_type or '').lower()
        if mt.startswith('image/'):
            return 'image'
        if mt == 'application/pdf':
            return 'pdf'
        if mt.startswith('video/'):
            return 'video'
        return 'other'

    @api.depends('file', 'kind')
    def _compute_image_preview(self):
        for rec in self:
            rec.image_preview = rec.file if rec.kind == 'image' else False

    @api.onchange('file', 'filename')
    def _onchange_file(self):
        """UX helper only — default the visible name from the filename
        before save. The actual file_size / mime_type / kind values are
        produced by stored compute methods so they're always correct
        regardless of upload path."""
        for rec in self:
            if rec.filename and not rec.name:
                rec.name = rec.filename

    @api.constrains('file')
    def _check_media_file_size(self):
        """Hard per-file size cap, enforced at upload/write so it can't be
        bypassed by the Ready-for-Eval / Run-Models paths (unlike the soft
        check in scripts/qc_media.py, which only runs on the Media Submit
        button). Set mm_tasker.media_max_file_mb=0 to disable.
        """
        max_mb = self._cfg_int('mm_tasker.media_max_file_mb', 50)
        if max_mb <= 0:
            return
        max_bytes = max_mb * 1024 * 1024
        for rec in self:
            size = rec.file_size or 0
            if not size and rec.file:
                try:
                    size = len(base64.b64decode(rec.file))
                except Exception:
                    size = 0
            if size > max_bytes:
                raise ValidationError(_(
                    'Media file "%(name)s" is %(mb).1f MB, over the %(max)s MB '
                    'per-file limit.'
                ) % {
                    'name': rec.filename or rec.name or '(unnamed)',
                    'mb': size / (1024.0 * 1024.0),
                    'max': max_mb,
                })

    @api.constrains('file', 'task_id')
    def _check_parent_media_limits(self):
        """Enforce the task's media count + total-bytes caps from the media
        side too. The task-side @api.constrains('media_ids') only fires when
        media is added *through* the task (the form/o2m path); creating media
        standalone (e.g. via RPC) bypasses it. This media-side check fires on
        every media create/write, closing that gap. The task-side constraint
        is kept as well — it covers the link-existing-media path (6,0,ids),
        which creates no media row so this one wouldn't fire.
        """
        max_count = self._cfg_int('mm_tasker.media_max_count', 20)
        max_total_mb = self._cfg_int('mm_tasker.media_max_total_mb', 140)
        if max_count <= 0 and max_total_mb <= 0:
            return
        for task in self.mapped('task_id'):
            media = task.media_ids
            if max_count > 0 and len(media) > max_count:
                raise ValidationError(_(
                    'This task has %(n)s media files, over the limit of %(max)s.'
                ) % {'n': len(media), 'max': max_count})
            if max_total_mb > 0:
                total = sum(m.file_size or 0 for m in media)
                if total > max_total_mb * 1024 * 1024:
                    raise ValidationError(_(
                        'Total media is %(mb).1f MB, over the %(max)s MB limit '
                        'for one task.'
                    ) % {
                        'mb': total / (1024.0 * 1024.0),
                        'max': max_total_mb,
                    })

    @api.model
    def _cfg_int(self, key, default):
        raw = self.env['ir.config_parameter'].sudo().get_param(key, default=str(default))
        try:
            return int(raw)
        except (TypeError, ValueError):
            return default
