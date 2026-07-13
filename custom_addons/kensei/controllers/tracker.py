# -*- coding: utf-8 -*-
import datetime
import logging

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)

# Funnel stages: (css-key, label, list of allocation statuses that count toward it).
# css-key matches the .o_ktd_funnel_* pastel classes in tracker_dashboard.scss.
# Every non-terminal-failed status maps into exactly one bucket; the earlier
# "Input Bundles"/'task_created' and 'pass1' entries were dropped because the
# model never produces those statuses (see STATUS_SELECTION).
_FUNNEL = [
    ("in_authoring", "In Authoring", ["in_progress", "tasker_qc_completed"]),
    ("in_trajectory", "In Trajectory", ["ready_baseline", "baseline_generated"]),
    ("manual_qc", "Manual QC", ["manual_qc"]),
    # A non-final stage that finished its pipeline: the task is NOT delivered, it
    # is waiting to be handed to the next stage. Its own funnel step so it is
    # never mistaken for a delivered task.
    ("ready_next_stage", "Ready for Next Stage", ["ready_next_stage"]),
    ("verified", "Verified", ["deliverable"]),
    ("failed", "Failed", ["failed"]),
]

# Non-terminal statuses (used for the "In Progress" stat + drill-down).
# 'ready_next_stage' counts as active: the TASK is still in flight — it just needs
# handing off — so it must not be counted as completed.
_ACTIVE_STATUSES = [
    "in_progress", "tasker_qc_completed", "ready_baseline",
    "baseline_generated", "manual_qc", "ready_next_stage",
]

# allocation status -> progress-table column. 'ready_next_stage' has its own
# column: a stage in that state is FINISHED and waiting to be handed off, which is
# not the same thing as sitting in Manual QC (where it used to be folded in).
_PROGRESS_BUCKET = {
    "in_progress": "in_auth", "tasker_qc_completed": "in_auth",
    "ready_baseline": "ready", "baseline_generated": "in_traj",
    "manual_qc": "manual_qc", "ready_next_stage": "handed_off",
    "deliverable": "verified", "failed": "blocked",
}

# A stage's own work is finished in these states — used by the Daily Tracker,
# which credits the tasker who completed THAT stage (finishing a non-final stage
# is still a real day's work).
_STAGE_DONE_STATUSES = ["deliverable", "ready_next_stage"]

# Progress-table grouping axis -> the m2o field to group allocations by.
_PROGRESS_GROUPS = {
    "pl": "pl_id",
    "ql": "assigned_ql_id",
    "tasker": "tasker_member_id",
}


def _funnel(sc):
    """Build the RL-pipeline funnel cards from a {status: count} map. Shared by
    the org dashboard and the per-tasker performance view."""
    return [
        {"key": key, "label": label,
         "value": sum(sc.get(s, 0) for s in statuses), "statuses": statuses}
        for key, label, statuses in _FUNNEL
    ]


def _iso_date(v):
    """Parse a 'YYYY-MM-DD' string to a date; None if empty/invalid."""
    if not v:
        return None
    try:
        return datetime.date.fromisoformat(str(v)[:10])
    except Exception:
        return None


def _to_int(v):
    """Best-effort int cast for request params; None on anything non-numeric
    (so a bad client value yields a clean filter skip, not a 500)."""
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _progress_rows(Alloc, group_field, base_domain=None):
    """Per-<group> progress buckets via grouped SQL. ``group_field`` is any m2o
    (``pl_id`` -> hr.employee, ``assigned_ql_id`` -> res.users,
    ``tasker_member_id`` -> team member); ``display_name`` labels every model.
    ``base_domain`` scopes the allocations (e.g. the dashboard date range)."""
    base_domain = base_domain or []
    rows = {}

    def _row(rec):
        rid = rec.id if rec else 0
        row = rows.get(rid)
        if not row:
            row = rows[rid] = {
                "id": rid, "name": rec.display_name if rec else "Unassigned",
                "total": 0, "in_auth": 0, "ready": 0, "in_traj": 0,
                "manual_qc": 0, "handed_off": 0, "verified": 0, "blocked": 0,
                "avg_score": None,
            }
        return row

    for rec, status, count in Alloc._read_group(base_domain, [group_field, "status"], ["__count"]):
        row = _row(rec)
        row["total"] += count
        col = _PROGRESS_BUCKET.get(status)
        if col:
            row[col] += count
    for rec, avg in Alloc._read_group(
            base_domain + [("overall_score", ">", 0)], [group_field], ["overall_score:avg"]):
        _row(rec)["avg_score"] = round(avg, 1) if avg else None
    return sorted(rows.values(), key=lambda r: (-r["total"], r["name"].lower()))


