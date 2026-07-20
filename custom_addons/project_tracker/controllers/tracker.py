# -*- coding: utf-8 -*-
import datetime
import logging

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)

# Funnel stages.
#
# THE SAME STORED STATUS MEANS DIFFERENT WORK ON DIFFERENT STAGES. `baseline_generated`
# on stage 1 is a Baseline trajectory; on stage 2 it is a Pass @ K trajectory. They
# are separate steps of the pipeline, not one step counted twice — so they get
# separate buckets, and a bucket is keyed on (stage_no, status), never on status
# alone. Folding them together is what made a stage-2 task vanish into "In Trajectory"
# with nothing anywhere showing it was in Pass @ K.
#
# `stage` = None means the bucket counts that status on EITHER stage.
# css-key matches the .o_ktd_funnel_* pastel classes in tracker_dashboard.scss.
_FUNNEL = [
    # Ordered as the two workflows actually run: stage 1's ladder, then stage 2's.
    # The two stages are INDEPENDENT pipelines that happen to share stored status
    # values, so every step either names its stage or is genuinely common to both.
    #
    #   Stage 1: Authoring -> Tasker QC -> Ready for Baseline -> Baseline Generated
    #            -> Stage 1 Manual QC -> Ready for Next Stage
    #   Stage 2: Ready for Pass @ K -> Pass @ K Generated -> Stage 2 Manual QC
    #            -> Deliverable
    #
    # `stage` = None means the bucket counts that status on EITHER stage.
    # css-key matches the .o_ktd_funnel_* pastel classes in tracker_dashboard.scss.

    # Authoring is NOT stage-gated. In practice only stage 1 reaches it (see
    # _compute_status), but the status field admits it on either stage, and a bucket
    # that gates on stage 1 would leave such a row in no card at all -- counted in
    # Total and shown nowhere.
    ("in_authoring",  "In Authoring",           ["in_progress", "tasker_qc_completed"], None),
    # --- stage 1 ---
    ("in_trajectory", "Stage 1 Baseline",          ["ready_baseline", "baseline_generated"], 1),
    ("manual_qc_s1",  "Stage 1 Manual QC",         ["manual_qc"], 1),
    # A non-final stage that finished its pipeline: the task is NOT delivered, it
    # is waiting to be handed to stage 2. Its own step so it is never mistaken for
    # a delivered task. Only stage 1 can reach it, but it is left un-gated so a
    # task configured with >2 stages still lands somewhere.
    ("ready_next_stage", "Ready for Next Stage", ["ready_next_stage"], None),
    # --- stage 2 ---
    ("pass_it_k",     "Stage 2 Pass @ K",         ["ready_baseline", "baseline_generated"], 2),
    ("manual_qc_s2",  "Stage 2 Manual QC",         ["manual_qc"], 2),
    # Only the FINAL stage can deliver, so in a two-stage task this is stage 2's
    # terminal step. Un-gated for the same reason as ready_next_stage.
    ("verified",      "Deliverable",            ["deliverable"], None),
    ("failed",        "Failed",                 ["failed"], None),
]

# Non-terminal statuses (used for the "In Progress" stat + drill-down).
# 'ready_next_stage' counts as active: the TASK is still in flight — it just needs
# handing off — so it must not be counted as completed.
_ACTIVE_STATUSES = [
    "in_progress", "tasker_qc_completed", "ready_baseline",
    "baseline_generated", "manual_qc", "ready_next_stage",
]

# (stage_no, status) -> progress-table column. Stage-aware for the same reason the
# funnel is: stage 1's Baseline and stage 2's Pass @ K share a stored value but are
# different work, and a table that merges them cannot show where anything actually is.
# A `None` stage matches either.
_PROGRESS_BUCKET = {
    # (stage_no, status) -> progress-table column. Stage-aware because the two
    # ladders share stored values under different names: 'baseline_generated' is
    # Baseline on stage 1 and Pass @ K on stage 2, and 'manual_qc' is each stage's
    # OWN QC gate. A table that merges them cannot show where anything actually is.
    # A `None` stage matches either.
    (None, "in_progress"): "in_auth",
    (None, "tasker_qc_completed"): "in_auth",
    # stage 1
    (1, "ready_baseline"): "ready",
    (1, "baseline_generated"): "in_traj",
    (1, "manual_qc"): "s1_qc",
    (None, "ready_next_stage"): "handed_off",
    # stage 2
    (2, "ready_baseline"): "pik_ready",
    (2, "baseline_generated"): "pass_it_k",
    (2, "manual_qc"): "s2_qc",
    (None, "deliverable"): "verified",
    # either
    (None, "failed"): "blocked",
}

