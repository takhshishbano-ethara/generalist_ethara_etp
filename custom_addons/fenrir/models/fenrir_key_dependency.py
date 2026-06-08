from odoo import api, fields, models


class FenrirKeyDependency(models.Model):
    _name = "fenrir.key.dependency"
    _description = "Fenrir Key Dependency / Tool"
    _order = "runtime_id, name"
    _rec_name = "name"

    name = fields.Char(
        string="Dependency",
        required=True,
        help="apt / apk package or CLI tool name, e.g. imagemagick, "
             "librsvg2-bin, admesh, ffmpeg, file.")
    description = fields.Text(string="Description")
    runtime_id = fields.Many2one(
        comodel_name="fenrir.environment.runtime",
        string="Runtime",
        required=True,
        ondelete="cascade",
        index=True,
        help="The base image / runtime this dependency belongs to.")
    active = fields.Boolean(default=True)

    _sql_constraints = [
        ("fenrir_dep_unique_per_runtime",
         "unique(runtime_id, name)",
         "Dependency name must be unique within a runtime."),
    ]

    @api.depends("name", "runtime_id.name")
    def _compute_display_name(self):
        for rec in self:
            if rec.runtime_id:
                rec.display_name = f"{rec.name} ({rec.runtime_id.name})"
            else:
                rec.display_name = rec.name or ""