_PERSONA_SAMPLE_CSV = (
    "Persona Name,L1,L2\n"
    "Marcus,Finance,Accounts Payable\n"
    "Sophia,Healthcare,Patient Intake\n"
    "Leo,Retail,\n"
)

_TEAM_SAMPLE_CSV = (
    "Email,Assigned PL,Assigned QL,Status\n"
    "alice@example.com,maya.rodriguez@example.com,Sofia Nguyen,Active\n"
    "bob@example.com,Liam Patel,,On Hold\n"
    "carol@example.com,,,\n"
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

    @http.route("/kensei/tracker/team/sample_csv", type="http", auth="user")
    def team_sample_csv(self, **kw):
        """Serve a downloadable sample CSV for the team bulk import."""
        return request.make_response(
            _TEAM_SAMPLE_CSV,
            headers=[
                ("Content-Type", "text/csv"),
                ("Content-Disposition", 'attachment; filename="team_sample.csv"'),
            ],
        )

    # ------------------------------------------------------------------ #
    #  Daily Tracker (custom pivot table)
    # ------------------------------------------------------------------ #
    @http.route("/kensei/tracker/daily/filters", type="json", auth="user")
    def daily_filters(self, **kw):
        """Distinct filter-dropdown values (PLs, team leads, projects, statuses)
        for the Daily Tracker, sourced from grouped queries."""
        Alloc = request.env["kensei.tracker.allocation"]
        # Distinct dropdown values via grouped queries — the previous
        # search([]).mapped(...) materialised every allocation just to collect a
        # few dozen PLs/leads/projects (a full-table load on each page open).
        pls = [{"id": pl.id, "name": pl.name}
               for pl, in Alloc._read_group([("pl_id", "!=", False)], ["pl_id"])]
        leads = [{"id": lead.id, "name": lead.name}
                 for lead, in Alloc._read_group([("team_lead_id", "!=", False)], ["team_lead_id"])]
        projects = sorted(
            p for p, in Alloc._read_group([("project", "!=", False)], ["project"]) if p)
        statuses = [{"value": k, "label": v}
                    for k, v in Alloc._fields["status"].selection]
        return {
            "pls": sorted(pls, key=lambda x: (x["name"] or "").lower()),
            "team_leads": sorted(leads, key=lambda x: (x["name"] or "").lower()),
            "projects": projects,
            "statuses": statuses,
        }

    @http.route("/kensei/tracker/daily/data", type="json", auth="user")
    def daily_data(self, date_from=None, date_to=None, employee=None, pl_id=None,
                   status=None, project=None, team_lead_id=None,
                   sort_by="name", sort_dir="asc", page=1, page_size=20,
                   export=False, **kw):
        """Paginated per-tasker daily completion pivot for the Daily Tracker,
        with date-range/PL/lead/project/status filters, sorting and an export
        payload. Aggregated via grouped queries (record rules scope the rows to
        the caller)."""
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
        pl_int, lead_int = _to_int(pl_id), _to_int(team_lead_id)
        if pl_int is not None:
            domain.append(("pl_id", "=", pl_int))
        if lead_int is not None:
            domain.append(("team_lead_id", "=", lead_int))
        if status:
            domain.append(("status", "=", status))
        if project:
            domain.append(("project", "=", project))
        if employee:
            domain += ["|", ("tasker_name", "ilike", employee),
                       ("tasker_email", "ilike", employee)]
        # date columns (cap the window at 120 days)
        dates = []
        d = d_from
        while d <= d_to and len(dates) < 120:
            dates.append(d)
            d += datetime.timedelta(days=1)
        date_keys = {d.isoformat() for d in dates}

        # Roster: one lightweight grouped row per (tasker, pl) instead of
        # instantiating every allocation record. Deduplicated by tasker email.
        emp = {}
        for email, name, pl, _count in Alloc._read_group(
                domain, ["tasker_email", "tasker_name", "pl_id"],
                ["__count"]):
            key = (email or "").lower() or (name or "unknown")
            if key not in emp:
                emp[key] = {
                    "name": name or email or "Unknown",
                    "email": email or "",
                    "pl": pl.name if pl else "",
                    "cells": {k: 0 for k in date_keys},
                    "total": 0,
                }

        # Completions: grouped by tasker + completion day, scoped to the window.
        # A tasker who finished a NON-final stage ('ready_next_stage') completed a
        # real piece of work, so it counts here just like a delivered task —
        # otherwise their credit would vanish the moment the task is handed off.
        comp_domain = domain + [
            ("status", "in", _STAGE_DONE_STATUSES),
            ("date_final", ">=", d_from),
            ("date_final", "<", d_to + datetime.timedelta(days=1)),
        ]
        for email, day, count in Alloc._read_group(
                comp_domain, ["tasker_email", "date_final:day"], ["__count"]):
            e = emp.get((email or "").lower())
            if not e or not day:
                continue
            ck = (day.date() if hasattr(day, "date") else day).isoformat()
            if ck in e["cells"]:
                e["cells"][ck] += count
                e["total"] += count

        rows = list(emp.values())
        reverse = sort_dir == "desc"
        keyfn = {
            "total": lambda r: r["total"],
            "pl": lambda r: (r["pl"] or "").lower(),
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
    def tracker_dashboard(self, date_from=None, date_to=None,
                          group_by=None, stage=None, **kw):
        """Aggregate allocation data for the Tracker Dashboard.

        All figures come from grouped SQL (``_read_group``) rather than loading
        every allocation into memory — the previous ``search([])`` did not scale
        past a few thousand rows. Card colours live in the SCSS (keyed by
        ``card.key``), not here.
        """
        user = request.env.user
        # Kensei Admin is the in-app equivalent of a sysadmin for this dashboard:
        # it is the only role whose scope is the whole organisation. Keeping
        # base.group_system alongside it means an Odoo sysadmin never loses access.
        is_admin = (user.has_group("base.group_system")
                    or user.has_group("kensei.group_kensei_admin"))
        is_lead = user.has_group("kensei.group_kensei_ql")  # PL implies QL
        if not (is_admin or is_lead):
            return {"error": "access_denied"}

        # ----- role scope -----
        # Admin sees the whole org. A PL/QL sees the same scoped view: only the
        # tasks / members where they are the assigned PL OR assigned QL.
        if is_admin:
            scope, member_scope = [], []
        else:
            scope = ['|', ("assigned_pl_id", "=", user.id),
                     ("assigned_ql_id", "=", user.id)]
            member_scope = list(scope)

        Alloc = request.env["kensei.tracker.allocation"]

        # ----- date range (scopes the allocation-based sections by assignment
        # date; the roster composition below is not date-bound). -----
        df, dt = _iso_date(date_from), _iso_date(date_to)
        if df and dt and df > dt:
            df, dt = dt, df
        date_domain = []
        if df:
            date_domain.append(("assigned_date", ">=", df))
        if dt:
            date_domain.append(("assigned_date", "<=", dt))

        # TWO domains, because the dashboard answers two questions in two different
        # units. Tasks are counted once; people are credited per stage.
        #
        # `task_domain` ("how many tasks, and where are they stuck?") counts each
        # TASK once. A task is worked in stages and each stage is its own allocation
        # record, so `is_current_stage` (True only for the latest stage of a task)
        # collapses the chain down to the one stage the task is actually sitting in.
        task_domain = list(scope) + date_domain
        task_domain.append(("is_current_stage", "=", True))

        # `people_domain` ("what has each person done?") must NOT apply that filter:
        # a stage record IS the unit of a person's work, so filtering to the current
        # stage would erase stage 1's tasker/PL/QL — and their scores — the instant
        # the task was handed off, i.e. penalise them for finishing. The Daily
        # Tracker already credits per stage for the same reason.
        people_domain = list(scope) + date_domain
        stage_no = _to_int(stage)
        if stage_no in (1, 2):
            people_domain.append(("stage_no", "=", stage_no))

        # ----- counts by status (one grouped query) -----
        sc = {status: count
              for status, count in Alloc._read_group(task_domain, ["status"], ["__count"])}

        funnel = _funnel(sc)

        # ----- team composition (roster counts, scoped to the caller's team) -----
        Member = request.env["kensei.tracker.team.member"]
        role_counts = {role: c for role, c in
                       Member._read_group(member_scope, ["role"], ["__count"])}
        team_composition = [
            {"key": "total", "label": "Total Members",
             "value": sum(role_counts.values()), "role": False},
            {"key": "tasker", "label": "Taskers",
             "value": role_counts.get("tasker", 0), "role": "tasker"},
            {"key": "ql", "label": "QLs",
             "value": role_counts.get("ql", 0), "role": "ql"},
            {"key": "pl", "label": "PLs",
             "value": role_counts.get("pl", 0), "role": "pl"},
        ]

        # ----- stats (within range; drill-down where meaningful) -----
        total = sum(sc.values())
        completed = sc.get("deliverable", 0)
        failed = sc.get("failed", 0)
        active = total - completed - failed
        avg_overall = next((avg for avg, in Alloc._read_group(
            task_domain + [("overall_score", ">", 0)], [], ["overall_score:avg"])), None)
        stats = [
            {"key": "total", "label": "Total Tasks", "value": total},
            {"key": "active", "label": "In Progress",
             "value": active, "statuses": _ACTIVE_STATUSES},
            {"key": "completions", "label": "Completed",
             "value": completed, "statuses": ["deliverable"]},
            {"key": "failed", "label": "Failed",
             "value": failed, "statuses": ["failed"]},
            {"key": "avg_score", "label": "Avg Score",
             "value": round(avg_overall, 1) if avg_overall else None},
        ]

        # One progress table, two axes: WHO to group by, and WHICH stage to count.
        group_key = group_by if group_by in _PROGRESS_GROUPS else "pl"
        group_field = _PROGRESS_GROUPS[group_key]

        return {
            "team_composition": team_composition,
            "funnel": funnel,
            "stats": stats,
            "rows": _progress_rows(Alloc, group_field, people_domain),
            "group_by": group_key,
            "stage": stage_no if stage_no in (1, 2) else None,
            "date_from": df.isoformat() if df else None,
            "date_to": dt.isoformat() if dt else None,
        }

    # ------------------------------------------------------------------ #
    #  Per-tasker performance — powers both "My Performance" (self) and the
    #  Team Management drill-in (a QL/PL viewing a specific member).
    # ------------------------------------------------------------------ #
    @http.route("/kensei/tracker/performance", type="json", auth="user")
    def tracker_performance(self, member_id=None, date_from=None, date_to=None, **kw):
        env = request.env
        caller = env.user
        if member_id:
            mid = _to_int(member_id)
            member = env["kensei.tracker.team.member"].browse(mid).exists() if mid else None
            if not member:
                return {"error": "not_found"}
            target = member.user_id
            is_lead = (caller.has_group("kensei.group_kensei_ql")
                       or caller.has_group("base.group_system"))
            if not is_lead and target != caller:
                return {"error": "access_denied"}
        else:
            target = caller
        if not target:
            return {"error": "no_user"}
        return self._performance(target, date_from, date_to)

    def _performance(self, user, date_from, date_to):
        """Aggregate one tasker's allocation stats (grouped SQL). Runs as the
        caller, so a Tasker's own record rule keeps them scoped to their rows;
        a QL/PL (sees-all) is narrowed to the target via the domain."""
        Alloc = request.env["kensei.tracker.allocation"]
        Member = request.env["kensei.tracker.team.member"]

        df, dt = _iso_date(date_from), _iso_date(date_to)
        if df and dt and df > dt:
            df, dt = dt, df
        dom = [("tasker_user_id", "=", user.id)]
        if df:
            dom.append(("assigned_date", ">=", df))
        if dt:
            dom.append(("assigned_date", "<=", dt))

        sc = {s: c for s, c in Alloc._read_group(dom, ["status"], ["__count"])}
        total = sum(sc.values())
        # This is a PERSONAL performance view, so it measures what this tasker
        # finished. Completing a non-final stage ('ready_next_stage') is real work
        # they delivered — it counts, even though the task itself is not delivered
        # yet. (The org dashboard's "Completed" stat deliberately counts only
        # 'deliverable', because there it is TASKS being counted, not effort.)
        completed = sum(sc.get(s, 0) for s in _STAGE_DONE_STATUSES)
        blocked = sc.get("failed", 0)
        active = total - completed - blocked
        comp_rate = round(100 * completed / (completed + blocked), 1) \
            if (completed + blocked) else None

        def _avg(field):
            v = next((a for a, in Alloc._read_group(
                dom + [(field, ">", 0)], [], ["%s:avg" % field])), None)
            return round(v, 1) if v else None

        # cycle time (assigned -> completed), in days. search_read pulls only the
        # two date columns instead of materialising full records.
        cycles = []
        for r in Alloc.search_read(
                dom + [("date_final", "!=", False)], ["assigned_date", "date_final"]):
            if r["assigned_date"] and r["date_final"]:
                cycles.append((r["date_final"].date() - r["assigned_date"]).days)
        avg_cycle = round(sum(cycles) / len(cycles), 1) if cycles else None

        personas = sum(1 for p, in Alloc._read_group(
            dom + [("persona_id", "!=", False)], ["persona_id"]) if p)

        kpis = [
            {"key": "total", "label": "Total Tasks", "value": total},
            {"key": "active", "label": "Active", "value": active},
            {"key": "completed", "label": "Completed", "value": completed},
            {"key": "blocked", "label": "Blocked", "value": blocked},
            {"key": "rate", "label": "Completion Rate", "value": comp_rate, "suffix": "%"},
            {"key": "overall", "label": "Avg Overall", "value": _avg("overall_score"), "suffix": "%"},
            {"key": "rubric", "label": "Avg Rubric", "value": _avg("rubric_score"), "suffix": "%"},
            {"key": "pytest", "label": "Avg Pytest", "value": _avg("pytest_score"), "suffix": "%"},
            {"key": "cycle", "label": "Avg Cycle (days)", "value": avg_cycle},
            {"key": "personas", "label": "Personas", "value": personas},
        ]

        funnel = _funnel(sc)

        # recent tasks
        status_labels = dict(Alloc._fields["status"].selection)
        recent = []
        for a in Alloc.search(dom, order="assigned_date desc, id desc", limit=10):
            recent.append({
                "id": a.id, "task_id": a.task_id,
                "stage": a.stage_no, "total_stages": a.total_stages,
                "persona": a.persona_id.name or "",
                "status": a.status, "status_label": status_labels.get(a.status, a.status),
                "overall": a.overall_score or None,
                "assigned": a.assigned_date and a.assigned_date.isoformat(),
                "completed": a.date_final and a.date_final.date().isoformat(),
            })

        member = Member.search([("user_id", "=", user.id)], limit=1)
        role_label = dict(Member._fields["role"].selection).get(member.role, "") \
            if member else ""
        subject = {
            "name": user.name,
            "email": user.email or user.login,
            "role": role_label,
        }

        return {
            "subject": subject, "kpis": kpis, "funnel": funnel,
            "recent": recent,
            "date_from": df.isoformat() if df else None,
            "date_to": dt.isoformat() if dt else None,
        }
