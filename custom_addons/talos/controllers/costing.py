# -*- coding: utf-8 -*-
import logging
from datetime import datetime, timedelta

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)


class TalosCostingController(http.Controller):
    """Aggregates token usage from talos.turn for the costing dashboard."""

    @http.route("/talos/costing/data", type="json", auth="user")
    def costing_data(self, period="week", **kw):
        """
        Return token-usage rows grouped by employee.

        :param period: 'week' | 'month' | 'all'
        :returns: {
            period, start_date, end_date,
            rows: [{
                employee_id, employee_name,
                bedrock_input, bedrock_output, bedrock_total,
                trajectory_input, trajectory_output, trajectory_total,
                grand_total
            }],
            totals: { bedrock_input, bedrock_output, bedrock_total,
                      trajectory_input, trajectory_output, trajectory_total,
                      grand_total }
        }
        """
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
                }
            emp_map[eid]["bedrock_input"] += t.bedrock_input_tokens or 0
            emp_map[eid]["bedrock_output"] += t.bedrock_output_tokens or 0
            emp_map[eid]["trajectory_input"] += t.trajectory_input_tokens or 0
            emp_map[eid]["trajectory_output"] += t.trajectory_output_tokens or 0

        rows = []
        totals = {
            "bedrock_input": 0,
            "bedrock_output": 0,
            "bedrock_total": 0,
            "trajectory_input": 0,
            "trajectory_output": 0,
            "trajectory_total": 0,
            "grand_total": 0,
        }

        for data in sorted(emp_map.values(), key=lambda r: r["employee_name"]):
            bt = data["bedrock_input"] + data["bedrock_output"]
            tt = data["trajectory_input"] + data["trajectory_output"]
            row = {
                "employee_id": data["employee_id"],
                "employee_name": data["employee_name"],
                "bedrock_input": data["bedrock_input"],
                "bedrock_output": data["bedrock_output"],
                "bedrock_total": bt,
                "trajectory_input": data["trajectory_input"],
                "trajectory_output": data["trajectory_output"],
                "trajectory_total": tt,
                "grand_total": bt + tt,
            }
            rows.append(row)
            totals["bedrock_input"] += data["bedrock_input"]
            totals["bedrock_output"] += data["bedrock_output"]
            totals["bedrock_total"] += bt
            totals["trajectory_input"] += data["trajectory_input"]
            totals["trajectory_output"] += data["trajectory_output"]
            totals["trajectory_total"] += tt
            totals["grand_total"] += bt + tt

        return {
            "period": period,
            "start_date": start.isoformat() if start else None,
            "end_date": today.isoformat(),
            "rows": rows,
            "totals": totals,
        }
