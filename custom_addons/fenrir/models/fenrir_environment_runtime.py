from odoo import fields, models


class FenrirEnvironmentRuntime(models.Model):
    _name = "fenrir.environment.runtime"
    _description = "Fenrir Environment Base / Runtime"
    _order = "name"

    name = fields.Char(
        string="Runtime",
        required=True,
        help="Base image or runtime identifier — e.g. nginx:1.25-alpine, "
             "node:20-alpine, python:3.12-alpine, blender:3.6.")
    description = fields.Text(string="Description")
    active = fields.Boolean(default=True)

    key_dependency_ids = fields.One2many(
        comodel_name="fenrir.key.dependency",
        inverse_name="runtime_id",
        string="Key Dependencies",
    )
    dependency_count = fields.Integer(
        string="Dependencies",
        compute="_compute_dependency_count")

    _sql_constraints = [
        ("fenrir_runtime_name_unique",
         "unique(name)",
         "Runtime name must be unique."),
    ]

    def _compute_dependency_count(self):
        for rec in self:
            rec.dependency_count = len(rec.key_dependency_ids)
