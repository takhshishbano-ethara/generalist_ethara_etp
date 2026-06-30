from odoo import api, fields, models


class EtpBatchBudgetConnectedRecord(models.Model):
    _name = "etp.batch.budget.connected.record"
    _description = "Phase Budget Connected-Table Record"
    _order = "id"

    batch_id = fields.Many2one(
        "etp.batch.budget",
        string="Phase Budget",
        required=True,
        ondelete="cascade",
        index=True,
    )
    connected_model = fields.Char(
        string="Connected Model",
        related="batch_id.project_id.connected_table",
        store=True,
        readonly=True,
        index=True,
    )
    res_id = fields.Many2oneReference(
        string="Record",
        model_field="connected_model",
        required=True,
    )
    record_display = fields.Char(
        string="Record Name",
        compute="_compute_record_display",
        store=True,
    )

    @api.depends("connected_model", "res_id")
    def _compute_record_display(self):
        for rec in self:
            model = rec.connected_model
            res_id = rec.res_id
            if model and res_id and model in self.env:
                target = self.env[model].sudo().browse(res_id).exists()
                rec.record_display = target.display_name if target else False
            else:
                rec.record_display = False
