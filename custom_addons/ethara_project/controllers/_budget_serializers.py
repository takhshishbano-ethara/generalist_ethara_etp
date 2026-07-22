# -*- coding: utf-8 -*-
"""Shared JSON serializers for the ethara_project budget lifecycle HTTP API.

Ported from the etp_projects budget controllers, adapted to the ethara data
models. Field-name deltas applied throughout:

    etp.project.aws.budget  -> ethara.project.budget
    budget.project_id       -> budget.ethara_project_id   (FK to ethara.project)
    etp.batch.budget        -> ethara.project.phase        (searched via budget_id)

`ethara.project.budget` has no `budget_sub_type` and its budget/model lines have
no `usage_tag`; both are emitted as safe defaults so the existing Flutter
response models keep parsing.
"""

import json


def _sel_label(record, field, value):
    """Human label for a Selection field value ('' when unknown)."""
    if not value:
        return ""
    try:
        return dict(record._fields[field].selection).get(value, value) or ""
    except Exception:
        return value or ""


def _iso(value):
    return value.isoformat() if value else None


def _parse_attachment_urls(raw):
    """`attachment_urls` is stored as Text (JSON list or newline/comma list)."""
    if not raw:
        return []
    if isinstance(raw, (list, tuple)):
        return list(raw)
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return parsed
    except Exception:
        pass
    return [p.strip() for p in str(raw).replace("\n", ",").split(",") if p.strip()]


def _phase_ids_for_budget(budget):
    """ethara.project.budget has no phase One2many — resolve phases by FK."""
    return budget.env["ethara.project.phase"].sudo().search(
        [("budget_id", "=", budget.id)], order="start_date, id",
    )


def _model_line_dict(ln):
    model = ln.ai_model_id
    return {
        "id": ln.id,
        "ai_model_id": model.id,
        "ai_model_name": ln.ai_model_name or "",
        "model_name": ln.ai_model_name or model.display_name or model.name,
        "provider": getattr(model, "provider", "") or "",
        "usage_tag": (
            ln.usage_tag if "usage_tag" in ln._fields else "trajectory_building"
        ),
        "cost_type": ln.cost_type,
        "per_task_cost": ln.per_task_cost,
        "per_trajectory_cost": ln.per_trajectory_cost,
        "iterations": ln.iterations,
    }


def _infra_line_dict(ln):
    infra = ln.infra_type_id
    return {
        "id": ln.id,
        "infra_id": infra.id,
        "infra_name": infra.display_name or infra.name,
        "description": ln.description or "",
        "cost": ln.budget_amount,
        "per_day_cost": ln.per_day_cost,
        "instance_type": ln.instance_type or "",
        "unit_price_usd": ln.unit_price_usd or 0.0,
        "price_unit": ln.price_unit or "",
        "quantity": ln.quantity or 0.0,
        "duration_hours": ln.duration_hours or 0.0,
        "ebs_storage_gb": ln.ebs_storage_gb or 0.0,
        "volume_type": ln.volume_type or "",
        "volume_rate_usd_per_gb_mo": ln.volume_rate_usd_per_gb_mo or 0.0,
        "compute_amount": (
            ln.compute_amount if "compute_amount" in ln._fields else 0.0
        ),
        "storage_amount": (
            ln.storage_amount if "storage_amount" in ln._fields else 0.0
        ),
        "computed_amount": (
            ln.computed_amount if "computed_amount" in ln._fields else 0.0
        ),
    }


def _subscription_line_dict(ln):
    sub = ln.subscription_id
    return {
        "id": ln.id,
        "subscription_id": sub.id,
        "subscription_name": ln.name or sub.display_name or sub.name,
        "cost_per_seat": ln.cost_per_subscription,
        "assigned_to": ln.assigned_user_ids.ids,
        "seat_count": ln.subscription_count,
        "monthly_total": ln.final_amount,
        "per_day_cost": ln.per_day_cost,
    }


def budget_to_summary(budget):
    """Lightweight row for `budget/list`."""
    project = budget.ethara_project_id
    return {
        "id": budget.id,
        "name": budget.name,
        "project_id": project.id,
        "project_name": project.display_name or project.name or "",
        "budget_type": budget.project_type,
        "budget_type_label": _sel_label(budget, "project_type", budget.project_type),
        "team_type": budget.team_type or "",
        "team_type_label": _sel_label(budget, "team_type", budget.team_type),
        "budget_sub_type": budget.budget_sub_type or "",
        "budget_sub_type_label": _sel_label(budget, "budget_sub_type", budget.budget_sub_type),
        "state": budget.state,
        "state_label": _sel_label(budget, "state", budget.state),
        "total_tasks": budget.total_tasks,
        "est_trajectories_per_task": budget.est_trajectories_per_task or 0,
        "total_trajectories": budget.total_trajectories or 0,
        "budget_amount": budget.budget_amount,
        "batch_count": len(_phase_ids_for_budget(budget)),
        "approver_count": len(budget.approver_user_ids),
        "create_date": _iso(budget.create_date),
    }


def budget_to_dict(budget):
    """Full detail for `budget/detail` and the update response."""
    budget.ensure_one()
    project = budget.ethara_project_id
    phases = _phase_ids_for_budget(budget)
    return {
        "id": budget.id,
        "name": budget.name,
        "description": budget.description or "",
        "project_id": project.id,
        "project_name": project.display_name or project.name or "",
        "budget_type": budget.project_type,
        "budget_type_label": _sel_label(budget, "project_type", budget.project_type),
        "team_type": budget.team_type or "",
        "team_type_label": _sel_label(budget, "team_type", budget.team_type),
        "budget_sub_type": budget.budget_sub_type or "",
        "budget_sub_type_label": _sel_label(budget, "budget_sub_type", budget.budget_sub_type),
        "state": budget.state,
        "state_label": _sel_label(budget, "state", budget.state),
        "priority": budget.priority or "",
        "priority_label": _sel_label(budget, "priority", budget.priority),
        "total_tasks": budget.total_tasks,
        "est_trajectories_per_task": budget.est_trajectories_per_task or 0,
        "total_trajectories": budget.total_trajectories or 0,
        "buffer_pct": budget.buffer_pct,
        "budget_amount": budget.budget_amount,
        "approver_ids": budget.approver_user_ids.ids,
        "approvers": [
            {"id": u.id, "name": u.name or "", "email": u.email or (
                u.partner_id.email if u.partner_id else "") or ""}
            for u in budget.approver_user_ids
        ],
        "model_lines": [_model_line_dict(ln) for ln in budget.model_line_ids],
        "infra_lines": [_infra_line_dict(ln) for ln in budget.infra_line_ids],
        "subscription_lines": [
            _subscription_line_dict(ln) for ln in budget.subscription_line_ids
        ],
        "batches": [
            {
                "id": p.id,
                "name": p.name,
                "total_tasks": p.total_tasks,
                "start_date": _iso(p.start_date),
                "end_date": _iso(p.end_date),
                "buffer_pct": p.buffer_pct,
                "state": p.state,
            }
            for p in phases
        ],
        "attachment_ids": _parse_attachment_urls(budget.attachment_urls),
        "create_date": _iso(budget.create_date),
        "write_date": _iso(budget.write_date),
    }
