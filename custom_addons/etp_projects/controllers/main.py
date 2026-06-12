import base64
import io
import json
import logging
import math
from datetime import timedelta

from odoo import fields, http
from odoo.exceptions import UserError
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import (
    generate_s3_link,
    return_Response,
    validate_token,
)

BUDGET_MODEL = 'etp.project.aws.budget'
COST_LINE_MODEL = 'etp.project.aws.cost.line'
TPR_MODEL = 'etp.project.token.purchase.request'

# Output timestamps/filenames in IST (the deployment's local zone).
IST_OFFSET = timedelta(hours=5, minutes=30)

_logger = logging.getLogger(__name__)


# Role gate for the portfolio budget read. Spend across ALL projects is
# CTO/PL-level finance, so the consolidation endpoint is limited to leadership
# (CTO/TPM) and project leads — mirroring the etp_projects TPR role xmlids and
# the leviathan_extension pattern. Taskers/QC must not see portfolio-wide spend.
BUDGET_CONSOLIDATION_ROLE_XMLIDS = (
    "api_auth_gateway.role_cto_technical",
    "api_auth_gateway.role_tpm_technical",
    "api_auth_gateway.role_pl_technical",
    "api_auth_gateway.role_pl_stem",
    "api_auth_gateway.role_pl_non_stem",
)


