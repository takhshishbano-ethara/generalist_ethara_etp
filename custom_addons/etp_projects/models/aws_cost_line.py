import logging

from odoo import api, fields, models

_logger = logging.getLogger(__name__)

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

BEDROCK_SERVICE_NAME = "Amazon Bedrock"
OPENROUTER_SERVICE_NAME = "OpenRouter"
INFRA_TAG_KEYS = {"project"}
MODEL_NAME_FROM_MODEL_FIELD_KEYWORDS = ("Amazon Bedrock", "OpenRouter")


def _normalize_tag_key(value):
    return (value or "").strip().lower().replace("-", "_").replace(" ", "_")


def _service_uses_model_name_field(service):
    s = (service or "").strip().lower()
    if s in MODEL_NAME_FROM_MODEL_FIELD_KEYWORDS:
        return True
    return False


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

    _CATALOG_TRIGGER_FIELDS = frozenset(
        {"service_name", "model_name", "source", "source_tag_key"}
    )

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        records._ensure_ai_models_from_lines()
        return records

    def write(self, vals):
        res = super().write(vals)
        if self and self._CATALOG_TRIGGER_FIELDS.intersection(vals or {}):
            self._ensure_ai_models_from_lines()
        return res

    def _classify_for_catalog(self, rec):
        source = rec.source or ""
        service = (rec.service_name or "").strip()
        model = (rec.model_name or "").strip()
        tag_key = _normalize_tag_key(rec.source_tag_key)
        source_label = dict(SOURCE_SELECTION).get(source, source)

        if source == "aws" and tag_key in INFRA_TAG_KEYS:
            return ("infra", service, "") if service else (None, "", "")

        name = model if _service_uses_model_name_field(service) else service
        if not name:
            return (None, "", "")
        return ("model", name, source_label)

    def _ensure_ai_models_from_lines(self):
        AiModel = self.env["etp.ai.model"].sudo().with_context(active_test=False)
        Infra = self.env["etp.infra.type"].sudo().with_context(active_test=False)

        models_by_name = {}
        infra_names = set()
        counts = {"model": 0, "infra": 0, "skipped": 0}
        skipped_samples = []
        for rec in self:
            kind, name, provider = self._classify_for_catalog(rec)
            if kind == "model" and name:
                models_by_name.setdefault(name, provider)
                counts["model"] += 1
            elif kind == "infra" and name:
                infra_names.add(name)
                counts["infra"] += 1
            else:
                counts["skipped"] += 1
                if len(skipped_samples) < 10:
                    skipped_samples.append(
                        f"source={rec.source!r} service={rec.service_name!r} "
                        f"model={rec.model_name!r} tag_key={rec.source_tag_key!r}"
                    )

        models_created = 0
        if models_by_name:
            existing_models = AiModel.search([("name", "in", list(models_by_name))])
            existing_names = set(existing_models.mapped("name"))
            to_create = [
                {"name": n, "provider": p}
                for n, p in sorted(models_by_name.items())
                if n not in existing_names
            ]
            if to_create:
                AiModel.create(to_create)
                models_created = len(to_create)
            for m in existing_models:
                provider = models_by_name.get(m.name)
                if provider and not m.provider:
                    m.provider = provider

        infra_created = 0
        if infra_names:
            existing_infra = set(
                Infra.search([("name", "in", list(infra_names))]).mapped("name")
            )
            to_create_infra = [{"name": n} for n in sorted(infra_names - existing_infra)]
            if to_create_infra:
                Infra.create(to_create_infra)
                infra_created = len(to_create_infra)

        _logger.info(
            "etp.project.aws.cost.line catalog sync: lines=%s "
            "classified_model=%s classified_infra=%s skipped=%s "
            "models_created=%s infra_created=%s. Skipped samples: %s",
            len(self),
            counts["model"], counts["infra"], counts["skipped"],
            models_created, infra_created,
            skipped_samples,
        )
        counts["models_created"] = models_created
        counts["infra_created"] = infra_created
        counts["total"] = len(self)
        return counts
