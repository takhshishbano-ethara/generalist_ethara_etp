# -*- coding: utf-8 -*-
"""ethara_project budget estimation / daily-task HTTP API.

Ports the `estimation/*` endpoints from
`etp_projects/budget_controllers/estimation_controller.py` onto the
`ethara.project.*` model family. Every route lives under the canonical
`/api/v1/ethara_project/budget/estimation/*` prefix.

Model / field deltas applied throughout (etp -> ethara):

    etp.batch.budget                  -> ethara.project.phase
    etp.batch.budget.daily.task       -> ethara.project.phase.daily.task
      daily_task.batch_id             -> daily_task.phase_id
    batch.project_budget_id           -> phase.budget_id
    batch.matched_cost_line_ids       -> resolved via ethara.project.cost.line
                                         (project + granularity=day + period range)

IMPORTANT (Flutter compatibility): the Flutter frontend keeps sending the
param names `batch_id` and `project_budget_id`. Those SAME param names are
accepted here, but `batch_id`'s value is treated as an `ethara.project.phase`
id (written to the daily task's `phase_id`) and `project_budget_id` filters on
`phase.budget_id`. Every JSON field name in the response envelopes is kept
identical to the etp responses so the existing Flutter models keep parsing.

The `ethara.project.phase.daily.task` model already:
  * auto-populates `model_breakdown_ids` from the phase's model lines on create
  * flips the phase state `approved` -> `in_progress` on create
  * validates `entry_date` within `phase.start_date .. phase.end_date`
so the controller relies on the model's own `create()` for that behaviour and
does not duplicate it.
"""

import json
import logging

from odoo import fields, http
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    validate_token,
)

from .budget_api import (
    _coerce_float,
    _coerce_int,
    _pagination,
    _read_multipart_or_json,
)

_logger = logging.getLogger(__name__)

PHASE_MODEL = "ethara.project.phase"
DAILY_TASK_MODEL = "ethara.project.phase.daily.task"
DAILY_TASK_MODEL_LINE = "ethara.project.phase.daily.task.model"
AI_MODEL_MODEL = "ethara.project.ai.model"
COST_LINE_MODEL = "ethara.project.cost.line"

VALID_HEALTH = ("healthy", "warning", "critical", "unknown")

WARNING_RATIO = 1.0
CRITICAL_RATIO = 1.1

LLM_COST_SOURCES = ("openai", "moonshot", "openrouter")
LLM_AWS_SERVICE_NAME = "Amazon Bedrock"

