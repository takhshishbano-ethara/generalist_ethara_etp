# -*- coding: utf-8 -*-
"""Post-migrate for 19.0.1.101.0 (drop orphaned ir.cron rows).

Old installs still carry ir.cron rows whose code calls
``_cron_generate_pending_questions`` / ``_cron_extract_pending_skills`` —
methods renamed long ago to ``_cron_generate_from_sop`` / ``_cron_extract_tags``
(the latter cron itself retired in 98.0). Those rows were never orphan-deleted
(their XML-IDs are gone, so ``env.ref`` cannot reach them), so the scheduler
keeps firing them and logging AttributeError every minute. Unlink any ir.cron
whose code still references a dead method name. Idempotent: a no-op once clean.
"""
import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)

_DEAD_METHODS = ("_cron_generate_pending_questions", "_cron_extract_pending_skills")


def migrate(cr, version):
    if not version:
        return
    env = api.Environment(cr, SUPERUSER_ID, {})
    domain = ["|"] * (len(_DEAD_METHODS) - 1)
    domain += [("code", "ilike", name) for name in _DEAD_METHODS]
    crons = env["ir.cron"].search(domain)
    if crons:
        names = crons.mapped("cron_name")
        crons.unlink()
        _logger.info(
            "post-migrate 19.0.1.101.0: removed %d orphaned ir.cron row(s) "
            "calling dead methods %s: %s",
            len(crons), _DEAD_METHODS, names)
