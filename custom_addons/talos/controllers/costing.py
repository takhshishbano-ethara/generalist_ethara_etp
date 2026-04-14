# -*- coding: utf-8 -*-
import logging
from datetime import datetime, timedelta

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)


class TalosCostingController(http.Controller):

    @http.route("/talos/costing/data", type="json", auth="user")
    def costing_data(self, period="week", **kw):
        today = datetime.now().date()

        if period == "week":
            start = today - timedelta(days=today.weekday())
        elif period == "month":
            start = today.replace(day=1)
        else:
            start = None

        domain = []
        if start:
            domain.append(("create_date", ">=", start.strftime("%Y-%m-%d 00:00:00")))

        Turn = request.env["talos.turn"].sudo()
        turns = Turn.search(domain)

        emp_map = {}
        # Claude/GLM tokens are cumulative per-session stored on each turn.
        # Use only the latest turn per sandbox to avoid double-counting.
        sandbox_latest = {}

        for t in turns:
            eid = t.employee_id.id if t.employee_id else 0
            ename = t.employee_id.name if t.employee_id else "Unassigned"
            if eid not in emp_map:
                emp_map[eid] = {
                    "employee_id": eid,
                    "employee_name": ename,
                    "bedrock_input": 0,
                    "bedrock_output": 0,
                    "trajectory_input": 0,
                    "trajectory_output": 0,
                    "claude_input": 0,
                    "claude_output": 0,
                    "glm_input": 0,
                    "glm_output": 0,
                }
            emp_map[eid]["bedrock_input"] += t.bedrock_input_tokens or 0
            emp_map[eid]["bedrock_output"] += t.bedrock_output_tokens or 0
            emp_map[eid]["trajectory_input"] += t.trajectory_input_tokens or 0
            emp_map[eid]["trajectory_output"] += t.trajectory_output_tokens or 0

            sid = t.sandbox_id.id if t.sandbox_id else 0
            if sid:
                key = (eid, sid)
                prev = sandbox_latest.get(key)
                if not prev or t.turn_number > prev.turn_number:
                    sandbox_latest[key] = t

        for (eid, _sid), t in sandbox_latest.items():
            if eid not in emp_map:
                continue
            emp_map[eid]["claude_input"] += t.claude_input_tokens or 0
            emp_map[eid]["claude_output"] += t.claude_output_tokens or 0
            emp_map[eid]["glm_input"] += t.glm_input_tokens or 0
            emp_map[eid]["glm_output"] += t.glm_output_tokens or 0

        rows = []
        totals = {
            "bedrock_input": 0,
            "bedrock_output": 0,
            "bedrock_total": 0,
            "claude_input": 0,
            "claude_output": 0,
            "claude_total": 0,
            "glm_input": 0,
            "glm_output": 0,
            "glm_total": 0,
            "trajectory_input": 0,
            "trajectory_output": 0,
            "trajectory_total": 0,
            "grand_total": 0,
        }

        for data in sorted(emp_map.values(), key=lambda r: r["employee_name"]):
            bt = data["bedrock_input"] + data["bedrock_output"]
            ct = data["claude_input"] + data["claude_output"]
            gt = data["glm_input"] + data["glm_output"]
            tt = data["trajectory_input"] + data["trajectory_output"]
            row = {
                "employee_id": data["employee_id"],
                "employee_name": data["employee_name"],
                "bedrock_input": data["bedrock_input"],
                "bedrock_output": data["bedrock_output"],
                "bedrock_total": bt,
                "claude_input": data["claude_input"],
                "claude_output": data["claude_output"],
                "claude_total": ct,
                "glm_input": data["glm_input"],
                "glm_output": data["glm_output"],
                "glm_total": gt,
                "trajectory_input": data["trajectory_input"],
                "trajectory_output": data["trajectory_output"],
                "trajectory_total": tt,
                "grand_total": bt + ct + gt + tt,
            }
            rows.append(row)
            for k in (
                "bedrock_input", "bedrock_output", "bedrock_total",
                "claude_input", "claude_output", "claude_total",
                "glm_input", "glm_output", "glm_total",
                "trajectory_input", "trajectory_output", "trajectory_total",
            ):
                totals[k] += row[k]
            totals["grand_total"] += row["grand_total"]

        return {
            "period": period,
            "start_date": start.isoformat() if start else None,
            "end_date": today.isoformat(),
            "rows": rows,
            "totals": totals,
        }
