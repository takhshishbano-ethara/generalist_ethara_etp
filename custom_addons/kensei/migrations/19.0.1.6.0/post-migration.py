# -*- coding: utf-8 -*-
"""Backfill the new "Ready for Baseline Trajectory" sign-off.

Until 19.0.1.6.0 the Ready-for-Baseline gate *was* ``pl_verified_status == 'done'``.
It is now a sign-off of its own (``baseline_ready_status``) and ``_compute_status``
requires BOTH. A new Selection column lands on existing rows as 'in_progress', so
without this backfill every allocation that had already cleared PL verification
would silently fall back to an earlier status — and an in-flight task would look
like it had lost its progress.

Marking the new gate Done exactly where the old gate was Done reproduces each
record's existing status, so the recompute below is a no-op for every row: zero
drift by construction.
"""
from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    if not version:
        return

    cr.execute("""
        UPDATE kensei_tracker_allocation
           SET baseline_ready_status = 'done'
         WHERE pl_verified_status = 'done'
    """)

    env = api.Environment(cr, SUPERUSER_ID, {})
    Alloc = env['kensei.tracker.allocation']
    recs = Alloc.with_context(active_test=False).search([])
    if not recs:
        return

    # status / final_status / function / failure_reason are STORED computes: Odoo
    # does not recompute them just because the compute's logic (or its dependency
    # list) changed. Force it once, exactly as the 19.0.1.5.0 migration does.
    recs._compute_status()          # also (re)derives final_status
    recs._compute_function()        # function is keyed off status
    recs._compute_failure_reason()  # gained baseline_ready_reason as a source
    recs.flush_recordset()
