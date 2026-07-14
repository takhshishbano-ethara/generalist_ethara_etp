# -*- coding: utf-8 -*-
"""Post-migrate for 19.0.1.98.0 (tag extraction is manual-only).

The scheduled tag-extraction ir.cron record (``ir_cron_extract_tags``) is
removed from data/cron.xml: tags now extract ONLY when the admin clicks the
Extract Tags button (which runs the extraction inline). Odoo does NOT
orphan-delete a ``noupdate`` data record when it disappears from the manifest,
so this script explicitly unlinks the now-retired cron on existing installs.
The tag model/fields and the reusable _cron_extract_tags /
_finalize_tag_extract_state methods are intentionally kept.
"""
import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return
    env = api.Environment(cr, SUPERUSER_ID, {})
    cron = env.ref("etp_assessment_pro.ir_cron_extract_tags",
                   raise_if_not_found=False)
    if cron:
        cron.unlink()
        _logger.info(
            "post-migrate 19.0.1.98.0: removed the retired tag-extraction cron; "
            "tags now extract only via the manual Extract Tags button.")
