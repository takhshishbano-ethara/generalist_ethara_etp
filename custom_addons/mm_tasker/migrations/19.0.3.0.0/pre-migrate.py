# -*- coding: utf-8 -*-
"""Retire the single-script verdict in favour of three per-section QCs.

Drops the obsolete columns on mm_tasker_task and removes the now-unused
ir.config_parameter records. Safe to run on a fresh install too — every
operation is guarded with IF EXISTS.
"""


def migrate(cr, version):
    cr.execute(
        """
        ALTER TABLE mm_tasker_task
            DROP COLUMN IF EXISTS script_verdict,
            DROP COLUMN IF EXISTS script_verdict_message,
            DROP COLUMN IF EXISTS script_verdict_at;
        """
    )
    cr.execute(
        """
        DELETE FROM ir_config_parameter
         WHERE key IN ('mm_tasker.verify_script', 'mm_tasker.verify_timeout');
        """
    )
