import logging
from datetime import datetime, timedelta

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)

TOKEN_CATEGORIES = [
    ("glm", "GLM", "glm_input_tokens", "glm_output_tokens"),
    ("qc", "Prompt QC", "qc_input_tokens", "qc_output_tokens"),
    ("goal", "Goal Generation", "goal_input_tokens", "goal_output_tokens"),
    ("rubric", "Rubric Generation", "rubric_input_tokens", "rubric_output_tokens"),
    ("rubric_qc", "Rubric QC", "rubric_qc_input_tokens", "rubric_qc_output_tokens"),
    ("rubric_trial", "Rubric Trial", "rubric_trial_input_tokens", "rubric_trial_output_tokens"),
]


def _caller_can_see_all():
    user = request.env.user
    return (
        user.has_group("base.group_system")
        or user.has_group("atlas.group_atlas_admin")
        or user.has_group("etp_user_roles.group_quality_lead")
    )


class AtlasCostingController(http.Controller):
    @http.route("/atlas/costing/data", type="json", auth="user")
    def costing_data(self, period="week", **kw):
        today = datetime.now().date()

        if period == "day":
            start = today
        elif period == "week":
            start = today - timedelta(days=today.weekday())
        elif period == "month":
            start = today.replace(day=1)
        else:
            start = None

        task_domain = []
        if start:
            task_domain.append(
                ("create_date", ">=", start.strftime("%Y-%m-%d 00:00:00"))
            )

        if _caller_can_see_all():
            Task = request.env["atlas.atlas"].sudo()
        else:
            Task = request.env["atlas.atlas"]
            task_domain.append(
                ("employee_id.user_id", "=", request.env.user.id)
            )
        tasks = Task.search(task_domain)

        emp_map = {}
        for task in tasks:
            eid = task.employee_id.id if task.employee_id else 0
            ename = task.employee_id.name if task.employee_id else "Unassigned"
            if eid not in emp_map:
                emp_map[eid] = {"employee_id": eid, "employee_name": ename}
                for key, _label, _inf, _outf in TOKEN_CATEGORIES:
                    emp_map[eid][key + "_input"] = 0
                    emp_map[eid][key + "_output"] = 0

            for key, _label, in_field, out_field in TOKEN_CATEGORIES:
                emp_map[eid][key + "_input"] += getattr(task, in_field, 0) or 0
                emp_map[eid][key + "_output"] += getattr(task, out_field, 0) or 0

        rows = []
        totals = {"grand_total": 0}
        for key, _label, _inf, _outf in TOKEN_CATEGORIES:
            totals[key + "_input"] = 0
            totals[key + "_output"] = 0
            totals[key + "_total"] = 0

        for data in sorted(emp_map.values(), key=lambda r: r["employee_name"]):
            row = {
                "employee_id": data["employee_id"],
                "employee_name": data["employee_name"],
            }
            grand = 0
            for key, _label, _inf, _outf in TOKEN_CATEGORIES:
                inp = data[key + "_input"]
                out = data[key + "_output"]
                total = inp + out
                row[key + "_input"] = inp
                row[key + "_output"] = out
                row[key + "_total"] = total
                grand += total
                totals[key + "_input"] += inp
                totals[key + "_output"] += out
                totals[key + "_total"] += total

            row["grand_total"] = grand
            totals["grand_total"] += grand
            rows.append(row)

        categories = [
            {"key": key, "label": label}
            for key, label, _inf, _outf in TOKEN_CATEGORIES
        ]

        return {
            "period": period,
            "start_date": start.isoformat() if start else None,
            "end_date": today.isoformat(),
            "categories": categories,
            "rows": rows,
            "totals": totals,
        }
