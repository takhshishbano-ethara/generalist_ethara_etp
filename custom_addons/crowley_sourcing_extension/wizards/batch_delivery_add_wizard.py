# -*- coding: utf-8 -*-
from odoo import _, api, fields, models
from odoo.exceptions import UserError


class CrowleySourcingBatchDeliveryAddWizard(models.TransientModel):
    _name = "crowley.sourcing.batch.delivery.add.wizard"
    _description = "Add Projects to Crowley Sourcing Batch Delivery"

    mode = fields.Selection(
        [("new", "Create new batch"), ("existing", "Add to existing batch")],
        string="Mode",
        default="new",
        required=True,
    )
    batch_id = fields.Many2one(
        "crowley.sourcing.batch.delivery",
        string="Existing Batch",
        domain=[("state", "=", "draft")],
    )
    name = fields.Char(string="New Batch Reference")
    notes = fields.Text(string="Notes")
    job_ids = fields.Many2many(
        "video.editor.project",
        relation="crowley_sourcing_batch_wizard_rel",
        column1="wizard_id",
        column2="job_id",
        string="Selected Projects",
    )

    @api.model
    def default_get(self, fields_list):
        result = super().default_get(fields_list)
        active_ids = self.env.context.get("active_ids") or []
        if active_ids:
            result["job_ids"] = [(6, 0, active_ids)]
        return result

    def action_apply(self):
        self.ensure_one()
        if not self.job_ids:
            raise UserError(_("No projects selected."))
        if self.mode == "existing":
            if not self.batch_id:
                raise UserError(_("Please pick an existing batch."))
            batch = self.batch_id
            batch.write({"job_ids": [(4, job.id) for job in self.job_ids]})
        else:
            vals = {"job_ids": [(6, 0, self.job_ids.ids)]}
            if self.name:
                vals["name"] = self.name
            if self.notes:
                vals["notes"] = self.notes
            batch = self.env["crowley.sourcing.batch.delivery"].create(vals)
        return {
            "type": "ir.actions.act_window",
            "res_model": "crowley.sourcing.batch.delivery",
            "res_id": batch.id,
            "view_mode": "form",
            "target": "current",
        }