def _user_has_role(env, xmlids):
    """True when the token user's role is one of ``xmlids`` (missing roles are
    skipped). Mirrors the helper in ``token_purchase_request.py``."""
    role = env.user.user_role
    if not role:
        return False
    allowed = []
    for xmlid in xmlids:
        rec = env.ref(xmlid, raise_if_not_found=False)
        if rec:
            allowed.append(rec.id)
    return role.id in allowed


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
        '/api/v1/etp_projects/aws_cost/update_all',
        methods=['POST'], type='http', auth='none', csrf=False, cors='*',
    )
    @validate_token
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

            # active_test=False so 'include_inactive' is honoured — the default
            # context would silently re-filter inactive rows even with the
            # ('active','=',True) clause omitted. The domain handles the
            # default (active-only) case.
            Budget = request.env['etp.project.aws.budget'].sudo()
            budgets = Budget.with_context(active_test=False).search(domain)

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
                    budget._maybe_alert_thresholds()
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
                        "status": "error",
                        "error": str(e),
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
                        "status": "error",
                        "error": str(e),
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

            # active_test=False so 'include_inactive' is honoured (see
            # update_all_aws_cost) — the domain handles the active-only default.
            Budget = request.env[BUDGET_MODEL].sudo()
            budgets = Budget.with_context(active_test=False).search(
                domain, order='project_id, name')

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

    # ──────────────────────────────────────────────────────────────────────
    # Shared helpers
    # ──────────────────────────────────────────────────────────────────────

    def _filter_domain(self, jdata):
        """Build the common budget search domain from the filter body.

        Raises ``ValueError`` (caught by callers -> 400) if ``budget_ids`` or
        ``project_ids`` are present but not lists of integers.
        """
        domain = []
        if not bool(jdata.get('include_inactive')):
            domain.append(('active', '=', True))

        budget_ids = jdata.get('budget_ids') or []
        project_ids = jdata.get('project_ids') or []
        if budget_ids:
            if not isinstance(budget_ids, list) or not all(isinstance(x, int) for x in budget_ids):
                raise ValueError("'budget_ids' must be a list of integers.")
            domain.append(('id', 'in', budget_ids))
        if project_ids:
            if not isinstance(project_ids, list) or not all(isinstance(x, int) for x in project_ids):
                raise ValueError("'project_ids' must be a list of integers.")
            domain.append(('project_id', 'in', project_ids))
        return domain

    def _attributed_models(self, project_spend, approved_by_model):
        """Distribute a project's real consumption across its models.

        ``approved_by_model`` maps ``model_name -> sum(approved_amount)`` over
        the project's completed Token Purchase Requests. Consumption is split
        proportionally to those amounts; a project with no completed TPRs (or
        only blank model names) attributes everything to ``"(no model)"``.

        Returns a list of ``{model_name, spend, share_pct}`` (share over the
        project's spend), sorted by spend desc. The unrounded ``spend`` values
        sum to ``project_spend``.
        """
        total_approved = sum(approved_by_model.values())
        if total_approved > 0:
            raw = [
                {"model_name": name, "spend": project_spend * (amount / total_approved)}
                for name, amount in approved_by_model.items()
            ]
        else:
            raw = [{"model_name": "(no model)", "spend": project_spend}]
        for m in raw:
            m["share_pct"] = round((m["spend"] / project_spend * 100.0), 2) if project_spend else 0.0
        raw.sort(key=lambda m: m["spend"], reverse=True)
        return raw

    # ──────────────────────────────────────────────────────────────────────
    # 3. Budget Consolidation dashboard roll-up
    # ──────────────────────────────────────────────────────────────────────

    @http.route(
        '/api/v1/etp_projects/budget_consolidation',
        methods=['POST'], type='http', auth='none', csrf=False, cors='*',
    )
    @validate_token
    def budget_consolidation(self, **params):
        try:
            jdata = self._read_json_body()

            # Authorization: portfolio-wide spend is CTO/PL-level finance. Allow
            # leadership/PL by role, OR anyone already trusted to approve/complete
            # TPRs (covers a CTO whose role record isn't set but who is a
            # configured approver). Everyone else (taskers/QC) is forbidden.
            env = request.env
            tpr = env[TPR_MODEL]
            if not (
                _user_has_role(env, BUDGET_CONSOLIDATION_ROLE_XMLIDS)
                or env.uid in tpr._get_approver_users().ids
                or env.uid in tpr._get_finance_users().ids
            ):
                return return_Response(
                    message="You are not allowed to view portfolio budgets.",
                    status=403, errors=[{"code": "forbidden"}],
                )

            try:
                domain = self._filter_domain(jdata)
            except ValueError as ve:
                return return_Response(message=str(ve), status=400)

            threshold = jdata.get('needs_attention_threshold_pct')
            try:
                threshold = float(threshold) if threshold is not None else 80.0
            except (TypeError, ValueError):
                threshold = 80.0

            # active_test=False so 'include_inactive' is honoured (see
            # update_all_aws_cost) — the domain handles the active-only default.
            Budget = request.env[BUDGET_MODEL].sudo()
            budgets = Budget.with_context(active_test=False).search(
                domain, order='project_id, name')

            # Currency from the first budget in scope, falling back to INR.
            currency = budgets[0].currency_id if budgets and budgets[0].currency_id else \
                request.env.ref('base.INR', raise_if_not_found=False)
            currency_name = currency.name if currency else ""
            currency_symbol = currency.symbol if currency else ""

            # Aggregate spend / budget / burn per project (a project may carry
            # more than one budget record).
            projects = {}
            for b in budgets:
                pid = b.project_id.id if b.project_id else False
                proj = projects.setdefault(pid, {
                    "project_id": pid,
                    "project_name": b.project_id.name if b.project_id else "",
                    "spend": 0.0, "budget": 0.0, "burn": 0.0,
                })
                proj["spend"] += float(b.total_consumed or 0.0)
                proj["budget"] += float(b.budget_amount or 0.0)
                proj["burn"] += float(b._budget_snapshot().get("daily_burn_rate") or 0.0)

            # Completed TPR approved amounts per project, grouped by model.
            approved = {}
            if projects:
                TPR = request.env[TPR_MODEL].sudo()
                tprs = TPR.search([
                    ('state', '=', 'completed'),
                    ('project_id', 'in', list(projects.keys())),
                ])
                for t in tprs:
                    pid = t.project_id.id if t.project_id else False
                    if pid not in projects:
                        continue
                    key = (t.model_name or "").strip() or "(no model)"
                    bucket = approved.setdefault(pid, {})
                    bucket[key] = bucket.get(key, 0.0) + float(t.approved_amount or 0.0)

            total_spend = sum(p["spend"] for p in projects.values())
            total_budget = sum(p["budget"] for p in projects.values())

            global_model_spend = {}
            budget_by_projects = []
            for pid, p in projects.items():
                spend = p["spend"]
                models = self._attributed_models(spend, approved.get(pid, {}))
                for m in models:
                    global_model_spend[m["model_name"]] = \
                        global_model_spend.get(m["model_name"], 0.0) + m["spend"]
                top_model = models[0]["model_name"] if models else ""
                # Round per-model spend for output (after global accumulation).
                for m in models:
                    m["spend"] = round(m["spend"], 2)

                budget = p["budget"]
                remaining = budget - spend
                utilization = (spend / budget * 100.0) if budget else 0.0
                burn = p["burn"]
                runway = math.floor(remaining / burn) if burn > 0 else None
                budget_by_projects.append({
                    "project_id": pid,
                    "project_name": p["project_name"],
                    "spend": round(spend, 2),
                    "budget": round(budget, 2),
                    "remaining": round(remaining, 2),
                    "utilization_pct": round(utilization, 2),
                    "runway_days": runway,
                    "top_model": top_model,
                    "models": models,
                })

            budget_by_projects.sort(key=lambda r: r["utilization_pct"], reverse=True)

            spend_by_project = sorted([
                {
                    "project_id": p["project_id"],
                    "project_name": p["project_name"],
                    "amount": round(p["spend"], 2),
                    "share_pct": round(p["spend"] / total_spend * 100.0, 2) if total_spend else 0.0,
                }
                for p in projects.values()
            ], key=lambda x: x["amount"], reverse=True)

            spend_by_model = sorted([
                {
                    "model_name": name,
                    "amount": round(amount, 2),
                    "share_pct": round(amount / total_spend * 100.0, 2) if total_spend else 0.0,
                }
                for name, amount in global_model_spend.items()
            ], key=lambda x: x["amount"], reverse=True)

            needs_attention_count = sum(
                1 for r in budget_by_projects if r["utilization_pct"] >= threshold
            )
            top_model = spend_by_model[0] if spend_by_model else \
                {"model_name": "", "amount": 0.0, "share_pct": 0.0}

            kpis = {
                "total_spend": round(total_spend, 2),
                "total_remaining": round(total_budget - total_spend, 2),
                "percent_consumed": round(total_spend / total_budget * 100.0, 2) if total_budget else 0.0,
                "total_budget": round(total_budget, 2),
                "active_project_count": len(projects),
                "needs_attention_count": needs_attention_count,
                "needs_attention_threshold_pct": round(threshold, 2),
                "top_model": dict(top_model),
                "currency": currency_name,
                "currency_symbol": currency_symbol,
            }

            return return_Response(
                message="OK",
                status=200,
                data={"data": {
                    "kpis": kpis,
                    "spend_by_model": spend_by_model,
                    "spend_by_project": spend_by_project,
                    "budget_by_projects": budget_by_projects,
                }},
            )
        except Exception as e:
            _logger.exception("budget_consolidation failed")
            return return_Response(
                message="Something went wrong.",
                status=400,
                errors=[str(e)],
            )

    # ──────────────────────────────────────────────────────────────────────
    # 4. Export the consolidation table to a styled .xlsx on S3
    # ──────────────────────────────────────────────────────────────────────

    @http.route(
        '/api/v1/etp_projects/aws_budget/export',
        methods=['POST'], type='http', auth='none', csrf=False, cors='*',
    )
    @validate_token
    def export_aws_budgets(self, **params):
        try:
            try:
                import xlsxwriter  # noqa: F401
            except ImportError:
                return return_Response(
                    message="Python package 'xlsxwriter' is not installed on the server.",
                    status=400,
                )

            jdata = self._read_json_body()
            try:
                domain = self._filter_domain(jdata)
            except ValueError as ve:
                return return_Response(message=str(ve), status=400)

            # active_test=False so 'include_inactive' is honoured (see
            # update_all_aws_cost) — the domain handles the active-only default.
            Budget = request.env[BUDGET_MODEL].sudo()
            budgets = Budget.with_context(active_test=False).search(
                domain, order='project_id, name')

            try:
                output, size = self._build_export_workbook(budgets)
            except Exception as e:
                _logger.exception("aws_budget export workbook build failed")
                return return_Response(
                    message="Something went wrong.",
                    status=400,
                    errors=[str(e)],
                )

            timestamp = (fields.Datetime.now() + IST_OFFSET).strftime('%Y%m%d_%H%M%S')
            filename = "aws_budget_export_%s.xlsx" % timestamp
            file_b64 = base64.b64encode(output).decode('utf-8')

            try:
                download_url = generate_s3_link(file_b64, prefix='reports', filename=filename)
            except Exception as e:
                _logger.exception("aws_budget export S3 upload failed")
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
                data={"data": {"data": {
                    "download_url": download_url,
                    "filename": filename,
                    "size": size,
                    "total_budgets": len(budgets),
                }}},
            )
        except Exception as e:
            _logger.exception("export_aws_budgets failed")
            return return_Response(
                message="Something went wrong.",
                status=400,
                errors=[str(e)],
            )

    def _build_export_workbook(self, budgets):
        """Build the two-sheet consolidation workbook, return (bytes, size)."""
        import xlsxwriter

        stream = io.BytesIO()
        wb = xlsxwriter.Workbook(stream, {'in_memory': True})

        # ── Reusable formats ──────────────────────────────────────────────
        PRIMARY = '#1B2A4A'
        ACCENT = '#2E86DE'
        LIGHT = '#F0F4F8'
        BORDER = '#CBD5E1'
        f_title = wb.add_format({
            'bold': True, 'font_size': 16, 'font_color': '#FFFFFF',
            'bg_color': PRIMARY, 'align': 'left', 'valign': 'vcenter',
        })
        f_kpi_label = wb.add_format({
            'bold': True, 'font_size': 10, 'font_color': '#FFFFFF',
            'bg_color': ACCENT, 'align': 'left', 'valign': 'vcenter', 'border': 1, 'border_color': BORDER,
        })
        f_kpi_value = wb.add_format({
            'font_size': 10, 'font_color': PRIMARY, 'bg_color': LIGHT,
            'align': 'left', 'valign': 'vcenter', 'border': 1, 'border_color': BORDER,
        })
        f_kpi_num = wb.add_format({
            'font_size': 10, 'font_color': PRIMARY, 'bg_color': LIGHT, 'num_format': '#,##0.00',
            'align': 'left', 'valign': 'vcenter', 'border': 1, 'border_color': BORDER,
        })
        f_kpi_pct = wb.add_format({
            'font_size': 10, 'font_color': PRIMARY, 'bg_color': LIGHT, 'num_format': '0.00"%"',
            'align': 'left', 'valign': 'vcenter', 'border': 1, 'border_color': BORDER,
        })
        f_header = wb.add_format({
            'bold': True, 'font_size': 10, 'font_color': '#FFFFFF', 'bg_color': ACCENT,
            'align': 'center', 'valign': 'vcenter', 'border': 1, 'border_color': BORDER, 'text_wrap': True,
        })
        f_text = wb.add_format({'font_size': 10, 'border': 1, 'border_color': BORDER, 'valign': 'vcenter'})
        f_int = wb.add_format({'font_size': 10, 'border': 1, 'border_color': BORDER, 'num_format': '0', 'align': 'center'})
        f_num = wb.add_format({'font_size': 10, 'border': 1, 'border_color': BORDER, 'num_format': '#,##0.00'})
        f_pct = wb.add_format({'font_size': 10, 'border': 1, 'border_color': BORDER, 'num_format': '0.00"%"'})
        f_tot_text = wb.add_format({'bold': True, 'font_size': 10, 'bg_color': LIGHT, 'border': 1, 'border_color': BORDER})
        f_tot_num = wb.add_format({'bold': True, 'font_size': 10, 'bg_color': LIGHT, 'border': 1, 'border_color': BORDER, 'num_format': '#,##0.00'})
        f_tot_pct = wb.add_format({'bold': True, 'font_size': 10, 'bg_color': LIGHT, 'border': 1, 'border_color': BORDER, 'num_format': '0.00"%"'})

        currency = budgets[0].currency_id if budgets and budgets[0].currency_id else \
            request.env.ref('base.INR', raise_if_not_found=False)
        currency_name = currency.name if currency else ""

        self._write_budgets_sheet(
            wb, f_title, f_kpi_label, f_kpi_value, f_kpi_num, f_kpi_pct, f_header,
            f_text, f_int, f_num, f_pct, f_tot_text, f_tot_num, f_tot_pct,
            budgets, currency_name,
        )
        self._write_service_sheet(
            wb, f_title, f_kpi_label, f_kpi_value, f_kpi_num, f_header,
            f_text, f_int, f_num, f_pct, f_tot_text, f_tot_num,
            budgets,
        )

        wb.close()
        data = stream.getvalue()
        stream.close()
        return data, len(data)

    def _write_budgets_sheet(self, wb, f_title, f_kpi_label, f_kpi_value, f_kpi_num,
                             f_kpi_pct, f_header, f_text, f_int, f_num, f_pct,
                             f_tot_text, f_tot_num, f_tot_pct, budgets, currency_name):
        ws = wb.add_worksheet('AWS Budgets')
        headers = [
            '#', 'Budget Seq', 'Project', 'Currency', 'Project Budget', 'Final Budget',
            'Total Used Cost', 'Remaining', '% Consumed', 'Daily Burn Rate', 'Runway Days',
            'Runway Depletes On', 'Tag Key', 'Tag Value', 'Last Fetched At',
        ]
        last_col = len(headers) - 1

        # Row 1: title banner.
        ws.merge_range(0, 0, 0, last_col, 'AWS Budget Consolidation', f_title)
        ws.set_row(0, 30)

        total_budget = sum(float(b.budget_amount or 0.0) for b in budgets)
        total_consumed = sum(float(b.total_consumed or 0.0) for b in budgets)
        total_remaining = sum(float(b.remaining or 0.0) for b in budgets)
        overall_pct = (total_consumed / total_budget * 100.0) if total_budget else 0.0
        project_count = len({b.project_id.id for b in budgets if b.project_id})

        # Rows 3-5: KPI block (0-based rows 2-4).
        ws.write(2, 0, 'Total Budgets', f_kpi_label)
        ws.write(2, 1, len(budgets), f_int)
        ws.write(2, 2, 'Projects', f_kpi_label)
        ws.write(2, 3, project_count, f_int)
        ws.write(2, 4, 'Currency', f_kpi_label)
        ws.write(2, 5, currency_name, f_kpi_value)
        ws.write(3, 0, 'Total Budget', f_kpi_label)
        ws.write_number(3, 1, total_budget, f_kpi_num)
        ws.write(3, 2, 'Total Consumed', f_kpi_label)
        ws.write_number(3, 3, total_consumed, f_kpi_num)
        ws.write(3, 4, 'Total Remaining', f_kpi_label)
        ws.write_number(3, 5, total_remaining, f_kpi_num)
        ws.write(4, 0, 'Overall % Consumed', f_kpi_label)
        ws.write_number(4, 1, round(overall_pct, 2), f_kpi_pct)

        # Row 7 (0-based 6): headers.
        header_row = 6
        for col, h in enumerate(headers):
            ws.write(header_row, col, h, f_header)

        row = header_row + 1
        for idx, b in enumerate(budgets, start=1):
            snap = b._budget_snapshot()
            ws.write_number(row, 0, idx, f_int)
            ws.write(row, 1, b.name or '', f_text)
            ws.write(row, 2, b.project_id.name if b.project_id else '', f_text)
            ws.write(row, 3, b.currency_id.name if b.currency_id else '', f_text)
            ws.write_number(row, 4, float(b.project_budget or 0.0), f_num)
            ws.write_number(row, 5, float(b.budget_amount or 0.0), f_num)
            ws.write_number(row, 6, float(b.total_consumed or 0.0), f_num)
            ws.write_number(row, 7, float(b.remaining or 0.0), f_num)
            ws.write_number(row, 8, round(float(b.percent_consumed or 0.0), 2), f_pct)
            ws.write_number(row, 9, float(snap.get('daily_burn_rate') or 0.0), f_num)
            ws.write_number(row, 10, int(snap.get('runway_days') or 0), f_int)
            ws.write(row, 11, snap.get('runway_depletes_on') or '', f_text)
            ws.write(row, 12, b.tag_key or '', f_text)
            ws.write(row, 13, b.tag_value or '', f_text)
            ws.write(row, 14, (
                b.last_fetched_at.strftime('%Y-%m-%d %H:%M:%S') if b.last_fetched_at else ''
            ), f_text)
            row += 1

        # Totals row.
        ws.write(row, 0, '', f_tot_text)
        ws.write(row, 1, 'TOTAL', f_tot_text)
        ws.write(row, 2, '', f_tot_text)
        ws.write(row, 3, '', f_tot_text)
        ws.write_number(row, 4, sum(float(b.project_budget or 0.0) for b in budgets), f_tot_num)
        ws.write_number(row, 5, total_budget, f_tot_num)
        ws.write_number(row, 6, total_consumed, f_tot_num)
        ws.write_number(row, 7, total_remaining, f_tot_num)
        ws.write_number(row, 8, round(overall_pct, 2), f_tot_pct)

        # Column widths + freeze + autofilter.
        widths = [5, 18, 22, 9, 15, 15, 16, 14, 12, 15, 12, 18, 14, 16, 20]
        for col, w in enumerate(widths):
            ws.set_column(col, col, w)
        last_data_row = row - 1 if budgets else header_row
        ws.autofilter(header_row, 0, last_data_row, last_col)
        ws.freeze_panes(header_row + 1, 3)

    def _write_service_sheet(self, wb, f_title, f_kpi_label, f_kpi_value, f_kpi_num,
                             f_header, f_text, f_int, f_num, f_pct,
                             f_tot_text, f_tot_num, budgets):
        ws = wb.add_worksheet('Service Spend')
        headers = [
            '#', 'Project', 'Budget Seq', 'Service',
            'Total Cost (USD)', 'Total Cost (INR)', '% of Budget',
        ]
        last_col = len(headers) - 1

        # Aggregate cost lines by (budget, service) across all stored periods.
        rows = []
        for b in budgets:
            agg = {}
            for line in b.cost_line_ids:
                svc = line.service_name or ''
                cell = agg.setdefault(svc, [0.0, 0.0])
                cell[0] += float(line.amount_source or 0.0)
                cell[1] += float(line.amount_inr or 0.0)
            budget_amount = float(b.budget_amount or 0.0)
            for svc, (usd, inr) in agg.items():
                rows.append({
                    'project': b.project_id.name if b.project_id else '',
                    'seq': b.name or '',
                    'service': svc,
                    'usd': usd,
                    'inr': inr,
                    'pct': (inr / budget_amount * 100.0) if budget_amount else 0.0,
                })
        # Sort by project, seq, then INR desc (top spender first within a budget).
        rows.sort(key=lambda r: (r['project'], r['seq'], -r['inr']))

        ws.merge_range(0, 0, 0, last_col, 'Service-Wise Spend', f_title)
        ws.set_row(0, 30)

        total_usd = sum(r['usd'] for r in rows)
        total_inr = sum(r['inr'] for r in rows)
        top = max(rows, key=lambda r: r['inr']) if rows else None

        # Row 3 (0-based 2): KPI block.
        ws.write(2, 0, 'Total Services', f_kpi_label)
        ws.write(2, 1, len(rows), f_int)
        ws.write(2, 2, 'Top Service', f_kpi_label)
        ws.write(2, 3, top['service'] if top else '', f_kpi_value)
        ws.write(2, 4, 'Top Spend (INR)', f_kpi_label)
        ws.write_number(2, 5, round(top['inr'], 2) if top else 0.0, f_kpi_num)

        # Row 5 (0-based 4): headers.
        header_row = 4
        for col, h in enumerate(headers):
            ws.write(header_row, col, h, f_header)

        row = header_row + 1
        for idx, r in enumerate(rows, start=1):
            ws.write_number(row, 0, idx, f_int)
            ws.write(row, 1, r['project'], f_text)
            ws.write(row, 2, r['seq'], f_text)
            ws.write(row, 3, r['service'], f_text)
            ws.write_number(row, 4, r['usd'], f_num)
            ws.write_number(row, 5, r['inr'], f_num)
            ws.write_number(row, 6, round(r['pct'], 2), f_pct)
            row += 1

        # Grand-total row.
        ws.write(row, 0, '', f_tot_text)
        ws.write(row, 1, 'GRAND TOTAL', f_tot_text)
        ws.write(row, 2, '', f_tot_text)
        ws.write(row, 3, '', f_tot_text)
        ws.write_number(row, 4, total_usd, f_tot_num)
        ws.write_number(row, 5, total_inr, f_tot_num)
        ws.write(row, 6, '', f_tot_text)

        widths = [5, 22, 18, 28, 16, 16, 12]
        for col, w in enumerate(widths):
            ws.set_column(col, col, w)
        last_data_row = row - 1 if rows else header_row
        ws.autofilter(header_row, 0, last_data_row, last_col)
        ws.freeze_panes(header_row + 1, 3)
