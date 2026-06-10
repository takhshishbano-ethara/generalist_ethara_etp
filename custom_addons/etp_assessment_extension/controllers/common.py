"""Shared helpers for the ETP Assessment Extension controllers."""

from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import return_Response

DEFAULT_LIMIT = 20
MAX_LIMIT = 200

QUESTION_TYPES = ("image_comparison", "text", "coding", "image_text", "video")
ASSESSMENT_STATES = ("draft", "in_progress", "done", "cancelled")
EVALUATOR_STATES = ("pending", "in_progress", "submitted")
RESPONSE_STATES = ("draft", "submitted")


def coerce_int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def coerce_bool(value, default=None):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("true", "1", "yes", "y", "on"):
        return True
    if text in ("false", "0", "no", "n", "off"):
        return False
    return default


def pct(part, whole):
    if not whole:
        return 0.0
    return round((part / whole) * 100.0, 2)


def user_role_tag(env):
    """Coarse role tag for the caller, mirroring aurora_extension's `role`.

    Returns "manager" for assessment managers, "evaluator" for regular
    candidates with read access, or None if the caller has neither group.
    """
    user = env.user
    if user.has_group("etp_assessment.group_assessment_manager"):
        return "manager"
    if user.has_group("etp_assessment.group_assessment_evaluator"):
        return "evaluator"
    return None


def require_assessment_user():
    """Return a 403 Response if the caller has no assessment access, else None."""
    if user_role_tag(request.env) is None:
        return return_Response(
            message="You are not allowed to access ETP Assessment data.",
            status=403,
        )
    return None


def require_assessment_manager():
    """Return a 403 Response if the caller is not an assessment manager."""
    if user_role_tag(request.env) != "manager":
        return return_Response(
            message="Only assessment managers can perform this action.",
            status=403,
        )
    return None


def paginate(params):
    """Read `page` / `limit` from params and clamp to safe bounds."""
    page = max(1, coerce_int(params.get("page"), 1))
    limit = min(
        max(1, coerce_int(params.get("limit"), DEFAULT_LIMIT)),
        MAX_LIMIT,
    )
    offset = (page - 1) * limit
    return page, limit, offset


def pagination_block(total, page, limit):
    total_pages = (total + limit - 1) // limit if total else 0
    return {
        "total_records": total,
        "page": page,
        "limit": limit,
        "total_pages": total_pages,
    }


def resolve_order(params, allowed, default_field, default_dir="desc"):
    """Map sort_by/sort_order params to a safe `field direction, id desc` order.

    `allowed` is a dict mapping the public sort key to the actual Odoo field.
    Returns `(order_string, error_response_or_None)`.
    """
    raw_sort = (params.get("sort_by") or default_field).strip()
    if raw_sort not in allowed:
        return None, return_Response(
            message=(
                f"Invalid sort_by '{raw_sort}'. "
                f"Allowed: {', '.join(sorted(allowed))}."
            ),
            status=400,
        )
    raw_dir = (params.get("sort_order") or default_dir).strip().lower()
    direction = "asc" if raw_dir == "asc" else "desc"
    return f"{allowed[raw_sort]} {direction}, id desc", None


def iso_or_none(value):
    return value.isoformat() if value else None


def parse_json_body():
    """Return the merged JSON body + query params, mirroring validate_request."""
    import json

    data = {}
    if request.params:
        data.update(request.params)
    try:
        jdata = json.loads(request.httprequest.stream.read())
    except Exception:
        try:
            jdata = json.loads(request.httprequest.data)
        except Exception:
            jdata = {}
    if jdata:
        data.update(jdata)
    return data


def m2o_link(rec):
    """Many2one → `{"id": N, "display_name": "..."}` or `False`.

    Mirrors Odoo's `web_read` shape so the frontend can render a chip /
    label for the relation without making a second call.
    """
    if not rec:
        return False
    return {
        "id": rec.id,
        "display_name": (
            rec.display_name
            or (rec.name if hasattr(rec, "name") and rec.name else "")
            or ""
        ),
    }


def x2many_links(records):
    """Many2many / One2many → `[{"id": N, "display_name": "..."}, ...]`.

    Empty recordsets serialize to `[]` (matches Odoo `web_read`).
    """
    return [
        {
            "id": r.id,
            "display_name": (
                r.display_name
                or (r.name if hasattr(r, "name") and r.name else "")
                or ""
            ),
        }
        for r in records
    ]


def jsonrpc_response(result, request_id=None):
    """Render a JSON-RPC 2.0 success envelope as an HTTP response.

    Usage:
        return jsonrpc_response([record_dict])

    Returns a Werkzeug Response with `Content-Type: application/json`.
    The `request_id` is echoed back; defaults to 1 if not supplied.
    """
    import json

    if request_id is None:
        request_id = coerce_int((request.params or {}).get("id"), 1)
    payload = {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": result,
    }
    return request.make_response(
        json.dumps(payload, default=str),
        headers=[
            ("Content-Type", "application/json; charset=utf-8"),
            ("Access-Control-Allow-Origin", "*"),
            ("Cache-Control", "no-store"),
        ],
    )


def jsonrpc_error(code, message, request_id=None, http_status=400):
    """Render a JSON-RPC 2.0 error envelope.

    Body shape:
        {"jsonrpc": "2.0", "id": <id>, "error": {"code": N, "message": "..."}}
    """
    import json

    if request_id is None:
        request_id = coerce_int((request.params or {}).get("id"), 1)
    payload = {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }
    return request.make_response(
        json.dumps(payload, default=str),
        status=http_status,
        headers=[
            ("Content-Type", "application/json; charset=utf-8"),
            ("Access-Control-Allow-Origin", "*"),
            ("Cache-Control", "no-store"),
        ],
    )
