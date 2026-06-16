from odoo import fields, models


class ClientDeliverableS3Link(models.Model):
    """A single S3 file link on a deliverable.

    We store only the URL (and an optional label) — never the file itself.
    A deliverable can have many of these.
    """

    _name = "client.deliverable.s3.link"
    _description = "Client Deliverable S3 File Link"
    _order = "id"

    deliverable_id = fields.Many2one(
        "client.deliverable",
        string="Deliverable",
        required=True,
        ondelete="cascade",
        index=True,
    )
    name = fields.Char(string="Label")
    url = fields.Char(string="S3 URL", required=True)
