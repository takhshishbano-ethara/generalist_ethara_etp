import logging

from odoo import fields, http
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import validate_token

from .project_budget_controller import (
    BATCH_MODEL,
    _coerce_date,
    _coerce_float,
    _coerce_int,
    _pagination,
    _read_multipart_or_json,
    return_Response,
)

_logger = logging.getLogger(__name__)

DAILY_TASK_MODEL = "etp.batch.budget.daily.task"
BATCH_REQUEST_MODEL = "etp.batch.budget.request"

VALID_HEALTH = ("healthy", "warning", "critical", "unknown")

WARNING_RATIO = 1.0
CRITICAL_RATIO = 1.1

LLM_COST_SOURCES = ("openai", "moonshot", "openrouter")
LLM_AWS_SERVICE_NAME = "Amazon Bedrock"

BATCH_VIEW_PARTITIONS = {
    "current": ("in_progress",),
    "upcoming": ("draft",),
    "previous": ("delivered", "closed"),
}


def _derive_health(per_task_cost, ideal):
    if not ideal or not per_task_cost:
        return "unknown"
    ratio = per_task_cost / ideal
    if ratio <= WARNING_RATIO:
        return "healthy"
    if ratio <= CRITICAL_RATIO:
        return "warning"
    return "critical"


def _derive_daily_task_vals(batch, entry_date, done_count, total_cost, note):
    model_lines = batch.model_line_ids
    iterations_per_task = sum(model_lines.mapped("iterations"))
    no_of_trajectory = (done_count or 0) * iterations_per_task
    per_task_cost = (
        (total_cost / done_count) if (done_count and total_cost) else 0.0
    )
    per_trajectory_cost = (
        (total_cost / no_of_trajectory)
        if (no_of_trajectory and total_cost) else 0.0
    )
    ideal_per_task_cost = sum(model_lines.mapped("per_task_cost"))
    ideal_per_trajectory_cost = sum(
        model_lines.mapped("per_trajectory_cost")
    )
    infra_cost = sum(batch.infra_line_ids.mapped("per_day_cost"))
    subscription_cost = sum(
        batch.subscription_line_ids.mapped("per_day_cost")
    )
    return {
        "batch_id": batch.id,
        "entry_date": entry_date,
        "connected_model": batch.connected_model or False,
        "done_count": done_count,
        "no_of_trajectory": no_of_trajectory,
        "per_task_cost": per_task_cost,
        "per_trajectory_cost": per_trajectory_cost,
        "total_cost": total_cost,
        "ideal_per_task_cost": ideal_per_task_cost,
        "ideal_per_trajectory_cost": ideal_per_trajectory_cost,
        "infra_cost": infra_cost,
        "subscription_cost": subscription_cost,
        "health_status": _derive_health(per_task_cost, ideal_per_task_cost),
        "note": (note or "").strip() or False,
    }


def _dt_to_string(value):
    if not value:
        return None
    return fields.Datetime.to_string(value)


def _date_to_string(value):
    if not value:
        return None
    return value.isoformat()


def _health_label(value):
    selection = request.env[DAILY_TASK_MODEL]._fields["health_status"].selection
    return dict(selection).get(value, value or "")


def _daily_task_model_brief(mb):
    return {
        "id": mb.id,
        "ai_model_id": mb.ai_model_id.id,
        "ai_model_name": (
            mb.ai_model_id.display_name or mb.ai_model_id.name or ""
        ) if mb.ai_model_id else "",
        "cost_type": mb.cost_type or "",
        "per_task_cost": mb.per_task_cost or 0.0,
        "per_trajectory_cost": mb.per_trajectory_cost or 0.0,
        "iterations": mb.iterations or 0,
        "done_count": mb.done_count or 0,
        "trajectory_count": mb.trajectory_count or 0,
        "ideal_cost": mb.ideal_cost or 0.0,
        "allocated_cost": mb.allocated_cost or 0.0,
        "variance": mb.variance or 0.0,
        "health_status": mb.health_status or "unknown",
    }


