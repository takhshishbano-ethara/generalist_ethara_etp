# -*- coding: utf-8 -*-
"""Re-derive the pipeline status now that stage 2 runs a SHORTER pipeline.

Until 19.0.1.7.0 every stage replayed all five steps, so a freshly handed-off stage 2
sat at 'In Progress (Authoring)' until someone re-entered a Drive Link and re-ran PL
verification — work stage 1 had already done. Stage 2 now starts at 'Ready for
Baseline Trajectory' and only generates the trajectory and manual-QCs it.

``status`` / ``final_status`` / ``function`` are STORED computes, and Odoo does not
recompute a stored field just because the compute's logic changed, so every existing
stage-2 row would keep its stale value. Force it once here.

Only stage >= 2 rows can move: the stage-1 ladder is untouched, so recomputing them is
a no-op. They are recomputed anyway rather than filtered, because that is cheaper to
reason about than trusting the filter — and a no-op write costs nothing.
"""
from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    if not version:
        return

    env = api.Environment(cr, SUPERUSER_ID, {})
    Alloc = env['kensei.tracker.allocation']
    recs = Alloc.with_context(active_test=False).search([])
    if not recs:
        return

    recs._compute_status()          # also (re)derives final_status
    recs._compute_function()        # function is keyed off status
    recs.flush_recordset()

    # date_final is stamped from status, so a stage-2 row that just moved off a
    # terminal state (or onto one) needs its completion timestamp re-evaluated —
    # the Daily Tracker credits a tasker's completion off exactly this field.
    recs._stamp_completion()
    recs.flush_recordset()
