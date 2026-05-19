# -*- coding: utf-8 -*-
from odoo import fields, models


class IrAttachment(models.Model):
    _inherit = "ir.attachment"

    crowley_job_id = fields.Many2one(
        "crowley.ai.vid.gen.job",
        string="Crowley Vid Gen Job",
        index=True,
        ondelete="set null",
    )
