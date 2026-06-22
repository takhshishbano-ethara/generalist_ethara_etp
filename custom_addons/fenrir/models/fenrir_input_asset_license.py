from odoo import fields, models


class FenrirInputAssetLicense(models.Model):
    _name = "fenrir.input.asset.license"
    _description = "Fenrir Input Asset License"
    _order = "task_id, sequence, id"
    _rec_name = "asset"

    task_id = fields.Many2one(
        comodel_name="fenrir.task",
        string="Task",
        required=True,
        ondelete="cascade",
        index=True,
    )
    sequence = fields.Integer(default=10)
    asset = fields.Char(
        string="Asset",
        required=True,
        help="Name or identifier of the external input asset.",
    )
    source = fields.Char(
        string="Source",
        help="Where the asset came from (site, author, marketplace, ...).",
    )
    license = fields.Selection(
        selection=[
            ("self_created", "Self-created"),
            ("public_domain", "Public Domain"),
            ("cc0", "CC0"),
            ("cc_by", "CC-BY"),
            ("cc_by_sa", "CC-BY-SA"),
            ("mit", "MIT"),
            ("apache_2", "Apache 2.0"),
            ("proprietary", "Proprietary"),
            ("other", "Other"),
        ],
        string="License",
        help="License under which the external input asset is provided.",
    )
    url = fields.Char(
        string="URL",
        help="Link to the asset or its license terms.",
    )

    def license_label(self):
        """Human-readable license label (matches fenrir.task.attachment), or ''."""
        self.ensure_one()
        return dict(self._fields["license"].selection).get(self.license, "")
