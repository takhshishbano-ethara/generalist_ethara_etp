from odoo import fields, models


class EtpBatchBudgetInfoLink(models.Model):
    _name = "etp.batch.budget.info.link"
    _description = "Batch Budget General Info Link"
    _order = "id"

    batch_id = fields.Many2one(
        "etp.batch.budget",
        string="Batch Budget",
        required=True,
        ondelete="cascade",
        index=True,
    )
    label = fields.Char(string="Label", required=True)
    url = fields.Char(string="URL", required=True)
