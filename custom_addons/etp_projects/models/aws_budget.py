import calendar
import json
import logging
import re
from datetime import date, timedelta

import requests
from dateutil.relativedelta import relativedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError

OPENROUTER_SERVICE_NAME = "OpenRouter"
OPENROUTER_ACTIVITY_URL = "https://openrouter.ai/api/v1/activity"

MOONSHOT_SERVICE_NAME = "Moonshot"
MOONSHOT_BALANCE_URL = "https://api.moonshot.ai/v1/users/me/balance"

OPENAI_SERVICE_NAME = "OpenAI"
OPENAI_COSTS_URL = "https://api.openai.com/v1/organization/costs"
OPENAI_USAGE_COMPLETIONS_URL = (
    "https://api.openai.com/v1/organization/usage/completions"
)

GCP_DEFAULT_SERVICE_NAME = "GCP"

AWS_CE_COST_PER_REQUEST_USD = 0.01

BEDROCK_SERVICE_NAME = "Amazon Bedrock"
BEDROCK_REGION_PREFIX_RE = re.compile(r"^[A-Z]{2,4}\d?-")
BEDROCK_SERVICE_PREFIX_RE = re.compile(r"^bedrock:", re.IGNORECASE)
BEDROCK_TOKEN_SUFFIXES = (
    ("-CacheReadInputTokens", "cache_read"),
    ("-CacheWriteInputTokens", "cache_write"),
    ("-InputTokens", "input"),
    ("-OutputTokens", "output"),
)


def _parse_bedrock_usage_type(usage_type):
    """Parse an AWS Cost Explorer Bedrock USAGE_TYPE into (model_name, token_type).

    Examples:
        'USE1-Bedrock:Anthropic.Claude-3-Sonnet-InputTokens'
            -> ('Anthropic.Claude-3-Sonnet', 'input')
        'USW2-bedrock:Claude-3-Haiku-OutputTokens'
            -> ('Claude-3-Haiku', 'output')
        'USE1-Bedrock:Claude-3-Sonnet-CacheReadInputTokens'
            -> ('Claude-3-Sonnet', 'cache_read')
    """
    if not usage_type:
        return "", "other"
    remainder = BEDROCK_REGION_PREFIX_RE.sub("", usage_type, count=1)
    remainder = BEDROCK_SERVICE_PREFIX_RE.sub("", remainder, count=1)
    token_type = "other"
    for suffix, token_label in BEDROCK_TOKEN_SUFFIXES:
        if remainder.endswith(suffix):
            remainder = remainder[: -len(suffix)]
            token_type = token_label
            break
    return remainder.strip(" -"), token_type


_logger = logging.getLogger(__name__)


