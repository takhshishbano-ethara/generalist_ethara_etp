# -*- coding: utf-8 -*-
"""Pre-migration for 19.0.7.0.0 — webhook token safety.

Runs BEFORE the new module code is loaded.

Steps
-----
1.  **Invalidate the placeholder webhook token.**  In 19.0.6 the module
    shipped a default ``kaiju.webhook_token = 'CHANGE-ME-...'`` so a fresh
    install could be poked.  That default is now removed (callbacks 401
    until the operator sets a real token).  If an existing deployment is
    still using the placeholder we clear it during upgrade so the operator
    cannot ship the upgrade with the well-known default in place.
"""

import logging

_logger = logging.getLogger(__name__)

PLACEHOLDER_TOKEN = "CHANGE-ME-generate-a-secure-token"


def migrate(cr, version):
    """Run via odoo.modules.migration on module upgrade."""
    if not version:
        # Fresh install — nothing to migrate.
        return

    # 1. Wipe placeholder webhook token if still in place.
    cr.execute(
        """
        SELECT value
          FROM ir_config_parameter
         WHERE key = 'kaiju.webhook_token'
        """
    )
    row = cr.fetchone()
    if row and row[0] == PLACEHOLDER_TOKEN:
        cr.execute(
            """
            DELETE FROM ir_config_parameter
             WHERE key = 'kaiju.webhook_token'
            """
        )
        _logger.warning(
            "kaiju_commit0 19.0.7: removed placeholder webhook token. "
            "Operator MUST set kaiju.webhook_token in Settings before "
            "Argo callbacks will be accepted."
        )
