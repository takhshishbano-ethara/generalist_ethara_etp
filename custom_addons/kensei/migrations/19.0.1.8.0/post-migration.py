# -*- coding: utf-8 -*-
"""Re-derive the pipeline status now that the ladder is STRICTLY sequential.

Until 19.0.1.8.0 _compute_status was a flat series of independent ``if``s, so the last
rung won regardless of the ones below it: a task whose Drive Link was cleared, or whose
PL Verification was reverted, AFTER it reached Deliverable simply stayed Deliverable —
and kept its date_final, so the Daily Tracker went on crediting a completion whose
inputs no longer existed. The rungs are nested now, so a broken chain drops the status
back to wherever it breaks.

``status`` / ``final_status`` / ``function`` are STORED computes, and Odoo does not
recompute a stored field just because the compute's logic changed. Force it once here —
this is what retroactively drops the already-inconsistent tasks out of Deliverable.
Rows whose chain is intact recompute to the value they already hold, so they are a
no-op; they are recomputed anyway rather than filtered, because a no-op write costs
nothing and is cheaper to reason about than trusting the filter.
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

    # date_final is stamped from status, so a row that just fell out of a terminal
    # state has to lose its completion timestamp — that is what withdraws the delivery
    # credit the Daily Tracker had already counted.
    recs._stamp_completion()
    recs.flush_recordset()