def _daily_task_brief(dt):
    batch = dt.batch_id
    project = batch.project_id if batch else False
    return {
        "id": dt.id,
        "batch_id": batch.id if batch else 0,
        "batch_name": batch.name if batch else "",
        "batch_state": batch.state if batch else "",
        "project_id": project.id if project else 0,
        "project_name": project.display_name if project else "",
        "entry_date": _date_to_string(dt.entry_date),
        "start_date": _dt_to_string(dt.start_date),
        "end_date": _dt_to_string(dt.end_date),
        "done_count": dt.done_count or 0,
        "no_of_trajectory": dt.no_of_trajectory or 0,
        "per_task_cost": dt.per_task_cost or 0.0,
        "per_trajectory_cost": dt.per_trajectory_cost or 0.0,
        "total_cost": dt.total_cost or 0.0,
        "infra_cost": dt.infra_cost or 0.0,
        "subscription_cost": dt.subscription_cost or 0.0,
        "health_status": dt.health_status or "unknown",
        "health_status_label": _health_label(dt.health_status),
        "note": dt.note or "",
        "model_breakdown_count": len(dt.model_breakdown_ids),
    }


def _daily_task_detail(dt):
    data = _daily_task_brief(dt)
    data.update({
        "connected_model": dt.connected_model or "",
        "ideal_per_task_cost": dt.ideal_per_task_cost or 0.0,
        "ideal_per_trajectory_cost": dt.ideal_per_trajectory_cost or 0.0,
        "create_date": _dt_to_string(dt.create_date),
        "write_date": _dt_to_string(dt.write_date),
        "model_breakdown": [
            _daily_task_model_brief(mb) for mb in dt.model_breakdown_ids
        ],
    })
    return data


def _is_llm_line(line):
    if line.source in LLM_COST_SOURCES:
        return True
    if line.source == "aws" and (line.service_name or "").strip() == LLM_AWS_SERVICE_NAME:
        return True
    return False


def _batch_health_label(value):
    selection = request.env[BATCH_MODEL]._fields["health_status"].selection
    return dict(selection).get(value, value or "")


def _batch_state_label(value):
    selection = request.env[BATCH_MODEL]._fields["state"].selection
    return dict(selection).get(value, value or "")


def _request_status_label(value):
    selection = request.env[BATCH_REQUEST_MODEL]._fields["state"].selection
    return dict(selection).get(value, value or "")


def _batch_view_request_brief(req):
    requester = req.requester_id
    return {
        "id": req.id,
        "name": req.name or "",
        "raised_date": _dt_to_string(req.request_date or req.create_date),
        "sent_by_id": requester.id if requester else 0,
        "sent_by": (requester.name or "") if requester else "",
        "status": req.state or "",
        "status_label": _request_status_label(req.state),
        "amount": req.requested_total or 0.0,
    }


def _batch_view_brief(batch):
    daily_tasks = batch.daily_task_ids
    estimated = sum(dt.total_cost or 0.0 for dt in daily_tasks)
    llm_lines = batch.matched_cost_line_ids.filtered(_is_llm_line)
    actual = sum(llm_lines.mapped("amount_source"))
    variance = estimated - actual
    project = batch.project_id
    project_budget = batch.project_budget_id
    requests = batch.request_ids.sorted(
        lambda r: (r.request_date or r.create_date or fields.Datetime.now(), r.id),
        reverse=True,
    )
    return {
        "id": batch.id,
        "name": batch.name or "",
        "state": batch.state or "",
        "state_label": _batch_state_label(batch.state),
        "project_id": project.id if project else 0,
        "project_name": project.display_name if project else "",
        "project_budget_id": project_budget.id if project_budget else 0,
        "project_budget_name": project_budget.name if project_budget else "",
        "start_date": _date_to_string(batch.start_date),
        "end_date": _date_to_string(batch.end_date),
        "budget_amount": batch.approved_amount or 0.0,
        "actual": actual,
        "estimated": estimated,
        "variance": variance,
        "health": batch.health_status or "unknown",
        "health_label": _batch_health_label(batch.health_status),
        "total_task_count": batch.total_tasks or 0,
        "done_task_count": batch.done_tasks or 0,
        "remaining_task_count": batch.remaining_tasks or 0,
        "avg_qc": None,
        "created_date": _dt_to_string(batch.create_date),
        "requests": [_batch_view_request_brief(r) for r in requests],
    }