# Every column the progress table can hold, in render order: stage 1's ladder, then
# stage 2's, then the shared failure bucket.
_PROGRESS_COLUMNS = ("in_auth", "ready", "in_traj", "s1_qc", "handed_off",
                     "pik_ready", "pass_it_k", "s2_qc", "verified", "blocked")


def _pipeline_stage(stage_no):
    """Which of the two PIPELINES a stage runs — 1 (authoring) or 2 (Pass @ K).

    Mirrors _compute_status, which branches on `stage_no >= 2`: everything past
    stage 2 runs the stage-2 ladder and so produces stage-2 statuses. Matching the
    raw stage number instead would leave a stage-3 row in no bucket at all — absent
    from the funnel and from every progress column while still counted in Total, so
    the row's cells would not add up to its own total.
    """
    return 2 if stage_no and stage_no >= 2 else 1


def _bucket_for(stage_no, status):
    """The progress-table column for one (stage, status), or None if it has none."""
    return (_PROGRESS_BUCKET.get((_pipeline_stage(stage_no), status))
            or _PROGRESS_BUCKET.get((None, status)))


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

# Hard ceiling on rows returned in one Daily Tracker response. The export path
# skips pagination, so without this a client-supplied page_size was the one way to
# pull the entire allocation table down in a single JSON payload.
_MAX_PAGE_SIZE = 5000


def _funnel(sc):
    """Funnel cards from a {(stage_no, status): count} map.

    Keyed on the PAIR, not on status alone — stage 1's Baseline and stage 2's Pass
    It K share the stored value `baseline_generated` but are different steps, and
    they get their own cards. Each card carries the (statuses, stage) it counted so
    the drill-down opens exactly those records and nothing else.
    """
    cards = []
    for key, label, statuses, stage in _FUNNEL:
        value = sum(c for (st, s_), c in sc.items()
                    if s_ in statuses
                    and (stage is None or _pipeline_stage(st) == stage))
        cards.append({"key": key, "label": label, "value": value,
                      "statuses": statuses, "stage": stage})
    return cards


def _status_counts(Alloc, domain):
    """{(stage_no, status): count} — one grouped query, stage included."""
    return {(stage, status): count
            for stage, status, count in Alloc._read_group(
                domain, ["stage_no", "status"], ["__count"])}


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


def _label(record):
    """Label for a grouped m2o — safe to read as a non-HR user.

    NEVER read ``.name`` off one of these. The group key is often an ``hr.employee``
    (``pl_id``, ``team_lead_id``), and a PL/QL is NOT an HR user, so hr.employee
    exposes only its *public profile* to them. Reading ``.name`` makes the ORM
    prefetch the whole row, which trips the public-profile restriction and raises
    AccessError — killing the entire request. That is what made the Daily Tracker
    show every PL "Failed to load Daily Tracker data.": ``daily_data`` and
    ``daily_filters`` both read ``pl.name`` / ``lead.name`` directly.

    Measured on hr.employee as a PL with no HR groups:

        rec.name           -> AccessError
        rec.display_name   -> OK
        rec.sudo().name    -> OK

    ``display_name`` is the reason this works; the ``sudo()`` is deliberate
    belt-and-braces so the call stays correct even if a future hr.employee tightens
    what a public profile may compute. Route every grouped label through here.
    """
    if not record:
        return ""
    return record.sudo().display_name or ""


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
                "id": rid, "name": _label(rec) if rec else "Unassigned",
                "total": 0, "avg_score": None,
                **{c: 0 for c in _PROGRESS_COLUMNS},
            }
        return row

    for rec, stage, status, count in Alloc._read_group(
            base_domain, [group_field, "stage_no", "status"], ["__count"]):
        row = _row(rec)
        row["total"] += count
        col = _bucket_for(stage, status)
        if col:
            row[col] += count
    for rec, avg in Alloc._read_group(
            base_domain + [("overall_score", ">", 0)], [group_field], ["overall_score:avg"]):
        _row(rec)["avg_score"] = round(avg, 1) if avg else None
    return sorted(rows.values(), key=lambda r: (-r["total"], r["name"].lower()))


