from datetime import datetime, timedelta

from odoo import http
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    validate_token,
)

from .analytics_dashboard import _user_role_tag, _total_tokens

LIST_DEFAULT_LIMIT = 50
LIST_MAX_LIMIT = 500


def _user_scope_domain(env, tag):
    if tag in ("full", "pl", "qr"):
        return []
    if tag == "tasker":
        return [("user_id", "=", env.user.id)]
    return [("id", "=", 0)]


STAGE_SELECTION = (
    ("s1_draft", "S1 Draft"),
    ("s2_enriching", "S2 Enriching"),
    ("s2_qc", "S2 QC"),
    ("failed", "Failed"),
)

STATUS_SELECTION = (
    ("unstarted", "unstarted"),
    ("generating", "generating"),
    ("pending_review", "pending_review"),
    ("approved", "approved"),
    ("failed_qc", "failed_qc"),
)


def _coerce_int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _iso(dt):
    return dt.isoformat() if dt else ""


def _error_response(message, status):
    return return_Response(message=message, status=status, errors=[message])


def _get_category_maps(env):
    selection = list(env["talos.talos"]._fields["task_type"].selection)
    slug_to_label = dict(selection)
    label_to_slug = {v.lower(): k for k, v in slug_to_label.items()}
    return slug_to_label, label_to_slug


def _resolve_category_param(env, raw):
    slug_to_label, label_to_slug = _get_category_maps(env)
    out = []
    invalid = []
    for piece in (p.strip() for p in str(raw).split(",") if p.strip()):
        if piece in slug_to_label:
            out.append(piece)
            continue
        slug = label_to_slug.get(piece.lower())
        if slug:
            out.append(slug)
        else:
            invalid.append(piece)
    if invalid:
        return None, (
            f"Invalid category {invalid}. "
            f"Allowed slugs: {', '.join(slug_to_label)}."
        )
    return out, None


def _status_domain_for(status_slug):
    if status_slug == "unstarted":
        return [
            "&",
            ("task_status", "=", "NotSubmitted"),
            ("golden_status", "=", "idle"),
        ]
    if status_slug == "generating":
        return [("golden_status", "=", "generating")]
    if status_slug == "pending_review":
        return [
            "&",
            ("task_status", "=", "Submitted"),
            ("qc_status", "=", "pending"),
        ]
    if status_slug == "approved":
        return [("qc_status", "=", "passed")]
    if status_slug == "failed_qc":
        return [
            "|",
            ("qc_status", "=", "failed"),
            ("golden_status", "=", "error"),
        ]
    return []


def _stage_domain_for(stage_slug):
    if stage_slug == "s1_draft":
        return [
            "&",
            ("task_status", "=", "NotSubmitted"),
            ("golden_status", "in", ("idle", "generating")),
        ]
    if stage_slug == "s2_enriching":
        return [("golden_status", "=", "generating")]
    if stage_slug == "s2_qc":
        return [
            "&",
            ("task_status", "=", "Submitted"),
            ("qc_status", "=", "pending"),
        ]
    if stage_slug == "failed":
        return [
            "|",
            ("golden_status", "=", "error"),
            ("qc_status", "=", "failed"),
        ]
    return []


def _or_join(sub_domains):
    sub_domains = [d for d in sub_domains if d]
    if not sub_domains:
        return []
    if len(sub_domains) == 1:
        return sub_domains[0]
    out = ["|"] * (len(sub_domains) - 1)
    for d in sub_domains:
        out += d
    return out


def _build_task_view_domain(env, params):
    domain = []

    raw_start = (params.get("start_date") or "").strip()
    if raw_start:
        try:
            start = datetime.strptime(raw_start, "%Y-%m-%d").date()
        except ValueError:
            return None, _error_response(
                f"Invalid start_date '{raw_start}'. Expected YYYY-MM-DD.",
                status=400,
            )
        domain.append(
            ("create_date", ">=", datetime.combine(start, datetime.min.time()))
        )

    raw_end = (params.get("end_date") or "").strip()
    if raw_end:
        try:
            end = datetime.strptime(raw_end, "%Y-%m-%d").date()
        except ValueError:
            return None, _error_response(
                f"Invalid end_date '{raw_end}'. Expected YYYY-MM-DD.",
                status=400,
            )
        domain.append(
            (
                "create_date",
                "<",
                datetime.combine(end, datetime.min.time()) + timedelta(days=1),
            )
        )

    raw_cat = (params.get("category") or "").strip()
    if raw_cat:
        slugs, err = _resolve_category_param(env, raw_cat)
        if err:
            return None, _error_response(err, status=400)
        if slugs:
            domain.append(("task_type", "in", slugs))

    raw_ql = (params.get("ql") or "").strip()
    if raw_ql:
        if raw_ql.isdigit():
            domain.append(("user_id", "=", int(raw_ql)))
        else:
            domain.append(("user_id.name", "ilike", raw_ql))

    stage_raw = (params.get("stage") or "").strip()
    if stage_raw:
        allowed = dict(STAGE_SELECTION)
        requested = [s.strip() for s in stage_raw.split(",") if s.strip()]
        invalid = [s for s in requested if s not in allowed]
        if invalid:
            return None, _error_response(
                f"Invalid stage {invalid}. Allowed: {', '.join(allowed)}.",
                status=400,
            )
        sub_domains = [_stage_domain_for(s) for s in requested]
        domain += _or_join(sub_domains)

    status_raw = (params.get("status") or "").strip()
    if status_raw:
        allowed = dict(STATUS_SELECTION)
        requested = [s.strip() for s in status_raw.split(",") if s.strip()]
        invalid = [s for s in requested if s not in allowed]
        if invalid:
            return None, _error_response(
                f"Invalid status {invalid}. Allowed: {', '.join(allowed)}.",
                status=400,
            )
        sub_domains = [_status_domain_for(s) for s in requested]
        domain += _or_join(sub_domains)

    search = (params.get("search") or "").strip()
    if search:
        domain += [
            "|", "|", "|",
            ("task_id", "ilike", search),
            ("seed_prompt", "ilike", search),
            ("initial_prompt", "ilike", search),
            ("user_id.name", "ilike", search),
        ]

    return domain, None


