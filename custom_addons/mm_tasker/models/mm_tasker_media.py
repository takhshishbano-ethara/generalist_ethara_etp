# -*- coding: utf-8 -*-
import base64
import mimetypes
import os

from odoo import api, fields, models


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
