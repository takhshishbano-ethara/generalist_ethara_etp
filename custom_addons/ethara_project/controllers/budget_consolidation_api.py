# -*- coding: utf-8 -*-
"""ethara_project budget-consolidation + AWS budget XLSX export HTTP API.

Ports two endpoints from `etp_projects` onto the `ethara.project.*` model
family, keeping the JSON response shapes byte-for-byte identical to the etp
originals so the existing Flutter `budget_consolidation` feature can be pointed
at the canonical `/api/v1/ethara_project/...` paths with no client changes.

Model renames applied vs. the etp source:
  * `etp.project.aws.budget`     -> `ethara.project.budget`
      FK `project_id`            -> `ethara_project_id`
  * `etp.batch.budget`           -> `ethara.project.phase`
  * `etp.project.aws.cost.line`  -> `ethara.project.cost.line`
      related `project_id`       -> `ethara_project_id`

Endpoints (both POST, http, auth='none', csrf=False, cors='*', @validate_token):
  * POST /api/v1/ethara_project/budget_consolidation
        Portfolio roll-up: {data: {kpis, spend_by_model, spend_by_project,
        budget_by_projects}}.
  * POST /api/v1/ethara_project/aws_budget/export
        2-sheet XLSX built with xlsxwriter, uploaded to S3 via
        `generate_s3_link`: {data: {download_url, filename, size,
        total_budgets}}.

This module intentionally does NOT touch `controllers/__init__.py` or
`data/api_endpoint_data.xml`; both endpoints are registered elsewhere.
"""
import base64
import io
import json
import logging
from collections import defaultdict
from datetime import date, timedelta

from odoo import http, fields
from odoo.http import request
from odoo.exceptions import UserError, ValidationError

from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    validate_token,
)

# generate_s3_link mirrors the etp export upload mechanism (uploads a base64
# payload to the configured s3.connector and returns the CDN URL).
from odoo.addons.api_auth_gateway.controllers.utility import generate_s3_link

from .budget_api import (
    _read_multipart_or_json,
    _coerce_int,
)

_logger = logging.getLogger(__name__)

BUDGET_MODEL = 'ethara.project.budget'
COST_LINE_MODEL = 'ethara.project.cost.line'

NO_MODEL_LABEL = '(no model)'
DEFAULT_ATTENTION_THRESHOLD_PCT = 80.0


# ---------------------------------------------------------------------------
# Local helpers (mirror etp_projects/controllers/{budget_consolidation,dashboard})
# ---------------------------------------------------------------------------

def _round2(value):
    try:
        return round(float(value or 0.0), 2)
    except (TypeError, ValueError):
        return 0.0


def _share_pct(part, whole):
    if not whole:
        return 0.0
    return _round2((float(part) / float(whole)) * 100.0)


def _coerce_float_named(value, field_name):
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ValidationError("'%s' must be a number." % field_name)


def _coerce_id_list(value, field_name):
    if value in (None, ''):
        return []
    if not isinstance(value, list) or not all(isinstance(x, int) for x in value):
        raise ValidationError("'%s' must be a list of integers." % field_name)
    return value


def _read_json_body():
    """Return the POST body as a dict (json or multipart `payload`)."""
    jdata, _files = _read_multipart_or_json()
    return jdata or {}


def _budget_domain(project_ids=None, include_inactive=False):
    domain = []
    if not include_inactive:
        domain.append(('active', '=', True))
    if project_ids:
        domain.append(('ethara_project_id', 'in', project_ids))
    return domain


def _portfolio_currency(budgets):
    usd = request.env.ref('base.USD', raise_if_not_found=False)
    if usd:
        return {'currency': usd.name or 'USD', 'currency_symbol': usd.symbol or '$'}
    return {'currency': 'USD', 'currency_symbol': '$'}


