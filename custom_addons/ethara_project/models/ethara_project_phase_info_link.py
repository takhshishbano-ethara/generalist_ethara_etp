from odoo import fields, models


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