def _derive_stage(rec):
    if rec.task_status == "NotSubmitted":
        if rec.golden_status == "generating":
            return "s2_enriching", "S2 Enriching"
        return "s1_draft", "S1 Draft"
    if rec.golden_status == "error" or rec.qc_status == "failed":
        return "failed", "Failed"
    if rec.task_status == "Submitted":
        return "s2_qc", "S2 QC"
    return rec.task_status or "", rec.task_status or ""


def _derive_status(rec):
    if rec.golden_status == "error" or rec.qc_status == "failed":
        return "failed_qc"
    if rec.qc_status == "passed":
        return "approved"
    if rec.task_status == "Submitted" and rec.qc_status == "pending":
        return "pending_review"
    if rec.golden_status == "generating":
        return "generating"
    if rec.task_status == "NotSubmitted" and rec.golden_status == "idle":
        return "unstarted"
    return rec.task_status or ""


def _spec_string(rec):
    parts = [
        rec.difficulty or "-",
        rec.task_type or "-",
        rec.trajectory_modifier or "-",
    ]
    return " · ".join(parts)


def _prompts(rec):
    raw = (rec.initial_prompt or "").strip()
    golden = (rec.seed_prompt or "").strip()
    if not raw:
        raw = golden
        golden = ""
    elif raw == golden:
        golden = ""
    return raw, golden


def _serialize_task(rec, slug_to_label):
    stage_slug, stage_label = _derive_stage(rec)
    raw_prompt, golden_prompt = _prompts(rec)
    return {
        "id": rec.id,
        "seq": rec.task_id or "",
        "spec": _spec_string(rec),
        "raw_prompt": raw_prompt,
        "golden_prompt": golden_prompt,
        "is_enriched": bool(golden_prompt),
        "assigned_ql_id": False,
        "assigned_ql_name": "",
        "category_slug": rec.task_type or "",
        "category": slug_to_label.get(rec.task_type, ""),
        "stage": stage_slug,
        "stage_label": stage_label,
        "status": _derive_status(rec),
        "state": rec.task_status or "",
        "review_state": rec.qc_status or "",
        "cost_usd": float(_total_tokens(rec)),
        "attempts_used": 0,
        "attempts_remaining": 0,
        "updated_at": _iso(rec.write_date),
        "created_at": _iso(rec.create_date),
    }


class TalosTaskViewDashboardController(http.Controller):

    @http.route(
        "/api/v1/talos_ext/task_view_dashboard",
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
        cors="*",
        save_session=False,
    )
    @validate_token
    def talos_ext_task_view_dashboard(self, **kwargs):
        env = request.env
        role_tag = _user_role_tag(env)
        if role_tag is None:
            return return_Response(
                message="You are not allowed to access Talos task view.",
                status=403,
            )
        scope_domain = _user_scope_domain(env, role_tag)
        params = request.params or {}

        domain, error = _build_task_view_domain(env, params)
        if error is not None:
            return error
        domain = scope_domain + domain

        page = max(1, _coerce_int(params.get("page"), 1))
        limit = _coerce_int(params.get("limit"), LIST_DEFAULT_LIMIT)
        limit = max(1, min(limit, LIST_MAX_LIMIT))
        offset = (page - 1) * limit

        Talos = env["talos.talos"].sudo()
        total = Talos.search_count(domain)
        records = Talos.search(
            domain,
            limit=limit,
            offset=offset,
            order="write_date desc, id desc",
        )

        slug_to_label, _ = _get_category_maps(env)
        tasks = [_serialize_task(r, slug_to_label) for r in records]
        return return_Response(
            message="OK",
            status=200,
            data={
                "role": role_tag,
                "columns": [
                    {"key": "seq", "label": "Reference", "type": "string"},
                    {"key": "raw_prompt", "label": "Raw Prompt", "type": "string"},
                    {"key": "golden_prompt", "label": "Golden Prompt", "type": "string"},
                    {"key": "assigned_ql_name", "label": "QL", "type": "string"},
                    {"key": "category", "label": "Category", "type": "string"},
                    {"key": "stage_label", "label": "Stage", "type": "string"},
                    {"key": "status", "label": "Status", "type": "string"},
                    {"key": "cost_usd", "label": "Cost", "type": "currency"},
                    {"key": "updated_at", "label": "Updated", "type": "datetime"},
                ],
                "rows": tasks,
                "total_records": total,
                "page": page,
                "limit": limit,
            },
        )
