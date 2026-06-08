"""Aurora logs tab: per-instance evaluation logs (build / run / test / fix),
returned as a block-schema table rendered generically by the frontend.
"""

from odoo import http
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    validate_token,
)

from .common import user_role_tag

# (field on aurora.evaluation.instance, display label)
LOG_KINDS = [
    ("build_log_tail", "Build"),
    ("run_log_tail", "Run"),
    ("test_patch_log_tail", "Test Patch"),
    ("fix_patch_log_tail", "Fix Patch"),
]

LOG_COLUMNS = [
    {"key": "instance", "label": "Instance", "type": "string"},
    {"key": "log_type", "label": "Log", "type": "string"},
    {"key": "status", "label": "Status", "type": "string"},
    {"key": "preview", "label": "Preview", "type": "string"},
]


class AuroraLogsController(http.Controller):

    @http.route(
        "/api/v1/aurora_ext/logs",
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
        cors="*",
        save_session=False,
    )
    @validate_token
    def aurora_ext_logs(self, **kwargs):
        env = request.env
        role_tag = user_role_tag(env)
        if role_tag is None:
            return return_Response(
                message="You are not allowed to access Aurora data.",
                status=403,
            )

        Inst = env["aurora.evaluation.instance"].sudo()

        rows = []
        for inst in Inst.search([], order="write_date desc"):
            name = inst.instance_id or inst.display_name or str(inst.id)
            for field, label in LOG_KINDS:
                content = (getattr(inst, field, "") or "").strip()
                if not content:
                    continue
                last_line = content.splitlines()[-1][:140]
                rows.append({
                    "instance": name,
                    "log_type": label,
                    "status": inst.status or "",
                    "preview": last_line,
                })

        return return_Response(
            message="OK",
            status=200,
            data={
                "role": role_tag,
                "blocks": [
                    {
                        "type": "table",
                        "title": "Instance logs",
                        "columns": LOG_COLUMNS,
                        "rows": rows,
                    },
                ],
            },
        )