class EtpBudgetEstimationController(http.Controller):

    @http.route(
        "/api/v1/etp_projects/budget/estimation/create",
        type="http", auth="none", methods=["POST"], csrf=False, cors="*",
    )
    @validate_token
    def create_estimation(self, **params):
        jdata, _uploaded = _read_multipart_or_json()

        batch_id = _coerce_int(jdata.get("batch_id"))
        if not batch_id:
            return return_Response(
                message="batch_id is required.", status=400, data={},
            )
        batch = request.env[BATCH_MODEL].sudo().browse(batch_id).exists()
        if not batch:
            return return_Response(
                message=f"Phase budget {batch_id} does not exist.",
                status=400, data={},
            )

        done_count = _coerce_int(jdata.get("done_count"))
        if not done_count or done_count <= 0:
            return return_Response(
                message="done_count is required and must be > 0.",
                status=400, data={},
            )

        total_cost = _coerce_float(jdata.get("total_cost"), None)
        if total_cost is None or total_cost <= 0.0:
            return return_Response(
                message="total_cost is required and must be > 0.",
                status=400, data={},
            )

        raw_entry_date = jdata.get("entry_date")
        if raw_entry_date:
            entry_date = _coerce_date(raw_entry_date)
            if not entry_date:
                return return_Response(
                    message="entry_date must be a valid date (YYYY-MM-DD).",
                    status=400, data={},
                )
        else:
            entry_date = fields.Date.context_today(request.env.user)

        if batch.start_date and entry_date < batch.start_date:
            return return_Response(
                message=(
                    f"entry_date {entry_date} is before the phase budget "
                    f"start date {batch.start_date}."
                ),
                status=400, data={},
            )
        if batch.end_date and entry_date > batch.end_date:
            return return_Response(
                message=(
                    f"entry_date {entry_date} is after the phase budget "
                    f"end date {batch.end_date}."
                ),
                status=400, data={},
            )

        vals = _derive_daily_task_vals(
            batch, entry_date, done_count, total_cost, jdata.get("note"),
        )

        try:
            record = request.env[DAILY_TASK_MODEL].sudo().create(vals)
        except Exception as exc:
            _logger.exception(
                "Failed to create daily task for batch %s", batch.id,
            )
            return return_Response(
                message=f"Failed to create daily task: {exc}",
                status=400, data={},
            )
        return return_Response(
            message="Daily task record created.", status=200,
            data={"id": record.id, "data": _daily_task_detail(record)},
        )

    @http.route(
        "/api/v1/etp_projects/budget/estimation/list",
        type="http", auth="none", methods=["GET"], csrf=False, cors="*",
    )
    @validate_token
    def list_estimation(self, **params):
        limit, offset, err = _pagination(params)
        if err:
            return err
        domain = []
        batch_id = _coerce_int(params.get("batch_id"))
        if batch_id:
            domain.append(("batch_id", "=", batch_id))
        project_id = _coerce_int(params.get("project_id"))
        if project_id:
            domain.append(("batch_id.project_id", "=", project_id))
        project_budget_id = _coerce_int(params.get("project_budget_id"))
        if project_budget_id:
            domain.append(
                ("batch_id.project_budget_id", "=", project_budget_id),
            )
        raw_from = params.get("date_from")
        if raw_from:
            date_from = _coerce_date(raw_from)
            if not date_from:
                return return_Response(
                    message="date_from must be a valid date (YYYY-MM-DD).",
                    status=400, data={},
                )
            domain.append(("entry_date", ">=", date_from))
        raw_to = params.get("date_to")
        if raw_to:
            date_to = _coerce_date(raw_to)
            if not date_to:
                return return_Response(
                    message="date_to must be a valid date (YYYY-MM-DD).",
                    status=400, data={},
                )
            domain.append(("entry_date", "<=", date_to))
        health_status = (params.get("health_status") or "").strip()
        if health_status:
            if health_status not in VALID_HEALTH:
                return return_Response(
                    message=(
                        f"health_status must be one of {list(VALID_HEALTH)}."
                    ),
                    status=400, data={},
                )
            domain.append(("health_status", "=", health_status))
        search = (params.get("search") or "").strip()
        if search:
            domain.append(("note", "ilike", search))
        Model = request.env[DAILY_TASK_MODEL].sudo()
        total = Model.search_count(domain)
        records = Model.search(
            domain, limit=limit, offset=offset,
            order="entry_date desc, id desc",
        )
        items = [_daily_task_brief(r) for r in records]
        return return_Response(
            message="Daily task estimations fetched.", status=200,
            data={
                "total": total,
                "limit": limit,
                "offset": offset,
                "estimations": items,
            },
        )

    @http.route(
        "/api/v1/etp_projects/budget/estimation/detail",
        type="http", auth="none", methods=["GET"], csrf=False, cors="*",
    )
    @validate_token
    def detail_estimation(self, **params):
        dt_id = _coerce_int(params.get("id"))
        if not dt_id:
            return return_Response(
                message="id is required.", status=400, data={},
            )
        record = request.env[DAILY_TASK_MODEL].sudo().browse(dt_id).exists()
        if not record:
            return return_Response(
                message="Daily task record not found.",
                status=404, data={},
            )
        return return_Response(
            message="Daily task detail fetched.", status=200,
            data={"data": _daily_task_detail(record)},
        )

    @http.route(
        "/api/v1/etp_projects/budget/estimation/kpi",
        type="http", auth="none", methods=["GET"], csrf=False, cors="*",
    )
    @validate_token
    def kpi_estimation(self, **params):
        batch_id = _coerce_int(params.get("batch_id"))
        if not batch_id:
            return return_Response(
                message="batch_id is required.", status=400, data={},
            )
        batch = request.env[BATCH_MODEL].sudo().browse(batch_id).exists()
        if not batch:
            return return_Response(
                message=f"Phase budget {batch_id} does not exist.",
                status=404, data={},
            )

        today = fields.Date.context_today(request.env.user)
        daily_tasks = batch.daily_task_ids

        estimated_cost = sum(dt.total_cost or 0.0 for dt in daily_tasks)
        actual_cost = sum(
            batch.matched_cost_line_ids.mapped("amount_source")
        )
        variance = estimated_cost - actual_cost

        total_tasks = batch.total_tasks or 0
        done_tasks = batch.done_tasks or 0
        remaining_tasks = batch.remaining_tasks or 0
        avg_cost_per_task = (
            estimated_cost / done_tasks if done_tasks else 0.0
        )

        today_tasks = daily_tasks.filtered(
            lambda r: r.entry_date == today,
        )
        today_done_tasks = sum(dt.done_count or 0 for dt in today_tasks)
        today_estimated_cost = sum(
            dt.total_cost or 0.0 for dt in today_tasks
        )
        today_lines = batch.matched_cost_line_ids.filtered(
            lambda l: l.period == today,
        )
        today_actual_cost = sum(today_lines.mapped("amount_source"))
        today_variance = today_estimated_cost - today_actual_cost

        return return_Response(
            message="Estimation KPI fetched.", status=200,
            data={
                "batch_id": batch.id,
                "batch_name": batch.name or "",
                "project_id": batch.project_id.id if batch.project_id else 0,
                "project_name": (
                    batch.project_id.display_name if batch.project_id else ""
                ),
                "total_tasks": total_tasks,
                "done_tasks": done_tasks,
                "remaining_tasks": remaining_tasks,
                "estimated_cost": estimated_cost,
                "actual_cost": actual_cost,
                "variance": variance,
                "avg_cost_per_task": avg_cost_per_task,
                "today": _date_to_string(today),
                "today_done_tasks": today_done_tasks,
                "today_estimated_cost": today_estimated_cost,
                "today_actual_cost": today_actual_cost,
                "today_variance": today_variance,
            },
        )

    @http.route(
        "/api/v1/etp_projects/budget/estimation/batch_view",
        type="http", auth="none", methods=["GET"], csrf=False, cors="*",
    )
    @validate_token
    def batch_view(self, **params):
        limit, offset, err = _pagination(params)
        if err:
            return err
        base_domain = []
        project_id = _coerce_int(params.get("project_id"))
        if project_id:
            base_domain.append(("project_id", "=", project_id))
        project_budget_id = _coerce_int(params.get("project_budget_id"))
        if project_budget_id:
            base_domain.append(("project_budget_id", "=", project_budget_id))
        Model = request.env[BATCH_MODEL].sudo()
        result = {}
        for partition, states in BATCH_VIEW_PARTITIONS.items():
            domain = base_domain + [("state", "in", list(states))]
            total = Model.search_count(domain)
            order = (
                "start_date asc, id asc" if partition == "upcoming"
                else "start_date desc, id desc"
            )
            records = Model.search(
                domain, limit=limit, offset=offset, order=order,
            )
            result[partition] = {
                "total": total,
                "limit": limit,
                "offset": offset,
                "batches": [_batch_view_brief(r) for r in records],
            }
        return return_Response(
            message="Phase budgets fetched.", status=200,
            data=result,
        )
