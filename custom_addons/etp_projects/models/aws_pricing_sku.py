from odoo import fields, models


class EtpAwsPricingSku(models.Model):
    _name = "etp.aws.pricing.sku"
    _description = "AWS Pricing SKU (Region-specific priced item)"
    _order = "service_code, item_name, region_label"
    _rec_name = "aws_sku"

    item_id = fields.Many2one(
        "etp.aws.pricing.item",
        string="Item",
        required=True,
        ondelete="cascade",
        index=True,
    )
    service_code = fields.Char(
        string="Service Code",
        related="item_id.service_code",
        store=True,
        index=True,
    )
    item_name = fields.Char(
        string="Item Name",
        related="item_id.name",
        store=True,
        index=True,
    )
    aws_sku = fields.Char(
        string="AWS SKU",
        required=True,
        index=True,
        help="AWS internal SKU identifier (e.g., HBVJM3Q9S8K6MNPJ).",
    )
    region_label = fields.Char(
        string="Region",
        required=True,
        index=True,
        help="AWS Pricing 'location' label (e.g., 'US East (N. Virginia)').",
    )
    on_demand_usd = fields.Float(
        string="On-Demand USD",
        digits=(16, 6),
        help="Unit price for On-Demand pricing tier.",
    )
    unit = fields.Char(string="Unit", help="Billing unit (Hrs / GB-Mo / Requests / ...).")
    price_description = fields.Char(string="Price Description")

    vcpu = fields.Integer(string="vCPU")
    memory = fields.Char(string="Memory")
    storage = fields.Char(string="Storage")
    network_performance = fields.Char(string="Network Performance")
    operating_system = fields.Char(string="Operating System")
    tenancy = fields.Char(string="Tenancy")
    instance_family = fields.Char(string="Instance Family")

    attributes_json = fields.Text(
        string="Attributes (JSON)",
        help="Full AWS product attributes as JSON string.",
    )
    has_reserved = fields.Boolean(string="Has Reserved Pricing", default=False)

    last_synced_at = fields.Datetime(string="Last Synced At", index=True)
    cache_expiry_at = fields.Datetime(
        string="Cache Expires At",
        index=True,
        help="When this cached SKU should be re-fetched.",
    )
    sync_batch_id = fields.Char(string="Sync Batch ID")
    active = fields.Boolean(default=True, index=True)

    _sql_constraints = [
        ("aws_sku_uniq", "unique(aws_sku)", "This AWS SKU already exists."),
    ]
