from odoo import fields, models


class EtharaProjectAwsEbsPricing(models.Model):
    _name = "ethara.project.aws.ebs.pricing"
    _description = "Ethara Project AWS EBS Volume Pricing Cache (Region-specific list prices)"
    _order = "region_label, volume_code"
    _rec_name = "volume_code"

    region_label = fields.Char(
        string="Region",
        required=True,
        index=True,
    )
    volume_code = fields.Char(
        string="Volume Code",
        required=True,
        index=True,
    )
    volume_label = fields.Char(string="Volume Label")
    rate_usd_per_gb_mo = fields.Float(
        string="Rate (USD / GB-mo)",
        digits=(16, 6),
    )
    unit = fields.Char(string="Unit", default="GB-Mo")
    last_synced_at = fields.Datetime(string="Last Synced At")
    cache_expiry_at = fields.Datetime(
        string="Cache Expiry At",
        index=True,
    )
    source = fields.Selection(
        [("live", "Live AWS"), ("fallback", "Static Fallback")],
        default="live",
        index=True,
    )
    active = fields.Boolean(default=True, index=True)

    _sql_constraints = [
        (
            "region_volume_uniq",
            "unique(region_label, volume_code, active)",
            "Duplicate EBS pricing row for the same region + volume.",
        ),
    ]
