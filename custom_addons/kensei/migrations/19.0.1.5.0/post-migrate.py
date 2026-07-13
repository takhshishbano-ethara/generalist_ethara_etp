# -*- coding: utf-8 -*-
from odoo import api, SUPERUSER_ID


def migrate(cr, version):
    """Recompute the pipeline status of existing allocations for multi-stage tasks.

    Introducing the stage chain changed what "finished" means: completing a
    NON-final stage now yields 'ready_next_stage' instead of 'deliverable' (only
    the final stage delivers). ``status`` / ``final_status`` / ``function`` are
    STORED computed fields, so rows written before this change keep their stale
    'deliverable' value — Odoo does not recompute stored fields just because the
    compute's logic changed. Force it once here.
    """
    env = api.Environment(cr, SUPERUSER_ID, {})
    Alloc = env['kensei.tracker.allocation']
    recs = Alloc.with_context(active_test=False).search([])
    if not recs:
        return

    # is_final_stage is new; make sure it is materialised before status reads it.
    recs._compute_is_final_stage()
    recs._compute_is_current_stage()
    recs._compute_status()          # also (re)derives final_status
    recs._compute_function()        # function is keyed off status
    recs.flush_recordset()
