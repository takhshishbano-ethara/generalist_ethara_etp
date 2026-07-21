import json
import logging
import re
import threading
from datetime import date, timedelta
import requests
from dateutil.relativedelta import relativedelta
from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.modules.registry import Registry

_logger = logging.getLogger(__name__)


BUDGET_STATE_SELECTION = [
    ('draft', 'Draft'),
    ('approved', 'Approved'),
    ('in_progress', 'In Progress'),
    ('delivered', 'Delivered'),
    ('closed', 'Closed'),
    ('rejected', 'Rejected'),
    ('withdrawn', 'Withdrawn'),
]

BUDGET_TYPE_SELECTION = [
    ('rnd', 'R&D'),
    # Stored key stays 'operations' (no data migration); only the display
    # label is "Production".
    ('operations', 'Production'),
]

PRIORITY_SELECTION = [
    ('low', 'Low'),
    ('normal', 'Normal'),
    ('high', 'High'),
    ('urgent', 'Urgent'),
]

HEALTH_SELECTION = [
    ('unknown', 'Unknown'),
    ('healthy', 'Healthy'),
    ('warning', 'Warning'),
    ('at_risk', 'At Risk'),
    ('critical', 'Critical'),
]

HEALTH_HEALTHY_PCT = 60.0
HEALTH_WARNING_PCT = 80.0
HEALTH_AT_RISK_PCT = 100.0

OPENROUTER_SERVICE_NAME = 'OpenRouter'
OPENROUTER_ACTIVITY_URL = 'https://openrouter.ai/api/v1/activity'
MOONSHOT_SERVICE_NAME = 'Moonshot'
MOONSHOT_BALANCE_URL = 'https://api.moonshot.ai/v1/users/me/balance'
OPENAI_SERVICE_NAME = 'OpenAI'
OPENAI_COSTS_URL = 'https://api.openai.com/v1/organization/costs'
OPENAI_USAGE_COMPLETIONS_URL = 'https://api.openai.com/v1/organization/usage/completions'
GCP_DEFAULT_SERVICE_NAME = 'GCP'

BEDROCK_SERVICE_NAME = 'Amazon Bedrock'
BEDROCK_REGION_PREFIX_RE = re.compile(r'^[A-Z]{2,4}\d?-')
BEDROCK_SERVICE_PREFIX_RE = re.compile(r'^bedrock:', re.IGNORECASE)
BEDROCK_TOKEN_SUFFIXES = (
    ('-CacheReadInputTokens', 'cache_read'),
    ('-CacheWriteInputTokens', 'cache_write'),
    ('-InputTokens', 'input'),
    ('-OutputTokens', 'output'),
)

FETCH_SUMMARY_TEMPLATE = 'ethara_project.mail_template_ethara_cost_fetch_summary'


def _parse_bedrock_usage_type(usage_type):
    if not usage_type:
        return '', 'other'
    remainder = BEDROCK_REGION_PREFIX_RE.sub('', usage_type, count=1)
    remainder = BEDROCK_SERVICE_PREFIX_RE.sub('', remainder, count=1)
    token_type = 'other'
    for suffix, token_label in BEDROCK_TOKEN_SUFFIXES:
        if remainder.endswith(suffix):
            remainder = remainder[: -len(suffix)]
            token_type = token_label
            break
    return remainder.strip(' -'), token_type