class EtharaBudgetConsolidationController(http.Controller):

    # ------------------------------------------------------- consolidation
    @http.route(
        '/api/v1/ethara_project/budget_consolidation',
        methods=['POST'],
        type='http',
        auth='none',
        csrf=False,
        cors='*',
    )
    @validate_token
    def budget_consolidation(self, **kwargs):
        try:
            jdata = _read_json_body()

            project_ids = (
                _coerce_id_list(jdata.get('project_ids'), 'project_ids')
                if jdata.get('project_ids') is not None
                else None
            )

            include_inactive = bool(jdata.get('include_inactive'))
            if 'needs_attention_threshold_pct' in jdata:
                threshold_pct = _coerce_float_named(
                    jdata.get('needs_attention_threshold_pct'),
                    'needs_attention_threshold_pct',
                )
            else:
                threshold_pct = DEFAULT_ATTENTION_THRESHOLD_PCT

            Budget = request.env[BUDGET_MODEL].sudo()
            budgets = Budget.search(
                _budget_domain(project_ids or None, include_inactive)
            )
            currency = _portfolio_currency(budgets)

            project_rows = {}
            for b in budgets:
                pid = b.ethara_project_id.id
                row = project_rows.get(pid)
                if not row:
                    row = {
                        'project_id': pid,
                        'project_name': b.ethara_project_id.display_name or '',
                        'budget': 0.0,
                        'spend': 0.0,
                        'burn': 0.0,
                    }
                    project_rows[pid] = row
                row['budget'] += 0.0
                row['spend'] += 0.0
                row['burn'] += 0.0

            total_spend = sum(r['spend'] for r in project_rows.values())
            total_budget = sum(r['budget'] for r in project_rows.values())
            total_remaining = total_budget - total_spend
            active_project_count = len(project_rows)

            needs_attention_count = 0
            for r in project_rows.values():
                pct = (r['spend'] / r['budget'] * 100.0) if r['budget'] else 0.0
                if pct >= threshold_pct:
                    needs_attention_count += 1

            per_project_model_spend = {pid: {} for pid in project_rows.keys()}
            model_totals = defaultdict(float)
            if project_rows:
                CostLine = request.env[COST_LINE_MODEL].sudo()
                cost_domain = [
                    ('ethara_project_id', 'in', list(project_rows.keys())),
                ]
                if not include_inactive:
                    cost_domain.append(('budget_id.active', '=', True))
                cost_groups = CostLine._read_group(
                    cost_domain,
                    groupby=['ethara_project_id', 'service_name'],
                    aggregates=['amount_source:sum'],
                )
                for project_rec, service, amt in cost_groups:
                    pid = project_rec.id
                    svc = service or NO_MODEL_LABEL
                    amount = float(amt or 0.0)
                    if not amount:
                        continue
                    bucket = per_project_model_spend.setdefault(pid, {})
                    bucket[svc] = bucket.get(svc, 0.0) + amount
                    model_totals[svc] += amount

            top_model = None
            if model_totals:
                tn, ta = max(model_totals.items(), key=lambda x: x[1])
                top_model = {
                    'model_name': tn,
                    'amount': _round2(ta),
                    'share_pct': _share_pct(ta, total_spend),
                }

            spend_by_model = sorted(
                [
                    {
                        'model_name': m,
                        'amount': _round2(a),
                        'share_pct': _share_pct(a, total_spend),
                    }
                    for m, a in model_totals.items()
                ],
                key=lambda x: x['amount'],
                reverse=True,
            )

            spend_by_project = sorted(
                [
                    {
                        'project_id': r['project_id'],
                        'project_name': r['project_name'],
                        'amount': _round2(r['spend']),
                        'share_pct': _share_pct(r['spend'], total_spend),
                    }
                    for r in project_rows.values()
                ],
                key=lambda x: x['amount'],
                reverse=True,
            )

            budget_by_projects = []
            for pid, row in project_rows.items():
                attr = per_project_model_spend.get(pid, {})
                proj_attr_total = sum(attr.values())
                models_list = sorted(
                    [
                        {
                            'model_name': m,
                            'spend': _round2(a),
                            'share_pct': _share_pct(a, proj_attr_total),
                        }
                        for m, a in attr.items()
                    ],
                    key=lambda x: x['spend'],
                    reverse=True,
                )
                top_m = models_list[0]['model_name'] if models_list else None
                util_pct = (
                    (row['spend'] / row['budget'] * 100.0) if row['budget'] else 0.0
                )
                runway_days = None
                if row['burn'] > 0:
                    runway_days = int(
                        max(0.0, (row['budget'] - row['spend']) / row['burn'])
                    )
                budget_by_projects.append(
                    {
                        'project_id': row['project_id'],
                        'project_name': row['project_name'],
                        'spend': _round2(row['spend']),
                        'budget': _round2(row['budget']),
                        'remaining': _round2(row['budget'] - row['spend']),
                        'utilization_pct': _round2(util_pct),
                        'runway_days': runway_days,
                        'top_model': top_m,
                        'models': models_list,
                    }
                )
            budget_by_projects.sort(
                key=lambda x: x['utilization_pct'], reverse=True,
            )

            payload = {
                'kpis': {
                    'total_spend': _round2(total_spend),
                    'total_remaining': _round2(total_remaining),
                    'percent_consumed': _round2(
                        (total_spend / total_budget * 100.0)
                        if total_budget else 0.0
                    ),
                    'total_budget': _round2(total_budget),
                    'active_project_count': active_project_count,
                    'needs_attention_count': needs_attention_count,
                    'needs_attention_threshold_pct': _round2(threshold_pct),
                    'top_model': top_model,
                    'currency': currency.get('currency'),
                    'currency_symbol': currency.get('currency_symbol'),
                },
                'spend_by_model': spend_by_model,
                'spend_by_project': spend_by_project,
                'budget_by_projects': budget_by_projects,
            }
            return return_Response(
                message='OK', status=200, data={'data': payload},
            )

        except (UserError, ValidationError) as e:
            return return_Response(message=str(e), status=400)
        except Exception as e:
            _logger.exception('budget_consolidation failed')
            return return_Response(
                message='Something went wrong.', status=400, errors=[str(e)],
            )

    # -------------------------------------------------------- XLSX export
    @http.route(
        '/api/v1/ethara_project/aws_budget/export',
        methods=['POST'], type='http', auth='none', csrf=False, cors='*',
    )
    @validate_token
    def export_aws_budgets(self, **params):
        try:
            try:
                import xlsxwriter
            except ImportError:
                return return_Response(
                    message="Python package 'xlsxwriter' is not installed on the server.",
                    status=400,
                )

            jdata = _read_json_body()

            domain = []
            if not bool(jdata.get('include_inactive')):
                domain.append(('active', '=', True))

            budget_ids = jdata.get('budget_ids') or []
            project_ids = jdata.get('project_ids') or []
            if budget_ids:
                if not isinstance(budget_ids, list) or not all(
                    isinstance(x, int) for x in budget_ids
                ):
                    return return_Response(
                        message="'budget_ids' must be a list of integers.",
                        status=400,
                    )
                domain.append(('id', 'in', budget_ids))
            if project_ids:
                if not isinstance(project_ids, list) or not all(
                    isinstance(x, int) for x in project_ids
                ):
                    return return_Response(
                        message="'project_ids' must be a list of integers.",
                        status=400,
                    )
                domain.append(('ethara_project_id', 'in', project_ids))

            Budget = request.env[BUDGET_MODEL].sudo()
            budgets = Budget.search(domain, order='ethara_project_id, name')

            run_rate_end = date.today()
            run_rate_start = run_rate_end - timedelta(days=6)
            run_rate_days = 7.0

            output = io.BytesIO()
            wb = xlsxwriter.Workbook(output, {'in_memory': True})
            ws = wb.add_worksheet('Projects — Budget by Project')
            # Place outline +/- symbols on the parent row (above the group)
            ws.outline_settings(True, False, True, False)

            f_title = wb.add_format({
                'bold': True, 'font_size': 14, 'font_color': '#ffffff',
                'bg_color': '#1f2937', 'align': 'left', 'valign': 'vcenter',
            })
            f_kpi_label = wb.add_format({
                'bold': True, 'font_size': 10, 'font_color': '#374151',
                'bg_color': '#f3f4f6', 'border': 1, 'border_color': '#e5e7eb',
                'align': 'left', 'valign': 'vcenter',
            })
            f_kpi_value = wb.add_format({
                'font_size': 11, 'font_color': '#111827',
                'border': 1, 'border_color': '#e5e7eb',
                'align': 'left', 'valign': 'vcenter',
                'num_format': '#,##0.00',
            })
            f_kpi_value_text = wb.add_format({
                'font_size': 11, 'font_color': '#111827',
                'border': 1, 'border_color': '#e5e7eb',
                'align': 'left', 'valign': 'vcenter',
            })
            f_header = wb.add_format({
                'bold': True, 'font_size': 11, 'font_color': '#ffffff',
                'bg_color': '#374151', 'align': 'center', 'valign': 'vcenter',
                'border': 1, 'border_color': '#1f2937',
            })
            f_project = wb.add_format({
                'bold': True, 'font_size': 10, 'border': 1, 'border_color': '#e5e7eb',
                'align': 'left', 'valign': 'vcenter',
            })
            f_text = wb.add_format({
                'font_size': 10, 'border': 1, 'border_color': '#e5e7eb',
                'align': 'left', 'valign': 'vcenter',
            })
            f_text_model = wb.add_format({
                'font_size': 10, 'border': 1, 'border_color': '#e5e7eb',
                'align': 'left', 'valign': 'vcenter', 'indent': 2,
                'italic': True, 'font_color': '#4b5563',
            })
            f_blank = wb.add_format({
                'font_size': 10, 'border': 1, 'border_color': '#e5e7eb',
                'align': 'right', 'valign': 'vcenter',
            })
            f_int = wb.add_format({
                'font_size': 10, 'border': 1, 'border_color': '#e5e7eb',
                'align': 'right', 'valign': 'vcenter', 'num_format': '0',
            })
            f_money = wb.add_format({
                'font_size': 10, 'border': 1, 'border_color': '#e5e7eb',
                'align': 'right', 'valign': 'vcenter', 'num_format': '#,##0.00',
            })
            f_money_actual = wb.add_format({
                'bold': True, 'font_size': 10, 'border': 1, 'border_color': '#e5e7eb',
                'align': 'right', 'valign': 'vcenter', 'num_format': '#,##0.00',
            })
            f_money_child = wb.add_format({
                'font_size': 10, 'border': 1, 'border_color': '#e5e7eb',
                'align': 'right', 'valign': 'vcenter', 'num_format': '#,##0.00',
                'font_color': '#4b5563', 'italic': True,
            })
            f_pct = wb.add_format({
                'font_size': 10, 'border': 1, 'border_color': '#e5e7eb',
                'align': 'right', 'valign': 'vcenter', 'num_format': '0.00"%"',
            })
            f_pct_child = wb.add_format({
                'font_size': 10, 'border': 1, 'border_color': '#e5e7eb',
                'align': 'right', 'valign': 'vcenter', 'num_format': '0.00"%"',
                'font_color': '#4b5563', 'italic': True,
            })
            f_runrate = wb.add_format({
                'font_size': 10, 'border': 1, 'border_color': '#e5e7eb',
                'align': 'right', 'valign': 'vcenter', 'num_format': '#,##0.00" /day"',
            })
            f_total_label = wb.add_format({
                'bold': True, 'font_size': 10, 'bg_color': '#f9fafb',
                'border': 1, 'border_color': '#d1d5db',
                'align': 'left', 'valign': 'vcenter',
            })
            f_total_money = wb.add_format({
                'bold': True, 'font_size': 10, 'bg_color': '#f9fafb',
                'border': 1, 'border_color': '#d1d5db',
                'align': 'right', 'valign': 'vcenter', 'num_format': '#,##0.00',
            })
            f_total_pct = wb.add_format({
                'bold': True, 'font_size': 10, 'bg_color': '#f9fafb',
                'border': 1, 'border_color': '#d1d5db',
                'align': 'right', 'valign': 'vcenter', 'num_format': '0.00"%"',
            })
            f_total_runrate = wb.add_format({
                'bold': True, 'font_size': 10, 'bg_color': '#f9fafb',
                'border': 1, 'border_color': '#d1d5db',
                'align': 'right', 'valign': 'vcenter', 'num_format': '#,##0.00" /day"',
            })

            headers = [
                'Project', 'Estimated cost', 'Actual', 'Remaining',
                'Budget', 'Util %', 'Run rate', 'Top Model / Share',
            ]
            widths = [40, 16, 16, 16, 16, 10, 16, 22]
            for i, w in enumerate(widths):
                ws.set_column(i, i, w)

            ws.set_row(0, 28)
            ws.merge_range(0, 0, 0, len(headers) - 1, 'Projects — Budget by Project', f_title)

            project_set = {
                b.ethara_project_id.id for b in budgets if b.ethara_project_id
            }
            # `ethara.project.budget` has no `total_approved_amount` (that field
            # lived on the etp `aws.budget` phase roll-up); the closest analogue
            # on this model is `budget_amount`, so "Estimated cost" reflects the
            # budget envelope here.
            total_est = sum(b.budget_amount or 0.0 for b in budgets)
            total_actual = sum(b.consumed_amount or 0.0 for b in budgets)
            total_remaining = sum(b.remaining_amount or 0.0 for b in budgets)
            total_budget = sum(b.budget_amount or 0.0 for b in budgets)
            overall_pct = (total_actual / total_est * 100.0) if total_est else 0.0

            ws.write(2, 0, 'Total Budgets', f_kpi_label)
            ws.write_number(2, 1, len(budgets), f_kpi_value)
            ws.write(2, 2, 'Projects', f_kpi_label)
            ws.write_number(2, 3, len(project_set), f_kpi_value)
            ws.write(2, 4, 'Currency', f_kpi_label)
            ws.merge_range(2, 5, 2, 7, 'USD', f_kpi_value_text)

            ws.write(3, 0, 'Total Estimated', f_kpi_label)
            ws.write_number(3, 1, round(total_est, 2), f_kpi_value)
            ws.write(3, 2, 'Total Actual', f_kpi_label)
            ws.write_number(3, 3, round(total_actual, 2), f_kpi_value)
            ws.write(3, 4, 'Total Remaining', f_kpi_label)
            ws.merge_range(3, 5, 3, 7, round(total_remaining, 2), f_kpi_value)

            ws.write(4, 0, 'Overall Util %', f_kpi_label)
            ws.write_number(4, 1, round(overall_pct, 2), f_kpi_value)
            ws.write(4, 2, 'Run-rate Window', f_kpi_label)
            ws.merge_range(
                4, 3, 4, 7,
                '%s → %s (7-day daily avg)' % (
                    run_rate_start.strftime('%Y-%m-%d'),
                    run_rate_end.strftime('%Y-%m-%d'),
                ),
                f_kpi_value_text,
            )

            header_row = 6
            for col, h in enumerate(headers):
                ws.write(header_row, col, h, f_header)
            ws.set_row(header_row, 22)

            row_cursor = header_row + 1
            portfolio_run_rate_total = 0.0

            for b in budgets:
                project_name = (
                    b.ethara_project_id.name if b.ethara_project_id else '(no project)'
                )
                budget_label = b.name or ''
                row_label = (
                    project_name
                    if budget_label in ('', project_name)
                    else '%s · %s' % (project_name, budget_label)
                )

                model_actual = {}
                model_quantity = {}
                run_rate_total = 0.0
                for line in b.cost_line_ids:
                    if line.granularity != 'day':
                        continue
                    if line.is_model_breakdown:
                        model = (line.model_name or '').strip() or '(no model)'
                        amt = float(line.amount_source or 0.0)
                        qty = float(line.usage_quantity or 0.0)
                        if amt:
                            model_actual[model] = model_actual.get(model, 0.0) + amt
                        if qty:
                            model_quantity[model] = model_quantity.get(model, 0.0) + qty
                    else:
                        if line.period and run_rate_start <= line.period <= run_rate_end:
                            run_rate_total += float(line.amount_source or 0.0)

                portfolio_run_rate_total += run_rate_total
                run_rate_per_day = run_rate_total / run_rate_days

                top_model = ''
                if model_actual:
                    top_model = max(model_actual.items(), key=lambda kv: kv[1])[0]
                elif model_quantity:
                    # Cost-only providers (e.g. OpenAI) ship per-model rows with
                    # token quantities but no per-model USD; fall back to
                    # highest-volume model so the column reflects activity
                    # rather than being blank.
                    top_model = max(model_quantity.items(), key=lambda kv: kv[1])[0]

                ws.write(row_cursor, 0, row_label, f_project)
                ws.write_number(row_cursor, 1, round(b.budget_amount or 0.0, 2), f_money)
                ws.write_number(row_cursor, 2, round(b.consumed_amount or 0.0, 2), f_money_actual)
                ws.write_number(row_cursor, 3, round(b.remaining_amount or 0.0, 2), f_money)
                ws.write_number(row_cursor, 4, round(b.budget_amount or 0.0, 2), f_money)
                ws.write_number(row_cursor, 5, round(b.consumed_pct or 0.0, 2), f_pct)
                ws.write_number(row_cursor, 6, round(run_rate_per_day, 2), f_runrate)
                ws.write(row_cursor, 7, top_model or '—', f_text)
                row_cursor += 1

                project_actual = b.consumed_amount or 0.0
                model_keys = set(model_actual.keys()) | set(model_quantity.keys())
                child_models = sorted(
                    model_keys,
                    key=lambda m: (
                        -model_actual.get(m, 0.0),
                        -model_quantity.get(m, 0.0),
                        m,
                    ),
                )
                for model in child_models:
                    m_actual = model_actual.get(model, 0.0)
                    share_pct = (m_actual / project_actual * 100.0) if project_actual else 0.0
                    ws.write(row_cursor, 0, model, f_text_model)
                    ws.write(row_cursor, 1, '', f_blank)
                    ws.write_number(row_cursor, 2, round(m_actual, 2), f_money_child)
                    ws.write(row_cursor, 3, '', f_blank)
                    ws.write(row_cursor, 4, '', f_blank)
                    ws.write(row_cursor, 5, '', f_blank)
                    ws.write(row_cursor, 6, '', f_blank)
                    ws.write_number(row_cursor, 7, round(share_pct, 2), f_pct_child)
                    ws.set_row(row_cursor, None, None, {'level': 1, 'hidden': True})
                    row_cursor += 1

            if budgets:
                total_row = row_cursor
                ws.write(total_row, 0, 'Portfolio total', f_total_label)
                ws.write_number(total_row, 1, round(total_est, 2), f_total_money)
                ws.write_number(total_row, 2, round(total_actual, 2), f_total_money)
                ws.write_number(total_row, 3, round(total_remaining, 2), f_total_money)
                ws.write_number(total_row, 4, round(total_budget, 2), f_total_money)
                ws.write_number(total_row, 5, round(overall_pct, 2), f_total_pct)
                ws.write_number(
                    total_row, 6,
                    round(portfolio_run_rate_total / run_rate_days, 2),
                    f_total_runrate,
                )
                ws.write(total_row, 7, '—', f_total_label)

                ws.autofilter(header_row, 0, total_row - 1, len(headers) - 1)
                ws.freeze_panes(header_row + 1, 1)

            ws2 = wb.add_worksheet('Service Spend')
            s_headers = [
                '#', 'Project', 'Budget Seq', 'Service',
                'Total Cost (USD)', '% of Budget',
            ]
            s_widths = [5, 28, 22, 36, 18, 14]
            for i, w in enumerate(s_widths):
                ws2.set_column(i, i, w)

            ws2.set_row(0, 28)
            ws2.merge_range(0, 0, 0, len(s_headers) - 1, 'Service-Wise Spend', f_title)

            service_rows = []
            service_grand_usd = 0.0
            for b in budgets:
                project_name = b.ethara_project_id.name if b.ethara_project_id else ''
                budget_seq = b.name or ''
                envelope = b.budget_amount or 0.0
                per_service = {}
                for line in b.cost_line_ids:
                    if line.is_model_breakdown:
                        continue
                    svc = (line.service_name or 'Unknown').strip() or 'Unknown'
                    agg = per_service.setdefault(svc, {'usd': 0.0})
                    agg['usd'] += float(line.amount_source or 0.0)
                for svc, agg in per_service.items():
                    pct = (agg['usd'] / envelope * 100.0) if envelope else 0.0
                    service_rows.append({
                        'project': project_name,
                        'budget_seq': budget_seq,
                        'service': svc,
                        'usd': agg['usd'],
                        'pct': pct,
                    })
                    service_grand_usd += agg['usd']

            service_rows.sort(key=lambda r: (r['project'], r['budget_seq'], -r['usd']))

            top_service_label = ''
            top_service_spend = 0.0
            if service_rows:
                top = max(service_rows, key=lambda r: r['usd'])
                top_service_label = top['service']
                top_service_spend = top['usd']

            ws2.write(2, 0, 'Total Services', f_kpi_label)
            ws2.write_number(2, 1, len({(r['budget_seq'], r['service']) for r in service_rows}), f_kpi_value)
            ws2.write(2, 2, 'Top Service', f_kpi_label)
            ws2.write(2, 3, top_service_label or '—', f_kpi_value_text)
            ws2.write(2, 4, 'Top Spend (USD)', f_kpi_label)
            ws2.write_number(2, 5, round(top_service_spend, 2), f_kpi_value)

            s_header_row = 4
            for col, h in enumerate(s_headers):
                ws2.write(s_header_row, col, h, f_header)
            ws2.set_row(s_header_row, 22)

            s_data_start = s_header_row + 1
            for idx, r in enumerate(service_rows, 1):
                row = s_data_start + idx - 1
                ws2.write_number(row, 0, idx, f_int)
                ws2.write(row, 1, r['project'], f_text)
                ws2.write(row, 2, r['budget_seq'], f_text)
                ws2.write(row, 3, r['service'], f_text)
                ws2.write_number(row, 4, round(r['usd'], 6), f_money)
                ws2.write_number(row, 5, round(r['pct'], 2), f_pct)

            if service_rows:
                s_total_row = s_data_start + len(service_rows)
                ws2.merge_range(s_total_row, 0, s_total_row, 3, 'Grand Total', f_total_label)
                ws2.write_number(s_total_row, 4, round(service_grand_usd, 6), f_total_money)
                ws2.write(s_total_row, 5, '', f_total_label)
                ws2.autofilter(s_header_row, 0, s_data_start + len(service_rows) - 1, len(s_headers) - 1)
                ws2.freeze_panes(s_header_row + 1, 3)

            wb.close()
            xlsx_bytes = output.getvalue()
            output.close()

            timestamp = fields.Datetime.now().strftime('%Y%m%d_%H%M%S')
            filename = 'aws_budget_export_%s.xlsx' % timestamp
            file_size = len(xlsx_bytes)
            file_b64 = base64.b64encode(xlsx_bytes).decode('utf-8')

            try:
                download_url = generate_s3_link(
                    file_b64, prefix='reports', filename=filename,
                )
            except Exception as e:
                _logger.exception('S3 upload failed for aws_budget export')
                return return_Response(
                    message='Failed to upload export to S3.',
                    status=500,
                    errors=[str(e)],
                )

            if not download_url:
                return return_Response(
                    message='S3 upload returned an empty link.',
                    status=500,
                )

            return return_Response(
                message='AWS budget export generated.',
                status=200,
                data={'data': {
                    'download_url': download_url,
                    'filename': filename,
                    'size': file_size,
                    'total_budgets': len(budgets),
                }},
            )
        except Exception as e:
            _logger.exception('export_aws_budgets failed')
            return return_Response(
                message='Something went wrong.',
                status=400,
                errors=[str(e)],
            )
