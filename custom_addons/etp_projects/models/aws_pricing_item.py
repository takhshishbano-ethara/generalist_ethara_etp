from odoo import api, fields, models


class EtpAwsPricingItem(models.Model):
    _name = "etp.aws.pricing.item"
    _description = "AWS Pricing Item (Instance Type / Storage Class / etc.)"
    _order = "service_code, name"
    _rec_name = "name"

    infra_type_id = fields.Many2one(
        "etp.infra.type",
        string="Service",
        required=True,
        ondelete="cascade",
        index=True,
    )
    service_code = fields.Char(
        string="AWS Service Code",
        related="infra_type_id.aws_service_code",
        store=True,
        index=True,
    )
    name = fields.Char(
        string="Item",
        required=True,
        index=True,
        help="Value of the service's primary attribute (e.g., 't3.large', 'S3 Standard').",
    )
    sku_count = fields.Integer(
        string="Cached SKUs",
        compute="_compute_sku_count",
        store=False,
    )
    last_synced_at = fields.Datetime(string="Last Synced At")
    active = fields.Boolean(default=True, index=True)

    _sql_constraints = [
        (
            "item_uniq",
            "unique(infra_type_id, name)",
            "This item already exists for the selected service.",
        ),
    ]

    def _compute_sku_count(self):
        if not self:
            return
        # Batch aggregation avoids the N+1 problem when listing many items.
        # Why: sku_count is display-only; per-row search_count() explodes on
        # the ~30k-row list view. read_group returns counts in one query.
        Sku = self.env["etp.aws.pricing.sku"].sudo()
        real_ids = [rec.id for rec in self if rec.id]
        counts = {}
        if real_ids:
            for row in Sku.read_group(
                [("item_id", "in", real_ids)],
                ["item_id"],
                ["item_id"],
            ):
                item = row.get("item_id")
                if item:
                    counts[item[0]] = row.get("item_id_count") or 0
        for rec in self:
            rec.sku_count = counts.get(rec.id, 0)
