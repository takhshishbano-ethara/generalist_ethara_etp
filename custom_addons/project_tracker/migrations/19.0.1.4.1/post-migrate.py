# -*- coding: utf-8 -*-
"""Fully remove the withdrawn Stage 2 Automated QC import wizard.

Its Python model, view, action and access rows were deleted from the module, but
Odoo leaves the orphaned ``ir.model`` (and its transient table) behind on a plain
upgrade — only a full uninstall drops them. Unlink it through the ORM so the
model, its table/columns, fields and access all cascade away. Idempotent: a no-op
once the model is gone.
"""
from odoo import api, SUPERUSER_ID


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    model = env["ir.model"].search(
        [("model", "=", "project.tracker.automated.qc.import")])
    if model:
        model.unlink()
    # Odoo's own -u cleanup may have already dropped the ir.model on a prior
    # upgrade while leaving the (empty, transient) table behind, so drop it
    # explicitly too.
    cr.execute(
        "DROP TABLE IF EXISTS project_tracker_automated_qc_import CASCADE")
