# -*- coding: utf-8 -*-
from odoo import _, api, fields, models
from odoo.exceptions import UserError


class FenrirBatchDeliveryAddWizard(models.TransientModel):
    _name = "fenrir.batch.delivery.add.wizard"
    _description = "Add Tasks to Fenrir Batch Delivery"

    mode = fields.Selection(
        [("new", "Create a new batch"), ("existing", "Add to existing batch")],
        default="new",
        required=True,
    )
    batch_id = fields.Many2one(
        "fenrir.batch.delivery",
        string="Existing Batch",
        domain=[("state", "=", "draft")],
    )
    name = fields.Char("Batch Reference")
    notes = fields.Text("Notes")
    job_ids = fields.Many2many(
        "fenrir.task",
        relation="fenrir_batch_delivery_wizard_job_rel",
        column1="wizard_id",
        column2="job_id",
        string="Tasks",
    )

    @api.model
    def default_get(self, fields_list):
        result = super().default_get(fields_list)
        context = self.env.context or {}
        active_model = context.get("active_model")
        active_ids = context.get("active_ids") or []
        if active_model == "fenrir.task" and active_ids:
            result["job_ids"] = [(6, 0, active_ids)]
        return result

    def action_apply(self):
        self.ensure_one()
        if not self.job_ids:
            raise UserError(_("No tasks selected."))
        if self.mode == "existing":
            if not self.batch_id:
                raise UserError(_("Select a draft batch to add the tasks to."))
            self.batch_id.write(
                {"job_ids": [(4, job.id) for job in self.job_ids]}
            )
            batch = self.batch_id
        else:
            vals = {"job_ids": [(6, 0, self.job_ids.ids)]}
            if self.name:
                vals["name"] = self.name
            if self.notes:
                vals["notes"] = self.notes
            batch = self.env["fenrir.batch.delivery"].create(vals)
        return {
            "type": "ir.actions.act_window",
            "res_model": "fenrir.batch.delivery",
            "res_id": batch.id,
            "view_mode": "form",
            "target": "current",
        }