class EtharaProjectBudget(models.Model):
    _name = 'ethara.project.budget'
    _description = 'Ethara Project Budget'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'ethara_project_id, name'
    _rec_name = 'name'

    name = fields.Char(
        string='Name',
        required=True,
        copy=False,
        readonly=True,
        default='New',
        tracking=True,
    )
    ethara_project_id = fields.Many2one(
        comodel_name='ethara.project',
        string='Project',
        required=True,
        ondelete='cascade',
        index=True,
        tracking=True,
    )
    project_type = fields.Selection(
        selection=BUDGET_TYPE_SELECTION,
        string='Budget Type',
        tracking=True,
    )
    # R&D budgets are split into sub-types (mirrors the create flow's
    # `budget_sub_type`). Only meaningful when project_type == 'rnd'.
    budget_sub_type = fields.Selection(
        selection=[('testing', 'Testing'), ('sampling', 'Sampling')],
        string='R&D Sub-type',
        tracking=True,
    )
    active = fields.Boolean(default=True)
    state = fields.Selection(
        selection=BUDGET_STATE_SELECTION,
        string='Status',
        default='draft',
        required=True,
        tracking=True,
        copy=False,
    )

    budget_amount = fields.Float(string='Budget (USD)', tracking=True)
    buffer_pct = fields.Float(string='Buffer %', default=0.0, tracking=True)
    total_tasks = fields.Integer(string='Total Tasks', default=0, tracking=True)
    description = fields.Text(string='Description', tracking=True)
    priority = fields.Selection(
        selection=PRIORITY_SELECTION,
        string='Priority',
        default='normal',
        tracking=True,
    )
    attachment_urls = fields.Text(
        string='Attachment URLs',
        help='Comma-separated URLs of files attached to this budget.',
    )

    model_line_ids = fields.One2many(
        comodel_name='ethara.project.budget.model.line',
        inverse_name='budget_id',
        string='Model Lines',
    )
    infra_line_ids = fields.One2many(
        comodel_name='ethara.project.budget.infra.line',
        inverse_name='budget_id',
        string='Infrastructure Lines',
    )
    subscription_line_ids = fields.One2many(
        comodel_name='ethara.project.budget.subscription.line',
        inverse_name='budget_id',
        string='Subscription Lines',
    )
    tag_line_ids = fields.One2many(
        comodel_name='ethara.project.budget.tag.line',
        inverse_name='budget_id',
        string='AWS Tag Filters',
    )
    cost_line_ids = fields.One2many(
        comodel_name='ethara.project.cost.line',
        inverse_name='budget_id',
        string='Cost Lines',
    )
    fetch_log_ids = fields.One2many(
        comodel_name='ethara.project.cost.fetch.log',
        inverse_name='budget_id',
        string='Fetch History',
    )
    cost_line_count = fields.Integer(
        string='Cost Line Count',
        compute='_compute_cost_line_count',
    )
    fetch_log_count = fields.Integer(
        string='Fetch Log Count',
        compute='_compute_fetch_log_count',
    )

    approver_user_ids = fields.Many2many(
        comodel_name='res.users',
        relation='ethara_project_budget_approver_rel',
        column1='budget_id',
        column2='user_id',
        string='Approvers',
    )

    fetch_months = fields.Integer(default=6, string='Months to Fetch')

    aws_access_key_id = fields.Char(string='AWS Access Key ID')
    aws_secret_access_key = fields.Char(string='AWS Secret Access Key')
    aws_region = fields.Char(default='us-east-1', string='AWS Region')

    openrouter_enabled = fields.Boolean(string='Fetch OpenRouter Costs')
    openrouter_api_key = fields.Char(string='OpenRouter API Key')
    openrouter_inference_key_hash = fields.Char(
        string='OpenRouter Inference Key Hash',
    )
    last_openrouter_fetched_at = fields.Datetime(
        readonly=True, string='Last OpenRouter Fetch',
    )

    moonshot_enabled = fields.Boolean(string='Fetch Moonshot Costs')
    moonshot_api_key = fields.Char(string='Moonshot API Key')
    moonshot_last_used_usd = fields.Float(
        readonly=True, string='Moonshot Baseline (USD)',
    )
    moonshot_last_used_at = fields.Datetime(
        readonly=True, string='Moonshot Baseline Recorded At',
    )
    last_moonshot_fetched_at = fields.Datetime(
        readonly=True, string='Last Moonshot Fetch',
    )

    openai_enabled = fields.Boolean(string='Fetch OpenAI Costs')
    openai_api_key = fields.Char(string='OpenAI Admin API Key')
    openai_project_id = fields.Char(string='OpenAI Project ID')
    last_openai_fetched_at = fields.Datetime(
        readonly=True, string='Last OpenAI Fetch',
    )

    gcp_enabled = fields.Boolean(string='Fetch GCP Costs')
    gcp_project_id = fields.Char(string='GCP Project ID')
    gcp_bq_dataset = fields.Char(string='BigQuery Dataset', default='billing_export')
    gcp_bq_table = fields.Char(string='BigQuery Table')
    gcp_service_account_json = fields.Text(string='GCP Service Account JSON')
    gcp_service_filter = fields.Char(string='GCP Service Filter')
    gcp_label_key = fields.Char(string='GCP Label Key')
    gcp_label_value = fields.Char(string='GCP Label Value')
    last_gcp_fetched_at = fields.Datetime(
        readonly=True, string='Last GCP Fetch',
    )

    last_fetched_at = fields.Datetime(readonly=True, tracking=True)

    llm_consumed_amount = fields.Float(
        string='LLM Consumed (USD)',
        compute='_compute_totals',
    )
    consumed_amount = fields.Float(
        string='Consumed (USD)',
        compute='_compute_totals',
    )
    remaining_amount = fields.Float(
        string='Remaining (USD)',
        compute='_compute_totals',
    )
    consumed_pct = fields.Float(
        string='Consumed %',
        compute='_compute_totals',
    )
    health_status = fields.Selection(
        selection=HEALTH_SELECTION,
        string='Budget Health',
        compute='_compute_totals',
        store=True,
    )
    is_rnd = fields.Boolean(
        string='R&D Project',
        compute='_compute_is_rnd',
        store=True,
    )

    @api.depends('cost_line_ids')
    def _compute_cost_line_count(self):
        for rec in self:
            rec.cost_line_count = len(rec.cost_line_ids)

    @api.depends('fetch_log_ids')
    def _compute_fetch_log_count(self):
        for rec in self:
            rec.fetch_log_count = len(rec.fetch_log_ids)

    @api.depends('project_type')
    def _compute_is_rnd(self):
        for rec in self:
            rec.is_rnd = rec.project_type == 'rnd'

    @api.depends(
        'budget_amount',
        'cost_line_ids.amount_source',
        'cost_line_ids.granularity',
        'cost_line_ids.is_model_breakdown',
    )
    def _compute_totals(self):
        for rec in self:
            envelope = rec.budget_amount or 0.0
            llm_consumed = sum(
                line.amount_source or 0.0
                for line in rec.cost_line_ids
                if line.granularity == 'day' and not line.is_model_breakdown
            )
            rec.llm_consumed_amount = llm_consumed
            rec.consumed_amount = llm_consumed
            rec.remaining_amount = envelope - llm_consumed
            pct = (llm_consumed / envelope * 100.0) if envelope else 0.0
            rec.consumed_pct = pct
            if not envelope:
                rec.health_status = 'unknown'
            elif pct < HEALTH_HEALTHY_PCT:
                rec.health_status = 'healthy'
            elif pct < HEALTH_WARNING_PCT:
                rec.health_status = 'warning'
            elif pct < HEALTH_AT_RISK_PCT:
                rec.health_status = 'at_risk'
            else:
                rec.health_status = 'critical'

    def _active_tag_pairs(self):
        self.ensure_one()
        pairs = []
        for line in self.tag_line_ids:
            if not line.active:
                continue
            key = (line.tag_key or '').strip()
            value = (line.tag_value or '').strip()
            if not key or not value:
                continue
            pairs.append((key, value))
        return pairs

    def _post_project_thread_note(self, body, partner_ids=None):
        if self.env.context.get('ethara_skip_notify'):
            return
        dbname = self.env.cr.dbname
        uid = self.env.uid
        ctx = dict(self.env.context)
        posts = []
        for rec in self:
            project = rec.ethara_project_id
            if not project:
                continue
            targets = partner_ids
            if targets is None:
                targets = project._ethara_thread_partner_ids()
            posts.append((project.id, rec.id, list(targets or []), body))
        if not posts:
            return

        def _launch():
            def _run():
                try:
                    registry = Registry(dbname)
                    with registry.cursor() as new_cr:
                        new_env = api.Environment(new_cr, uid, ctx)
                        for project_id, rec_id, targets, body_str in posts:
                            proj = new_env['ethara.project'].sudo().browse(project_id).exists()
                            if not proj:
                                continue
                            try:
                                kwargs = {
                                    'body': body_str,
                                    'subtype_xmlid': 'mail.mt_comment',
                                    'message_type': 'comment',
                                    'partner_ids': targets,
                                }
                                kwargs = proj._ethara_thread_post_kwargs(kwargs)
                                message = proj.message_post(**kwargs)
                                proj._ethara_capture_root(message)
                            except Exception:
                                _logger.exception(
                                    'Deferred chatter post failed for budget %s',
                                    rec_id,
                                )
                        new_cr.commit()
                except Exception:
                    _logger.exception('Deferred budget chatter setup failed')
            threading.Thread(target=_run, daemon=True).start()

        self.env.cr.postcommit.add(_launch)

    def _ethara_send_budget_approval_mail(self):
        dbname = self.env.cr.dbname
        uid = self.env.uid
        ctx = dict(self.env.context)
        if ctx.get('ethara_skip_notify'):
            return
        payloads = []
        for rec in self:
            project = rec.ethara_project_id
            if not project:
                continue
            approver_partner_ids = list({
                pid for pid in rec.approver_user_ids.mapped('partner_id').ids
                if pid
            })
            if not approver_partner_ids:
                continue
            payloads.append((project.id, rec.id, approver_partner_ids))
        if not payloads:
            return

        def _launch():
            def _run():
                try:
                    registry = Registry(dbname)
                    with registry.cursor() as new_cr:
                        new_env = api.Environment(new_cr, uid, ctx)
                        for project_id, budget_id, partners in payloads:
                            proj = new_env['ethara.project'].sudo().browse(project_id).exists()
                            bud = new_env['ethara.project.budget'].sudo().browse(budget_id).exists()
                            if not proj or not bud:
                                continue
                            try:
                                proj._ethara_post_thread_message(
                                    'ethara_project.mail_template_ethara_project_budget_created',
                                    bud,
                                    partners,
                                )
                            except Exception:
                                _logger.exception(
                                    'Deferred budget approval mail failed for budget %s',
                                    budget_id,
                                )
                        new_cr.commit()
                except Exception:
                    _logger.exception('Deferred budget approval mail setup failed')
            threading.Thread(target=_run, daemon=True).start()

        self.env.cr.postcommit.add(_launch)

    def _send_fetch_summary_mail(self, provider_label, created, updated):
        self.ensure_one()
        if created == 0 and updated == 0:
            return
        project = self.ethara_project_id
        if not project:
            return
        try:
            project._ethara_post_thread_message(
                FETCH_SUMMARY_TEMPLATE,
                self,
                project._ethara_thread_partner_ids(),
                email_values={
                    'subject': '[%s] %s cost fetch: +%s new, %s updated' % (
                        project.name or '', provider_label, created, updated,
                    ),
                },
            )
        except Exception:
            _logger.exception(
                'Failed to send fetch summary mail for budget %s (%s)',
                self.id, provider_label,
            )

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', 'New') == 'New':
                vals['name'] = (
                    self.env['ir.sequence'].next_by_code('ethara.project.budget')
                    or 'New'
                )
        records = super().create(vals_list)
        for rec in records:
            body = _(
                '<p><strong>Project Budget created:</strong> %s (%.2f USD, %s)</p>'
            ) % (
                rec.name or '',
                rec.budget_amount or 0.0,
                dict(BUDGET_STATE_SELECTION).get(rec.state, rec.state or ''),
            )
            rec._post_project_thread_note(body)
        records._ethara_send_budget_approval_mail()
        return records

    def _log_fetch(self, provider, status, created, updated, error=None):
        self.ensure_one()
        return self.env['ethara.project.cost.fetch.log'].sudo().create({
            'budget_id': self.id,
            'source': (
                'manual_button'
                if self.env.context.get('ethara_manual_fetch') else 'cron'
            ),
            'provider': provider,
            'status': status,
            'created_count': created,
            'updated_count': updated,
            'error_message': error or False,
            'fetch_months': self.fetch_months or 6,
            'tags_summary': ', '.join(
                '%s=%s' % (k, v) for k, v in self._active_tag_pairs()
            ),
            'triggered_by_id': self.env.user.id,
        })

    def _upsert_provider_rows(self, service_name, source, by_day):
        self.ensure_one()
        Line = self.env['ethara.project.cost.line']
        created = updated = 0
        for period, amount in by_day.items():
            if amount == 0.0:
                continue
            existing = Line.search(
                [
                    ('budget_id', '=', self.id),
                    ('period', '=', period),
                    ('granularity', '=', 'day'),
                    ('service_name', '=', service_name),
                    ('is_model_breakdown', '=', False),
                ],
                limit=1,
            )
            vals = {
                'budget_id': self.id,
                'period': period,
                'granularity': 'day',
                'service_name': service_name,
                'amount_source': amount,
                'source': source,
            }
            if existing:
                existing.write(vals)
                updated += 1
            else:
                Line.create(vals)
                created += 1
        return created, updated

    def _get_aws_credentials(self):
        self.ensure_one()
        global_creds = self.env['ethara.project.aws.credentials'].sudo().get_credentials()
        return {
            'aws_access_key_id': (
                self.aws_access_key_id
                or global_creds.get('aws_access_key_id')
                or ''
            ),
            'aws_secret_access_key': (
                self.aws_secret_access_key
                or global_creds.get('aws_secret_access_key')
                or ''
            ),
            'region_name': (
                self.aws_region
                or global_creds.get('region_name')
                or 'us-east-1'
            ),
        }

    def _fetch_bedrock_model_breakdown(
        self, client, base_tag_filter, start_month, end_day,
        source_tag_key='', source_tag_value='',
    ):
        self.ensure_one()
        bedrock_filter = {
            'And': [
                base_tag_filter,
                {'Dimensions': {'Key': 'SERVICE', 'Values': [BEDROCK_SERVICE_NAME]}},
            ]
        }
        ce_groupby = [{'Type': 'DIMENSION', 'Key': 'USAGE_TYPE'}]
        metrics = ['UnblendedCost', 'UsageQuantity']
        resp_day = client.get_cost_and_usage(
            TimePeriod={
                'Start': start_month.strftime('%Y-%m-%d'),
                'End': end_day.strftime('%Y-%m-%d'),
            },
            Granularity='DAILY',
            Metrics=metrics,
            Filter=bedrock_filter,
            GroupBy=ce_groupby,
        )
        Line = self.env['ethara.project.cost.line']
        created = updated = 0
        for result in resp_day.get('ResultsByTime', []):
            period = fields.Date.from_string(result['TimePeriod']['Start'])
            for group in result.get('Groups', []):
                usage_type = (group['Keys'][0] if group.get('Keys') else '').strip()
                if not usage_type:
                    continue
                metrics_dict = group.get('Metrics') or {}
                cost_metric = metrics_dict.get('UnblendedCost') or {}
                qty_metric = metrics_dict.get('UsageQuantity') or {}
                amount = float(cost_metric.get('Amount') or 0.0)
                quantity = float(qty_metric.get('Amount') or 0.0)
                if amount == 0.0 and quantity == 0.0:
                    continue
                model_name, token_type = _parse_bedrock_usage_type(usage_type)
                if not model_name:
                    model_name = usage_type
                existing = Line.search(
                    [
                        ('budget_id', '=', self.id),
                        ('period', '=', period),
                        ('granularity', '=', 'day'),
                        ('service_name', '=', BEDROCK_SERVICE_NAME),
                        ('model_name', '=', model_name),
                        ('token_type', '=', token_type),
                        ('is_model_breakdown', '=', True),
                        ('source_tag_key', '=', source_tag_key),
                        ('source_tag_value', '=', source_tag_value),
                    ],
                    limit=1,
                )
                vals = {
                    'budget_id': self.id,
                    'period': period,
                    'granularity': 'day',
                    'service_name': BEDROCK_SERVICE_NAME,
                    'source': 'aws',
                    'model_name': model_name,
                    'usage_type': usage_type,
                    'token_type': token_type,
                    'usage_quantity': quantity,
                    'usage_unit': qty_metric.get('Unit') or '',
                    'amount_source': amount,
                    'is_model_breakdown': True,
                    'source_tag_key': source_tag_key,
                    'source_tag_value': source_tag_value,
                }
                if existing:
                    existing.write(vals)
                    updated += 1
                else:
                    Line.create(vals)
                    created += 1
        return created, updated

    def action_fetch_cost(self):
        for rec in self.with_context(ethara_manual_fetch=True):
            rec._fetch_aws_cost_one()

    def _fetch_aws_cost_one(self):
        self.ensure_one()
        try:
            creds = self._get_aws_credentials()
            aws_key = creds.get('aws_access_key_id')
            aws_secret = creds.get('aws_secret_access_key')
            aws_region = creds.get('region_name') or 'us-east-1'
            if not (aws_key and aws_secret):
                raise UserError(_(
                    'AWS credentials are not configured. Set them under '
                    'Ethara Projects -> AWS -> Credentials.'
                ))
            tag_pairs = self._active_tag_pairs()
            if not tag_pairs:
                raise UserError(_('Add at least one AWS Tag Filter (Key + Value).'))
            try:
                import boto3
            except ImportError:
                raise UserError(_("Python package 'boto3' is not installed."))
            end_month = date.today().replace(day=1)
            start_month = end_month - relativedelta(months=self.fetch_months or 6)
            end_day = date.today() + timedelta(days=1)
            client = boto3.client(
                'ce',
                aws_access_key_id=aws_key,
                aws_secret_access_key=aws_secret,
                region_name=aws_region,
            )
            ce_groupby = [{'Type': 'DIMENSION', 'Key': 'SERVICE'}]
            Line = self.env['ethara.project.cost.line']
            created = updated = 0
            for ce_tag_key, ce_tag_value in tag_pairs:
                ce_filter = {
                    'Tags': {'Key': ce_tag_key, 'Values': [ce_tag_value]}
                }
                try:
                    resp_day = client.get_cost_and_usage(
                        TimePeriod={
                            'Start': start_month.strftime('%Y-%m-%d'),
                            'End': end_day.strftime('%Y-%m-%d'),
                        },
                        Granularity='DAILY',
                        Metrics=['UnblendedCost'],
                        Filter=ce_filter,
                        GroupBy=ce_groupby,
                    )
                except Exception as e:
                    raise UserError(_(
                        'AWS Cost Explorer call failed for tag %(k)s=%(v)s: %(e)s'
                    ) % {'k': ce_tag_key, 'v': ce_tag_value, 'e': e})
                for result in resp_day.get('ResultsByTime', []):
                    period = fields.Date.from_string(result['TimePeriod']['Start'])
                    for group in result.get('Groups', []):
                        service_name = (
                            group['Keys'][0] if group.get('Keys') else ''
                        ).strip() or 'Unknown'
                        amount = float(group['Metrics']['UnblendedCost']['Amount'])
                        if amount == 0.0:
                            continue
                        existing = Line.search(
                            [
                                ('budget_id', '=', self.id),
                                ('period', '=', period),
                                ('granularity', '=', 'day'),
                                ('service_name', '=', service_name),
                                ('is_model_breakdown', '=', False),
                                ('source_tag_key', '=', ce_tag_key),
                                ('source_tag_value', '=', ce_tag_value),
                            ],
                            limit=1,
                        )
                        vals = {
                            'budget_id': self.id,
                            'period': period,
                            'granularity': 'day',
                            'service_name': service_name,
                            'amount_source': amount,
                            'source': 'aws',
                            'source_tag_key': ce_tag_key,
                            'source_tag_value': ce_tag_value,
                        }
                        if existing:
                            existing.write(vals)
                            updated += 1
                        else:
                            Line.create(vals)
                            created += 1
                try:
                    br_c, br_u = self._fetch_bedrock_model_breakdown(
                        client, ce_filter, start_month, end_day,
                        source_tag_key=ce_tag_key,
                        source_tag_value=ce_tag_value,
                    )
                    created += br_c
                    updated += br_u
                except Exception:
                    _logger.exception(
                        'Bedrock breakdown failed for budget %s tag %s=%s',
                        self.id, ce_tag_key, ce_tag_value,
                    )
            self.last_fetched_at = fields.Datetime.now()
            self.invalidate_recordset(['cost_line_ids'])
            self._log_fetch('aws', 'success', created, updated)
            self._post_project_thread_note(_(
                '<p><strong>AWS Cost Explorer:</strong> +%s new, %s updated</p>'
            ) % (created, updated))
            self._send_fetch_summary_mail('AWS Cost Explorer', created, updated)
            return created, updated
        except UserError as e:
            self._log_fetch('aws', 'error', 0, 0, error=str(e))
            raise
        except Exception as e:
            _logger.exception('Unexpected AWS fetch error for budget %s', self.id)
            self._log_fetch('aws', 'error', 0, 0, error=str(e))
            raise

    def action_fetch_openrouter(self):
        for rec in self.with_context(ethara_manual_fetch=True):
            if not rec.openrouter_enabled:
                raise UserError(_('OpenRouter is not enabled on this budget.'))
            rec._fetch_openrouter_cost_one()

    def _fetch_openrouter_cost_one(self):
        self.ensure_one()
        try:
            if not self.openrouter_api_key:
                raise UserError(_('OpenRouter API Key required for this budget.'))
            params = None
            key_hash = (self.openrouter_inference_key_hash or '').strip()
            if key_hash:
                params = {'api_key_hash': key_hash}
            try:
                resp = requests.get(
                    OPENROUTER_ACTIVITY_URL,
                    headers={'Authorization': 'Bearer %s' % self.openrouter_api_key},
                    params=params,
                    timeout=60,
                )
            except requests.RequestException as e:
                raise UserError(_('OpenRouter request failed: %s') % e)
            if resp.status_code in (401, 403, 404):
                raise UserError(_(
                    'OpenRouter activity denied (HTTP %(s)s). Provide a valid '
                    'inference key, or a management key with matching Inference '
                    'Key Hash.'
                ) % {'s': resp.status_code})
            if resp.status_code != 200:
                raise UserError(_(
                    'OpenRouter returned HTTP %(s)s: %(t)s'
                ) % {'s': resp.status_code, 't': resp.text[:300]})
            try:
                payload = resp.json()
            except ValueError:
                raise UserError(_('OpenRouter returned non-JSON response.'))
            rows = payload.get('data') if isinstance(payload, dict) else None
            if not isinstance(rows, list):
                raise UserError(_('OpenRouter returned unexpected response shape.'))

            cost_by_day_model = {}
            tokens_by_day_model = {}
            for row in rows:
                if not isinstance(row, dict):
                    continue
                raw_date = row.get('date')
                if not raw_date:
                    continue
                try:
                    day = fields.Date.from_string(str(raw_date)[:10])
                except (TypeError, ValueError):
                    continue
                if not day:
                    continue
                try:
                    usd = float(row.get('usage') or 0.0)
                except (TypeError, ValueError):
                    usd = 0.0
                permaslug = (row.get('model_permaslug') or '').strip()
                requested = (row.get('model') or '').strip()
                model = permaslug or requested or 'unknown'
                try:
                    inp = int(row.get('prompt_tokens') or 0)
                except (TypeError, ValueError):
                    inp = 0
                try:
                    out = int(row.get('completion_tokens') or 0)
                except (TypeError, ValueError):
                    out = 0
                key = (day, model)
                cost_by_day_model[key] = cost_by_day_model.get(key, 0.0) + usd
                slot = tokens_by_day_model.setdefault(
                    key, {'input': 0, 'output': 0},
                )
                slot['input'] += inp
                slot['output'] += out

            Line = self.env['ethara.project.cost.line']
            created = updated = 0
            seen_days = list({d for d, _m in cost_by_day_model})
            existing_rows = Line.search([
                ('budget_id', '=', self.id),
                ('granularity', '=', 'day'),
                ('service_name', '=', OPENROUTER_SERVICE_NAME),
                ('period', 'in', seen_days),
            ]) if seen_days else Line.browse()
            existing_by_key = {
                (row.period, row.model_name or '', row.token_type or False): row
                for row in existing_rows
            }
            to_create = []
            for (day, model), amount in cost_by_day_model.items():
                vals = {
                    'budget_id': self.id,
                    'period': day,
                    'granularity': 'day',
                    'service_name': OPENROUTER_SERVICE_NAME,
                    'source': 'openrouter',
                    'model_name': model,
                    'token_type': False,
                    'amount_source': amount,
                    'usage_quantity': 0.0,
                    'is_model_breakdown': True,
                }
                existing = existing_by_key.get((day, model, False))
                if existing:
                    existing.write(vals)
                    updated += 1
                else:
                    to_create.append(vals)
                tokens = tokens_by_day_model.get((day, model)) or {}
                for token_type in ('input', 'output'):
                    qty = tokens.get(token_type, 0)
                    if qty == 0:
                        continue
                    tvals = {
                        'budget_id': self.id,
                        'period': day,
                        'granularity': 'day',
                        'service_name': OPENROUTER_SERVICE_NAME,
                        'source': 'openrouter',
                        'model_name': model,
                        'token_type': token_type,
                        'usage_quantity': float(qty),
                        'usage_unit': 'Tokens',
                        'amount_source': 0.0,
                        'is_model_breakdown': True,
                    }
                    existing = existing_by_key.get((day, model, token_type))
                    if existing:
                        existing.write(tvals)
                        updated += 1
                    else:
                        to_create.append(tvals)
            if to_create:
                Line.create(to_create)
                created += len(to_create)

            self.last_openrouter_fetched_at = fields.Datetime.now()
            self.invalidate_recordset(['cost_line_ids'])
            self._log_fetch('openrouter', 'success', created, updated)
            self._post_project_thread_note(_(
                '<p><strong>OpenRouter:</strong> +%s new, %s updated</p>'
            ) % (created, updated))
            self._send_fetch_summary_mail('OpenRouter', created, updated)
            return created, updated
        except UserError as e:
            self._log_fetch('openrouter', 'error', 0, 0, error=str(e))
            raise
        except Exception as e:
            _logger.exception('Unexpected OpenRouter error for budget %s', self.id)
            self._log_fetch('openrouter', 'error', 0, 0, error=str(e))
            raise

    def action_fetch_moonshot(self):
        for rec in self.with_context(ethara_manual_fetch=True):
            if not rec.moonshot_enabled:
                raise UserError(_('Moonshot is not enabled on this budget.'))
            rec._fetch_moonshot_cost_one()

    def _fetch_moonshot_cost_one(self):
        self.ensure_one()
        try:
            if not self.moonshot_api_key:
                raise UserError(_(
                    'Moonshot API Key required. Create one at '
                    'platform.moonshot.ai/console/api-keys.'
                ))
            try:
                resp = requests.get(
                    MOONSHOT_BALANCE_URL,
                    headers={'Authorization': 'Bearer %s' % self.moonshot_api_key},
                    timeout=60,
                )
            except requests.RequestException as e:
                raise UserError(_('Moonshot request failed: %s') % e)
            if resp.status_code in (401, 403):
                raise UserError(_(
                    'Moonshot returned HTTP %(s)s. Check that the API key is valid.'
                ) % {'s': resp.status_code})
            if resp.status_code != 200:
                raise UserError(_(
                    'Moonshot returned HTTP %(s)s: %(t)s'
                ) % {'s': resp.status_code, 't': resp.text[:300]})
            try:
                payload = resp.json()
            except ValueError:
                raise UserError(_('Moonshot returned non-JSON response.'))
            data = payload.get('data') if isinstance(payload, dict) else None
            if not isinstance(data, dict):
                raise UserError(_('Moonshot returned unexpected response shape.'))

            available = float(data.get('available_balance') or 0.0)
            cash = float(data.get('cash_balance') or 0.0)
            current_used = max(cash - available, 0.0)

            today = date.today()
            Line = self.env['ethara.project.cost.line']
            created = updated = 0
            if not self.moonshot_last_used_at:
                existing_day = Line.search(
                    [
                        ('budget_id', '=', self.id),
                        ('period', '=', today),
                        ('granularity', '=', 'day'),
                        ('service_name', '=', MOONSHOT_SERVICE_NAME),
                    ],
                    limit=1,
                )
                vals = {
                    'budget_id': self.id,
                    'period': today,
                    'granularity': 'day',
                    'service_name': MOONSHOT_SERVICE_NAME,
                    'amount_source': current_used,
                    'source': 'moonshot',
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
                            ('budget_id', '=', self.id),
                            ('period', '=', today),
                            ('granularity', '=', 'day'),
                            ('service_name', '=', MOONSHOT_SERVICE_NAME),
                        ],
                        limit=1,
                    )
                    if day_row:
                        day_row.amount_source = (day_row.amount_source or 0.0) + delta
                        updated += 1
                    else:
                        Line.create({
                            'budget_id': self.id,
                            'period': today,
                            'granularity': 'day',
                            'service_name': MOONSHOT_SERVICE_NAME,
                            'amount_source': delta,
                            'source': 'moonshot',
                        })
                        created += 1
            self.moonshot_last_used_usd = current_used
            self.moonshot_last_used_at = fields.Datetime.now()
            self.last_moonshot_fetched_at = fields.Datetime.now()
            self.invalidate_recordset(['cost_line_ids'])
            self._log_fetch('moonshot', 'success', created, updated)
            self._post_project_thread_note(_(
                '<p><strong>Moonshot:</strong> +%s new, %s updated</p>'
            ) % (created, updated))
            self._send_fetch_summary_mail('Moonshot', created, updated)
            return created, updated
        except UserError as e:
            self._log_fetch('moonshot', 'error', 0, 0, error=str(e))
            raise
        except Exception as e:
            _logger.exception('Unexpected Moonshot error for budget %s', self.id)
            self._log_fetch('moonshot', 'error', 0, 0, error=str(e))
            raise

    def action_fetch_openai(self):
        for rec in self.with_context(ethara_manual_fetch=True):
            if not rec.openai_enabled:
                raise UserError(_('OpenAI is not enabled on this budget.'))
            rec._fetch_openai_cost_one()

    def _fetch_openai_cost_one(self):
        self.ensure_one()
        try:
            if not self.openai_api_key:
                raise UserError(_(
                    'OpenAI Admin API Key required. Use a sk-admin-* key from '
                    'platform.openai.com/settings/organization/admin-keys.'
                ))
            import time
            end = date.today() + timedelta(days=1)
            start = end.replace(day=1) - relativedelta(months=self.fetch_months or 6)
            start_ts = int(time.mktime(start.timetuple()))
            end_ts = int(time.mktime(end.timetuple()))
            params = {
                'start_time': start_ts,
                'end_time': end_ts,
                'bucket_width': '1d',
                'limit': 180,
            }
            if self.openai_project_id:
                params['project_ids'] = [self.openai_project_id.strip()]

            buckets = []
            next_page = None
            while True:
                page_params = dict(params)
                if next_page:
                    page_params['page'] = next_page
                try:
                    resp = requests.get(
                        OPENAI_COSTS_URL,
                        headers={'Authorization': 'Bearer %s' % self.openai_api_key},
                        params=page_params,
                        timeout=60,
                    )
                except requests.RequestException as e:
                    raise UserError(_('OpenAI request failed: %s') % e)
                if resp.status_code in (401, 403):
                    raise UserError(_(
                        'OpenAI returned HTTP %(s)s. Requires an Admin API key '
                        '(sk-admin-*), not a regular project key.'
                    ) % {'s': resp.status_code})
                if resp.status_code != 200:
                    raise UserError(_(
                        'OpenAI returned HTTP %(s)s: %(t)s'
                    ) % {'s': resp.status_code, 't': resp.text[:300]})
                try:
                    payload = resp.json()
                except ValueError:
                    raise UserError(_('OpenAI returned non-JSON response.'))
                page_buckets = payload.get('data') if isinstance(payload, dict) else None
                if not isinstance(page_buckets, list):
                    raise UserError(_('OpenAI returned unexpected response shape.'))
                buckets.extend(page_buckets)
                if not payload.get('has_more'):
                    break
                next_page = payload.get('next_page')
                if not next_page:
                    break

            by_day = {}
            for bucket in buckets:
                if not isinstance(bucket, dict):
                    continue
                bstart = bucket.get('start_time')
                if bstart is None:
                    continue
                try:
                    bday = date.fromtimestamp(int(bstart))
                except (TypeError, ValueError, OSError):
                    continue
                for result in bucket.get('results') or []:
                    if not isinstance(result, dict):
                        continue
                    amt = result.get('amount') or {}
                    try:
                        value = float(amt.get('value') or 0.0)
                    except (TypeError, ValueError):
                        continue
                    by_day[bday] = by_day.get(bday, 0.0) + value

            created, updated = self._upsert_provider_rows(
                OPENAI_SERVICE_NAME, 'openai', by_day,
            )
            try:
                br_c, br_u = self._fetch_openai_model_breakdown(start_ts, end_ts)
                created += br_c
                updated += br_u
            except UserError as bre:
                _logger.warning(
                    'OpenAI model breakdown failed for budget %s: %s', self.id, bre,
                )
            except Exception:
                _logger.exception(
                    'Unexpected OpenAI breakdown error for budget %s', self.id,
                )
            self.last_openai_fetched_at = fields.Datetime.now()
            self.invalidate_recordset(['cost_line_ids'])
            self._log_fetch('openai', 'success', created, updated)
            self._post_project_thread_note(_(
                '<p><strong>OpenAI:</strong> +%s new, %s updated</p>'
            ) % (created, updated))
            self._send_fetch_summary_mail('OpenAI', created, updated)
            return created, updated
        except UserError as e:
            self._log_fetch('openai', 'error', 0, 0, error=str(e))
            raise
        except Exception as e:
            _logger.exception('Unexpected OpenAI error for budget %s', self.id)
            self._log_fetch('openai', 'error', 0, 0, error=str(e))
            raise

    def _fetch_openai_model_breakdown(self, start_ts, end_ts):
        self.ensure_one()
        headers = {'Authorization': 'Bearer %s' % self.openai_api_key}
        base_params = {
            'start_time': start_ts,
            'end_time': end_ts,
            'bucket_width': '1d',
            'limit': 180,
            'group_by': ['model'],
        }
        if self.openai_project_id:
            base_params['project_ids'] = [self.openai_project_id.strip()]

        tokens_by_model = {}
        next_page = None
        while True:
            page_params = dict(base_params)
            if next_page:
                page_params['page'] = next_page
            resp = requests.get(
                OPENAI_USAGE_COMPLETIONS_URL,
                headers=headers,
                params=page_params,
                timeout=60,
            )
            if resp.status_code in (401, 403):
                raise UserError(_(
                    "OpenAI per-model usage denied (HTTP %(s)s). The admin key "
                    "must have 'api.usage.read' scope."
                ) % {'s': resp.status_code})
            if resp.status_code != 200:
                raise UserError(_(
                    'OpenAI /usage/completions HTTP %(s)s: %(t)s'
                ) % {'s': resp.status_code, 't': resp.text[:200]})
            try:
                payload = resp.json()
            except ValueError:
                raise UserError(_('OpenAI /usage/completions returned non-JSON'))
            data = payload.get('data') if isinstance(payload, dict) else None
            if not isinstance(data, list):
                raise UserError(_('OpenAI /usage/completions returned unexpected shape'))
            for bucket in data:
                if not isinstance(bucket, dict):
                    continue
                try:
                    day = date.fromtimestamp(int(bucket.get('start_time')))
                except (TypeError, ValueError, OSError):
                    continue
                for result in bucket.get('results') or []:
                    if not isinstance(result, dict):
                        continue
                    model = (result.get('model') or '').strip()
                    if not model:
                        continue
                    try:
                        inp = int(result.get('input_tokens') or 0)
                        out = int(result.get('output_tokens') or 0)
                        cached = int(result.get('input_cached_tokens') or 0)
                    except (TypeError, ValueError):
                        continue
                    if inp == 0 and out == 0 and cached == 0:
                        continue
                    slot = tokens_by_model.setdefault(
                        (day, model),
                        {'input': 0, 'output': 0, 'cache_read': 0},
                    )
                    slot['input'] += inp
                    slot['output'] += out
                    slot['cache_read'] += cached
            if not payload.get('has_more'):
                break
            next_page = payload.get('next_page')
            if not next_page:
                break

        Line = self.env['ethara.project.cost.line']
        created = updated = 0
        for (day, model), tokens in tokens_by_model.items():
            for token_type in ('input', 'output', 'cache_read'):
                qty = tokens[token_type]
                if qty == 0:
                    continue
                existing = Line.search(
                    [
                        ('budget_id', '=', self.id),
                        ('period', '=', day),
                        ('granularity', '=', 'day'),
                        ('service_name', '=', OPENAI_SERVICE_NAME),
                        ('model_name', '=', model),
                        ('token_type', '=', token_type),
                        ('is_model_breakdown', '=', True),
                    ],
                    limit=1,
                )
                vals = {
                    'budget_id': self.id,
                    'period': day,
                    'granularity': 'day',
                    'service_name': OPENAI_SERVICE_NAME,
                    'source': 'openai',
                    'model_name': model,
                    'usage_type': '',
                    'token_type': token_type,
                    'usage_quantity': float(qty),
                    'usage_unit': 'Tokens',
                    'amount_source': 0.0,
                    'is_model_breakdown': True,
                }
                if existing:
                    existing.write(vals)
                    updated += 1
                else:
                    Line.create(vals)
                    created += 1
        return created, updated

    def action_fetch_gcp(self):
        for rec in self.with_context(ethara_manual_fetch=True):
            if not rec.gcp_enabled:
                raise UserError(_('GCP BigQuery is not enabled on this budget.'))
            rec._fetch_gcp_cost_one()

    def _fetch_gcp_cost_one(self):
        self.ensure_one()
        try:
            if not (self.gcp_project_id and self.gcp_bq_dataset and self.gcp_bq_table):
                raise UserError(_(
                    'Set GCP Project ID, BigQuery Dataset, and BigQuery Table first.'
                ))
            if not self.gcp_service_account_json:
                raise UserError(_(
                    'Paste the GCP service-account JSON key into the budget.'
                ))
            for ref_name, ref_val in (
                ('GCP Project ID', self.gcp_project_id),
                ('BigQuery Dataset', self.gcp_bq_dataset),
                ('BigQuery Table', self.gcp_bq_table),
            ):
                # BigQuery table refs can't be parameterised; allowlist keeps SQL injection off.
                if not re.fullmatch(r'[A-Za-z0-9_\-.*]+', ref_val or ''):
                    raise UserError(_(
                        "%(n)s contains unsupported characters. Allowed: "
                        "letters, digits, '_', '-', '.', '*'."
                    ) % {'n': ref_name})
            try:
                sa_info = json.loads(self.gcp_service_account_json)
            except ValueError as e:
                raise UserError(_(
                    'GCP service-account JSON is not valid JSON: %s'
                ) % e)
            try:
                from google.oauth2 import service_account
                from google.cloud import bigquery
            except ImportError:
                raise UserError(_(
                    "Python packages 'google-cloud-bigquery' and 'google-auth' "
                    'must be installed on the Odoo server.'
                ))
            try:
                credentials = service_account.Credentials.from_service_account_info(sa_info)
            except Exception as e:
                raise UserError(_('Failed to load GCP credentials: %s') % e)
            try:
                client = bigquery.Client(
                    project=self.gcp_project_id, credentials=credentials,
                )
            except Exception as e:
                raise UserError(_('Failed to build BigQuery client: %s') % e)

            end_date = date.today()
            start_date = (
                end_date.replace(day=1)
                - relativedelta(months=self.fetch_months or 6)
            )
            table_ref = '`%s.%s.%s`' % (
                self.gcp_project_id, self.gcp_bq_dataset, self.gcp_bq_table,
            )
            where_clauses = ['DATE(usage_start_time) BETWEEN @start_date AND @end_date']
            params = [
                bigquery.ScalarQueryParameter('start_date', 'DATE', start_date),
                bigquery.ScalarQueryParameter('end_date', 'DATE', end_date),
            ]
            if self.gcp_service_filter:
                where_clauses.append('service.description = @service_filter')
                params.append(bigquery.ScalarQueryParameter(
                    'service_filter', 'STRING', self.gcp_service_filter,
                ))
            if self.gcp_label_key and self.gcp_label_value:
                where_clauses.append(
                    'EXISTS (SELECT 1 FROM UNNEST(labels) AS l '
                    'WHERE l.key = @label_key AND l.value = @label_value)'
                )
                params.append(bigquery.ScalarQueryParameter(
                    'label_key', 'STRING', self.gcp_label_key,
                ))
                params.append(bigquery.ScalarQueryParameter(
                    'label_value', 'STRING', self.gcp_label_value,
                ))
            where_sql = ' AND '.join(where_clauses)
            sql = (
                'WITH base AS (\n'
                '  SELECT\n'
                '    DATE(usage_start_time) AS day,\n'
                '    service.description AS service_name,\n'
                '    cost\n'
                '  FROM ' + table_ref + '\n'
                '  WHERE ' + where_sql + '\n'
                ')\n'
                'SELECT day AS period, service_name, '
                'SUM(cost) AS amount\n'
                'FROM base GROUP BY day, service_name HAVING amount != 0'
            )
            job_config = bigquery.QueryJobConfig(query_parameters=params)
            try:
                rows = list(client.query(sql, job_config=job_config).result())
            except Exception as e:
                raise UserError(_('BigQuery query failed: %s') % e)

            by_day = {}
            service_seen = set()
            for row in rows:
                try:
                    period = row['period']
                    service_name = (
                        row['service_name'] or GCP_DEFAULT_SERVICE_NAME
                    ).strip() or GCP_DEFAULT_SERVICE_NAME
                    amount = float(row['amount'] or 0.0)
                except (KeyError, TypeError, ValueError):
                    continue
                if amount == 0.0:
                    continue
                by_day.setdefault(service_name, {})[period] = (
                    by_day[service_name].get(period, 0.0) + amount
                )
                service_seen.add(service_name)

            created = updated = 0
            for service_name in service_seen:
                day_bucket = by_day.get(service_name, {})
                sub_c, sub_u = self._upsert_provider_rows(
                    service_name, 'gcp', day_bucket,
                )
                created += sub_c
                updated += sub_u

            self.last_gcp_fetched_at = fields.Datetime.now()
            self.invalidate_recordset(['cost_line_ids'])
            self._log_fetch('gcp', 'success', created, updated)
            self._post_project_thread_note(_(
                '<p><strong>GCP BigQuery:</strong> +%s new, %s updated</p>'
            ) % (created, updated))
            self._send_fetch_summary_mail('GCP BigQuery', created, updated)
            return created, updated
        except UserError as e:
            self._log_fetch('gcp', 'error', 0, 0, error=str(e))
            raise
        except Exception as e:
            _logger.exception('Unexpected GCP error for budget %s', self.id)
            self._log_fetch('gcp', 'error', 0, 0, error=str(e))
            raise

    def action_view_cost_lines(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Cost Lines'),
            'res_model': 'ethara.project.cost.line',
            'view_mode': 'list,form',
            'domain': [('budget_id', '=', self.id)],
            'context': {'default_budget_id': self.id},
        }

    def action_view_fetch_logs(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Fetch History'),
            'res_model': 'ethara.project.cost.fetch.log',
            'view_mode': 'list,form',
            'domain': [('budget_id', '=', self.id)],
            'context': {'default_budget_id': self.id},
        }

    def action_resync_ai_catalogs(self):
        messages = []
        for rec in self:
            lines = rec.cost_line_ids
            counts = lines._ensure_ai_models_from_lines() or {}
            messages.append(
                '%s: lines=%s model=%s infra=%s skipped=%s '
                '(created models=%s, infra=%s)'
                % (
                    rec.name,
                    counts.get('total', 0),
                    counts.get('model', 0),
                    counts.get('infra', 0),
                    counts.get('skipped', 0),
                    counts.get('models_created', 0),
                    counts.get('infra_created', 0),
                )
            )
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'type': 'info',
                'title': _('AI / Infra catalog resync'),
                'message': '\n'.join(messages) or _('No records.'),
                'sticky': True,
            },
        }

    @api.model
    def cron_fetch_costs(self):
        budgets = self.sudo().search([('active', '=', True)])
        for budget in budgets:
            for provider, enabled_field, method in [
                ('aws', None, '_fetch_aws_cost_one'),
                ('openrouter', 'openrouter_enabled', '_fetch_openrouter_cost_one'),
                ('moonshot', 'moonshot_enabled', '_fetch_moonshot_cost_one'),
                ('openai', 'openai_enabled', '_fetch_openai_cost_one'),
                ('gcp', 'gcp_enabled', '_fetch_gcp_cost_one'),
            ]:
                if enabled_field and not budget[enabled_field]:
                    continue
                try:
                    getattr(budget, method)()
                except UserError:
                    continue
                except Exception:
                    _logger.exception(
                        'Unhandled error fetching %s for budget %s',
                        provider, budget.id,
                    )
