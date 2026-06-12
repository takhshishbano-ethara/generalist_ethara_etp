import base64
import io
import json
import logging

from odoo import fields, http
from odoo.exceptions import UserError
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import (
    generate_s3_link,
    return_Response,
    validate_token,
)

BUDGET_MODEL = 'etp.project.aws.budget'

_logger = logging.getLogger(__name__)


class EtpProjectsAwsCostController(http.Controller):

    def _read_json_body(self):
        raw = b""
        try:
            raw = request.httprequest.stream.read() or b""
        except Exception:
            raw = request.httprequest.data or b""
        if not raw:
            return {}
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    @http.route(
        '/api/v1/etp_projects/aws_cost/status_all',
        methods=['POST'], type='http', auth='none', csrf=False, cors='*',
    )
    def update_all_aws_cost(self, **params):
        try:
            jdata = self._read_json_body()

            include_inactive = bool(jdata.get('include_inactive'))
            domain = []
            if not include_inactive:
                domain.append(('active', '=', True))

            budget_ids = jdata.get('budget_ids') or []
            project_ids = jdata.get('project_ids') or []
            if budget_ids:
                if not isinstance(budget_ids, list) or not all(isinstance(x, int) for x in budget_ids):
                    return return_Response(
                        message="'budget_ids' must be a list of integers.",
                        status=400,
                    )
                domain.append(('id', 'in', budget_ids))
            if project_ids:
                if not isinstance(project_ids, list) or not all(isinstance(x, int) for x in project_ids):
                    return return_Response(
                        message="'project_ids' must be a list of integers.",
                        status=400,
                    )
                domain.append(('project_id', 'in', project_ids))

            Budget = request.env['etp.project.aws.budget'].sudo()
            budgets = Budget.search(domain)

            if not budgets:
                return return_Response(
                    message="No AWS budgets matched the given filters.",
                    status=200,
                    data={"data": {
                        "total_budgets": 0,
                        "success_count": 0,
                        "error_count": 0,
                        "total_created": 0,
                        "total_updated": 0,
                        "results": [],
                    }},
                )

            results = []
            success_count = 0
            error_count = 0
            total_created = 0
            total_updated = 0

            for budget in budgets:
                try:
                    created, updated = budget._fetch_cost_one()
                    try:
                        budget._maybe_alert_thresholds()
                    except Exception:
                        _logger.exception(
                            "Threshold alert failed for budget id=%s name=%s; fetch succeeded.",
                            budget.id, budget.name,
                        )
                    success_count += 1
                    total_created += created
                    total_updated += updated
                    results.append({
                        "budget_id": budget.id,
                        "budget_name": budget.name or "",
                        "project_id": budget.project_id.id if budget.project_id else False,
                        "project_name": budget.project_id.name if budget.project_id else "",
                        "tag_key": budget.tag_key or "",
                        "tag_value": budget.tag_value or "",
                        "status": "success",
                        "created": created,
                        "updated": updated,
                        "budget_amount": float(budget.budget_amount or 0.0),
                        "total_consumed": float(budget.total_consumed or 0.0),
                        "remaining": float(budget.remaining or 0.0),
                        "percent_consumed": round(float(budget.percent_consumed or 0.0), 2),
                        "daily_burn_rate": float(budget.daily_burn_rate or 0.0),
                        "last_fetched_at": (
                            budget.last_fetched_at.strftime("%Y-%m-%d %H:%M:%S")
                            if budget.last_fetched_at else ""
                        ),
                    })
                except UserError as e:
                    error_count += 1
                    results.append({
                        "budget_id": budget.id,
                        "budget_name": budget.name or "",
                        "project_id": budget.project_id.id if budget.project_id else False,
                        "project_name": budget.project_id.name if budget.project_id else "",
                        "tag_key": budget.tag_key or "",
                        "tag_value": budget.tag_value or "",
                        "status": "error",
                        "error": str(e),
                        "created": 0,
                        "updated": 0,
                        "budget_amount": float(budget.budget_amount or 0.0),
                        "total_consumed": float(budget.total_consumed or 0.0),
                        "remaining": float(budget.remaining or 0.0),
                        "percent_consumed": round(float(budget.percent_consumed or 0.0), 2),
                        "daily_burn_rate": float(budget.daily_burn_rate or 0.0),
                        "last_fetched_at": (
                            budget.last_fetched_at.strftime("%Y-%m-%d %H:%M:%S")
                            if budget.last_fetched_at else ""
                        ),
                    })
                except Exception as e:
                    error_count += 1
                    _logger.exception(
                        "AWS cost fetch failed for budget id=%s name=%s",
                        budget.id, budget.name,
                    )
                    results.append({
                        "budget_id": budget.id,
                        "budget_name": budget.name or "",
                        "project_id": budget.project_id.id if budget.project_id else False,
                        "project_name": budget.project_id.name if budget.project_id else "",
                        "tag_key": budget.tag_key or "",
                        "tag_value": budget.tag_value or "",
                        "status": "error",
                        "error": str(e),
                        "created": 0,
                        "updated": 0,
                        "budget_amount": float(budget.budget_amount or 0.0),
                        "total_consumed": float(budget.total_consumed or 0.0),
                        "remaining": float(budget.remaining or 0.0),
                        "percent_consumed": round(float(budget.percent_consumed or 0.0), 2),
                        "daily_burn_rate": float(budget.daily_burn_rate or 0.0),
                        "last_fetched_at": fields.Datetime.to_string(budget.last_fetched_at) if budget.last_fetched_at else "",
                    })
 
            return return_Response(
                message="AWS cost update completed.",
                status=200,
                data={"data": {
                    "total_budgets": len(budgets),
                    "success_count": success_count,
                    "error_count": error_count,
                    "total_created": total_created,
                    "total_updated": total_updated,
                    "results": results,
                }},
            )
        except Exception as e:
            _logger.exception("update_all_aws_cost failed")
            return return_Response(
                message="Something went wrong.",
                status=400,
                errors=[str(e)],
            )

    @http.route(
        '/api/v1/etp_projects/aws_cost/update_all',
        methods=['POST'], type='http', auth='none', csrf=False, cors='*',
    )
    @validate_token
    def status_all_aws_cost(self, **params):
        try:
            jdata = self._read_json_body()

            include_inactive = bool(jdata.get('include_inactive'))
            domain = []
            if not include_inactive:
                domain.append(('active', '=', True))

            budget_ids = jdata.get('budget_ids') or []
            project_ids = jdata.get('project_ids') or []
            if budget_ids:
                if not isinstance(budget_ids, list) or not all(isinstance(x, int) for x in budget_ids):
                    return return_Response(
                        message="'budget_ids' must be a list of integers.",
                        status=400,
                    )
                domain.append(('id', 'in', budget_ids))
            if project_ids:
                if not isinstance(project_ids, list) or not all(isinstance(x, int) for x in project_ids):
                    return return_Response(
                        message="'project_ids' must be a list of integers.",
                        status=400,
                    )
                domain.append(('project_id', 'in', project_ids))

            Budget = request.env[BUDGET_MODEL].sudo()
            budgets = Budget.search(domain)

            if not budgets:
                return return_Response(
                    message="No AWS budgets matched the given filters.",
                    status=200,
                    data={"data": {
                        "total_budgets": 0,
                        "success_count": 0,
                        "error_count": 0,
                        "total_created": 0,
                        "total_updated": 0,
                        "results": [],
                    }},
                )

            results = []
            for budget in budgets:
                results.append({
                    "budget_id": budget.id,
                    "budget_name": budget.name or "",
                    "project_id": budget.project_id.id if budget.project_id else False,
                    "project_name": budget.project_id.name if budget.project_id else "",
                    "tag_key": budget.tag_key or "",
                    "tag_value": budget.tag_value or "",
                    "status": "success",
                    "created": 0,
                    "updated": 0,
                    "budget_amount": float(budget.budget_amount or 0.0),
                    "total_consumed": float(budget.total_consumed or 0.0),
                    "remaining": float(budget.remaining or 0.0),
                    "percent_consumed": round(float(budget.percent_consumed or 0.0), 2),
                    "daily_burn_rate": float(budget.daily_burn_rate or 0.0),
                    "last_fetched_at": (
                        budget.last_fetched_at.strftime("%Y-%m-%d %H:%M:%S")
                        if budget.last_fetched_at else ""
                    ),
                })

            return return_Response(
                message="AWS cost status retrieved.",
                status=200,
                data={"data": {
                    "total_budgets": len(budgets),
                    "success_count": len(budgets),
                    "error_count": 0,
                    "total_created": 0,
                    "total_updated": 0,
                    "results": results,
                }},
            )
        except Exception as e:
            _logger.exception("status_all_aws_cost failed")
            return return_Response(
                message="Something went wrong.",
                status=400,
                errors=[str(e)],
            )

    @http.route(
        '/api/v1/etp_projects/aws_budget/list',
        methods=['POST'], type='http', auth='none', csrf=False, cors='*',
    )
    @validate_token
    def list_aws_budgets(self, **params):
        try:
            jdata = self._read_json_body()

            domain = []
            if not bool(jdata.get('include_inactive')):
                domain.append(('active', '=', True))

            budget_ids = jdata.get('budget_ids') or []
            project_ids = jdata.get('project_ids') or []
            if budget_ids:
                if not isinstance(budget_ids, list) or not all(isinstance(x, int) for x in budget_ids):
                    return return_Response(
                        message="'budget_ids' must be a list of integers.",
                        status=400,
                    )
                domain.append(('id', 'in', budget_ids))
            if project_ids:
                if not isinstance(project_ids, list) or not all(isinstance(x, int) for x in project_ids):
                    return return_Response(
                        message="'project_ids' must be a list of integers.",
                        status=400,
                    )
                domain.append(('project_id', 'in', project_ids))

            Budget = request.env[BUDGET_MODEL].sudo()
            budgets = Budget.search(domain, order='project_id, name')

            records = []
            for budget in budgets:
                # Real-data burn/runway snapshot from the budget's cost lines.
                snapshot = budget._budget_snapshot()
                records.append({
                    "id": budget.id,
                    "seq": budget.name or "",
                    "project_id": budget.project_id.id if budget.project_id else False,
                    "project_name": budget.project_id.name if budget.project_id else "",
                    "project_budget": float(budget.project_budget or 0.0),
                    "final_budget": float(budget.budget_amount or 0.0),
                    "total_used_cost": float(budget.total_consumed or 0.0),
                    "remaining_cost": float(budget.remaining or 0.0),
                    "percent_consumed": round(float(budget.percent_consumed or 0.0), 2),
                    "currency": budget.currency_id.name if budget.currency_id else "",
                    "currency_symbol": snapshot["currency_symbol"],
                    "daily_burn_rate": snapshot["daily_burn_rate"],
                    "runway_days": snapshot["runway_days"],
                    "runway_days_exact": snapshot["runway_days_exact"],
                    "runway_depletes_on": snapshot["runway_depletes_on"],
                })

            return return_Response(
                message="OK",
                status=200,
                data={"data": {
                    "total": len(records),
                    "records": records,
                }},
            )
        except Exception as e:
            _logger.exception("list_aws_budgets failed")
            return return_Response(
                message="Something went wrong.",
                status=400,
                errors=[str(e)],
            )

    @http.route(
        '/api/v1/etp_projects/aws_budget/export',
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

            jdata = self._read_json_body()

            domain = []
            if not bool(jdata.get('include_inactive')):
                domain.append(('active', '=', True))

            budget_ids = jdata.get('budget_ids') or []
            project_ids = jdata.get('project_ids') or []
            if budget_ids:
                if not isinstance(budget_ids, list) or not all(isinstance(x, int) for x in budget_ids):
                    return return_Response(
                        message="'budget_ids' must be a list of integers.",
                        status=400,
                    )
                domain.append(('id', 'in', budget_ids))
            if project_ids:
                if not isinstance(project_ids, list) or not all(isinstance(x, int) for x in project_ids):
                    return return_Response(
                        message="'project_ids' must be a list of integers.",
                        status=400,
                    )
                domain.append(('project_id', 'in', project_ids))

            Budget = request.env[BUDGET_MODEL].sudo()
            budgets = Budget.search(domain, order='project_id, name')

            output = io.BytesIO()
            wb = xlsxwriter.Workbook(output, {'in_memory': True})
            ws = wb.add_worksheet('AWS Budgets')

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
            f_text = wb.add_format({
                'font_size': 10, 'border': 1, 'border_color': '#e5e7eb',
                'align': 'left', 'valign': 'vcenter',
            })
            f_int = wb.add_format({
                'font_size': 10, 'border': 1, 'border_color': '#e5e7eb',
                'align': 'right', 'valign': 'vcenter', 'num_format': '0',
            })
            f_money = wb.add_format({
                'font_size': 10, 'border': 1, 'border_color': '#e5e7eb',
                'align': 'right', 'valign': 'vcenter', 'num_format': '#,##0.00',
            })
            f_pct = wb.add_format({
                'font_size': 10, 'border': 1, 'border_color': '#e5e7eb',
                'align': 'right', 'valign': 'vcenter', 'num_format': '0.00"%"',
            })
            f_total_label = wb.add_format({
                'bold': True, 'font_size': 10, 'bg_color': '#f9fafb',
                'border': 1, 'border_color': '#d1d5db',
                'align': 'right', 'valign': 'vcenter',
            })
            f_total_money = wb.add_format({
                'bold': True, 'font_size': 10, 'bg_color': '#f9fafb',
                'border': 1, 'border_color': '#d1d5db',
                'align': 'right', 'valign': 'vcenter', 'num_format': '#,##0.00',
            })

            headers = [
                '#', 'Budget Seq', 'Project', 'Currency',
                'Project Budget', 'Final Budget', 'Total Used Cost', 'Remaining',
                '% Consumed', 'Daily Burn Rate', 'Runway Days', 'Runway Depletes On',
                'Tag Key', 'Tag Value', 'Last Fetched At',
            ]
            widths = [5, 22, 28, 10, 16, 16, 18, 16, 13, 18, 14, 22, 14, 18, 22]
            for i, w in enumerate(widths):
                ws.set_column(i, i, w)

            ws.set_row(0, 28)
            ws.merge_range(0, 0, 0, len(headers) - 1, 'AWS Budget Consolidation', f_title)

            total_budget = 0.0
            total_consumed = 0.0
            total_remaining = 0.0
            project_set = set()
            currency_label = ""

            for b in budgets:
                total_budget += float(b.budget_amount or 0.0)
                total_consumed += float(b.total_consumed or 0.0)
                total_remaining += float(b.remaining or 0.0)
                if b.project_id:
                    project_set.add(b.project_id.id)
                if not currency_label and b.currency_id:
                    currency_label = b.currency_id.name or ""

            overall_pct = (total_consumed / total_budget * 100.0) if total_budget else 0.0

            ws.write(2, 0, 'Total Budgets', f_kpi_label)
            ws.write_number(2, 1, len(budgets), f_kpi_value)
            ws.write(2, 2, 'Projects', f_kpi_label)
            ws.write_number(2, 3, len(project_set), f_kpi_value)
            ws.write(2, 4, 'Currency', f_kpi_label)
            ws.write(2, 5, 'USD', f_kpi_value_text)

            ws.write(3, 0, 'Total Budget', f_kpi_label)
            ws.write_number(3, 1, total_budget, f_kpi_value)
            ws.write(3, 2, 'Total Consumed', f_kpi_label)
            ws.write_number(3, 3, total_consumed, f_kpi_value)
            ws.write(3, 4, 'Total Remaining', f_kpi_label)
            ws.write_number(3, 5, total_remaining, f_kpi_value)

            ws.write(4, 0, 'Overall % Consumed', f_kpi_label)
            ws.write_number(4, 1, round(overall_pct, 2), f_kpi_value)

            header_row = 6
            for col, h in enumerate(headers):
                ws.write(header_row, col, h, f_header)
            ws.set_row(header_row, 22)

            data_start = header_row + 1
            for idx, b in enumerate(budgets, 1):
                snapshot = b._budget_snapshot()
                row = data_start + idx - 1
                ws.write_number(row, 0, idx, f_int)
                ws.write(row, 1, b.name or "", f_text)
                ws.write(row, 2, b.project_id.name if b.project_id else "", f_text)
                ws.write(row, 3, 'USD', f_text)
                ws.write_number(row, 4, float(b.project_budget or 0.0), f_money)
                ws.write_number(row, 5, float(b.budget_amount or 0.0), f_money)
                ws.write_number(row, 6, float(b.total_consumed or 0.0), f_money)
                ws.write_number(row, 7, float(b.remaining or 0.0), f_money)
                ws.write_number(row, 8, round(float(b.percent_consumed or 0.0), 2), f_pct)
                ws.write_number(row, 9, float(snapshot.get("daily_burn_rate") or 0.0), f_money)
                ws.write_number(row, 10, int(snapshot.get("runway_days") or 0), f_int)
                ws.write(row, 11, snapshot.get("runway_depletes_on") or "", f_text)
                ws.write(row, 12, b.tag_key or "", f_text)
                ws.write(row, 13, b.tag_value or "", f_text)
                ws.write(
                    row, 14,
                    b.last_fetched_at.strftime("%Y-%m-%d %H:%M:%S") if b.last_fetched_at else "",
                    f_text,
                )

            if budgets:
                total_row = data_start + len(budgets)
                ws.merge_range(total_row, 0, total_row, 3, 'Totals', f_total_label)
                ws.write_number(total_row, 4, total_budget, f_total_money)
                ws.write_number(total_row, 5, total_budget, f_total_money)
                ws.write_number(total_row, 6, total_consumed, f_total_money)
                ws.write_number(total_row, 7, total_remaining, f_total_money)
                ws.write_number(total_row, 8, round(overall_pct, 2), wb.add_format({
                    'bold': True, 'font_size': 10, 'bg_color': '#f9fafb',
                    'border': 1, 'border_color': '#d1d5db',
                    'align': 'right', 'valign': 'vcenter', 'num_format': '0.00"%"',
                }))
                for col in range(9, len(headers)):
                    ws.write(total_row, col, "", f_total_label)

                ws.autofilter(header_row, 0, data_start + len(budgets) - 1, len(headers) - 1)
                ws.freeze_panes(header_row + 1, 2)

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
                project_name = b.project_id.name if b.project_id else ""
                budget_seq = b.name or ""
                budget_amount_b = float(b.budget_amount or 0.0)
                per_service = {}
                for line in b.cost_line_ids:
                    svc = (line.service_name or "Unknown").strip() or "Unknown"
                    agg = per_service.setdefault(svc, {'usd': 0.0})
                    agg['usd'] += float(line.amount_source or 0.0)
                for svc, agg in per_service.items():
                    pct = (agg['usd'] / budget_amount_b * 100.0) if budget_amount_b else 0.0
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
            ws2.merge_range(2, 3, 2, 4, top_service_label or '—', f_kpi_value_text)
            ws2.write(2, 5, 'Top Spend (USD)', f_kpi_label)
            ws2.write_number(2, 6, round(top_service_spend, 2), f_kpi_value)

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

            timestamp = fields.Datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = "aws_budget_export_%s.xlsx" % timestamp
            file_size = len(xlsx_bytes)
            file_b64 = base64.b64encode(xlsx_bytes).decode('utf-8')

            try:
                download_url = generate_s3_link(
                    file_b64, prefix='reports', filename=filename,
                )
            except Exception as e:
                _logger.exception("S3 upload failed for aws_budget export")
                return return_Response(
                    message="Failed to upload export to S3.",
                    status=500,
                    errors=[str(e)],
                )

            if not download_url:
                return return_Response(
                    message="S3 upload returned an empty link.",
                    status=500,
                )

            return return_Response(
                message="AWS budget export generated.",
                status=200,
                data={"data": {
                    "download_url": download_url,
                    "filename": filename,
                    "size": file_size,
                    "total_budgets": len(budgets),
                }},
            )
        except Exception as e:
            _logger.exception("export_aws_budgets failed")
            return return_Response(
                message="Something went wrong.",
                status=400,
                errors=[str(e)],
            )