class EtpProjectAwsBudget(models.Model):
    _name = "etp.project.aws.budget"
    _description = "Project AWS Budget"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "project_id, name"
    _rec_name = "name"

    name = fields.Char(required=True, tracking=True)
    project_id = fields.Many2one(
        "project.project", required=True, ondelete="cascade", tracking=True,
    )
    project_type = fields.Selection(
        [("rnd", "R&D"), ("operations", "Operations")],
        string="Budget Type",
        tracking=True,
        help="Which project type this budget represents. Used by client deliverables "
             "to auto-pick the right budget once a project + project type are chosen.",
    )
    active = fields.Boolean(default=True)

    budget_amount = fields.Float(
        string="Budget (USD)",
        tracking=True,
        help="Top-level budget envelope for this project (USD).",
    )
    model_line_ids = fields.One2many(
        "etp.project.budget.model.line",
        "budget_id",
        string="Model Lines",
        help="AI models and their per-task cost. Used by Operations budgets and Batch Budget snapshots.",
    )
    infra_line_ids = fields.One2many(
        "etp.project.budget.infra.line",
        "budget_id",
        string="Infrastructure Lines",
        help="Infrastructure cost components (EC2, S3, RDS, ...).",
    )

    aws_access_key_id = fields.Char(string="AWS Access Key ID")
    aws_secret_access_key = fields.Char(string="AWS Secret Access Key")
    aws_region = fields.Char(default="us-east-1", string="AWS Region")
    fetch_months = fields.Integer(default=6, string="Months to Fetch")

    tag_key = fields.Char(string="Tag Key")
    tag_value = fields.Char(string="Tag Value")

    openrouter_enabled = fields.Boolean(
        string="Fetch OpenRouter Costs", default=False,
        help="When enabled, OpenRouter activity is fetched alongside AWS Cost Explorer.",
    )
    openrouter_api_key = fields.Char(
        string="OpenRouter API Key",
        help="Management API key (NOT a regular inference key). "
             "Create at openrouter.ai/settings/keys.",
    )
    last_openrouter_fetched_at = fields.Datetime(
        readonly=True, string="Last OpenRouter Fetch",
    )

    moonshot_enabled = fields.Boolean(
        string="Fetch Moonshot Costs", default=False,
        help="When enabled, Moonshot (Kimi) balance consumption is fetched alongside AWS Cost Explorer.",
    )
    moonshot_api_key = fields.Char(
        string="Moonshot API Key",
        help="API key from platform.moonshot.ai/console/api-keys. "
             "Used to query /v1/users/me/balance.",
    )
    last_moonshot_fetched_at = fields.Datetime(
        readonly=True, string="Last Moonshot Fetch",
    )
    moonshot_last_used_usd = fields.Float(
        readonly=True, string="Moonshot Last-Used Baseline (USD)",
        help="Snapshot of (cash_balance - available_balance) at the previous fetch. "
             "Subsequent fetches compute delta = current_used - this baseline and "
             "accumulate it into today's daily Moonshot cost row.",
    )
    moonshot_last_used_at = fields.Datetime(
        readonly=True, string="Moonshot Baseline Recorded At",
    )

    openai_enabled = fields.Boolean(
        string="Fetch OpenAI Costs", default=False,
        help="When enabled, OpenAI organization costs are fetched alongside AWS Cost Explorer.",
    )
    openai_api_key = fields.Char(
        string="OpenAI Admin API Key",
        help="Admin API key (sk-admin-*) from platform.openai.com/settings/organization/admin-keys. "
             "Regular sk-... project keys will NOT work for the /v1/organization/costs endpoint.",
    )
    openai_project_id = fields.Char(
        string="OpenAI Project ID",
        help="OpenAI Project ID (proj_...) to scope cost data to one project. "
             "Find at platform.openai.com/settings/organization/projects -> your project -> Settings. "
             "When set, only this project's spend is fetched; leave empty to pull the whole organization.",
    )
    last_openai_fetched_at = fields.Datetime(
        readonly=True, string="Last OpenAI Fetch",
    )

    gcp_enabled = fields.Boolean(
        string="Fetch GCP Costs", default=False,
        help="When enabled, GCP costs are pulled from a BigQuery Billing Export "
             "dataset. See GCP_SETUP_GUIDE.md in this module for the one-time "
             "setup steps (enable Billing Export → BigQuery + service account).",
    )
    gcp_project_id = fields.Char(
        string="GCP Project ID",
        help="GCP project that contains the BigQuery Billing Export dataset.",
    )
    gcp_bq_dataset = fields.Char(
        string="BigQuery Dataset", default="billing_export",
        help="Dataset name configured in GCP Console → Billing → Billing Export.",
    )
    gcp_bq_table = fields.Char(
        string="BigQuery Table",
        help="Billing export table, e.g. 'gcp_billing_export_v1_018F2A_XXXX_YYYYYY'. "
             "Use '*' suffix to query sharded tables.",
    )
    gcp_service_account_json = fields.Text(
        string="GCP Service Account JSON",
        help="Paste the full service-account JSON key. The account needs "
             "roles/bigquery.dataViewer + roles/bigquery.jobUser on the billing dataset.",
    )
    gcp_service_filter = fields.Char(
        string="GCP Service Filter",
        help="Optional — exact match on service.description, e.g. "
             "'Generative Language API' to restrict the result to Gemini spend.",
    )
    gcp_label_key = fields.Char(
        string="GCP Label Key",
        help="Optional — GCP resource label key (mirrors AWS tag pattern), e.g. 'team'.",
    )
    gcp_label_value = fields.Char(
        string="GCP Label Value",
        help="Optional — GCP resource label value, e.g. 'alpha'.",
    )
    last_gcp_fetched_at = fields.Datetime(
        readonly=True, string="Last GCP Fetch",
    )

    last_fetched_at = fields.Datetime(readonly=True, tracking=True)
    cost_line_ids = fields.One2many("etp.project.aws.cost.line", "budget_id")
    cost_line_count = fields.Integer(compute="_compute_totals", store=False)
    fetch_log_ids = fields.One2many(
        "etp.project.aws.cost.fetch.log", "budget_id",
        string="Fetch History",
    )
    fetch_log_count = fields.Integer(
        compute="_compute_fetch_log_count", store=False, string="Fetch Log Entries",
    )

    approver_user_ids = fields.Many2many(
        "res.users",
        "etp_aws_budget_approver_rel",
        "budget_id",
        "user_id",
        string="Approvers",
        help="Users authorised to approve Batch Budget and Top-up requests on this project.",
    )
    cto_user_id = fields.Many2one(
        "res.users",
        string="CTO (Threshold Alerts)",
        help="Recipient of batch consumption threshold alert mails.",
    )
    batch_budget_ids = fields.One2many(
        "etp.batch.budget", "project_budget_id", string="Batch Budgets",
    )
    topup_ids = fields.One2many(
        "etp.project.budget.topup", "project_budget_id", string="Top-up Requests",
    )
    allocated_amount = fields.Float(
        string="Allocated to Batches (USD)",
        compute="_compute_batch_totals", store=False,
        help="Sum of Approved Amount across all approved/in-progress/delivered/closed batches.",
    )
    consumed_amount = fields.Float(
        string="Consumed by Batches (USD)",
        compute="_compute_batch_totals", store=False,
    )
    topup_total_amount = fields.Float(
        string="Approved Top-ups (USD)",
        compute="_compute_batch_totals", store=False,
    )
    remaining_amount = fields.Float(
        string="Remaining (USD)",
        compute="_compute_batch_totals", store=False,
    )
    consumed_pct = fields.Float(
        string="Consumed %",
        compute="_compute_batch_totals", store=False,
    )
    health_status = fields.Selection(
        [
            ("unknown", "Unknown"),
            ("healthy", "Healthy"),
            ("warning", "Warning"),
            ("at_risk", "At Risk"),
            ("critical", "Critical"),
        ],
        string="Budget Health",
        compute="_compute_batch_totals", store=False,
    )
    is_rnd = fields.Boolean(
        string="R&D Project",
        compute="_compute_is_rnd",
        store=True,
    )
    llm_consumed_amount = fields.Float(
        string="LLM Consumed (USD)",
        compute="_compute_batch_totals", store=False,
        help="Sum of daily cost lines (service-level rows only) for this project.",
    )
    total_approved_amount = fields.Float(
        string="Total Approved Budget (USD)",
        compute="_compute_batch_totals", store=False,
        help="Initial budget plus all approved top-ups.",
    )
    batch_budget_remain = fields.Float(
        string="Delivered Leftover Pool (USD)",
        default=0.0,
        readonly=True,
        copy=False,
        tracking=True,
        help="Unused approved amount from delivered batches. Auto-allocated "
             "to the next new or restarted batch.",
    )

    @api.depends("project_type")
    def _compute_is_rnd(self):
        for rec in self:
            rec.is_rnd = rec.project_type == "rnd"

    @api.depends("cost_line_ids")
    def _compute_totals(self):
        for rec in self:
            rec.cost_line_count = len(rec.cost_line_ids)

    @api.depends(
        "project_type",
        "budget_amount",
        "batch_budget_ids.state",
        "batch_budget_ids.approved_amount",
        "batch_budget_ids.carried_over_amount",
        "batch_budget_ids.consumed_cost",
        "batch_budget_ids.closed_remaining",
        "topup_ids.state",
        "topup_ids.amount",
        "cost_line_ids.amount_source",
        "cost_line_ids.granularity",
        "cost_line_ids.is_model_breakdown",
    )
    def _compute_batch_totals(self):
        for rec in self:
            topups = sum(
                t.amount or 0.0
                for t in rec.topup_ids
                if t.state == "approved"
            )
            envelope = (rec.budget_amount or 0.0) + topups
            llm_consumed = sum(
                line.amount_source or 0.0
                for line in rec.cost_line_ids
                if line.granularity == "day" and not line.is_model_breakdown
            )
            rec.topup_total_amount = topups
            rec.llm_consumed_amount = llm_consumed
            if rec.project_type == "rnd":
                rec.total_approved_amount = envelope
                rec.allocated_amount = 0.0
                rec.consumed_amount = llm_consumed
                rec.remaining_amount = envelope - llm_consumed
                consumed_for_pct = llm_consumed
            else:
                locked = 0.0
                consumed = 0.0
                for batch in rec.batch_budget_ids:
                    if batch.state in ("rejected", "withdrawn"):
                        continue
                    if batch.state == "draft":
                        locked += batch.carried_over_amount or 0.0
                        continue
                    locked += batch.approved_amount or 0.0
                    consumed += batch.consumed_cost or 0.0
                rec.allocated_amount = locked
                rec.total_approved_amount = locked
                rec.consumed_amount = consumed
                rec.remaining_amount = envelope - locked
                consumed_for_pct = consumed
            pct = (consumed_for_pct / envelope * 100.0) if envelope else 0.0
            rec.consumed_pct = pct
            if not envelope:
                rec.health_status = "unknown"
            elif pct < 60.0:
                rec.health_status = "healthy"
            elif pct < 80.0:
                rec.health_status = "warning"
            elif pct < 100.0:
                rec.health_status = "at_risk"
            else:
                rec.health_status = "critical"

    def _propagate_batch_alerts(self):
        for rec in self:
            for batch in rec.batch_budget_ids:
                if batch.state in ("approved", "in_progress"):
                    batch._check_threshold_alerts()

    def action_fetch_cost(self):
        messages = []
        notif_type = "success"
        for rec in self:
            try:
                result = rec._fetch_cost_one(source="manual_button")
                created = result.get("created", 0)
                updated = result.get("updated", 0)
                messages.append(
                    "%s [%s=%s]: +%s new, %s updated"
                    % (rec.name, rec.tag_key or "?", rec.tag_value or "?", created, updated)
                )
            except UserError as e:
                messages.append("%s ERROR: %s" % (rec.name, e))
                notif_type = "warning"
        self._propagate_batch_alerts()
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "type": notif_type,
                "title": _("Project AWS cost fetch"),
                "message": "\n".join(messages) or _("No records."),
                "sticky": True,
            },
        }

    def action_fetch_openrouter(self):
        messages = []
        notif_type = "success"
        for rec in self:
            if not rec.openrouter_enabled:
                messages.append("%s: OpenRouter not enabled" % rec.name)
                notif_type = "warning"
                continue
            try:
                created, updated = rec._fetch_openrouter_cost_one()
                messages.append(
                    "%s [OpenRouter]: +%s new, %s updated"
                    % (rec.name, created, updated)
                )
            except UserError as e:
                messages.append("%s ERROR: %s" % (rec.name, e))
                notif_type = "warning"
        self._propagate_batch_alerts()
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "type": notif_type,
                "title": _("OpenRouter cost fetch"),
                "message": "\n".join(messages) or _("No records."),
                "sticky": True,
            },
        }

    def action_fetch_moonshot(self):
        messages = []
        notif_type = "success"
        for rec in self:
            if not rec.moonshot_enabled:
                messages.append("%s: Moonshot not enabled" % rec.name)
                notif_type = "warning"
                continue
            try:
                created, updated = rec._fetch_moonshot_cost_one()
                messages.append(
                    "%s [Moonshot]: +%s new, %s updated"
                    % (rec.name, created, updated)
                )
            except UserError as e:
                messages.append("%s ERROR: %s" % (rec.name, e))
                notif_type = "warning"
        self._propagate_batch_alerts()
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "type": notif_type,
                "title": _("Moonshot cost fetch"),
                "message": "\n".join(messages) or _("No records."),
                "sticky": True,
            },
        }

    def action_fetch_openai(self):
        messages = []
        notif_type = "success"
        for rec in self:
            if not rec.openai_enabled:
                messages.append("%s: OpenAI not enabled" % rec.name)
                notif_type = "warning"
                continue
            try:
                created, updated = rec._fetch_openai_cost_one()
                messages.append(
                    "%s [OpenAI]: +%s new, %s updated"
                    % (rec.name, created, updated)
                )
            except UserError as e:
                messages.append("%s ERROR: %s" % (rec.name, e))
                notif_type = "warning"
        self._propagate_batch_alerts()
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "type": notif_type,
                "title": _("OpenAI cost fetch"),
                "message": "\n".join(messages) or _("No records."),
                "sticky": True,
            },
        }

    def action_fetch_gcp(self):
        messages = []
        notif_type = "success"
        for rec in self:
            if not rec.gcp_enabled:
                messages.append("%s: GCP not enabled" % rec.name)
                notif_type = "warning"
                continue
            try:
                created, updated = rec._fetch_gcp_cost_one()
                messages.append(
                    "%s [GCP]: +%s new, %s updated"
                    % (rec.name, created, updated)
                )
            except UserError as e:
                messages.append("%s ERROR: %s" % (rec.name, e))
                notif_type = "warning"
        self._propagate_batch_alerts()
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "type": notif_type,
                "title": _("GCP cost fetch"),
                "message": "\n".join(messages) or _("No records."),
                "sticky": True,
            },
        }

    def _fetch_moonshot_cost_one(self):
        # Moonshot exposes only current balance, not history — delta tracking
        # against moonshot_last_used_usd is the only way to derive consumption.
        self.ensure_one()
        if not self.moonshot_api_key:
            raise UserError(_(
                "Moonshot API Key required. Create one at "
                "platform.moonshot.ai/console/api-keys."
            ))
        try:
            resp = requests.get(
                MOONSHOT_BALANCE_URL,
                headers={"Authorization": f"Bearer {self.moonshot_api_key}"},
                timeout=60,
            )
        except requests.RequestException as e:
            raise UserError(_("Moonshot request failed: %s") % e)
        if resp.status_code in (401, 403):
            raise UserError(_(
                "Moonshot returned HTTP %(s)s. Check that the API key is valid "
                "and has permission to read account balance."
            ) % {"s": resp.status_code})
        if resp.status_code != 200:
            raise UserError(
                _("Moonshot returned HTTP %(s)s: %(t)s")
                % {"s": resp.status_code, "t": resp.text[:300]}
            )
        try:
            payload = resp.json()
        except ValueError:
            raise UserError(_("Moonshot returned non-JSON response."))
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            raise UserError(_("Moonshot returned unexpected response shape."))

        available = float(data.get("available_balance") or 0.0)
        cash = float(data.get("cash_balance") or 0.0)
        current_used = max(cash - available, 0.0)

        today = date.today()
        Line = self.env["etp.project.aws.cost.line"]
        created = updated = 0

        if not self.moonshot_last_used_at:
            existing_day = Line.search(
                [
                    ("budget_id", "=", self.id),
                    ("period", "=", today),
                    ("granularity", "=", "day"),
                    ("service_name", "=", MOONSHOT_SERVICE_NAME),
                ],
                limit=1,
            )
            vals = {
                "budget_id": self.id,
                "period": today,
                "granularity": "day",
                "service_name": MOONSHOT_SERVICE_NAME,
                "amount_source": current_used,
                "source": "moonshot",
            }
            if existing_day:
                existing_day.write(vals)
                updated += 1
            else:
                Line.create(vals)
                created += 1
        else:
            delta = max(current_used - (self.moonshot_last_used_usd or 0.0), 0.0)
            if delta > 0.0:
                day_row = Line.search(
                    [
                        ("budget_id", "=", self.id),
                        ("period", "=", today),
                        ("granularity", "=", "day"),
                        ("service_name", "=", MOONSHOT_SERVICE_NAME),
                    ],
                    limit=1,
                )
                if day_row:
                    day_row.amount_source = (day_row.amount_source or 0.0) + delta
                    updated += 1
                else:
                    Line.create({
                        "budget_id": self.id,
                        "period": today,
                        "granularity": "day",
                        "service_name": MOONSHOT_SERVICE_NAME,
                        "amount_source": delta,
                        "source": "moonshot",
                    })
                    created += 1

        self.moonshot_last_used_usd = current_used
        self.moonshot_last_used_at = fields.Datetime.now()
        self.last_moonshot_fetched_at = fields.Datetime.now()
        self.invalidate_recordset(['cost_line_ids'])
        return created, updated

    def _fetch_openai_cost_one(self):
        self.ensure_one()
        if not self.openai_api_key:
            raise UserError(_(
                "OpenAI Admin API Key required. Use a sk-admin-* key from "
                "platform.openai.com/settings/organization/admin-keys "
                "(regular project keys will NOT work for /v1/organization/costs)."
            ))
        import time
        end = date.today() + timedelta(days=1)
        start = end.replace(day=1) - relativedelta(months=self.fetch_months or 6)
        start_ts = int(time.mktime(start.timetuple()))
        end_ts = int(time.mktime(end.timetuple()))
        params = {
            "start_time": start_ts,
            "end_time": end_ts,
            "bucket_width": "1d",
            "limit": 180,
        }
        if self.openai_project_id:
            params["project_ids"] = [self.openai_project_id.strip()]

        buckets = []
        next_page = None
        while True:
            page_params = dict(params)
            if next_page:
                page_params["page"] = next_page
            try:
                resp = requests.get(
                    OPENAI_COSTS_URL,
                    headers={"Authorization": f"Bearer {self.openai_api_key}"},
                    params=page_params,
                    timeout=60,
                )
            except requests.RequestException as e:
                raise UserError(_("OpenAI request failed: %s") % e)
            if resp.status_code in (401, 403):
                raise UserError(_(
                    "OpenAI returned HTTP %(s)s. The /v1/organization/costs endpoint "
                    "requires an Admin API key (sk-admin-*), not a regular project key. "
                    "Create one at platform.openai.com/settings/organization/admin-keys."
                ) % {"s": resp.status_code})
            if resp.status_code != 200:
                raise UserError(
                    _("OpenAI returned HTTP %(s)s: %(t)s")
                    % {"s": resp.status_code, "t": resp.text[:300]}
                )
            try:
                payload = resp.json()
            except ValueError:
                raise UserError(_("OpenAI returned non-JSON response."))
            page_buckets = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(page_buckets, list):
                raise UserError(_("OpenAI returned unexpected response shape."))
            buckets.extend(page_buckets)
            if not payload.get("has_more"):
                break
            next_page = payload.get("next_page")
            if not next_page:
                break

        by_day = {}
        for bucket in buckets:
            if not isinstance(bucket, dict):
                continue
            bstart = bucket.get("start_time")
            if bstart is None:
                continue
            try:
                bday = date.fromtimestamp(int(bstart))
            except (TypeError, ValueError, OSError):
                continue
            for result in bucket.get("results") or []:
                if not isinstance(result, dict):
                    continue
                amt = result.get("amount") or {}
                try:
                    value = float(amt.get("value") or 0.0)
                except (TypeError, ValueError):
                    continue
                by_day[bday] = by_day.get(bday, 0.0) + value

        created, updated = self._upsert_provider_rows(
            OPENAI_SERVICE_NAME, "openai", by_day,
        )
        try:
            br_c, br_u, _br_hits = self._fetch_openai_model_breakdown(
                start_ts, end_ts,
            )
            created += br_c
            updated += br_u
        except UserError as bre:
            _logger.warning(
                "OpenAI model breakdown failed for budget %s: %s", self.id, bre,
            )
        except Exception:
            _logger.exception(
                "Unexpected OpenAI breakdown error for budget %s", self.id,
            )
        self.last_openai_fetched_at = fields.Datetime.now()
        self.invalidate_recordset(['cost_line_ids'])
        return created, updated

    def _fetch_gcp_cost_one(self):
        self.ensure_one()
        if not (self.gcp_project_id and self.gcp_bq_dataset and self.gcp_bq_table):
            raise UserError(_(
                "Set GCP Project ID, BigQuery Dataset, and BigQuery Table first. "
                "See GCP_SETUP_GUIDE.md."
            ))
        if not self.gcp_service_account_json:
            raise UserError(_(
                "Paste the GCP service-account JSON key into the budget. "
                "See GCP_SETUP_GUIDE.md."
            ))
        for ref_name, ref_val in (
            ("GCP Project ID", self.gcp_project_id),
            ("BigQuery Dataset", self.gcp_bq_dataset),
            ("BigQuery Table", self.gcp_bq_table),
        ):
            # BigQuery doesn't parameterise table refs — guard against SQL injection
            # by allowlisting the small set of chars that legally appear in GCP refs.
            if not re.fullmatch(r"[A-Za-z0-9_\-.*]+", ref_val or ""):
                raise UserError(_(
                    "%(n)s contains unsupported characters. Allowed: "
                    "letters, digits, '_', '-', '.', '*'."
                ) % {"n": ref_name})
        try:
            sa_info = json.loads(self.gcp_service_account_json)
        except ValueError as e:
            raise UserError(_("GCP service-account JSON is not valid JSON: %s") % e)
        try:
            from google.oauth2 import service_account
            from google.cloud import bigquery
        except ImportError:
            raise UserError(_(
                "Python packages 'google-cloud-bigquery' and 'google-auth' must be "
                "installed on the Odoo server."
            ))
        try:
            credentials = service_account.Credentials.from_service_account_info(sa_info)
        except Exception as e:
            raise UserError(_("Failed to load GCP credentials: %s") % e)
        try:
            client = bigquery.Client(project=self.gcp_project_id, credentials=credentials)
        except Exception as e:
            raise UserError(_("Failed to build BigQuery client: %s") % e)

        end_date = date.today()
        start_date = (end_date.replace(day=1) - relativedelta(months=self.fetch_months or 6))
        table_ref = "`%s.%s.%s`" % (
            self.gcp_project_id, self.gcp_bq_dataset, self.gcp_bq_table,
        )
        where_clauses = ["DATE(usage_start_time) BETWEEN @start_date AND @end_date"]
        params = [
            bigquery.ScalarQueryParameter("start_date", "DATE", start_date),
            bigquery.ScalarQueryParameter("end_date", "DATE", end_date),
        ]
        if self.gcp_service_filter:
            where_clauses.append("service.description = @service_filter")
            params.append(bigquery.ScalarQueryParameter(
                "service_filter", "STRING", self.gcp_service_filter,
            ))
        if self.gcp_label_key and self.gcp_label_value:
            where_clauses.append(
                "EXISTS (SELECT 1 FROM UNNEST(labels) AS l "
                "WHERE l.key = @label_key AND l.value = @label_value)"
            )
            params.append(bigquery.ScalarQueryParameter(
                "label_key", "STRING", self.gcp_label_key,
            ))
            params.append(bigquery.ScalarQueryParameter(
                "label_value", "STRING", self.gcp_label_value,
            ))
        where_sql = " AND ".join(where_clauses)
        sql = (
            "WITH base AS (\n"
            "  SELECT\n"
            "    DATE(usage_start_time) AS day,\n"
            "    service.description AS service_name,\n"
            "    cost\n"
            "  FROM " + table_ref + "\n"
            "  WHERE " + where_sql + "\n"
            ")\n"
            "SELECT day AS period, service_name, "
            "SUM(cost) AS amount\n"
            "FROM base GROUP BY day, service_name HAVING amount != 0"
        )
        job_config = bigquery.QueryJobConfig(query_parameters=params)
        try:
            rows = list(client.query(sql, job_config=job_config).result())
        except Exception as e:
            raise UserError(_("BigQuery query failed: %s") % e)

        by_day = {}
        service_seen = {}
        for row in rows:
            try:
                period = row["period"]
                service_name = (row["service_name"] or GCP_DEFAULT_SERVICE_NAME).strip() \
                    or GCP_DEFAULT_SERVICE_NAME
                amount = float(row["amount"] or 0.0)
            except (KeyError, TypeError, ValueError):
                continue
            if amount == 0.0:
                continue
            by_day.setdefault(service_name, {})[period] = (
                by_day[service_name].get(period, 0.0) + amount
            )
            service_seen[service_name] = True

        Line = self.env["etp.project.aws.cost.line"]
        created = updated = 0
        for service_name in service_seen:
            day_bucket = by_day.get(service_name, {})
            sub_c, sub_u = self._upsert_provider_rows(
                service_name, "gcp", day_bucket,
            )
            created += sub_c
            updated += sub_u

        self.last_gcp_fetched_at = fields.Datetime.now()
        self.invalidate_recordset(['cost_line_ids'])
        return created, updated

    def _fetch_openrouter_cost_one(self):
        self.ensure_one()
        if not self.openrouter_api_key:
            raise UserError(_(
                "OpenRouter API Key required. Use a Management API key "
                "(not a regular inference key) — see openrouter.ai/settings/keys."
            ))
        try:
            resp = requests.get(
                OPENROUTER_ACTIVITY_URL,
                headers={"Authorization": f"Bearer {self.openrouter_api_key}"},
                timeout=60,
            )
        except requests.RequestException as e:
            raise UserError(_("OpenRouter request failed: %s") % e)
        if resp.status_code in (401, 403):
            raise UserError(_(
                "OpenRouter returned HTTP %(s)s. The /activity endpoint requires a "
                "Management API key, not a regular inference key. "
                "Create one at openrouter.ai/settings/keys."
            ) % {"s": resp.status_code})
        if resp.status_code != 200:
            raise UserError(
                _("OpenRouter returned HTTP %(s)s: %(t)s")
                % {"s": resp.status_code, "t": resp.text[:300]}
            )
        try:
            payload = resp.json()
        except ValueError:
            raise UserError(_("OpenRouter returned non-JSON response."))
        rows_raw = payload.get("data") if isinstance(payload, dict) else payload
        if not isinstance(rows_raw, list):
            raise UserError(_("OpenRouter returned unexpected response shape."))

        by_day = {}
        for r in rows_raw:
            day_str = (r.get("date") or "")[:10]
            if not day_str:
                continue
            try:
                day = fields.Date.from_string(day_str)
            except (TypeError, ValueError):
                continue
            usage = float(r.get("usage") or 0.0)
            by_day[day] = by_day.get(day, 0.0) + usage

        created, updated = self._upsert_provider_rows(
            OPENROUTER_SERVICE_NAME, "openrouter", by_day,
        )
        self.last_openrouter_fetched_at = fields.Datetime.now()
        self.invalidate_recordset(['cost_line_ids'])
        return created, updated

    def _upsert_provider_rows(self, service_name, source, by_day):
        self.ensure_one()
        Line = self.env["etp.project.aws.cost.line"]
        created = updated = 0
        for period, amount in by_day.items():
            if amount == 0.0:
                continue
            existing = Line.search(
                [
                    ("budget_id", "=", self.id),
                    ("period", "=", period),
                    ("granularity", "=", "day"),
                    ("service_name", "=", service_name),
                ],
                limit=1,
            )
            vals = {
                "budget_id": self.id,
                "period": period,
                "granularity": "day",
                "service_name": service_name,
                "amount_source": amount,
                "source": source,
            }
            if existing:
                existing.write(vals)
                updated += 1
            else:
                Line.create(vals)
                created += 1
        return created, updated

    def _fetch_bedrock_model_breakdown(
        self, client, base_tag_filter, start_month, end_day,
    ):
        self.ensure_one()
        bedrock_filter = {
            "And": [
                base_tag_filter,
                {"Dimensions": {"Key": "SERVICE", "Values": [BEDROCK_SERVICE_NAME]}},
            ]
        }
        ce_groupby = [{"Type": "DIMENSION", "Key": "USAGE_TYPE"}]
        metrics = ["UnblendedCost", "UsageQuantity"]
        api_hit_count = 0
        api_hit_count += 1
        resp_day = client.get_cost_and_usage(
            TimePeriod={
                "Start": start_month.strftime("%Y-%m-%d"),
                "End": end_day.strftime("%Y-%m-%d"),
            },
            Granularity="DAILY",
            Metrics=metrics,
            Filter=bedrock_filter,
            GroupBy=ce_groupby,
        )
        Line = self.env["etp.project.aws.cost.line"]
        created = updated = 0
        for result in resp_day.get("ResultsByTime", []):
            period = fields.Date.from_string(result["TimePeriod"]["Start"])
            for group in result.get("Groups", []):
                usage_type = (group["Keys"][0] if group.get("Keys") else "").strip()
                if not usage_type:
                    continue
                metrics_dict = group.get("Metrics") or {}
                cost_metric = metrics_dict.get("UnblendedCost") or {}
                qty_metric = metrics_dict.get("UsageQuantity") or {}
                amount = float(cost_metric.get("Amount") or 0.0)
                quantity = float(qty_metric.get("Amount") or 0.0)
                if amount == 0.0 and quantity == 0.0:
                    continue
                model_name, token_type = _parse_bedrock_usage_type(usage_type)
                if not model_name:
                    model_name = usage_type
                existing = Line.search(
                    [
                        ("budget_id", "=", self.id),
                        ("period", "=", period),
                        ("granularity", "=", "day"),
                        ("service_name", "=", BEDROCK_SERVICE_NAME),
                        ("model_name", "=", model_name),
                        ("token_type", "=", token_type),
                        ("is_model_breakdown", "=", True),
                    ],
                    limit=1,
                )
                vals = {
                    "budget_id": self.id,
                    "period": period,
                    "granularity": "day",
                    "service_name": BEDROCK_SERVICE_NAME,
                    "source": "aws",
                    "model_name": model_name,
                    "usage_type": usage_type,
                    "token_type": token_type,
                    "usage_quantity": quantity,
                    "usage_unit": qty_metric.get("Unit") or "",
                    "amount_source": amount,
                    "is_model_breakdown": True,
                }
                if existing:
                    existing.write(vals)
                    updated += 1
                else:
                    Line.create(vals)
                    created += 1
        return created, updated, api_hit_count

    def _fetch_openai_model_breakdown(self, start_ts, end_ts):
        self.ensure_one()
        headers = {"Authorization": f"Bearer {self.openai_api_key}"}
        base_params = {
            "start_time": start_ts,
            "end_time": end_ts,
            "bucket_width": "1d",
            "limit": 180,
            "group_by": ["model"],
        }
        if self.openai_project_id:
            base_params["project_ids"] = [self.openai_project_id.strip()]

        api_hit_count = 0
        tokens_by_model = {}
        next_page = None
        while True:
            page_params = dict(base_params)
            if next_page:
                page_params["page"] = next_page
            api_hit_count += 1
            resp = requests.get(
                OPENAI_USAGE_COMPLETIONS_URL,
                headers=headers,
                params=page_params,
                timeout=60,
            )
            if resp.status_code in (401, 403):
                raise UserError(_(
                    "OpenAI per-model usage denied (HTTP %(s)s). The admin "
                    "key must have the 'api.usage.read' scope (and the org "
                    "must have Usage API access). Body: %(t)s"
                ) % {"s": resp.status_code, "t": resp.text[:200]})
            if resp.status_code != 200:
                raise UserError(_(
                    "OpenAI /usage/completions HTTP %(s)s: %(t)s"
                ) % {"s": resp.status_code, "t": resp.text[:200]})
            try:
                payload = resp.json()
            except ValueError:
                raise UserError(_(
                    "OpenAI /usage/completions returned non-JSON"
                ))
            data = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(data, list):
                raise UserError(_(
                    "OpenAI /usage/completions returned unexpected shape"
                ))
            for bucket in data:
                if not isinstance(bucket, dict):
                    continue
                try:
                    day = date.fromtimestamp(int(bucket.get("start_time")))
                except (TypeError, ValueError, OSError):
                    continue
                for result in bucket.get("results") or []:
                    if not isinstance(result, dict):
                        continue
                    model = (result.get("model") or "").strip()
                    if not model:
                        continue
                    try:
                        inp = int(result.get("input_tokens") or 0)
                        out = int(result.get("output_tokens") or 0)
                        cached = int(result.get("input_cached_tokens") or 0)
                    except (TypeError, ValueError):
                        continue
                    if inp == 0 and out == 0 and cached == 0:
                        continue
                    slot = tokens_by_model.setdefault(
                        (day, model),
                        {"input": 0, "output": 0, "cache_read": 0},
                    )
                    slot["input"] += inp
                    slot["output"] += out
                    slot["cache_read"] += cached
            if not payload.get("has_more"):
                break
            next_page = payload.get("next_page")
            if not next_page:
                break

        Line = self.env["etp.project.aws.cost.line"]
        created = updated = 0
        for (day, model), tokens in tokens_by_model.items():
            for token_type in ("input", "output", "cache_read"):
                qty = tokens[token_type]
                if qty == 0:
                    continue
                existing = Line.search(
                    [
                        ("budget_id", "=", self.id),
                        ("period", "=", day),
                        ("granularity", "=", "day"),
                        ("service_name", "=", OPENAI_SERVICE_NAME),
                        ("model_name", "=", model),
                        ("token_type", "=", token_type),
                        ("is_model_breakdown", "=", True),
                    ],
                    limit=1,
                )
                vals = {
                    "budget_id": self.id,
                    "period": day,
                    "granularity": "day",
                    "service_name": OPENAI_SERVICE_NAME,
                    "source": "openai",
                    "model_name": model,
                    "usage_type": "",
                    "token_type": token_type,
                    "usage_quantity": float(qty),
                    "usage_unit": "Tokens",
                    "amount_source": 0.0,
                    "is_model_breakdown": True,
                }
                if existing:
                    existing.write(vals)
                    updated += 1
                else:
                    Line.create(vals)
                    created += 1
        _logger.info(
            "OpenAI model breakdown for budget %s: models=%s "
            "created=%s updated=%s api_hits=%s",
            self.id, len(tokens_by_model), created, updated, api_hit_count,
        )
        return created, updated, api_hit_count

    def _fetch_log_snapshot_base(self, source):
        self.ensure_one()
        return {
            "budget_id": self.id,
            "triggered_by_id": self.env.uid or False,
            "source": source or "other",
            "tag_key": self.tag_key or "",
            "tag_value": self.tag_value or "",
            "fetch_months": self.fetch_months or 0,
        }

    def _create_fetch_log(self, vals):
        return self.env["etp.project.aws.cost.fetch.log"].sudo().create(vals)

    def _fetch_cost_one(self, source="other"):
        self.ensure_one()
        snapshot = self._fetch_log_snapshot_base(source)
        api_hit_count = 0
        api_hit_cost_usd = 0.0
        try:
            if not (self.aws_access_key_id and self.aws_secret_access_key and self.aws_region):
                raise UserError(_("Set AWS Access Key ID, Secret, and Region first."))
            if not (self.tag_key and self.tag_value):
                raise UserError(_("Set Tag Key and Tag Value first."))
            try:
                import boto3
            except ImportError:
                raise UserError(_("Python package 'boto3' is not installed."))
            end_month = date.today().replace(day=1)
            start_month = end_month - relativedelta(months=self.fetch_months or 6)
            end_day = date.today() + timedelta(days=1)
            client = boto3.client(
                "ce",
                aws_access_key_id=self.aws_access_key_id,
                aws_secret_access_key=self.aws_secret_access_key,
                region_name=self.aws_region,
            )
            ce_filter = {
                "Tags": {"Key": self.tag_key, "Values": [self.tag_value]}
            }
            ce_groupby = [{"Type": "DIMENSION", "Key": "SERVICE"}]
            try:
                api_hit_count += 1
                resp_day = client.get_cost_and_usage(
                    TimePeriod={
                        "Start": start_month.strftime("%Y-%m-%d"),
                        "End": end_day.strftime("%Y-%m-%d"),
                    },
                    Granularity="DAILY",
                    Metrics=["UnblendedCost"],
                    Filter=ce_filter,
                    GroupBy=ce_groupby,
                )
            except Exception as e:
                raise UserError(_("AWS Cost Explorer call failed: %s") % e)
            api_hit_cost_usd = round(
                api_hit_count * AWS_CE_COST_PER_REQUEST_USD, 4,
            )
            Line = self.env["etp.project.aws.cost.line"]
            created = updated = 0
            for result in resp_day.get("ResultsByTime", []):
                period = fields.Date.from_string(result["TimePeriod"]["Start"])
                for group in result.get("Groups", []):
                    service_name = (group["Keys"][0] if group.get("Keys") else "").strip() or "Unknown"
                    amount = float(group["Metrics"]["UnblendedCost"]["Amount"])
                    if amount == 0.0:
                        continue
                    existing = Line.search(
                        [
                            ("budget_id", "=", self.id),
                            ("period", "=", period),
                            ("granularity", "=", "day"),
                            ("service_name", "=", service_name),
                            ("is_model_breakdown", "=", False),
                        ],
                        limit=1,
                    )
                    vals = {
                        "budget_id": self.id,
                        "period": period,
                        "granularity": "day",
                        "service_name": service_name,
                        "amount_source": amount,
                        "source": "aws",
                    }
                    if existing:
                        existing.write(vals)
                        updated += 1
                    else:
                        Line.create(vals)
                        created += 1
            try:
                br_created, br_updated, br_api_hits = self._fetch_bedrock_model_breakdown(
                    client, ce_filter, start_month, end_day,
                )
                created += br_created
                updated += br_updated
                api_hit_count += br_api_hits
                api_hit_cost_usd = round(
                    api_hit_count * AWS_CE_COST_PER_REQUEST_USD, 4,
                )
            except Exception:
                _logger.exception(
                    "Bedrock per-model breakdown failed for budget %s", self.id,
                )
            self.last_fetched_at = fields.Datetime.now()
            self.invalidate_recordset(['cost_line_ids'])
            if self.openrouter_enabled and self.openrouter_api_key:
                try:
                    or_created, or_updated = self._fetch_openrouter_cost_one()
                    created += or_created
                    updated += or_updated
                except UserError as ore:
                    _logger.warning(
                        "OpenRouter fetch failed for budget %s: %s", self.id, ore,
                    )
                except Exception:
                    _logger.exception(
                        "Unexpected OpenRouter error for budget %s", self.id,
                    )
            if self.moonshot_enabled and self.moonshot_api_key:
                try:
                    ms_created, ms_updated = self._fetch_moonshot_cost_one()
                    created += ms_created
                    updated += ms_updated
                except UserError as mse:
                    _logger.warning(
                        "Moonshot fetch failed for budget %s: %s", self.id, mse,
                    )
                except Exception:
                    _logger.exception(
                        "Unexpected Moonshot error for budget %s", self.id,
                    )
            if self.openai_enabled and self.openai_api_key:
                try:
                    oa_created, oa_updated = self._fetch_openai_cost_one()
                    created += oa_created
                    updated += oa_updated
                except UserError as oae:
                    _logger.warning(
                        "OpenAI fetch failed for budget %s: %s", self.id, oae,
                    )
                except Exception:
                    _logger.exception(
                        "Unexpected OpenAI error for budget %s", self.id,
                    )
            if (self.gcp_enabled and self.gcp_project_id
                    and self.gcp_bq_dataset and self.gcp_bq_table
                    and self.gcp_service_account_json):
                try:
                    g_created, g_updated = self._fetch_gcp_cost_one()
                    created += g_created
                    updated += g_updated
                except UserError as ge:
                    _logger.warning(
                        "GCP fetch failed for budget %s: %s", self.id, ge,
                    )
                except Exception:
                    _logger.exception(
                        "Unexpected GCP error for budget %s", self.id,
                    )
            self._create_fetch_log({
                **snapshot,
                "status": "success",
                "created_count": created,
                "updated_count": updated,
                "api_hit_count": api_hit_count,
                "api_hit_cost_usd": api_hit_cost_usd,
            })
            return {
                "created": created,
                "updated": updated,
                "api_hit_count": api_hit_count,
                "api_hit_cost_usd": api_hit_cost_usd,
            }
        except UserError as ue:
            self._create_fetch_log({
                **snapshot,
                "status": "error",
                "error_message": str(ue),
                "api_hit_count": api_hit_count,
                "api_hit_cost_usd": api_hit_cost_usd,
            })
            raise
        except Exception as exc:
            _logger.exception(
                "Unexpected error in _fetch_cost_one for budget %s", self.id,
            )
            self._create_fetch_log({
                **snapshot,
                "status": "error",
                "error_message": str(exc),
                "api_hit_count": api_hit_count,
                "api_hit_cost_usd": api_hit_cost_usd,
            })
            raise

    @api.depends("fetch_log_ids")
    def _compute_fetch_log_count(self):
        for rec in self:
            rec.fetch_log_count = len(rec.fetch_log_ids)
