# -*- coding: utf-8 -*-
"""Post-migration: heal stale data the v1.5 schema left behind.

Odoo invokes ``migrations/<version>/post-migration.py`` on module
upgrade — this is the mirror of ``post_init_hook`` for the
already-installed path. We run the same backfill (existing jobs get
attempt #1) and the same cron repoint (`model_id` from job → attempt)
so an installed-then-upgraded database ends up in the same shape as
a fresh install.
"""

import logging

from odoo import api, SUPERUSER_ID

from odoo.addons.crowley_ai_vid_gen.hooks import (
    migrate_jobs_to_attempts,
    repoint_cron_to_attempt,
)

_logger = logging.getLogger(__name__)


def migrate(cr, installed_version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    _logger.info(
        "Crowley migrate: post-migration starting (was v%s)…", installed_version,
    )
    migrate_jobs_to_attempts(env)
    repoint_cron_to_attempt(env)
    _logger.info("Crowley migrate: post-migration complete.")
