from odoo import api, fields, models


class EtharaProjectPhaseConnectedRecord(models.Model):
    _name = "ethara.project.phase.connected.record"
    _description = "Ethara Project Phase Connected-Table Record"
    _order = "id"

    phase_id = fields.Many2one(
        "ethara.project.phase",
        string="Phase",
        required=True,
        ondelete="cascade",
        index=True,
    )
    ethara_project_id = fields.Many2one(
        "ethara.project",
        related="phase_id.ethara_project_id",
        store=True,
        index=True,
    )
    connected_model = fields.Char(
        string="Connected Model",
        related="phase_id.connected_model",
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

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        for rec in records:
            phase = rec.phase_id
            if phase:
                phase._post_project_thread(
                    "<p><strong>Connected record linked:</strong> "
                    "%s (phase %s)</p>" % (
                        rec.record_display or rec.res_id or "",
                        phase.name or "",
                    )
                )
        return records