# state -> partition mapping for batch_view
BATCH_VIEW_PARTITIONS = {
    "current": ("approved", "in_progress",),
    "upcoming": ("draft",),
    "previous": ("delivered", "closed"),
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _coerce_date(value):
    if not value:
        return False
    if isinstance(value, str):
        try:
            return fields.Date.from_string(value)
        except Exception:
            return False
    return value


def _derive_health(per_task_cost, ideal):
    if not ideal or not per_task_cost:
        return "unknown"
    ratio = per_task_cost / ideal
    if ratio <= WARNING_RATIO:
        return "healthy"
    if ratio <= CRITICAL_RATIO:
        return "warning"
    return "critical"


def _derive_daily_task_vals(
    phase, entry_date, done_count, total_cost, note, no_of_trajectory=None
):
    model_lines = phase.model_line_ids
    iterations_per_task = sum(model_lines.mapped("iterations"))
    # Honour a user-entered "Successful trajectories" value from the popup;
    # otherwise fall back to done_count × iterations-per-task.
    if no_of_trajectory and no_of_trajectory > 0:
        no_of_trajectory = int(no_of_trajectory)
    else:
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
    infra_cost = sum(phase.infra_line_ids.mapped("per_day_cost"))
    subscription_cost = sum(
        phase.subscription_line_ids.mapped("per_day_cost")
    )
    return {
        "phase_id": phase.id,
        "entry_date": entry_date,
        "connected_model": phase.connected_model or False,
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


def _parse_task_quantity(value):
    """Mirror the popup's `getRowTaskQuantity`: a numeric Task cell is the task
    count for that row; any non-numeric (or <= 0) text counts as one task."""
    qty = _coerce_int(value)
    return qty if qty and qty > 0 else 1


def _build_model_breakdown_cmds(rows):
    """Translate the popup's manual `model_rows` into `model_breakdown_ids`
    create commands. Rows whose `model_id` doesn't resolve to a live
    `ethara.project.ai.model` are skipped. Returns [] when nothing usable."""
    if isinstance(rows, str):
        try:
            rows = json.loads(rows)
        except (ValueError, TypeError):
            return []
    if not isinstance(rows, (list, tuple)):
        return []
    ai_model_env = request.env[AI_MODEL_MODEL].sudo()
    cmds = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        model_id = _coerce_int(row.get("model_id"))
        if not model_id or not ai_model_env.browse(model_id).exists():
            continue
        cost = _coerce_float(row.get("cost"), 0.0) or 0.0
        task_qty = _parse_task_quantity(row.get("task"))
        cmds.append((0, 0, {
            "ai_model_id": model_id,
            "ai_model_name": (row.get("model_name") or "").strip() or False,
            "cost_type": "per_task",
            "task_label": (str(row.get("task") or "").strip() or False),
            "runs": (str(row.get("runs") or "").strip() or False),
            "input_tokens": _coerce_float(row.get("input_tokens"), 0.0) or 0.0,
            "output_tokens": _coerce_float(row.get("output_tokens"), 0.0) or 0.0,
            "line_cost": cost,
            "per_task_cost": (cost / task_qty) if task_qty else cost,
        }))
    return cmds


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


def _is_llm_line(line):
    if line.source in LLM_COST_SOURCES:
        return True
    if line.source == "aws" and (
        line.service_name or ""
    ).strip() == LLM_AWS_SERVICE_NAME:
        return True
    return False


def _phase_cost_lines(phase):
    """Return the daily cost lines matched to a phase.

    `ethara.project.phase` has no `matched_cost_line_ids` relation; mirror the
    model's own `_cost_line_domain()` (project + granularity=day + period
    within the phase window) and exclude model-breakdown rows to avoid double
    counting - identical to how the phase computes its consumed cost.
    """
    if not (phase.ethara_project_id and phase.start_date and phase.end_date):
        return request.env[COST_LINE_MODEL].sudo()
    domain = [
        ("ethara_project_id", "=", phase.ethara_project_id.id),
        ("granularity", "=", "day"),
        ("period", ">=", phase.start_date),
        ("period", "<=", phase.end_date),
        ("is_model_breakdown", "=", False),
    ]
    return request.env[COST_LINE_MODEL].sudo().search(domain)


def _daily_task_model_brief(mb):
    return {
        "id": mb.id,
        "ai_model_id": mb.ai_model_id.id,
        "ai_model_name": (
            mb.ai_model_name
            or (
                mb.ai_model_id.display_name or mb.ai_model_id.name
                if mb.ai_model_id else ""
            )
        ),
        "cost_type": mb.cost_type or "",
        "task_label": mb.task_label or "",
        "runs": mb.runs or "",
        "input_tokens": mb.input_tokens or 0.0,
        "output_tokens": mb.output_tokens or 0.0,
        "line_cost": mb.line_cost or 0.0,
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
    phase = dt.phase_id
    project = phase.ethara_project_id if phase else False
    return {
        "id": dt.id,
        "batch_id": phase.id if phase else 0,
        "batch_name": phase.name if phase else "",
        "batch_state": phase.state if phase else "",
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
        # Day-level token totals rolled up from the per-model breakdown rows
        # (populated by the "Log daily task" popup).
        "input_tokens": sum(dt.model_breakdown_ids.mapped("input_tokens")),
        "output_tokens": sum(dt.model_breakdown_ids.mapped("output_tokens")),
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


def _phase_state_label(value):
    selection = request.env[PHASE_MODEL]._fields["state"].selection
    return dict(selection).get(value, value or "")


def _phase_health_label(value):
    selection = request.env[PHASE_MODEL]._fields["health_status"].selection
    return dict(selection).get(value, value or "")


def _request_status_label(value):
    model = request.env["ethara.project.phase.request"]
    selection = model._fields["state"].selection
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


def _batch_view_brief(phase):
    daily_tasks = phase.daily_task_ids
    estimated = sum(dt.total_cost or 0.0 for dt in daily_tasks)
    cost_lines = _phase_cost_lines(phase)
    llm_lines = cost_lines.filtered(_is_llm_line)
    actual = sum(llm_lines.mapped("amount_source"))
    variance = estimated - actual
    project = phase.ethara_project_id
    project_budget = phase.budget_id
    requests = phase.request_ids.sorted(
        lambda r: (
            r.request_date or r.create_date or fields.Datetime.now(), r.id
        ),
        reverse=True,
    )
    return {
        "id": phase.id,
        "name": phase.name or "",
        "state": phase.state or "",
        "state_label": _phase_state_label(phase.state),
        "project_id": project.id if project else 0,
        "project_name": project.display_name if project else "",
        "project_budget_id": project_budget.id if project_budget else 0,
        "project_budget_name": project_budget.name if project_budget else "",
        "start_date": _date_to_string(phase.start_date),
        "end_date": _date_to_string(phase.end_date),
        "budget_amount": phase.approved_amount or 0.0,
        "actual": actual,
        "estimated": estimated,
        "variance": variance,
        "health": phase.health_status or "unknown",
        "health_label": _phase_health_label(phase.health_status),
        "total_task_count": phase.total_tasks or 0,
        "done_task_count": phase.done_tasks or 0,
        "remaining_task_count": phase.remaining_tasks or 0,
        "avg_qc": None,
        "models": ", ".join(
            name for name in phase.model_line_ids.mapped("ai_model_name") if name
        ),
        "created_date": _dt_to_string(phase.create_date),
        "requests": [_batch_view_request_brief(r) for r in requests],
    }


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------

class EtharaBudgetEstimationController(http.Controller):
    """Estimation / daily-task REST endpoints for the Flutter budget flow."""

    # ------------------------------------------------------------ create
    @http.route(
        "/api/v1/ethara_project/budget/estimation/create",
        type="http", auth="none", methods=["POST"], csrf=False, cors="*",
    )
    @validate_token
    def create_estimation(self, **params):
        jdata, _uploaded = _read_multipart_or_json()

        phase_id = _coerce_int(jdata.get("batch_id"))
        if not phase_id:
            return return_Response(
                message="batch_id is required.", status=400, data={},
            )
        phase = request.env[PHASE_MODEL].sudo().browse(phase_id).exists()
        if not phase:
            return return_Response(
                message=f"Phase budget {phase_id} does not exist.",
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

        # The daily-task model enforces entry_date within the phase window via
        # its own @api.constrains; validate here too for a clean 400 message.
        if phase.start_date and entry_date < phase.start_date:
            return return_Response(
                message=(
                    f"entry_date {entry_date} is before the phase budget "
                    f"start date {phase.start_date}."
                ),
                status=400, data={},
            )
        if phase.end_date and entry_date > phase.end_date:
            return return_Response(
                message=(
                    f"entry_date {entry_date} is after the phase budget "
                    f"end date {phase.end_date}."
                ),
                status=400, data={},
            )

        vals = _derive_daily_task_vals(
            phase, entry_date, done_count, total_cost, jdata.get("note"),
            no_of_trajectory=_coerce_int(jdata.get("no_of_trajectory")),
        )

        # When the popup sends per-model rows, persist them as the daily task's
        # model breakdown. Supplying `model_breakdown_ids` here suppresses the
        # model's auto-seed from the phase's model lines (see the daily-task
        # model's `create()` — it only seeds when the breakdown is empty).
        breakdown_cmds = _build_model_breakdown_cmds(jdata.get("model_rows"))
        if breakdown_cmds:
            vals["model_breakdown_ids"] = breakdown_cmds

        try:
            record = request.env[DAILY_TASK_MODEL].sudo().create(vals)
        except Exception as exc:
            _logger.exception(
                "Failed to create daily task for phase %s", phase.id,
            )
            return return_Response(
                message=f"Failed to create daily task: {exc}",
                status=400, data={},
            )
        return return_Response(
            message="Daily task record created.", status=200,
            data={"id": record.id, "data": _daily_task_detail(record)},
        )

    # -------------------------------------------------------------- list
    @http.route(
        "/api/v1/ethara_project/budget/estimation/list",
        type="http", auth="none", methods=["GET"], csrf=False, cors="*",
    )
    @validate_token
    def list_estimation(self, **params):
        limit, offset, err = _pagination(params)
        if err:
            return err
        domain = []
        phase_id = _coerce_int(params.get("batch_id"))
        if phase_id:
            domain.append(("phase_id", "=", phase_id))
        project_id = _coerce_int(params.get("project_id"))
        if project_id:
            domain.append(("phase_id.ethara_project_id", "=", project_id))
        project_budget_id = _coerce_int(params.get("project_budget_id"))
        if project_budget_id:
            domain.append(
                ("phase_id.budget_id", "=", project_budget_id),
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

    # ------------------------------------------------------------ detail
    @http.route(
        "/api/v1/ethara_project/budget/estimation/detail",
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

    # --------------------------------------------------------------- kpi
    @http.route(
        "/api/v1/ethara_project/budget/estimation/kpi",
        type="http", auth="none", methods=["GET"], csrf=False, cors="*",
    )
    @validate_token
    def kpi_estimation(self, **params):
        phase_id = _coerce_int(params.get("batch_id"))
        if not phase_id:
            return return_Response(
                message="batch_id is required.", status=400, data={},
            )
        phase = request.env[PHASE_MODEL].sudo().browse(phase_id).exists()
        if not phase:
            return return_Response(
                message=f"Phase budget {phase_id} does not exist.",
                status=404, data={},
            )

        today = fields.Date.context_today(request.env.user)
        daily_tasks = phase.daily_task_ids
        cost_lines = _phase_cost_lines(phase)

        estimated_cost = sum(dt.total_cost or 0.0 for dt in daily_tasks)
        actual_cost = sum(cost_lines.mapped("amount_source"))
        variance = estimated_cost - actual_cost

        total_tasks = phase.total_tasks or 0
        done_tasks = phase.done_tasks or 0
        remaining_tasks = phase.remaining_tasks or 0
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
        today_lines = cost_lines.filtered(
            lambda line: line.period == today,
        )
        today_actual_cost = sum(today_lines.mapped("amount_source"))
        today_variance = today_estimated_cost - today_actual_cost

        return return_Response(
            message="Estimation KPI fetched.", status=200,
            data={
                "batch_id": phase.id,
                "batch_name": phase.name or "",
                "project_id": (
                    phase.ethara_project_id.id
                    if phase.ethara_project_id else 0
                ),
                "project_name": (
                    phase.ethara_project_id.display_name
                    if phase.ethara_project_id else ""
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

    # -------------------------------------------------------- batch_view
    @http.route(
        "/api/v1/ethara_project/budget/estimation/batch_view",
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
            base_domain.append(("ethara_project_id", "=", project_id))
        project_budget_id = _coerce_int(params.get("project_budget_id"))
        if project_budget_id:
            base_domain.append(("budget_id", "=", project_budget_id))
        Model = request.env[PHASE_MODEL].sudo()
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
