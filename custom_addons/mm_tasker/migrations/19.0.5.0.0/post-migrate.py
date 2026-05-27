# -*- coding: utf-8 -*-
"""Backfill mm_tasker_media.file_size for rows where the onchange-only
field stayed at 0 despite the binary being present.

Runs in post-migrate (not pre-) because we need the ORM available to
read attachment-stored binary fields — Odoo stores those as files on
disk via ir.attachment, not in the column.
"""

import base64
import logging

from odoo import api, SUPERUSER_ID

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    Media = env['mm.tasker.media']
    stale = Media.search([('file_size', '=', 0), ('file', '!=', False)])
    if not stale:
        return
    fixed = 0
    for rec in stale:
        try:
            size = len(base64.b64decode(rec.file))
        except Exception:
            size = len(rec.file) if isinstance(rec.file, (bytes, bytearray)) else 0
        if size:
            rec.file_size = size
            fixed += 1
    _logger.info('mm_tasker: backfilled file_size on %s media row(s).', fixed)
