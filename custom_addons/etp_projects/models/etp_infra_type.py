from odoo import fields, models


class EtpInfraType(models.Model):
    _name = "etp.infra.type"
    _description = "Infrastructure Type"
    _order = "sequence, name"

    name = fields.Char(string="Name", required=True)
    code = fields.Char(string="Code")
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)

    is_aws_managed = fields.Boolean(
        string="AWS-Managed",
        default=False,
        index=True,
        help="True when this row was created by the AWS Pricing sync.",
    )
    aws_service_code = fields.Char(
        string="AWS Service Code",
        index=True,
        help="Matches the ServiceCode returned by AWS Price List API (e.g., 'AmazonEC2').",
    )
    primary_attr = fields.Char(
        string="Primary Attribute",
        help="AWS attribute name used to enumerate items (e.g., 'instanceType', 'storageClass').",
    )
    attribute_names = fields.Text(
        string="Attribute Names (JSON)",
        help="JSON list of all AWS attribute names supported by this service.",
    )
    sync_enabled = fields.Boolean(
        string="Sync Enabled",
        default=True,
        help="If enabled, nightly cron fetches items for this service from AWS Pricing.",
    )
    last_synced_at = fields.Datetime(string="Last Synced At")
    item_count = fields.Integer(string="Cached Items", default=0)

    _sql_constraints = [
        ("name_uniq", "unique(name)", "This infrastructure type already exists."),
    ]
