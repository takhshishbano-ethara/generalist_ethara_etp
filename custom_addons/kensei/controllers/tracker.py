# -*- coding: utf-8 -*-
import datetime
import logging

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)

# Funnel stages: (css-key, label, list of allocation statuses that count toward it).
# css-key matches the .o_ktd_funnel_* pastel classes in tracker_dashboard.scss.
_FUNNEL = [
    ("input_bundle", "Input Bundles", ["task_created"]),
    ("in_authoring", "In Authoring", ["in_progress"]),
    ("in_trajectory", "In Trajectory", ["baseline_generated", "pass1"]),
    ("manual_qc", "Manual QC", ["manual_qc"]),
    ("verified", "Verified", ["deliverable"]),
]


def _avg(values):
    vals = [v for v in values if v]
    return round(sum(vals) / len(vals), 1) if vals else None


_PERSONA_SAMPLE_CSV = (
    "Persona Name,L1,L2\n"
    "Marcus,Finance,Accounts Payable\n"
    "Sophia,Healthcare,Patient Intake\n"
    "Leo,Retail,\n"
)


class KenseiTrackerController(http.Controller):

    @http.route("/kensei/tracker/persona/sample_csv", type="http", auth="user")
    def persona_sample_csv(self, **kw):
        """Serve a downloadable sample CSV for the persona import."""
        return request.make_response(
            _PERSONA_SAMPLE_CSV,
            headers=[
                ("Content-Type", "text/csv"),
                ("Content-Disposition", 'attachment; filename="persona_sample.csv"'),
            ],
        )

    # ------------------------------------------------------------------ #
    #  Daily Tracker (custom pivot table)
    # ------------------------------------------------------------------ #
    @http.route("/kensei/tracker/daily/filters", type="json", auth="user")
    def daily_filters(self, **kw):
        Alloc = request.env["kensei.tracker.allocation"]
        allocs = Alloc.search([])
        pls = [{"id": p.id, "name": p.name} for p in allocs.mapped("pl_id")]
        leads = [{"id": p.id, "name": p.name} for p in allocs.mapped("team_lead_id")]
        projects = sorted({a.project for a in allocs if a.project})
        functions = [{"value": k, "label": v}
                     for k, v in Alloc._fields["function"].selection]
        statuses = [{"value": k, "label": v}
                    for k, v in Alloc._fields["status"].selection]
        return {
            "pls": sorted(pls, key=lambda x: (x["name"] or "").lower()),
            "team_leads": sorted(leads, key=lambda x: (x["name"] or "").lower()),
            "projects": projects,
            "functions": functions,
            "statuses": statuses,
        }

    @http.route("/kensei/tracker/daily/data", type="json", auth="user")
    def daily_data(self, date_from=None, date_to=None, employee=None, pl_id=None,
                   function=None, status=None, project=None, team_lead_id=None,
                   sort_by="name", sort_dir="asc", page=1, page_size=20,
                   export=False, **kw):
        Alloc = request.env["kensei.tracker.allocation"]

        def _to_date(v, default):
            if not v:
                return default
            try:
                return datetime.date.fromisoformat(v[:10])
            except Exception:
                return default

        today = datetime.date.today()
        d_to = _to_date(date_to, today)
        d_from = _to_date(date_from, d_to - datetime.timedelta(days=29))
        if d_from > d_to:
            d_from, d_to = d_to, d_from

        domain = []
        if pl_id:
            domain.append(("pl_id", "=", int(pl_id)))
        if team_lead_id:
            domain.append(("team_lead_id", "=", int(team_lead_id)))
        if function:
            domain.append(("function", "=", function))
        if status:
            domain.append(("status", "=", status))
        if project:
            domain.append(("project", "=", project))
        if employee:
            domain += ["|", ("tasker_name", "ilike", employee),
                       ("tasker_email", "ilike", employee)]
        allocs = Alloc.search(domain)

        # date columns
        dates = []
        d = d_from
        while d <= d_to and len(dates) <= 120:
            dates.append(d)
            d += datetime.timedelta(days=1)
        date_keys = [d.isoformat() for d in dates]
        func_labels = dict(Alloc._fields["function"].selection)

        emp = {}
        for a in allocs:
            key = (a.tasker_email or "").lower() or (a.tasker_name or "unknown")
            e = emp.get(key)
            if not e:
                e = {
                    "name": a.tasker_name or a.tasker_email or "Unknown",
                    "email": a.tasker_email or "",
                    "pl": a.pl_id.name or "",
                    "function": func_labels.get(a.function, ""),
                    "cells": {k: 0 for k in date_keys},
                    "total": 0,
                }
                emp[key] = e
            if a.status == "deliverable":
                cd = a.date_final or a.assigned_date
                if cd:
                    cdd = cd.date() if hasattr(cd, "date") else cd
                    ck = cdd.isoformat()
                    if ck in e["cells"]:
                        e["cells"][ck] += 1
                        e["total"] += 1

        rows = list(emp.values())
        reverse = sort_dir == "desc"
        keyfn = {
            "total": lambda r: r["total"],
            "pl": lambda r: (r["pl"] or "").lower(),
            "function": lambda r: (r["function"] or "").lower(),
        }.get(sort_by, lambda r: (r["name"] or "").lower())
        rows.sort(key=keyfn, reverse=reverse)

        total_rows = len(rows)
        total_completed = sum(r["total"] for r in rows)
        total_employees = total_rows
        avg = round(total_completed / total_employees, 1) if total_employees else 0.0

        if not export:
            page = max(1, int(page))
            page_size = max(1, int(page_size))
            rows = rows[(page - 1) * page_size:(page - 1) * page_size + page_size]

        date_cols = [{"key": d.isoformat(), "label": d.strftime("%d %b"),
                      "dow": d.strftime("%a"), "weekend": d.weekday() >= 5}
                     for d in dates]
        return {
            "dates": date_cols,
            "rows": rows,
            "summary": {
                "total_employees": total_employees,
                "total_completed": total_completed,
                "avg_completion": avg,
                "date_from": d_from.strftime("%d %b %Y"),
                "date_to": d_to.strftime("%d %b %Y"),
            },
            "total_rows": total_rows,
            "page": int(page),
            "page_size": int(page_size),
        }

    @http.route("/kensei/tracker/dashboard", type="json", auth="user")
    def tracker_dashboard(self, **kw):
        """Aggregate allocation data for the Tracker Dashboard."""
        if not request.env.user.has_group("kensei.group_kensei_ql"):
            return {"error": "access_denied"}

        allocs = request.env["kensei.tracker.allocation"].search([])

        # ----- counts by status -----
        sc = {}
        for a in allocs:
            sc[a.status] = sc.get(a.status, 0) + 1

        funnel = [
            {"key": key, "label": label,
             "value": sum(sc.get(s, 0) for s in statuses)}
            for key, label, statuses in _FUNNEL
        ]

        # ----- team composition -----
        taskers = set(a.tasker_email for a in allocs if a.tasker_email)
        pls = allocs.mapped("pl_id")
        trajectory_generated = sum(
            1 for a in allocs
            if a.status in ("baseline_generated", "pass1", "manual_qc", "deliverable"))
        team_composition = [
            {"key": "total_people", "label": "Total People", "value": len(taskers), "color": "#6366f1"},
            {"key": "tpms", "label": "TPMs", "value": 0, "color": "#06b6d4"},
            {"key": "pls", "label": "PLs", "value": len(pls), "color": "#3b82f6"},
            {"key": "task_authoring", "label": "Task Authoring", "value": sc.get("in_progress", 0), "color": "#f59e0b"},
            {"key": "trajectory", "label": "Trajectory", "value": trajectory_generated, "color": "#10b981"},
        ]

        # ----- latest activity -----
        latest_activity = [
            {"key": "present", "label": "Present", "value": 0, "color": "#10b981"},
            {"key": "wfh", "label": "WFH", "value": 0, "color": "#6366f1"},
            {"key": "completions", "label": "Completions", "value": sc.get("deliverable", 0), "color": "#3b82f6"},
            {"key": "avg_score", "label": "Avg Score", "value": _avg(allocs.mapped("overall_score")), "color": "#f59e0b"},
            {"key": "blockers", "label": "Blockers", "value": sc.get("failed", 0), "color": "#ef4444"},
        ]

        # ----- per-PL progress -----
        per_pl = {}
        for a in allocs:
            pl = a.pl_id
            pid = pl.id or 0
            row = per_pl.setdefault(pid, {
                "id": pid, "name": pl.name if pl else "Unassigned",
                "team": 0, "in_auth": 0, "ready": 0, "in_traj": 0,
                "manual_qc": 0, "verified": 0, "blocked": 0,
                "_scores": [],
            })
            row["team"] += 1
            if a.overall_score:
                row["_scores"].append(a.overall_score)
            st = a.status
            if st == "in_progress":
                row["in_auth"] += 1
            elif st == "ready_baseline":
                row["ready"] += 1
            elif st in ("baseline_generated", "pass1"):
                row["in_traj"] += 1
            elif st == "manual_qc":
                row["manual_qc"] += 1
            elif st == "deliverable":
                row["verified"] += 1
            elif st == "failed":
                row["blocked"] += 1

        per_pl_rows = []
        for row in sorted(per_pl.values(), key=lambda r: (-r["team"], r["name"].lower())):
            row["avg_score"] = _avg(row.pop("_scores"))
            per_pl_rows.append(row)

        return {
            "team_composition": team_composition,
            "funnel": funnel,
            "latest_activity": latest_activity,
            "per_pl": per_pl_rows,
        }
