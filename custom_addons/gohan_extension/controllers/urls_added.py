"""URLs Added tab endpoint for the Gohan internal project.

The project-detail "URLs Added" tab is a URL-centric view of the same
``gohan.job`` rows that power the score-centric "Tasks" tab
(``task_view_dashboard``). Where Tasks shows Score / Grade / QC Verdict, this
shows the columns the pen specifies: Website URL, Category, Added by, Assigned
Tasker, Source, Date added — i.e. the URLs that were added to the project and
by whom.

Field mapping (gohan.job -> column):
  url            -> Website URL
  category_id    -> Category        (display name)
  create_uid     -> Added by        (who created the job/url)
  user_id        -> Assigned Tasker
  via_batch      -> Source          (True => "Bulk CSV", False => "Single")
  create_date    -> Date added

The Flutter UrlsAddedTab parses {columns, rows, pagination} and renders each
row's cell by column.key, so empty values simply render as a dash — honouring
the "no data => leave empty, never fabricate" convention.
"""

from odoo import http
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    validate_token,
)

from .analytics_dashboard import (
    _category_badge,
    _create_date_domain,
    _domain_from_url,
    _format_long_date,
    _parse_date,
    _scope,
    _source_badge,
    _user_role_tag,
)
from .task_view_dashboard import DEFAULT_LIMIT, MAX_LIMIT, _coerce_int

# sort_by -> ORM field. Mirrors task_view_dashboard's allow-list approach.
URL_SORT_FIELDS = {
    "created_date": "create_date",
    "url": "url",
    "seq": "name",
}

# Self-describing columns for the pen "URLs Added" table (6 columns).
URL_COLUMNS = [
    {"key": "website_url", "label": "Website URL", "type": "url", "width": "fill"},
    {"key": "category", "label": "Category", "type": "badge", "width": 200},
    {"key": "added_by", "label": "Added by", "type": "string", "width": 150},
    {"key": "assigned_tasker", "label": "Assigned Tasker", "type": "string", "width": 150},
    {"key": "source", "label": "Source", "type": "badge", "width": 110},
    {"key": "date_added", "label": "Date added", "type": "date", "width": 110},
]


def _build_urls_domain(env, params):
    """Domain for the URLs Added view: jobs that have a URL, narrowed by the
    optional search / category / added_by / source / tasker / date filters the
    tab can send. Returns (domain, error_response)."""
    domain = [("url", "!=", False)]

    raw_start = (params.get("start_date") or "").strip()
    raw_end = (params.get("end_date") or "").strip()
    start = end = None
    if raw_start:
        start, error = _parse_date(raw_start, "start_date")
        if error is not None:
            return None, error
    if raw_end:
        end, error = _parse_date(raw_end, "end_date")
        if error is not None:
            return None, error
    if start and end and start > end:
        return None, return_Response(
            message="Invalid date range: start_date must be on or before end_date.",
            status=400,
        )
    domain += _create_date_domain(start, end)

    raw_category = (params.get("category") or "").strip()
    if raw_category:
        if raw_category.isdigit():
            domain.append(("category_id", "=", int(raw_category)))
        else:
            domain.append(("category_id.name", "ilike", raw_category))

    raw_added_by = (params.get("added_by") or "").strip()
    if raw_added_by:
        if raw_added_by.isdigit():
            domain.append(("create_uid", "=", int(raw_added_by)))
        else:
            domain.append(("create_uid.name", "ilike", raw_added_by))

    raw_tasker = (params.get("tasker") or "").strip()
    if raw_tasker:
        if raw_tasker.isdigit():
            domain.append(("user_id", "=", int(raw_tasker)))
        else:
            domain.append(("user_id.name", "ilike", raw_tasker))

    raw_source = (params.get("source") or "").strip().lower()
    if raw_source in ("bulk", "bulk_csv", "bulk csv", "csv"):
        domain.append(("via_batch", "=", True))
    elif raw_source in ("single", "manual"):
        domain.append(("via_batch", "=", False))

    search = (params.get("search") or "").strip()
    if search:
        domain += ["|", ("url", "ilike", search), ("site_name", "ilike", search)]

    return domain, None


def _serialize_url(job):
    """One "URLs Added" row in the pen shape: a Website URL link object, badge
    objects for Category and Source, plus the raw fields kept for filter/sort."""
    url = job.url or ""
    return {
        "id": job.id,
        "website_url": {
            "label": job.site_name or _domain_from_url(url) or url,
            "href": url,
        },
        "category": _category_badge(job.category_id),
        "added_by": job.create_uid.name or "",
        "assigned_tasker": job.user_id.name or "—",
        "source": _source_badge(bool(job.via_batch)),
        "date_added": _format_long_date(job.create_date),
        # Raw fields retained for client-side filtering / sorting.
        "seq": job.name or "",
        "url": url,
        "category_id": job.category_id.id or False,
        "via_batch": bool(job.via_batch),
        "created_at": job.create_date.isoformat() if job.create_date else None,
    }


class GohanUrlsAddedController(http.Controller):

    @http.route(
        "/api/v1/gohan_ext/urls_added",
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
        cors="*",
    )
    @validate_token
    def gohan_ext_urls_added(self, **kwargs):
        """Paginated, filterable URL listing for the "URLs Added" tab."""
        env = request.env
        if _user_role_tag(env) is None:
            return return_Response(
                message="You are not allowed to access Gohan URLs.",
                status=403,
            )

        params = request.params or {}
        domain, error = _build_urls_domain(env, params)
        if error is not None:
            return error

        raw_sort = (params.get("sort_by") or "created_date").strip()
        sort_col = URL_SORT_FIELDS.get(raw_sort, "create_date")
        direction = "asc" if (params.get("sort_order") or "").strip().lower() == "asc" else "desc"
        order = f"{sort_col} {direction}, id desc"

        tag, scope, projects = _scope(env)
        domain = scope + domain

        page = max(1, _coerce_int(params.get("page"), 1))
        limit = min(max(1, _coerce_int(params.get("limit"), DEFAULT_LIMIT)), MAX_LIMIT)
        offset = (page - 1) * limit

        Job = env["gohan.job"].sudo()
        total = Job.search_count(domain)
        records = Job.search(domain, limit=limit, offset=offset, order=order)
        rows = [_serialize_url(job) for job in records]
        total_pages = (total + limit - 1) // limit if total else 0
        data = {
            "role": _user_role_tag(env) or "tasker",
            "columns": URL_COLUMNS,
            "rows": rows,
            "pagination": {
                "total_records": total,
                "page": page,
                "limit": limit,
                "total_pages": total_pages,
            },
        }
        return return_Response(message="OK", status=200, data=data)