# ---------------------------------------------------------------------------- #
#  List-view stat cards (Task Allocation / Personas / Team Management)
#
#  Every builder is handed the model AS THE CALLING USER and the list's CURRENT
#  search domain, so the cards always agree with the rows underneath them: they
#  are scoped by the record rules (a PL sees their team's numbers, a tasker their
#  own) and they move when the user filters. Aggregation is grouped SQL — never
#  search([]) — so this stays cheap at 10k+ allocations.
# ---------------------------------------------------------------------------- #

def _card(key, label, value, icon="fa-tasks", tone="purple", suffix=None,
          share=None, filter_domain=None):
    """One stat card.

    Deliberately the same shape as the Daily Tracker's summary cards (icon tile +
    value + uppercase label): the two surfaces sit one menu click apart, so they
    should read as the same product.

    ``icon``          FontAwesome class for the tinted tile.
    ``tone``          which tint the tile carries — blue / green / amber / purple /
                      red, matching the Daily Tracker's .o_kd_ic_* set.
    ``share``         this value's fraction of the total (0..1), or None where the
                      metric is not a subset of anything (an average). Surfaced in
                      the tooltip, not as another mark on the card.
    ``filter_domain`` clicking the card adds this as a search filter. Cards that
                      cannot narrow anything (the total) leave it None and render
                      inert — a control that looks clickable and does nothing is
                      worse than one that plainly is not.
    """
    return {
        "key": key,
        "label": label,
        "value": value,
        "icon": icon,
        "tone": tone,
        "suffix": suffix,
        "share": share,
        "domain": filter_domain,
    }


def _share(value, total):
    return (value / total) if total else 0.0


def _stats_allocation(Alloc, domain):
    counts = {s: c for s, c in
              Alloc._read_group(domain, ["status"], ["__count"])}
    total = sum(counts.values())
    completed = counts.get("deliverable", 0)
    failed = counts.get("failed", 0)
    active = total - completed - failed
    avg = next((a for a, in Alloc._read_group(
        domain + [("overall_score", ">", 0)], [], ["overall_score:avg"])), None)
    return [
        _card("total", "Total Tasks", total,
              icon="fa-tasks", tone="purple"),
        _card("active", "In Progress", active,
              icon="fa-spinner", tone="blue",
              share=_share(active, total),
              filter_domain=[("status", "in", _ACTIVE_STATUSES)]),
        _card("completed", "Completed", completed,
              icon="fa-check-circle", tone="green",
              share=_share(completed, total),
              filter_domain=[("status", "=", "deliverable")]),
        _card("failed", "Failed", failed,
              icon="fa-times-circle", tone="red",
              share=_share(failed, total),
              filter_domain=[("status", "=", "failed")]),
        _card("avg_score", "Avg Score", round(avg, 1) if avg else None,
              icon="fa-line-chart", tone="amber", suffix="%"),
    ]


def _stats_persona(Persona, domain):
    counts = {s: c for s, c in
              Persona._read_group(domain, ["pt_assignment_status"], ["__count"])}
    assigned = counts.get("assigned", 0)
    unassigned = counts.get("unassigned", 0)
    total = assigned + unassigned
    return [
        _card("total", "Total Personas", total,
              icon="fa-id-badge", tone="purple"),
        _card("assigned", "Assigned", assigned,
              icon="fa-check-circle", tone="green",
              share=_share(assigned, total),
              filter_domain=[("pt_assignment_status", "=", "assigned")]),
        _card("unassigned", "Unassigned", unassigned,
              icon="fa-inbox", tone="blue",
              share=_share(unassigned, total),
              filter_domain=[("pt_assignment_status", "=", "unassigned")]),
    ]


