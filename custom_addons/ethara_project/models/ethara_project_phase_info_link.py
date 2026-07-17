from odoo import api, fields, models


class EtharaProjectPhaseInfoLink(models.Model):
    _name = "ethara.project.phase.info.link"
    _description = "Ethara Project Phase General Info Link"
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
    label = fields.Char(string="Label", required=True)
    url = fields.Char(string="URL", required=True)

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        for rec in records:
            phase = rec.phase_id
            if phase:
                phase._post_project_thread(
                    "<p><strong>Info link added:</strong> "
                    "<a href='%s' target='_blank'>%s</a> (phase %s)</p>" % (
                        rec.url or "", rec.label or "", phase.name or "",
                    )
                )
        return records
