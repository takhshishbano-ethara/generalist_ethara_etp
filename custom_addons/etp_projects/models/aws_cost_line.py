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
    source_tag_key = fields.Char(string="Source Tag Key", index=True)
    source_tag_value = fields.Char(string="Source Tag Value", index=True)

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
        "unique(budget_id, period, service_name, granularity, model_name, token_type, source_tag_key)",
        "One row per budget/period/service/granularity/model/token type/source tag.",
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

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        records._ensure_ai_models_from_lines()
        return records

    def _ensure_ai_models_from_lines(self):
        names = set()
        for rec in self:
            effective = (rec.model_name or rec.service_name or "").strip()
            if effective:
                names.add(effective)
        if not names:
            return
        Model = self.env["etp.ai.model"].sudo().with_context(active_test=False)
        existing = set(Model.search([("name", "in", list(names))]).mapped("name"))
        to_create = [{"name": n} for n in sorted(names - existing)]
        if to_create:
            Model.create(to_create)
