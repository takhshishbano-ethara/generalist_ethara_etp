from werkzeug.exceptions import Forbidden

from odoo import http
from odoo.http import request


class NonStemDashboardController(http.Controller):

    @http.route([
        "/non_stem_dashboard/tasker/<int:run_id>",
        "/non_stem_dashboard/tasker/<int:run_id>/<string:window>",
    ], type="http", auth="user", website=False)
    def tasker_dashboard(self, run_id, window=None, **kw):
        run = request.env["non.stem.run"].sudo().browse(run_id)
        if not run.exists():
            return request.not_found()
        if window and window != "all":
            html = run.get_window_html("tasker", window)
        else:
            html = run.tasker_dashboard_html
        if not html:
            return request.not_found()
        return http.Response(html, content_type="text/html", status=200)

    @http.route([
        "/non_stem_dashboard/management/<int:run_id>",
        "/non_stem_dashboard/management/<int:run_id>/<string:window>",
    ], type="http", auth="user", website=False)
    def management_dashboard(self, run_id, window=None, **kw):
        user = request.env.user
        has_access = user._is_admin() or user.has_group("non_stem_dashboard.group_viewer")
        if not has_access:
            raise Forbidden("You do not have access to the management dashboard.")
        run = request.env["non.stem.run"].sudo().browse(run_id)
        if not run.exists():
            return request.not_found()
        if window and window != "all":
            html = run.get_window_html("management", window)
        else:
            html = run.management_dashboard_html
        if not html:
            return request.not_found()
        return http.Response(html, content_type="text/html", status=200)

    @http.route(
        "/non_stem_dashboard/public/<string:token>",
        type="http", auth="public", website=False, csrf=False,
    )
    def public_tasker_dashboard(self, token, **kw):
        run = request.env["non.stem.run"].sudo().search(
            [("public_token", "=", token), ("state", "=", "done")], limit=1,
        )
        if not run or not run.tasker_dashboard_html:
            return request.not_found()
        return http.Response(
            run.tasker_dashboard_html,
            content_type="text/html",
            status=200,
        )

    @http.route([
        "/non_stem_dashboard/latest/tasker",
        "/non_stem_dashboard/latest/tasker/<string:window>",
    ], type="http", auth="user", website=False)
    def latest_tasker_dashboard(self, window=None, **kw):
        run = request.env["non.stem.run"].sudo().search(
            [("state", "=", "done"), ("tasker_dashboard_html", "!=", False)],
            order="create_date desc", limit=1,
        )
        if not run:
            return request.not_found()
        if window and window != "all":
            html = run.get_window_html("tasker", window)
        else:
            html = run.tasker_dashboard_html
        if not html:
            return request.not_found()
        return http.Response(html, content_type="text/html", status=200)

    @http.route([
        "/non_stem_dashboard/latest/management",
        "/non_stem_dashboard/latest/management/<string:window>",
    ], type="http", auth="user", website=False)
    def latest_management_dashboard(self, window=None, **kw):
        user = request.env.user
        has_access = user._is_admin() or user.has_group("non_stem_dashboard.group_viewer")
        if not has_access:
            raise Forbidden("You do not have access to the management dashboard.")
        run = request.env["non.stem.run"].sudo().search(
            [("state", "=", "done"), ("management_dashboard_html", "!=", False)],
            order="create_date desc", limit=1,
        )
        if not run:
            return request.not_found()
        if window and window != "all":
            html = run.get_window_html("management", window)
        else:
            html = run.management_dashboard_html
        if not html:
            return request.not_found()
        return http.Response(html, content_type="text/html", status=200)

    @http.route([
        "/non_stem_dashboard/public/tasker",
        "/non_stem_dashboard/public/tasker/<string:window>",
    ], type="http", auth="public", website=False, csrf=False)
    def public_latest_tasker(self, window=None, **kw):
        run = request.env["non.stem.run"].sudo().search(
            [("state", "=", "done"), ("tasker_dashboard_html", "!=", False)],
            order="create_date desc", limit=1,
        )
        if not run:
            return request.not_found()
        if window and window != "all":
            html = run.get_window_html("tasker", window)
        else:
            html = run.tasker_dashboard_html
        if not html:
            return request.not_found()
        return http.Response(html, content_type="text/html", status=200)

    @http.route([
        "/non_stem_dashboard/public/management",
        "/non_stem_dashboard/public/management/<string:window>",
    ], type="http", auth="public", website=False, csrf=False)
    def public_latest_management(self, window=None, **kw):
        run = request.env["non.stem.run"].sudo().search(
            [("state", "=", "done"), ("management_dashboard_html", "!=", False)],
            order="create_date desc", limit=1,
        )
        if not run:
            return request.not_found()
        if window and window != "all":
            html = run.get_window_html("management", window)
        else:
            html = run.management_dashboard_html
        if not html:
            return request.not_found()
        return http.Response(html, content_type="text/html", status=200)

    @http.route(
        "/non_stem_dashboard/api/runs",
        type="json", auth="user",
    )
    def get_runs(self, **kw):
        runs = request.env["non.stem.run"].search(
            [("state", "=", "done")], order="create_date desc", limit=20,
        )
        return [
            {
                "id": r.id,
                "name": r.name,
                "create_date": str(r.create_date),
                "state": r.state,
            }
            for r in runs
        ]
