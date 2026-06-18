from odoo import api, fields, models

SOURCE_SELECTION = [
    ("aws", "AWS"),
    ("openrouter", "OpenRouter"),
    ("moonshot", "Moonshot"),
    ("gcp", "GCP"),
    ("openai", "OpenAI"),
]

GRANULARITY_SELECTION = [
    ("month", "Monthly"),
    ("day", "Daily"),
]

TOKEN_TYPE_SELECTION = [
    ("input", "Input"),
    ("output", "Output"),
    ("cache_read", "Cache Read"),
    ("cache_write", "Cache Write"),
    ("other", "Other"),
]

class EtpProjectAwsCostLine(models.Model):
    _name = "etp.project.aws.cost.line"
    _description = "Project AWS Cost Line"
    _order = "period desc, granularity, amount_source desc"

    budget_id = fields.Many2one(
        "etp.project.aws.budget", required=True, ondelete="cascade", index=True,
    )
    project_id = fields.Many2one(
        related="budget_id.project_id", store=True, readonly=True, index=True,
    )
    tag_id = fields.Many2one(
        "etp.project.aws.budget.tag",
        string="Tag Filter",
        ondelete="cascade",
        index=True,
        help="Which (Tag Key, Tag Value) pair on the parent budget this line was "
             "fetched for. Empty for non-AWS provider lines (OpenRouter/OpenAI/etc.).",
    )
    tag_key = fields.Char(related="tag_id.tag_key", store=True, readonly=True)
    tag_value = fields.Char(related="tag_id.tag_value", store=True, readonly=True)

    period = fields.Date(required=True, index=True)
    period_label = fields.Char(compute="_compute_period_label", store=True)
    service_name = fields.Char(required=True, index=True)
    source = fields.Selection(
        SOURCE_SELECTION, default="aws", required=True, index=True,
        help="Origin of this cost line.",
    )
    granularity = fields.Selection(
        GRANULARITY_SELECTION, default="month", required=True, index=True,
    )

    amount_source = fields.Float(string="Cost (USD)")

    model_name = fields.Char(index=True)
    usage_type = fields.Char(index=True)
    token_type = fields.Selection(TOKEN_TYPE_SELECTION, index=True)
    usage_quantity = fields.Float()
    usage_unit = fields.Char()
    is_model_breakdown = fields.Boolean(default=False, index=True)

    _uniq_line = models.Constraint(
        "unique(budget_id, tag_id, period, service_name, granularity, model_name, token_type)",
        "One row per budget/tag/period/service/granularity/model/token type.",
    )

    @api.depends("period", "granularity")
    def _compute_period_label(self):
        for rec in self:
            if not rec.period:
                rec.period_label = ""
            elif rec.granularity == "day":
                rec.period_label = rec.period.strftime("%Y-%m-%d")
            else:
                rec.period_label = rec.period.strftime("%Y-%m")
