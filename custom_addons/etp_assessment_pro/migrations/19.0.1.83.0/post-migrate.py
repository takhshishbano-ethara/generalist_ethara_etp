# -*- coding: utf-8 -*-
"""Post-migrate for 19.0.1.83.0 (video_prompt Phase 3: async Veo generation).

Adds plain nullable columns that Odoo's ORM auto-creates during the ``-u``
schema sync, so there is NOTHING to do here:

- draft (etp.assessment.pro.prompt.question): video_state, video_op_json,
  video_files_json, video_error.

Existing video_prompt drafts intentionally keep them at their defaults
(video_state NULL/'none', the JSON/error columns NULL): async generation is
config-gated and only submits for drafts left 'pending' by NEW generation, and
every video path no-ops on an empty value, so an un-migrated draft simply keeps
the Phase-1 upload behaviour. This script exists so the version bump carries an
explicit, auditable record that the change is purely additive and
non-destructive.
"""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return
    _logger.info(
        "post-migrate 19.0.1.83.0: video_prompt async-generation columns are "
        "auto-created and left at defaults on existing rows (no backfill; the "
        "submit cron only touches 'pending' drafts and every path no-ops on an "
        "empty value, preserving the upload path).")
