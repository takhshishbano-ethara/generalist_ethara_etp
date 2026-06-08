"""Paginated, filterable list of Aurora benchmark instances.

Response shape matches talos_extension's task_view_dashboard: a dynamic table
contract with `role`, `columns` ([{key, label, type}]), `rows`, and
`pagination` ({total_records, page, limit, total_pages}). Selection fields are
emitted as raw value + `_label` pairs.
"""

from odoo import http
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    validate_token,
)

from .common import coerce_int, user_role_tag

DEFAULT_LIMIT = 20
MAX_LIMIT = 200
SORT_FIELDS = {
    "created_date": "create_date",
    "updated_date": "write_date",
    "instance_id": "instance_id",
    "status": "status",
}

# Dynamic table columns the frontend renders from the response.
COLUMNS = [
    {"key": "instance_id", "label": "Instance", "type": "string"},
    {"key": "repo", "label": "Repository", "type": "string"},
    {"key": "pr_range", "label": "PR Range", "type": "string"},
    {"key": "status_label", "label": "Status", "type": "string"},
    {"key": "resolved", "label": "Resolved", "type": "boolean"},
    {"key": "f2p_count", "label": "F2P", "type": "integer"},
    {"key": "p2p_count", "label": "P2P", "type": "integer"},
    {"key": "s2p_count", "label": "S2P", "type": "integer"},
    {"key": "n2p_count", "label": "N2P", "type": "integer"},
    {"key": "updated_at", "label": "Updated", "type": "datetime"},
]


def _split_pr_numbers(raw):
    if not raw:
        return []
    parts = []
    for chunk in str(raw).replace(";", ",").split(","):
        chunk = chunk.strip().lstrip("#")
        if chunk:
            parts.append(chunk)
    return parts


def _serialize(rec, status_labels):
    org = rec.org or ""
    repo = rec.repo or ""
    repo_url = f"https://github.com/{org}/{repo}" if org and repo else ""
    pr_numbers = _split_pr_numbers(rec.pr_numbers)
    pr_urls = [f"{repo_url}/pull/{n}" for n in pr_numbers] if repo_url else []

    tag_start = rec.tag_start or ""
    tag_end = rec.tag_end or ""
    if tag_start and tag_end:
        pr_range = f"{tag_start}..{tag_end}"
    else:
        pr_range = tag_start or tag_end or ""

    return {
        "id": rec.id,
        "instance_id": rec.instance_id or rec.display_name or str(rec.id),
        "org": org,
        "repo": repo,
        "repo_url": repo_url,
        "pr_numbers": pr_numbers,
        "pr_urls": pr_urls,
        "tag_start": tag_start,
        "tag_end": tag_end,
        "pr_range": pr_range,
        # Not tracked per-instance in the backend (see README); empty until a
        # source is wired. Frontend should treat these as optional.
        "language": "",
        "category": "",
        "status": rec.status or "",
        "status_label": status_labels.get(rec.status, ""),
        "resolved": bool(rec.resolved),
        "f2p_count": rec.f2p_count or 0,
        "p2p_count": rec.p2p_count or 0,
        "s2p_count": rec.s2p_count or 0,
        "n2p_count": rec.n2p_count or 0,
        "error_message": rec.error_message or "",
        "created_at": rec.create_date.isoformat() if rec.create_date else None,
        "updated_at": rec.write_date.isoformat() if rec.write_date else None,
    }


def _resolve_order(params):
    raw_sort = (params.get("sort_by") or "updated_date").strip()
    if raw_sort not in SORT_FIELDS:
        return None, return_Response(
            message=(
                f"Invalid sort_by '{raw_sort}'. "
                f"Allowed: {', '.join(sorted(SORT_FIELDS))}."
            ),
            status=400,
        )
    direction = (
        "asc"
        if (params.get("sort_order") or "").strip().lower() == "asc"
        else "desc"
    )
    return f"{SORT_FIELDS[raw_sort]} {direction}, id desc", None


def _build_domain(params):
    domain = []

    search = (params.get("search") or "").strip()
    if search:
        domain += [
            "|",
            "|",
            ("instance_id", "ilike", search),
            ("org", "ilike", search),
            ("repo", "ilike", search),
        ]

    status = (params.get("status") or "").strip()
    if status:
        requested = [s.strip() for s in status.split(",") if s.strip()]
        if requested:
            domain.append(("status", "in", requested))

    resolved = (params.get("resolved") or "").strip().lower()
    if resolved in ("true", "1", "yes"):
        domain.append(("resolved", "=", True))
    elif resolved in ("false", "0", "no"):
        domain.append(("resolved", "=", False))

    evaluation_id = coerce_int(params.get("evaluation_id"), 0)
    if evaluation_id:
        domain.append(("evaluation_id", "=", evaluation_id))

    return domain


class AuroraInstancesController(http.Controller):

    @http.route(
        "/api/v1/aurora_ext/instances",
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
        cors="*",
        save_session=False,
    )
    @validate_token
    def aurora_ext_instances(self, **kwargs):
        """Paginated, filterable, sortable benchmark instance listing."""
        env = request.env
        role_tag = user_role_tag(env)
        if role_tag is None:
            return return_Response(
                message="You are not allowed to access Aurora data.",
                status=403,
            )

        params = request.params or {}
        domain = _build_domain(params)
        order, error = _resolve_order(params)
        if error is not None:
            return error

        page = max(1, coerce_int(params.get("page"), 1))
        limit = min(max(1, coerce_int(params.get("limit"), DEFAULT_LIMIT)), MAX_LIMIT)
        offset = (page - 1) * limit

        Instance = env["aurora.evaluation.instance"].sudo()
        total = Instance.search_count(domain)
        records = Instance.search(domain, limit=limit, offset=offset, order=order)
        status_labels = dict(Instance._fields["status"].selection)
        rows = [_serialize(r, status_labels) for r in records]
        total_pages = (total + limit - 1) // limit if total else 0

        return return_Response(
            message="OK",
            status=200,
            data={
                "role": role_tag,
                "blocks": [
                    {
                        "type": "table",
                        "title": "Benchmark instances",
                        "columns": COLUMNS,
                        "rows": rows,
                        "pagination": {
                            "total_records": total,
                            "page": page,
                            "limit": limit,
                            "total_pages": total_pages,
                        },
                    },
                ],
            },
        )
