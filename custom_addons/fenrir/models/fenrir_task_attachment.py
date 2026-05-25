from odoo import fields, models


class FenrirTaskAttachment(models.Model):
    _name = "fenrir.task.attachment"
    _description = "Fenrir Task Attachment"
    _order = "task_id, folder, sequence, id"
    _rec_name = "file_name"

    task_id = fields.Many2one(
        comodel_name="fenrir.task",
        string="Task",
        required=True,
        ondelete="cascade",
        index=True,
    )
    sequence = fields.Integer(default=10)
    file_name = fields.Char(string="File Name", required=True)
    attachment = fields.Binary(string="Attachment", attachment=True)
    description = fields.Char(string="Description",
                              help="Optional short description / caption")
    folder = fields.Selection(
        selection=[
            ("resources", "Resources"),
            ("tests", "Tests"),
            ("environment", "Environment"),
        ],
        string="Export Folder",
        default="resources",
        required=True,
        help="Target subfolder in the strict task export.",
    )
    license = fields.Selection(
        selection=[
            ("self_created", "Self-created"),
            ("cc0", "CC0"),
            ("cc_by", "CC-BY"),
            ("cc_by_sa", "CC-BY-SA"),
            ("mit", "MIT"),
            ("apache_2", "Apache 2.0"),
            ("proprietary", "Proprietary"),
            ("other", "Other"),
        ],
        string="License",
        default="self_created",
        required=True,
        help="License under which this asset is provided.",
    )
    source_url = fields.Char(
        string="Source URL",
        help="Where the asset originates from (leave blank if self-created).",
    )
    notes = fields.Text(
        string="Notes",
        help="Free-form notes about this asset; emitted as the 'notes' field in license.json.",
    )

    _LICENSE_LABELS = {
        "self_created": "Self-created",
        "cc0": "CC0",
        "cc_by": "CC-BY",
        "cc_by_sa": "CC-BY-SA",
        "mit": "MIT",
        "apache_2": "Apache 2.0",
        "proprietary": "Proprietary",
        "other": "Other",
    }

    def license_label(self):
        self.ensure_one()
        return self._LICENSE_LABELS.get(self.license or "self_created", "Self-created")