def _stats_team(Member, domain):
    roles = {r: c for r, c in Member._read_group(domain, ["role"], ["__count"])}
    statuses = {s: c for s, c in
                Member._read_group(domain, ["status"], ["__count"])}
    total = sum(roles.values())
    benched = statuses.get("inactive", 0) + statuses.get("on_hold", 0)
    return [
        _card("total", "Total Members", total,
              icon="fa-users", tone="purple"),
        _card("taskers", "Taskers", roles.get("tasker", 0),
              icon="fa-user", tone="blue",
              share=_share(roles.get("tasker", 0), total),
              filter_domain=[("role", "=", "tasker")]),
        _card("qls", "Quality Leads", roles.get("ql", 0),
              icon="fa-check-square-o", tone="green",
              share=_share(roles.get("ql", 0), total),
              filter_domain=[("role", "=", "ql")]),
        _card("pls", "Project Leads", roles.get("pl", 0),
              icon="fa-briefcase", tone="amber",
              share=_share(roles.get("pl", 0), total),
              filter_domain=[("role", "=", "pl")]),
        _card("benched", "Not Active", benched,
              icon="fa-pause-circle", tone="red",
              share=_share(benched, total),
              filter_domain=[("status", "in", ["inactive", "on_hold"])]),
    ]


# Whitelist. The model name arrives from the client, so it is matched against this
# map rather than passed to request.env — otherwise the route would happily
# aggregate any model in the database.
_LIST_STATS = {
    "project.tracker.allocation": _stats_allocation,
    "kensei2.persona": _stats_persona,
    "project.tracker.team.member": _stats_team,
}


# Persona Name (required) + optional Domain and Source URL — the persona's own
# attributes. Task taxonomy (L1/L2, Agent Type) is set per task, not imported.
# Domains repeat across rows so the Bulk Allocation domain filter has several
# personas per domain to hand out.
_PERSONA_SAMPLE_CSV = (
    "Persona Name,Domain,Source URL\n"
    "Marcus Reed,Enterprise,https://example.com/personas/marcus-reed\n"
    "Sophia Lin,Enterprise,https://example.com/personas/sophia-lin\n"
    "Leo Alvarez,Enterprise,https://example.com/personas/leo-alvarez\n"
    "Amara Okafor,Enterprise,https://example.com/personas/amara-okafor\n"
    "Daniel Kim,Finance,https://example.com/personas/daniel-kim\n"
    "Priya Nair,Finance,https://example.com/personas/priya-nair\n"
    "Ethan Brooks,Finance,https://example.com/personas/ethan-brooks\n"
    "Yuki Tanaka,Coding,https://example.com/personas/yuki-tanaka\n"
    "Olivia Grant,Coding,https://example.com/personas/olivia-grant\n"
    "Noah Feld,Coding,https://example.com/personas/noah-feld\n"
)

_TEAM_SAMPLE_CSV = (
    "Email,Assigned PL,Assigned QL,Status\n"
    "alice@example.com,maya.rodriguez@example.com,Sofia Nguyen,Active\n"
    "bob@example.com,Liam Patel,,On Hold\n"
    "carol@example.com,,,\n"
)


