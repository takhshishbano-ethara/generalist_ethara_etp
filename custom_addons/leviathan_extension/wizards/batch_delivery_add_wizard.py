from odoo import _, api, fields, models
from odoo.exceptions import UserError


class LeviathanBatchDeliveryAddWizard(models.TransientModel):
    _name = "leviathan.batch.delivery.add.wizard"
    _description = "Add Leviathan Jobs to Batch Delivery"

    mode = fields.Selection(
        [("new", "Create New Batch"), ("existing", "Add to Existing Batch")],
        default="new",
        required=True,
    )
    batch_id = fields.Many2one(
        "leviathan.batch.delivery",
        string="Existing Batch",
        domain="[('state', '=', 'draft')]",
    )
    date_from = fields.Date(string="Date From")
    date_to = fields.Date(string="Date To")
    notes = fields.Text(string="Notes")
    job_ids = fields.Many2many(
        "leviathan.job",
        string="Selected Jobs",
        readonly=True,
    )

    @api.model
    def default_get(self, fields_list):
        defaults = super().default_get(fields_list)
        active_ids = self.env.context.get("active_ids") or []
        active_model = self.env.context.get("active_model")
        if active_model == "leviathan.job" and active_ids:
            defaults["job_ids"] = [(6, 0, active_ids)]
        return defaults

    def action_confirm(self):
        self.ensure_one()
        if not self.job_ids:
            raise UserError(_("No jobs selected. Pick rows in the list first."))
        if self.mode == "existing":
            if not self.batch_id:
                raise UserError(_("Pick an existing batch or switch to 'Create New Batch'."))
            if self.batch_id.state != "draft":
                raise UserError(_("Only draft batches can be modified."))
            self.batch_id.job_ids = [(4, jid) for jid in self.job_ids.ids]
            batch = self.batch_id
        else:
            batch = self.env["leviathan.batch.delivery"].create({
                "date_from": self.date_from,
                "date_to": self.date_to,
                "notes": self.notes,
                "job_ids": [(6, 0, self.job_ids.ids)],
            })
        return {
            "type": "ir.actions.act_window",
            "name": _("Batch Delivery"),
            "res_model": "leviathan.batch.delivery",
            "res_id": batch.id,
            "view_mode": "form",
            "target": "current",
        }