class Kensei2TrackerController(http.Controller):

    @http.route("/project_tracker/persona/sample_csv", type="http", auth="user")
    def persona_sample_csv(self, **kw):
        """Serve a downloadable, name-only sample CSV for the persona import."""
        return request.make_response(
            _PERSONA_SAMPLE_CSV,
            headers=[
                ("Content-Type", "text/csv"),
                ("Content-Disposition", 'attachment; filename="persona_sample.csv"'),
            ],
        )

    @http.route("/project_tracker/team/sample_csv", type="http", auth="user")
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
    #  Stat cards above the Task Allocation / Personas / Team list views
    # ------------------------------------------------------------------ #
    @http.route("/project_tracker/list_stats", type="json", auth="user")
    def list_stats(self, model=None, domain=None, **kw):
        """Stat cards for a list view, scoped to its CURRENT search domain.

        ``model`` is client-supplied, so it is looked up in the _LIST_STATS
        whitelist instead of being handed to request.env — an unrecognised model
        yields no cards rather than aggregating an arbitrary table. The domain is
        applied as the calling user, so the record rules scope the figures exactly
        the way they scope the rows below.
        """
        builder = _LIST_STATS.get(model)
        if not builder:
            return {"cards": []}
        if not isinstance(domain, list):
            domain = []
        return {"cards": builder(request.env[model], domain)}

    # ------------------------------------------------------------------ #
    #  Daily Tracker (custom pivot table)
    # ------------------------------------------------------------------ #
    @http.route("/project_tracker/daily/filters", type="json", auth="user")
    def daily_filters(self, **kw):
        """Distinct filter-dropdown values (PLs, team leads, projects, statuses)
        for the Daily Tracker, sourced from grouped queries."""
        Alloc = request.env["project.tracker.allocation"]
        # Distinct dropdown values via grouped queries — the previous
        # search([]).mapped(...) materialised every allocation just to collect a
        # few dozen PLs/leads/projects (a full-table load on each page open).
        # _label(): pl_id / team_lead_id are hr.employee, and a PL/QL is not an HR
        # user — reading .name direct raises AccessError (public-profile fields).
        pls = [{"id": pl.id, "name": _label(pl)}
               for pl, in Alloc._read_group([("pl_id", "!=", False)], ["pl_id"])]
        leads = [{"id": lead.id, "name": _label(lead)}
                 for lead, in Alloc._read_group([("team_lead_id", "!=", False)], ["team_lead_id"])]
        projects = sorted(
            p for p, in Alloc._read_group([("project", "!=", False)], ["project"]) if p)
        # The filter matches the STORED status, which two stages share under
        # different names: picking "Baseline Generated" also returns every stage-2
        # row, whose real name is "Pass @ K Generated". Naming only one of them
        # makes the dropdown lie about what it selects, so where the two ladders
        # disagree the option names both.
        s2 = dict(Alloc.STAGE2_STATUS_SELECTION)
        statuses = [
            {"value": k,
             "label": v if s2.get(k, v) == v else "%s / %s" % (v, s2[k])}
            for k, v in Alloc._fields["status"].selection
        ]
        return {
            "pls": sorted(pls, key=lambda x: (x["name"] or "").lower()),
            "team_leads": sorted(leads, key=lambda x: (x["name"] or "").lower()),
            "projects": projects,
            "statuses": statuses,
        }

    @http.route("/project_tracker/daily/data", type="json", auth="user")
    def daily_data(self, date_from=None, date_to=None, employee=None, pl_id=None,
                   status=None, project=None, team_lead_id=None, stage=None,
                   sort_by="name", sort_dir="asc", page=1, page_size=20,
                   export=False, **kw):
        """Paginated per-tasker daily completion pivot for the Daily Tracker,
        with date-range/PL/lead/project/status filters, sorting and an export
        payload. Aggregated via grouped queries (record rules scope the rows to
        the caller)."""
        Alloc = request.env["project.tracker.allocation"]

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
        # Stage scope. A cell counts COMPLETED STAGES, and the two stages are
        # different work -- a bare "3" merges an authoring pass with a Pass @ K run
        # and cannot say which. Scoping the whole domain (not just the completions)
        # is deliberate: filtering to Stage 2 should drop a tasker who has never
        # worked a Stage 2 out of the roster entirely, rather than leave them as an
        # all-zero row.
        stage_no = _to_int(stage)
        if stage_no in (1, 2):
            domain.append(("stage_no", "=", stage_no))

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
                    # pl is an hr.employee — see _label(): a PL/QL cannot read it
                    # directly without HR rights.
                    "pl": _label(pl),
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

        # Coerce defensively: these land straight from the client. A non-numeric
        # ``page`` used to raise on int() and surface as a 500 instead of degrading
        # to page 1 — every other request param in this file already goes through
        # _to_int.
        page = max(1, _to_int(page) or 1)
        page_size = min(max(1, _to_int(page_size) or 20), _MAX_PAGE_SIZE)
        # The export path deliberately skips pagination, so it is the one route that
        # can return the whole table in a single payload. Bound it, and TELL the
        # client when it was bounded — a silently truncated export reads as a
        # complete one.
        truncated = False
        if export:
            truncated = len(rows) > _MAX_PAGE_SIZE
            rows = rows[:_MAX_PAGE_SIZE]
        else:
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
            "page": page,
            "page_size": page_size,
            "truncated": truncated,
        }

    @http.route("/project_tracker/dashboard", type="json", auth="user")
    def tracker_dashboard(self, date_from=None, date_to=None,
                          group_by=None, stage=None, project_id=None, **kw):
        """Aggregate allocation data for the Tracker Dashboard.

        All figures come from grouped SQL (``_read_group``) rather than loading
        every allocation into memory — the previous ``search([])`` did not scale
        past a few thousand rows. Card colours live in the SCSS (keyed by
        ``card.key``), not here.
        """
        user = request.env.user
        # Kensei2 Admin is the in-app equivalent of a sysadmin for this dashboard:
        # it is the only role whose scope is the whole organisation. Keeping
        # base.group_system alongside it means an Odoo sysadmin never loses access.
        is_admin = (user.has_group("base.group_system")
                    or user.has_group("project_tracker.group_project_tracker_admin"))
        is_lead = user.has_group("project_tracker.group_project_tracker_ql")  # PL implies QL
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

        Alloc = request.env["project.tracker.allocation"]

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

        # ----- project scope. Appended to date_domain so BOTH the task view and
        # the per-person view are sliced to the chosen project; the roster
        # composition stays org/team-wide (membership is not per-project). -----
        proj_int = _to_int(project_id)
        if proj_int is not None:
            date_domain.append(("project_id", "=", proj_int))

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
        sc = _status_counts(Alloc, task_domain)
        funnel = _funnel(sc)

        # ----- team composition (roster counts, scoped to the caller's team) -----
        Member = request.env["project.tracker.team.member"]
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
        completed = sum(c for (_st, s_), c in sc.items() if s_ == "deliverable")
        failed = sum(c for (_st, s_), c in sc.items() if s_ == "failed")
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
            "project_id": proj_int,
            "projects": request.env["project.tracker.project"].search_read(
                [], ["name"], order="name"),
        }

    # ------------------------------------------------------------------ #
    #  Per-tasker performance — powers both "My Performance" (self) and the
    #  Team Management drill-in (a QL/PL viewing a specific member).
    # ------------------------------------------------------------------ #
    @http.route("/project_tracker/performance", type="json", auth="user")
    def tracker_performance(self, member_id=None, date_from=None, date_to=None, **kw):
        env = request.env
        caller = env.user
        if member_id:
            mid = _to_int(member_id)
            member = env["project.tracker.team.member"].browse(mid).exists() if mid else None
            if not member:
                return {"error": "not_found"}
            target = member.user_id
            is_lead = (caller.has_group("project_tracker.group_project_tracker_ql")
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
        Alloc = request.env["project.tracker.allocation"]
        Member = request.env["project.tracker.team.member"]

        df, dt = _iso_date(date_from), _iso_date(date_to)
        if df and dt and df > dt:
            df, dt = dt, df
        dom = [("tasker_user_id", "=", user.id)]
        if df:
            dom.append(("assigned_date", ">=", df))
        if dt:
            dom.append(("assigned_date", "<=", dt))

        sc = _status_counts(Alloc, dom)
        total = sum(sc.values())
        # This is a PERSONAL performance view, so it measures what this tasker
        # finished. Completing a non-final stage ('ready_next_stage') is real work
        # they delivered — it counts, even though the task itself is not delivered
        # yet. (The org dashboard's "Completed" stat deliberately counts only
        # 'deliverable', because there it is TASKS being counted, not effort.)
        completed = sum(c for (_st, s_), c in sc.items()
                        if s_ in _STAGE_DONE_STATUSES)
        blocked = sum(c for (_st, s_), c in sc.items() if s_ == "failed")
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

        # recent tasks. A tasker's recent list mixes stage-1 and stage-2 rows, so the
        # label MUST come from the record (status_label is stored and stage-aware) and
        # not from the status Selection -- that one table carries stage 1's names, and
        # would print a stage-2 row as "Baseline Generated" / "Stage 1 Manual QC".
        recent = []
        for a in Alloc.search(dom, order="assigned_date desc, id desc", limit=10):
            recent.append({
                "id": a.id, "task_id": a.task_id,
                "stage": a.stage_no, "total_stages": a.total_stages,
                "persona": a.persona_id.name or "",
                "status": a.status, "status_label": a.status_label,
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
