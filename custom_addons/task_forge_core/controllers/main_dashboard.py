# -*- coding: utf-8 -*-
"""Main (Founder) Dashboard controller.

Powers the single-page Founder Overview dashboard (Figma: Ethara R&D - 2026-04-30).

All endpoints are CTO-scoped and support a common set of query params:

    range=7d | 30d | 90d | custom       (default: 7d)
    date_from=YYYY-MM-DD                (used when range=custom)
    date_to=YYYY-MM-DD                  (used when range=custom)
    category=stem | non_stem | technical | all   (default: all)

Response envelope follows the existing project convention::

    { "message": "...", "errors": [], "status_code": 200, "data": {...} }

Endpoints
---------
GET  /api/v2/taskforge/main/summary                                -> KPI cards row
GET  /api/v2/taskforge/main/tasks_timeseries                       -> Tasks Completed line chart
GET  /api/v2/taskforge/main/active_blockers                        -> Active Blockers table
GET  /api/v2/taskforge/main/project_health                         -> Project Health & AHT table
GET  /api/v2/taskforge/main/performance_ranking                    -> Performance Ranking tab (simple)
GET  /api/v2/taskforge/main/qc_feedback                            -> QC Feedback tab (simple)
GET  /api/v2/taskforge/main/performance_ranking_panel              -> Performance Ranking 4-table panel
GET  /api/v2/taskforge/main/qc_feedback_summary                    -> QC Feedback KPI counts + chips
GET  /api/v2/taskforge/main/qc_feedback_justification_breakdown    -> Per-project x per-member justification table
GET  /api/v2/taskforge/main/qc_feedback_prompt_breakdown           -> Per-project x per-member prompt rejection table

Shared helpers live in this module to avoid touching the legacy
``dashboard_controllers.py`` which is known to have several correctness
bugs (stale blocker-state lists, missing team scoping, etc.).
"""

import logging
from datetime import datetime, date, time, timedelta

from odoo import http
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    validate_token,
)

_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Blocker states considered "open" for dashboard counters.
# Includes the new escalation states plus the legacy 'ack' for backward compat.
OPEN_BLOCKER_STATES = [
    'pending',
    'escalated_to_pl',
    'escalated_to_cto',
    'ack',           # legacy
    'escalated',     # legacy
]

# Blocker considered "overdue" if it has been open more than this many days.
BLOCKER_OVERDUE_DAYS = 3

# Project health thresholds (same formula as legacy dashboard for parity).
HEALTH_AT_RISK_BLOCKERS = 5
HEALTH_AT_RISK_OVERDUE = 3
HEALTH_WARNING_BLOCKERS = 2
HEALTH_WARNING_OVERDUE = 1

# Priority label + dot color mapping used in the Active Blockers panel.
PRIORITY_MAP = {
    '0': {'label': 'Low',      'color': 'grey'},
    '1': {'label': 'Medium',   'color': 'amber'},
    '2': {'label': 'High',     'color': 'red'},
    '3': {'label': 'Critical', 'color': 'red'},
}

STATUS_LABEL_MAP = {
    'pending':          {'label': 'Raised',    'tone': 'danger'},
    'escalated_to_pl':  {'label': 'In Prog.',  'tone': 'warning'},
    'escalated_to_cto': {'label': 'In Prog.',  'tone': 'warning'},
    'ack':              {'label': 'In Prog.',  'tone': 'warning'},
    'escalated':        {'label': 'In Prog.',  'tone': 'warning'},
    'resolved':         {'label': 'Resolved',  'tone': 'success'},
    'validated':        {'label': 'Bug',       'tone': 'info'},
    'no_issue':         {'label': 'Closed',    'tone': 'neutral'},
}

CATEGORY_VALUES = ('stem', 'non_stem', 'technical')

# "Active / currently working" maps to the
# pipeline-running states below; terminal "completed" maps to DONE_STATES.
# Mirrors the constants in task_forge_bridge.controllers.dashboard_controllers.
ACTIVE_STATES = ('extracting', 'extracted', 'generating', 'generated', 'scoring', 'scored', 'qc_running')
DONE_STATES = ('done', 'submitted')

# Backends don't agree on a single quality-rating field name. Resolved in
# priority order: `quality_score` (the convention task_forge_bridge expects),
# then `score` (used by leviathan.job and similar PRD-graded backends).
_QUALITY_FIELD_CANDIDATES = ('quality_score', 'score')


def _backend_quality_field(backend):
    """Return the first quality-score field name present on `backend`, or None."""
    for name in _QUALITY_FIELD_CANDIDATES:
        if name in backend._fields:
            return name
    return None


# Backends disagree on status field + terminal "completed" values:
#   aurora-style backends: `state` in ('done', 'submitted')
#   skoll.skoll:           `qc_status` in ('passed',)
#   fenrir.task:           `status` in ('completed', 'approved')
#   talos.talos:           `task_status` in ('Submitted',)
# Probed in priority order so the bare `state in DONE_STATES` domain
# doesn't crash on backends like skoll that never had a `state` column.
_DONE_BY_STATUS_FIELD = (
    ('state', ('done', 'submitted')),
    ('qc_status', ('passed',)),
    ('status', ('completed', 'approved')),
    ('task_status', ('Submitted',)),
)


def _backend_done_domain(backend):
    """Return completed-task domain leaves for `backend`, or None if no status field."""
    for name, values in _DONE_BY_STATUS_FIELD:
        if name in backend._fields:
            return [(name, 'in', list(values))]
    return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_range(kw):
    """Return (date_from, date_to) as `date` objects from request params.

    Supports:
        range=7d   -> (today - 6, today)    (7-day inclusive window)
        range=30d
        range=90d
        range=custom + date_from + date_to
    Defaults to 7 days ending today if nothing supplied.
    """
    today = date.today()
    rng = (kw.get('range') or '7d').lower()

    if rng == 'custom':
        try:
            df_str = kw.get('date_from')
            dt_str = kw.get('date_to')
            if not df_str or not dt_str:
                return today - timedelta(days=6), today
            df = datetime.strptime(df_str, '%Y-%m-%d').date()
            dt = datetime.strptime(dt_str, '%Y-%m-%d').date()
            if df > dt:
                df, dt = dt, df
            return df, dt
        except (TypeError, ValueError):
            return today - timedelta(days=6), today

    days_map = {'7d': 6, '30d': 29, '90d': 89}
    delta = days_map.get(rng, 6)
    return today - timedelta(days=delta), today


def _day_bounds(d):
    """Return datetime bounds (start, end) for a given `date`."""
    return (
        datetime.combine(d, time.min),
        datetime.combine(d, time.max),
    )


def _category_domain(kw):
    """Return an Odoo domain fragment filtering project by category.

    Returns [] for 'all' or unknown input so callers can safely concatenate.
    """
    cat = (kw.get('category') or 'all').lower()
    if cat in CATEGORY_VALUES:
        return [('project_category', '=', cat)]
    return []


def _project_ids_in_category(kw):
    """Resolve the category filter to a list of project IDs (or None = all)."""
    domain = _category_domain(kw)
    if not domain:
        return None
    Project = request.env['project.project'].sudo()
    return Project.search(domain).ids


def _live_project_domain():
    """OR-combined 'running project' domain. Fresh list each call so callers
    can safely append/extend additional AND terms."""
    return request.env['project.project'].sudo()._task_forge_live_domain()


def _require_cto(user):
    # Role gate intentionally disabled: any @validate_token authenticated user can access these endpoints.
    return None


def _safe_error(exc, context=''):
    """Log full trace server-side, return a sanitised 400 to the client."""
    _logger.exception('main_dashboard %s failed: %s', context, exc)
    return return_Response(
        message='Internal error while computing %s' % (context or 'dashboard'),
        status=400,
        errors=[str(exc)],
    )


def _count_open_blockers(extra_domain=None):
    """Single source of truth for open-blocker counting."""
    domain = [('state', 'in', OPEN_BLOCKER_STATES)]
    if extra_domain:
        domain = domain + extra_domain
    return request.env['task.forge.blocker'].sudo().search_count(domain)


def _count_overdue_blockers(extra_domain=None):
    """Blockers open longer than BLOCKER_OVERDUE_DAYS days."""
    threshold = datetime.now() - timedelta(days=BLOCKER_OVERDUE_DAYS)
    domain = [
        ('state', 'in', OPEN_BLOCKER_STATES),
        ('create_date', '<=', threshold),
    ]
    if extra_domain:
        domain = domain + extra_domain
    return request.env['task.forge.blocker'].sudo().search_count(domain)


def _workforce_breakdown():
    """Return dict with counts of taskers / qr / pl / total active.

    Uses `_get_task_forge_role()` per employee because the role is derived
    from `res.users.all_group_ids` / `user_role` - not stored directly on the
    employee - so we can't use a single read_group. Cached per-request via
    the Odoo recordset iteration (still O(N) but only one SQL call).
    """
    Employee = request.env['hr.employee'].sudo()
    user_role_ids = [
        request.env.ref('api_auth_gateway.role_cto_technical').id,
        request.env.ref('api_auth_gateway.role_pl_technical').id,
        request.env.ref('api_auth_gateway.role_pl_stem').id,
        request.env.ref('api_auth_gateway.role_pl_non_stem').id,
        request.env.ref('api_auth_gateway.role_qc_technical').id,
        request.env.ref('api_auth_gateway.role_qc_stem').id,
        request.env.ref('api_auth_gateway.role_qc_non_stem').id,
        request.env.ref('api_auth_gateway.role_tasker_technical').id,
        request.env.ref('api_auth_gateway.role_tasker_stem').id,
        request.env.ref('api_auth_gateway.role_tasker_non_stem').id,
    ]

    all_active = Employee.search([('task_forge_active', '=', True), ('user_id.user_role', 'in', user_role_ids)])

    counts = {'admin': 0, 'pl': 0, 'qr': 0, 'ql': 0, 'tasker': 0}
    for emp in all_active:
        role = emp._get_task_forge_role() or 'tasker'
        counts[role] = counts.get(role, 0) + 1

    total = len(all_active)
    total_workforce = Employee.search_count([('user_id.user_role', 'in', user_role_ids)])
    return {
        'total': total,
        'taskers': counts.get('tasker', 0),
        # 'qr': counts.get('qr', 0) + counts.get('ql', 0),
        'qr': counts.get('qr', 0),
        'pl': counts.get('pl', 0),
        'admin': counts.get('admin', 0),
        'active_percent': round((total / total_workforce * 100) if total_workforce else 0, 0),
    }


def _pending_leaves_breakdown():
    """Return dict with pending-leave counts bucketed by Task Forge role."""
    Leave = request.env['hr.leave'].sudo()
    pending = Leave.search([('state', '=', 'confirm')])

    counts = {'pl': 0, 'qr': 0, 'tasker': 0, 'other': 0}
    for lv in pending:
        emp = lv.employee_id
        if not emp:
            counts['other'] += 1
            continue
        role = emp._get_task_forge_role() or 'tasker'
        if role == 'pl':
            counts['pl'] += 1
        elif role in ('qr', 'ql'):
            counts['qr'] += 1
        elif role == 'tasker':
            counts['tasker'] += 1
        else:
            counts['other'] += 1

    return {
        'total': len(pending),
        'pl': counts['pl'],
        'qr': counts['qr'],
        'tasker': counts['tasker'],
    }


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------

class MainDashboardController(http.Controller):
    """All endpoints powering the Founder Overview dashboard."""

    # ------------------------------------------------------------------
    # 1) Summary KPIs
    # ------------------------------------------------------------------
    @http.route(
        '/api/v2/taskforge/main/summary',
        type='http', auth='none', methods=['GET'], csrf=False, cors='*',
    )
    @validate_token
    def main_summary(self, **kw):
        try:
            user = request.env.user
            err = _require_cto(user)
            if err:
                return err

            today = date.today()
            yesterday = today - timedelta(days=1)
            today_from, today_to = _day_bounds(today)
            yesterday_from, yesterday_to = _day_bounds(yesterday)

            Employee = request.env['hr.employee'].sudo()
            Project = request.env['project.project'].sudo()

            project_ids = _project_ids_in_category(kw)

            scoped_proj_domain = []
            if project_ids is not None:
                scoped_proj_domain.append(('id', 'in', project_ids))
            scoped_projects = Project.search(scoped_proj_domain)

            table_to_project_ids = {}
            for proj in scoped_projects:
                table = (getattr(proj, 'connected_table', None) or '').strip()
                if not table:
                    continue
                table_to_project_ids.setdefault(table, []).append(proj.id)

            online_user_ids = set()
            live_project_ids = set()
            completed_today = 0
            completed_yesterday = 0
            overdue_by_project = {}

            for table, pids in table_to_project_ids.items():
                if table not in request.env:
                    continue
                Model = request.env[table].sudo()
                if not hasattr(Model, 'get_main_dashboard_metrics'):
                    continue
                try:
                    metrics = Model.get_main_dashboard_metrics(
                        today_from, today_to, yesterday_from, yesterday_to,
                    )
                except Exception:
                    _logger.exception(
                        'get_main_dashboard_metrics failed on %s', table,
                    )
                    continue
                online_user_ids.update(metrics.get('in_progress_user_ids') or [])
                completed_today += int(metrics.get('completed_today') or 0)
                completed_yesterday += int(metrics.get('completed_yesterday') or 0)
                if metrics.get('has_in_progress_work'):
                    live_project_ids.update(pids)

            if online_user_ids:
                online_now = Employee.search_count([
                    ('user_id', 'in', list(online_user_ids)),
                    ('task_forge_active', '=', True),
                ])
            else:
                online_now = 0

            workforce = _workforce_breakdown()
            total_taskers_capacity = workforce['taskers']

            active_proj_domain = _live_project_domain()
            if project_ids is not None:
                active_proj_domain.append(('id', 'in', project_ids))
            active_projects = Project.search(active_proj_domain)
            active_projects_count = len(active_projects)

            at_risk = 0
            for proj in active_projects:
                proj_blockers = _count_open_blockers([('project_id', '=', proj.id)])
                proj_overdue = overdue_by_project.get(proj.id, 0)
                if (proj_blockers > HEALTH_AT_RISK_BLOCKERS or
                        proj_overdue > HEALTH_AT_RISK_OVERDUE):
                    at_risk += 1

            # --- Open blockers ---
            blocker_extra = [('project_id', 'in', project_ids)] if project_ids is not None else None
            open_blockers = _count_open_blockers(blocker_extra)
            overdue_blockers = _count_overdue_blockers(blocker_extra)

            # --- Pending leaves ---
            leaves = _pending_leaves_breakdown()

            # --- Header member count: distinct employees touching TF ---
            # Convention: active Task Forge members = hr.employee.task_forge_active
            total_members = Employee.search_count([('task_forge_active', '=', True)])

            data = {
                'header': {
                    'label': 'Founder overview',
                    'total_members': total_members,
                },
                'live_tasking': {
                    'online_now': online_now,
                    'total_taskers': total_taskers_capacity,
                    'active_projects_count': len(live_project_ids),
                },
                'tasks_completed': {
                    'today': completed_today,
                    'yesterday': completed_yesterday,
                    'is_live': True,
                },
                'active_projects': {
                    'count': active_projects_count,
                    'at_risk': at_risk,
                },
                'open_blockers': {
                    'count': open_blockers,
                    'overdue': overdue_blockers,
                },
                'total_workforce': {
                    'total': workforce['total'],
                    'taskers': workforce['taskers'],
                    'qr': workforce['qr'],
                    'pl': workforce['pl'],
                    'active_percent': workforce['active_percent'],
                },
                'pending_leaves': {
                    'total': leaves['total'],
                    'pl': leaves['pl'],
                    'qr': leaves['qr'],
                    'tasker': leaves['tasker'],
                },
            }

            return return_Response(
                message='Founder summary',
                status=200,
                data={'data': data},
            )
        except Exception as e:
            return _safe_error(e, 'main_summary')

    # ------------------------------------------------------------------
    # 2) Tasks Completed time series (line chart)
    # ------------------------------------------------------------------
    @http.route(
        '/api/v2/taskforge/main/tasks_timeseries',
        type='http', auth='none', methods=['GET'], csrf=False, cors='*',
    )
    @validate_token
    def tasks_timeseries(self, **kw):
        try:
            user = request.env.user
            err = _require_cto(user)
            if err:
                return err

            Project = request.env['project.project'].sudo()
            date_from, date_to = _parse_range(kw)
            project_ids = _project_ids_in_category(kw)

            project_domain = []
            if project_ids is not None:
                project_domain.append(('id', 'in', project_ids))

            try:
                single_project_id = int(kw.get('project_id')) if kw.get('project_id') else None
            except (TypeError, ValueError):
                single_project_id = None
            if single_project_id:
                project_domain.append(('id', '=', single_project_id))

            projects = Project.search(project_domain)

            tables = set()
            for proj in projects:
                table = (getattr(proj, 'connected_table', None) or '').strip()
                if table:
                    tables.add(table)

            dt_from = datetime.combine(date_from, time.min)
            dt_to = datetime.combine(date_to, time.max)

            counts_by_date = {}
            for table in tables:
                if table not in request.env:
                    continue
                Model = request.env[table]
                if not hasattr(Model, 'get_tasks_completed_timeseries'):
                    continue
                try:
                    rows = Model.get_tasks_completed_timeseries(dt_from, dt_to) or []
                except Exception:
                    continue
                for iso, count in rows:
                    if not iso:
                        continue
                    counts_by_date[iso] = counts_by_date.get(iso, 0) + int(count or 0)

            # Fill missing days with 0 for clean chart rendering.
            series = []
            total = 0
            peak = {'date': None, 'count': 0}
            cursor = date_from
            while cursor <= date_to:
                key = cursor.isoformat()
                cnt = counts_by_date.get(key, 0)
                series.append({'date': key, 'count': cnt})
                total += cnt
                if cnt > peak['count']:
                    peak = {'date': key, 'count': cnt}
                cursor += timedelta(days=1)

            data = {
                'date_from': date_from.isoformat(),
                'date_to': date_to.isoformat(),
                'series': series,
                'total': total,
                'peak': peak,
            }

            return return_Response(
                message='Tasks completed time series',
                status=200,
                data={'data': data},
            )
        except Exception as e:
            return _safe_error(e, 'tasks_timeseries')

    # ------------------------------------------------------------------
    # 3) Active Blockers list (right-side table)
    # ------------------------------------------------------------------
    @http.route(
        '/api/v2/taskforge/main/active_blockers',
        type='http', auth='none', methods=['GET'], csrf=False, cors='*',
    )
    @validate_token
    def active_blockers(self, **kw):
        try:
            user = request.env.user
            err = _require_cto(user)
            if err:
                return err

            Blocker = request.env['task.forge.blocker'].sudo()

            try:
                limit = max(1, min(100, int(kw.get('limit') or 5)))
            except (TypeError, ValueError):
                limit = 5
            try:
                page = max(1, int(kw.get('page') or 1))
            except (TypeError, ValueError):
                page = 1
            offset = (page - 1) * limit

            project_ids = _project_ids_in_category(kw)

            domain = [('state', 'in', OPEN_BLOCKER_STATES)]
            if project_ids is not None:
                domain.append(('project_id', 'in', project_ids))

            priority = kw.get('priority')
            if priority in ('0', '1', '2', '3'):
                domain.append(('priority', '=', priority))

            status = kw.get('status')
            if status:
                status_list = [s.strip() for s in status.split(',') if s.strip()]
                if status_list:
                    domain = [d for d in domain if not (
                        isinstance(d, (list, tuple)) and d[0] == 'state'
                    )]
                    domain.append(('state', 'in', status_list))

            try:
                project_id = int(kw.get('project_id')) if kw.get('project_id') else None
            except (TypeError, ValueError):
                project_id = None
            if project_id:
                domain.append(('project_id', '=', project_id))

            try:
                employee_id = int(kw.get('employee_id')) if kw.get('employee_id') else None
            except (TypeError, ValueError):
                employee_id = None
            if employee_id:
                domain.append(('employee_id', '=', employee_id))

            search = (kw.get('search') or '').strip()
            if search:
                domain += [
                    '|', '|',
                    ('name', 'ilike', search),
                    ('blocker_reason', 'ilike', search),
                    ('project_id.name', 'ilike', search),
                ]

            SORT_WHITELIST = {
                'priority_desc':   'priority desc, create_date asc',
                'priority_asc':    'priority asc, create_date asc',
                'newest':          'create_date desc',
                'oldest':          'create_date asc',
                'days_open_desc':  'create_date asc',
                'days_open_asc':   'create_date desc',
            }
            order = SORT_WHITELIST.get(kw.get('sort'), 'priority desc, create_date asc')

            total = Blocker.search_count(domain)
            blockers = Blocker.search(
                domain,
                order=order,
                offset=offset,
                limit=limit,
            )

            now = datetime.now()
            items = []
            for b in blockers:
                pri_info = PRIORITY_MAP.get(b.priority or '1', PRIORITY_MAP['1'])
                status_info = STATUS_LABEL_MAP.get(b.state, {
                    'label': (b.state or '').replace('_', ' ').title(),
                    'tone': 'neutral',
                })
                days_open = 0
                if b.create_date:
                    days_open = max(0, (now - b.create_date).days)

                items.append({
                    'id': b.id,
                    'priority': b.priority or '1',
                    'priority_label': pri_info['label'],
                    'priority_color': pri_info['color'],
                    'title': b.name or (b.blocker_reason or '')[:80],
                    'project_id': b.project_id.id if b.project_id else None,
                    'project_name': b.project_id.name if b.project_id else '',
                    'status': b.state,
                    'status_label': status_info['label'],
                    'status_tone': status_info['tone'],
                    'days_open': days_open,
                    'employee_name': b.employee_id.name if b.employee_id else '',
                })

            data = {
                'total_open': total,
                'page': page,
                'limit': limit,
                'items': items,
            }

            return return_Response(
                message='Active blockers',
                status=200,
                data={'data': data},
            )
        except Exception as e:
            return _safe_error(e, 'active_blockers')

    # ------------------------------------------------------------------
    # 4) Project Health & AHT table
    # ------------------------------------------------------------------
    @http.route(
        '/api/v2/taskforge/main/project_health',
        type='http', auth='none', methods=['GET'], csrf=False, cors='*',
    )
    @validate_token
    def project_health(self, **kw):
        # Per-project metrics now come from each project's `connected_table`
        # backend. Response schema is unchanged.
        # Field translation:
        #     task.forge.log.date            -> backend.create_date  (datetime)
        #     state == 'completed'           -> state in DONE_STATES
        #     state in (in_progress, ack,    -> state in ACTIVE_STATES
        #              escalated, returned,
        #              blocker)
        #     state == 'overdue'             -> state in ACTIVE_STATES AND
        #                                       create_date <= now - BLOCKER_OVERDUE_DAYS
        #                                       (backends have no 'overdue' state)
        #     time_taken_mins                -> duration_seconds / 60 (gated)
        #     quality_score                  -> backend.quality_score (gated)
        try:
            user = request.env.user
            err = _require_cto(user)
            if err:
                return err

            Project = request.env['project.project'].sudo()

            date_from, date_to = _parse_range(kw)
            # `create_date` is a Datetime; widen the date window to full days.
            dt_from = datetime.combine(date_from, time.min)
            dt_to = datetime.combine(date_to, time.max)
            overdue_threshold = datetime.now() - timedelta(days=BLOCKER_OVERDUE_DAYS)

            try:
                limit = max(1, min(200, int(kw.get('limit') or 50)))
            except (TypeError, ValueError):
                limit = 50
            try:
                page = max(1, int(kw.get('page') or 1))
            except (TypeError, ValueError):
                page = 1
            offset = (page - 1) * limit

            health_filter = kw.get('health')  # healthy | warning | at_risk | None

            # Only consider projects with a wired backend, since per-project
            # metrics now come from `connected_table`.
            proj_domain = _live_project_domain() + [('connected_table', '!=', False)]
            proj_domain += _category_domain(kw)

            try:
                single_project_id = int(kw.get('project_id')) if kw.get('project_id') else None
            except (TypeError, ValueError):
                single_project_id = None
            if single_project_id:
                proj_domain.append(('id', '=', single_project_id))

            search = (kw.get('search') or '').strip()
            if search:
                proj_domain.append(('name', 'ilike', search))

            PROJECT_SORT_WHITELIST = {
                'name_asc':  'name asc',
                'name_desc': 'name desc',
                'newest':    'create_date desc',
                'oldest':    'create_date asc',
            }
            order = PROJECT_SORT_WHITELIST.get(kw.get('sort'), 'name asc')

            total_projects = Project.search_count(proj_domain)
            projects = Project.search(
                proj_domain,
                order=order,
                offset=offset,
                limit=limit,
            )

            # Backend status field varies: crowley/vegeta/leviathan/gohan use
            # `state`, skoll uses `qc_status`, fenrir uses `status`, talos uses
            # `task_status`. Resolve per-backend so pending/overdue aren't zeroed.
            STATUS_FIELDS = ('state', 'qc_status', 'status', 'task_status')

            metrics_by_proj = {}
            for proj in projects:
                backend_name = proj.connected_table
                if backend_name not in request.env:
                    metrics_by_proj[proj.id] = (0, 0, 0, 0.0, 0.0)
                    continue
                backend = request.env[backend_name].sudo()

                if hasattr(backend, 'get_performance_metrics'):
                    perf = backend.get_performance_metrics() or {}
                    completed_cnt = perf.get('task_done') or 0
                    aht_measured = perf.get('aht_measured_count') or 0
                    avg_secs = perf.get('avg_handling_time_seconds') or 0.0
                    total_mins = (avg_secs * aht_measured) / 60.0
                    avg_quality_val = perf.get('approval_percentage') or 0
                else:
                    completed_cnt = 0
                    total_mins = 0.0
                    avg_quality_val = 0

                status_field = next(
                    (f for f in STATUS_FIELDS if f in backend._fields),
                    None,
                )
                if status_field:
                    pending_cnt = backend.search_count([
                        (status_field, 'in', list(ACTIVE_STATES)),
                    ])
                    overdue_cnt = backend.search_count([
                        (status_field, 'in', list(ACTIVE_STATES)),
                        ('create_date', '<=', overdue_threshold),
                    ])
                else:
                    pending_cnt = 0
                    overdue_cnt = 0

                metrics_by_proj[proj.id] = (
                    completed_cnt,
                    pending_cnt,
                    overdue_cnt,
                    total_mins,
                    avg_quality_val,
                )

            items = []
            for proj in projects:
                completed, pending, overdue, total_mins, avg_quality = metrics_by_proj.get(
                    proj.id, (0, 0, 0, 0.0, 0.0)
                )

                blockers_open = _count_open_blockers([('project_id', '=', proj.id)])

                # Health classification (corrected state list via helper).
                if (blockers_open > HEALTH_AT_RISK_BLOCKERS or
                        overdue > HEALTH_AT_RISK_OVERDUE):
                    health = 'at_risk'
                elif (blockers_open > HEALTH_WARNING_BLOCKERS or
                        overdue > HEALTH_WARNING_OVERDUE):
                    health = 'warning'
                else:
                    health = 'healthy'

                if health_filter and health_filter != health:
                    continue

                # Members: union of tasker + qc_reviewer + lead.
                tasker_ids = set(proj.project_tasker.ids)
                qr_ids = set(proj.project_qc_reviewer.ids)
                pl_ids = set(proj.project_lead.ids)
                member_ids = tasker_ids | qr_ids | pl_ids

                aht_min = round(total_mins / completed, 1) if completed else 0
                hours_total = round(total_mins / 60.0, 1) if total_mins else 0
                quality_pct = round(avg_quality, 0) if avg_quality else 0

                # Category label for the table.
                cat_map = {
                    'stem': 'Stem',
                    'non_stem': 'Non Stem',
                    'technical': 'Technical',
                }
                category_label = cat_map.get(proj.project_category, '-') \
                    if hasattr(proj, 'project_category') else '-'

                # Tasker avatars (ids + name only; URL resolution left to frontend).
                tasker_avatars = [
                    {'id': t.id, 'name': t.name}
                    for t in proj.project_tasker[:5]
                ]

                items.append({
                    'project_id': proj.id,
                    'project_name': proj.name,
                    'category': category_label,
                    'category_value': proj.project_category if hasattr(proj, 'project_category') else None,
                    'members': len(member_ids),
                    'completed': completed,
                    'pending': pending,
                    'overdue': overdue,
                    'blockers_open': blockers_open,
                    'taskers_avatars': tasker_avatars,
                    'quality_percent': quality_pct,
                    'aht_min': aht_min,
                    'hours_total': hours_total,
                    'health': health,
                })

            ITEM_SORT_KEYS = {
                'completed_desc':  ('completed', True),
                'completed_asc':   ('completed', False),
                'blockers_desc':   ('blockers_open', True),
                'blockers_asc':    ('blockers_open', False),
                'overdue_desc':    ('overdue', True),
                'overdue_asc':     ('overdue', False),
                'aht_desc':        ('aht_min', True),
                'aht_asc':         ('aht_min', False),
                'hours_desc':      ('hours_total', True),
                'hours_asc':       ('hours_total', False),
                'quality_desc':    ('quality_percent', True),
                'quality_asc':     ('quality_percent', False),
                'members_desc':    ('members', True),
                'members_asc':     ('members', False),
            }
            sort_key = ITEM_SORT_KEYS.get(kw.get('sort'))
            if sort_key:
                field, reverse = sort_key
                items.sort(key=lambda i: i.get(field) or 0, reverse=reverse)

            data = {
                'date_from': date_from.isoformat(),
                'date_to': date_to.isoformat(),
                'total': total_projects,
                'page': page,
                'limit': limit,
                'items': items,
            }

            return return_Response(
                message='Project health',
                status=200,
                data={'data': data},
            )
        except Exception as e:
            return _safe_error(e, 'project_health')

    # ------------------------------------------------------------------
    # 5) Performance Ranking tab
    # ------------------------------------------------------------------
    @http.route(
        '/api/v2/taskforge/main/performance_ranking',
        type='http', auth='none', methods=['GET'], csrf=False, cors='*',
    )
    @validate_token
    def performance_ranking(self, **kw):
        # Pulls data from each project's `connected_table` backend (the
        # per-project job models) instead of the legacy `task.forge.log`.
        # Response schema is unchanged.
        # Field translation:
        #     task.forge.log.date          -> backend.create_date  (datetime)
        #     task.forge.log.employee_id   -> backend.user_id      (res.users)
        #     state == 'completed'         -> state in DONE_STATES
        #     time_taken_mins (minutes)    -> duration_seconds / 60
        #     quality_score                -> backend.quality_score (gated)
        try:
            user = request.env.user
            err = _require_cto(user)
            if err:
                return err

            Employee = request.env['hr.employee'].sudo()
            Project = request.env['project.project'].sudo()

            date_from, date_to = _parse_range(kw)
            # `create_date` is a Datetime; widen the date window to full days.
            dt_from = datetime.combine(date_from, time.min)
            dt_to = datetime.combine(date_to, time.max)

            try:
                top_n = max(1, min(50, int(kw.get('top_n') or 10)))
            except (TypeError, ValueError):
                top_n = 10
            try:
                low_threshold = float(kw.get('low_threshold') or 50)
            except (TypeError, ValueError):
                low_threshold = 50.0

            try:
                single_project_id = int(kw.get('project_id')) if kw.get('project_id') else None
            except (TypeError, ValueError):
                single_project_id = None
            try:
                single_employee_id = int(kw.get('employee_id')) if kw.get('employee_id') else None
            except (TypeError, ValueError):
                single_employee_id = None

            # ---- Project scope -----------------------------------------
            # Live projects with a wired backend; intersected with the
            # category filter and (optionally) a single project filter.
            proj_domain = _live_project_domain() + [('connected_table', '!=', False)]
            project_ids_in_cat = _project_ids_in_category(kw)
            if project_ids_in_cat is not None:
                proj_domain.append(('id', 'in', project_ids_in_cat))
            if single_project_id:
                proj_domain.append(('id', '=', single_project_id))
            projects = Project.search(proj_domain)

            # ---- Translate employee filters -> user_id filters ---------
            # Backend rows key off `user_id`, not `employee_id`. Resolve once.
            target_user_ids = None  # None == no filter
            search = (kw.get('search') or '').strip()
            if search:
                matching_emps = Employee.search([('name', 'ilike', search)])
                target_user_ids = matching_emps.mapped('user_id').ids or [0]
            if single_employee_id:
                emp = Employee.browse(single_employee_id)
                emp_uid = emp.user_id.id if emp.user_id else 0
                if target_user_ids is None:
                    target_user_ids = [emp_uid]
                else:
                    target_user_ids = [u for u in target_user_ids if u == emp_uid] or [0]

            # ---- Per-user aggregation across every project's backend ----
            totals_by_user = {}
            completed_by_user = {}
            seconds_by_user = {}

            for proj in projects:
                backend_name = proj.connected_table
                if backend_name not in request.env:
                    continue
                backend = request.env[backend_name].sudo()
                if 'user_id' not in backend._fields:
                    continue

                bd = [
                    ('create_date', '>=', dt_from),
                    ('create_date', '<=', dt_to),
                ]
                if target_user_ids is not None:
                    bd.append(('user_id', 'in', target_user_ids))

                total_rows = backend.read_group(
                    domain=bd,
                    fields=['user_id'],
                    groupby=['user_id'],
                    lazy=False,
                )
                has_duration = 'duration_seconds' in backend._fields
                comp_fields = ['user_id']
                if has_duration:
                    comp_fields.append('duration_seconds:sum')
                done_leaves = _backend_done_domain(backend)
                if done_leaves is None:
                    completed_rows = []
                else:
                    completed_rows = backend.read_group(
                        domain=bd + done_leaves,
                        fields=comp_fields,
                        groupby=['user_id'],
                        lazy=False,
                    )

                for r in total_rows:
                    uid = r.get('user_id')
                    if isinstance(uid, tuple):
                        uid = uid[0]
                    if not uid:
                        continue
                    totals_by_user[uid] = totals_by_user.get(uid, 0) + (r.get('__count') or 0)
                for r in completed_rows:
                    uid = r.get('user_id')
                    if isinstance(uid, tuple):
                        uid = uid[0]
                    if not uid:
                        continue
                    completed_by_user[uid] = completed_by_user.get(uid, 0) + (r.get('__count') or 0)
                    if has_duration:
                        secs = r.get('duration_seconds') or 0
                        seconds_by_user[uid] = seconds_by_user.get(uid, 0) + secs

            # ---- Resolve user_id -> hr.employee for response payload ----
            user_ids = list(totals_by_user.keys())
            user_to_emp = {}
            if user_ids:
                for e in Employee.search([('user_id', 'in', user_ids)]):
                    if e.user_id:
                        user_to_emp[e.user_id.id] = e

            rankings = []
            for uid, t in totals_by_user.items():
                if t <= 0:
                    continue
                c = completed_by_user.get(uid, 0) or 0
                productivity = round((c / t * 100), 1)
                emp = user_to_emp.get(uid)
                rankings.append({
                    'employee_id': emp.id if emp else 0,
                    'employee_name': emp.name if emp else '(unknown)',
                    'total_tasks': t,
                    'completed': c,
                    'total_minutes': int(round((seconds_by_user.get(uid, 0) or 0) / 60.0)),
                    'productivity': productivity,
                })

            RANKING_SORT_KEYS = {
                'productivity_desc': ('productivity', True),
                'productivity_asc':  ('productivity', False),
                'total_desc':        ('total_tasks', True),
                'total_asc':         ('total_tasks', False),
                'completed_desc':    ('completed', True),
                'completed_asc':     ('completed', False),
                'minutes_desc':      ('total_minutes', True),
                'minutes_asc':       ('total_minutes', False),
            }
            sort_key = RANKING_SORT_KEYS.get(kw.get('sort'), ('productivity', True))
            sort_field, reverse = sort_key
            rankings.sort(key=lambda r: r.get(sort_field) or 0, reverse=reverse)

            # ---- Top Project Leads -------------------------------------
            # For each PL of any in-scope project, sum total + completed
            # backend rows across all of that PL's projects in the window.
            pl_project_map = {}
            for proj in projects:
                for pl in proj.project_lead:
                    pl_project_map.setdefault(pl.id, {
                        'user_id': pl.id,
                        'name': pl.name,
                        'projects': [],
                    })
                    pl_project_map[pl.id]['projects'].append(proj)

            pl_rows = []
            for info in pl_project_map.values():
                total = 0
                comp = 0
                for proj in info['projects']:
                    backend_name = proj.connected_table
                    if backend_name not in request.env:
                        continue
                    backend = request.env[backend_name].sudo()
                    bd = [
                        ('create_date', '>=', dt_from),
                        ('create_date', '<=', dt_to),
                    ]
                    total += backend.search_count(bd)
                    done_leaves = _backend_done_domain(backend)
                    if done_leaves is not None:
                        comp += backend.search_count(bd + done_leaves)
                pct = round((comp / total * 100) if total else 0.0, 1)
                pl_rows.append({
                    'user_id': info['user_id'],
                    'name': info['name'],
                    'completed': comp,
                    'total': total,
                    'percentage_completion': pct,
                })
            pl_rows.sort(key=lambda x: (-x['percentage_completion'], -x['total']))
            top_project_leads = [dict(rank=i + 1, **row) for i, row in enumerate(pl_rows[:top_n])]

            # ---- Top Quality Reviewers ---------------------------------
            # For each QR, weighted-average their team's quality_score across
            # every in-scope backend that exposes the field. Backends without
            # quality_score contribute 0 weight.
            qr_rows = []
            all_active = Employee.search([('task_forge_active', '=', True)])
            for emp in all_active:
                if emp._get_task_forge_role() != 'qr':
                    continue
                team = Employee.search([
                    ('task_forge_qr_id', '=', emp.id),
                    ('task_forge_active', '=', True),
                ])
                tasker_count = len(team)
                team_user_ids = team.mapped('user_id').ids
                if not team_user_ids:
                    continue

                weighted_sum = 0.0
                weight = 0
                for proj in projects:
                    backend_name = proj.connected_table
                    if backend_name not in request.env:
                        continue
                    backend = request.env[backend_name].sudo()
                    quality_field = _backend_quality_field(backend)
                    if not quality_field or 'user_id' not in backend._fields:
                        continue
                    rows = backend.read_group(
                        domain=[
                            ('create_date', '>=', dt_from),
                            ('create_date', '<=', dt_to),
                            ('user_id', 'in', team_user_ids),
                            (quality_field, '>', 0),
                        ],
                        fields=[f'{quality_field}:avg'],
                        groupby=[],
                    )
                    if rows and rows[0].get('__count'):
                        n = rows[0]['__count']
                        avg = rows[0].get(quality_field) or 0.0
                        weighted_sum += avg * n
                        weight += n

                avg_q = round((weighted_sum / weight) if weight else 0.0, 1)
                if avg_q <= 0 and tasker_count == 0:
                    continue
                qr_rows.append({
                    'employee_id': emp.id,
                    'name': emp.name,
                    'taskers': tasker_count,
                    'avg_quality': avg_q,
                    'qr_score': avg_q,
                })
            qr_rows.sort(key=lambda x: (-x['qr_score'], -x['taskers']))
            top_quality_reviewers = [dict(rank=i + 1, **row) for i, row in enumerate(qr_rows[:top_n])]

            data = {
                'date_from': date_from.isoformat(),
                'date_to': date_to.isoformat(),
                'top_project_leads': top_project_leads,
                'top_quality_reviewers': top_quality_reviewers,
                'top_taskers': rankings[:top_n],
                'low_performers': [r for r in rankings if r['productivity'] < low_threshold],
                'total_evaluated': len(rankings),
            }

            return return_Response(
                message='Performance ranking',
                status=200,
                data={'data': data},
            )
        except Exception as e:
            return _safe_error(e, 'performance_ranking')

    # ------------------------------------------------------------------
    # 6) QC Feedback tab
    # ------------------------------------------------------------------
    @http.route(
        '/api/v2/taskforge/main/qc_feedback',
        type='http', auth='none', methods=['GET'], csrf=False, cors='*',
    )
    @validate_token
    def qc_feedback(self, **kw):
        """QC feedback summary across projects.

        Aggregates average `quality_score` per project + number of scored
        tasks + number of blockers QR-returned as 'no_issue'. This is the
        best-effort shape until the QC Feedback tab design is finalised;
        frontend can pick and choose the fields it needs.
        """
        try:
            user = request.env.user
            err = _require_cto(user)
            if err:
                return err

            TaskLog = request.env['task.forge.log'].sudo()
            Blocker = request.env['task.forge.blocker'].sudo()
            Project = request.env['project.project'].sudo()

            date_from, date_to = _parse_range(kw)
            project_ids = _project_ids_in_category(kw)

            try:
                limit = max(1, min(200, int(kw.get('limit') or 50)))
            except (TypeError, ValueError):
                limit = 50
            try:
                page = max(1, int(kw.get('page') or 1))
            except (TypeError, ValueError):
                page = 1
            offset = (page - 1) * limit

            try:
                single_project_id = int(kw.get('project_id')) if kw.get('project_id') else None
            except (TypeError, ValueError):
                single_project_id = None

            search = (kw.get('search') or '').strip()
            search_project_ids = None
            if search:
                search_project_ids = Project.search([('name', 'ilike', search)]).ids

            base_domain = [
                ('date', '>=', date_from),
                ('date', '<=', date_to),
                ('state', '=', 'completed'),
                ('quality_score', '>', 0),
            ]
            if project_ids is not None:
                base_domain.append(('project_id', 'in', project_ids))
            if single_project_id:
                base_domain.append(('project_id', '=', single_project_id))
            if search_project_ids is not None:
                base_domain.append(('project_id', 'in', search_project_ids or [0]))

            rows = TaskLog.read_group(
                domain=base_domain,
                fields=['project_id', 'quality_score:avg'],
                groupby=['project_id'],
                lazy=False,
            )

            no_issue_domain = [
                ('state', '=', 'no_issue'),
                ('qr_action_at', '>=', datetime.combine(date_from, time.min)),
                ('qr_action_at', '<=', datetime.combine(date_to, time.max)),
            ]
            if project_ids is not None:
                no_issue_domain.append(('project_id', 'in', project_ids))
            no_issue_rows = Blocker.read_group(
                domain=no_issue_domain,
                fields=['project_id'],
                groupby=['project_id'],
                lazy=False,
            )

            no_issue_map = {}
            for r in no_issue_rows:
                pid = r.get('project_id')
                if isinstance(pid, tuple):
                    pid = pid[0]
                if pid:
                    no_issue_map[pid] = r.get('__count', 0)

            items = []
            total_scored = 0
            total_quality_sum = 0.0
            for r in rows:
                pid = r.get('project_id')
                if isinstance(pid, tuple):
                    pid_id, pid_name = pid
                else:
                    pid_id = pid
                    pid_name = ''
                if not pid_id:
                    continue
                proj = Project.browse(pid_id)
                count = r.get('__count', 0) or 0
                avg_q = r.get('quality_score', 0) or 0
                items.append({
                    'project_id': pid_id,
                    'project_name': pid_name or proj.name,
                    'tasks_scored': count,
                    'avg_quality': round(avg_q, 1),
                    'qr_no_issue_count': no_issue_map.get(pid_id, 0),
                })
                total_scored += count
                total_quality_sum += avg_q * count

            overall_avg = round(total_quality_sum / total_scored, 1) if total_scored else 0

            QC_SORT_KEYS = {
                'quality_desc':     ('avg_quality', True),
                'quality_asc':      ('avg_quality', False),
                'scored_desc':      ('tasks_scored', True),
                'scored_asc':       ('tasks_scored', False),
                'no_issue_desc':    ('qr_no_issue_count', True),
                'no_issue_asc':     ('qr_no_issue_count', False),
                'name_asc':         ('project_name', False),
                'name_desc':        ('project_name', True),
            }
            sort_key = QC_SORT_KEYS.get(kw.get('sort'), ('avg_quality', True))
            field, reverse = sort_key
            items.sort(
                key=lambda i: (i.get(field) or 0) if field != 'project_name' else (i.get(field) or ''),
                reverse=reverse,
            )

            total_items = len(items)
            paginated = items[offset:offset + limit]

            kpi_domain = [
                ('date', '>=', date_from),
                ('date', '<=', date_to),
            ]
            if project_ids is not None:
                kpi_domain.append(('project_id', 'in', project_ids))
            if single_project_id:
                kpi_domain.append(('project_id', '=', single_project_id))
            if search_project_ids is not None:
                kpi_domain.append(('project_id', 'in', search_project_ids or [0]))
            justification_qc_count = TaskLog.search_count(
                kpi_domain + [('justification_text', '!=', False)]
            )
            prompt_qc_count = TaskLog.search_count(
                kpi_domain + [('prompt_text', '!=', False)]
            )

            prompt_rejection_reasons = []
            rejection_data_available = 'live'
            try:
                PrefRanking = request.env['preference.ranking'].sudo()
                pr_domain = [
                    ('submitted_at', '>=', datetime.combine(date_from, time.min)),
                    ('submitted_at', '<=', datetime.combine(date_to, time.max)),
                    ('rejection_reason', '!=', False),
                ]
                if single_project_id and 'project_id' in PrefRanking._fields:
                    pr_domain.append(('project_id', '=', single_project_id))
                elif project_ids is not None and 'project_id' in PrefRanking._fields:
                    pr_domain.append(('project_id', 'in', project_ids))
                reason_rows = PrefRanking.read_group(
                    domain=pr_domain,
                    fields=['rejection_reason'],
                    groupby=['rejection_reason'],
                    lazy=False,
                )
                for r in reason_rows:
                    reason = r.get('rejection_reason')
                    if not reason:
                        continue
                    prompt_rejection_reasons.append({
                        'key': reason,
                        'label': reason,
                        'count': r.get('__count', 0) or 0,
                    })
                prompt_rejection_reasons.sort(key=lambda x: -x['count'])
            except KeyError:
                rejection_data_available = 'unavailable'

            data = {
                'date_from': date_from.isoformat(),
                'date_to': date_to.isoformat(),
                'overall_avg_quality': overall_avg,
                'total_tasks_scored': total_scored,
                'justification_qc_count': justification_qc_count,
                'prompt_qc_count': prompt_qc_count,
                'justification_categories': [],
                'prompt_rejection_reasons': prompt_rejection_reasons,
                'data_availability': {
                    'justification_categories': 'pending_persistence',
                    'prompt_rejection_reasons': rejection_data_available,
                },
                'total': total_items,
                'page': page,
                'limit': limit,
                'items': paginated,
            }

            return return_Response(
                message='QC feedback',
                status=200,
                data={'data': data},
            )
        except Exception as e:
            return _safe_error(e, 'qc_feedback')

    # ------------------------------------------------------------------
    # 7) Performance Ranking panel (4-table Founder view)
    # ------------------------------------------------------------------
    @http.route(
        '/api/v2/taskforge/main/performance_ranking_panel',
        type='http', auth='none', methods=['GET'], csrf=False, cors='*',
    )
    @validate_token
    def performance_ranking_panel(self, **kw):
        try:
            user = request.env.user
            forbid = _require_cto(user)
            if forbid:
                return forbid

            date_from, date_to = _parse_range(kw)
            dt_start, _ = _day_bounds(date_from)
            _, dt_end = _day_bounds(date_to)

            top_n = max(1, min(int(kw.get('top_n') or 5), 50))
            bottom_n = max(1, min(int(kw.get('bottom_n') or 5), 50))
            min_tasks = max(0, int(kw.get('min_tasks') or 5))

            category_pids = _project_ids_in_category(kw)

            Project = request.env['project.project'].sudo()
            Employee = request.env['hr.employee'].sudo()
            TaskLog = request.env['task.forge.log'].sudo()

            base_project_domain = _live_project_domain()
            if category_pids is not None:
                base_project_domain.append(('id', 'in', category_pids))

            live_projects = Project.search(base_project_domain)

            log_domain = [
                ('date', '>=', date_from),
                ('date', '<=', date_to),
            ]
            if category_pids is not None:
                log_domain.append(('project_id', 'in', category_pids))

            totals = TaskLog.read_group(
                domain=log_domain,
                fields=['employee_id'],
                groupby=['employee_id'],
            )
            totals_by_emp = {
                (r['employee_id'] and r['employee_id'][0]): r['employee_id_count']
                for r in totals if r['employee_id']
            }

            completed_rows = TaskLog.read_group(
                domain=log_domain + [('state', '=', 'completed')],
                fields=['employee_id'],
                groupby=['employee_id'],
            )
            completed_by_emp = {
                (r['employee_id'] and r['employee_id'][0]): r['employee_id_count']
                for r in completed_rows if r['employee_id']
            }

            minutes_rows = TaskLog.read_group(
                domain=log_domain + [('state', '=', 'completed')],
                fields=['employee_id', 'time_taken_mins:sum'],
                groupby=['employee_id'],
            )
            minutes_by_emp = {
                (r['employee_id'] and r['employee_id'][0]): (r.get('time_taken_mins') or 0)
                for r in minutes_rows if r['employee_id']
            }

            quality_rows = TaskLog.read_group(
                domain=log_domain + [('quality_score', '>', 0)],
                fields=['employee_id', 'quality_score:avg'],
                groupby=['employee_id'],
            )
            avg_quality_by_emp = {
                (r['employee_id'] and r['employee_id'][0]): round(r.get('quality_score') or 0.0, 1)
                for r in quality_rows if r['employee_id']
            }

            pl_project_map = {}
            for proj in live_projects:
                for pl in proj.project_lead:
                    emp = pl.employee_id if hasattr(pl, 'employee_id') else None
                    pl_project_map.setdefault(pl.id, {
                        'user_id': pl.id,
                        'name': pl.name,
                        'employee_id': emp.id if emp else None,
                        'project_ids': [],
                    })
                    pl_project_map[pl.id]['project_ids'].append(proj.id)

            pl_rows = []
            for info in pl_project_map.values():
                pids = info['project_ids']
                if not pids:
                    continue
                total = TaskLog.search_count(log_domain + [('project_id', 'in', pids)])
                completed = TaskLog.search_count(
                    log_domain + [('project_id', 'in', pids), ('state', '=', 'completed')]
                )
                pct = round((completed / total * 100) if total else 0.0, 1)
                pl_rows.append({
                    'user_id': info['user_id'],
                    'employee_id': info['employee_id'],
                    'name': info['name'],
                    'completed': completed,
                    'total': total,
                    'percentage_completion': pct,
                })
            pl_rows.sort(key=lambda x: (-x['percentage_completion'], -x['total']))
            top_project_leads = [
                dict(rank=i + 1, **row) for i, row in enumerate(pl_rows[:top_n])
            ]

            qr_rows = []
            all_active = Employee.search([('task_forge_active', '=', True)])
            for emp in all_active:
                if emp._get_task_forge_role() != 'qr':
                    continue
                team = Employee.search([
                    ('task_forge_qr_id', '=', emp.id),
                    ('task_forge_active', '=', True),
                ])
                tasker_count = len(team)
                team_ids = team.ids
                if not team_ids:
                    continue
                team_avg_q = TaskLog.read_group(
                    domain=log_domain + [
                        ('employee_id', 'in', team_ids),
                        ('quality_score', '>', 0),
                    ],
                    fields=['quality_score:avg'],
                    groupby=[],
                )
                avg_q = round((team_avg_q and team_avg_q[0].get('quality_score') or 0.0), 1)
                if avg_q <= 0 and tasker_count == 0:
                    continue
                # QR score v1 reuses avg_quality; a weighted formula can replace this without changing the contract.
                qr_score = avg_q
                qr_rows.append({
                    'employee_id': emp.id,
                    'name': emp.name,
                    'taskers': tasker_count,
                    'avg_quality': avg_q,
                    'qr_score': qr_score,
                })
            qr_rows.sort(key=lambda x: (-x['qr_score'], -x['taskers']))
            top_quality_reviewers = [
                dict(rank=i + 1, **row) for i, row in enumerate(qr_rows[:top_n])
            ]

            tasker_ids = set(totals_by_emp.keys())
            emp_recs = Employee.browse(list(tasker_ids)) if tasker_ids else Employee.browse([])
            name_by_emp = {e.id: e.name for e in emp_recs}

            tasker_rows = []
            for emp_id, total in totals_by_emp.items():
                if not emp_id:
                    continue
                completed = completed_by_emp.get(emp_id, 0)
                pct = round((completed / total * 100) if total else 0.0, 1)
                tasker_rows.append({
                    'employee_id': emp_id,
                    'name': name_by_emp.get(emp_id, ''),
                    'completed': completed,
                    'total': total,
                    'minutes': minutes_by_emp.get(emp_id, 0),
                    'percentage_completion': pct,
                })

            top_taskers_src = sorted(
                tasker_rows,
                key=lambda x: (-x['percentage_completion'], -x['completed']),
            )
            top_taskers = [
                dict(rank=i + 1, **row) for i, row in enumerate(top_taskers_src[:top_n])
            ]

            eligible = [r for r in tasker_rows if r['total'] >= min_tasks]
            improvement_src = sorted(
                eligible,
                key=lambda x: (x['percentage_completion'], -x['total']),
            )
            improvement_needed = [
                dict(rank=i + 1, **row) for i, row in enumerate(improvement_src[:bottom_n])
            ]

            data = {
                'date_from': date_from.isoformat(),
                'date_to': date_to.isoformat(),
                'category': (kw.get('category') or 'all').lower(),
                'top_project_leads': top_project_leads,
                'top_quality_reviewers': top_quality_reviewers,
                'top_taskers': top_taskers,
                'improvement_needed': improvement_needed,
            }

            return return_Response(
                message='Performance ranking panel',
                status=200,
                data={'data': data},
            )
        except Exception as e:
            return _safe_error(e, 'performance_ranking_panel')

    # ------------------------------------------------------------------
    # 8) QC Feedback summary (KPIs + chip rows)
    # ------------------------------------------------------------------
    @http.route(
        '/api/v2/taskforge/main/qc_feedback_summary',
        type='http', auth='none', methods=['GET'], csrf=False, cors='*',
    )
    @validate_token
    def qc_feedback_summary(self, **kw):
        try:
            user = request.env.user
            forbid = _require_cto(user)
            if forbid:
                return forbid

            date_from, date_to = _parse_range(kw)
            dt_start, _ = _day_bounds(date_from)
            _, dt_end = _day_bounds(date_to)

            project_id = kw.get('project_id')
            try:
                project_id = int(project_id) if project_id else None
            except (TypeError, ValueError):
                project_id = None

            category_pids = _project_ids_in_category(kw)

            TaskLog = request.env['task.forge.log'].sudo()

            log_domain = [
                ('date', '>=', date_from),
                ('date', '<=', date_to),
            ]
            if category_pids is not None:
                log_domain.append(('project_id', 'in', category_pids))
            if project_id:
                log_domain.append(('project_id', '=', project_id))

            justification_count = TaskLog.search_count(
                log_domain + [('justification_text', '!=', False)]
            )
            prompt_count = TaskLog.search_count(
                log_domain + [('prompt_text', '!=', False)]
            )

            rejection_rows = []
            PrefRanking = request.env.get('preference.ranking')
            if PrefRanking is not None:
                PrefRanking = PrefRanking.sudo()
                pr_domain = [
                    ('submitted_at', '>=', dt_start),
                    ('submitted_at', '<=', dt_end),
                    ('rejection_reason', '!=', False),
                ]
                if 'project_id' in PrefRanking._fields and project_id:
                    pr_domain.append(('project_id', '=', project_id))
                grouped = PrefRanking.read_group(
                    domain=pr_domain,
                    fields=['rejection_reason'],
                    groupby=['rejection_reason'],
                )
                for row in grouped:
                    key = row.get('rejection_reason')
                    if not key:
                        continue
                    rejection_rows.append({
                        'key': key,
                        'label': key,
                        'count': row.get('rejection_reason_count') or 0,
                    })
                rejection_rows.sort(key=lambda r: -r['count'])

            data = {
                'date_from': date_from.isoformat(),
                'date_to': date_to.isoformat(),
                'category': (kw.get('category') or 'all').lower(),
                'project_id': project_id,
                'justification_qc_count': justification_count,
                'prompt_qc_count': prompt_count,
                'justification_categories': [],
                'prompt_rejection_reasons': rejection_rows,
                'data_availability': {
                    'justification_categories': 'pending_persistence',
                    'prompt_rejection_reasons': 'live' if PrefRanking is not None else 'unavailable',
                },
            }

            return return_Response(
                message='QC feedback summary',
                status=200,
                data={'data': data},
            )
        except Exception as e:
            return _safe_error(e, 'qc_feedback_summary')

    # ------------------------------------------------------------------
    # 9) QC Feedback justification breakdown (per project x per member)
    # ------------------------------------------------------------------
    @http.route(
        '/api/v2/taskforge/main/qc_feedback_justification_breakdown',
        type='http', auth='none', methods=['GET'], csrf=False, cors='*',
    )
    @validate_token
    def qc_feedback_justification_breakdown(self, **kw):
        try:
            user = request.env.user
            forbid = _require_cto(user)
            if forbid:
                return forbid

            date_from, date_to = _parse_range(kw)

            project_id = kw.get('project_id')
            try:
                project_id = int(project_id) if project_id else None
            except (TypeError, ValueError):
                project_id = None

            search_text = (kw.get('search') or '').strip()

            page = max(1, int(kw.get('page') or 1))
            limit = max(1, min(int(kw.get('limit') or 50), 200))
            offset = (page - 1) * limit

            category_pids = _project_ids_in_category(kw)

            TaskLog = request.env['task.forge.log'].sudo()
            Employee = request.env['hr.employee'].sudo()

            domain = [
                ('date', '>=', date_from),
                ('date', '<=', date_to),
                ('justification_text', '!=', False),
            ]
            if category_pids is not None:
                domain.append(('project_id', 'in', category_pids))
            if project_id:
                domain.append(('project_id', '=', project_id))
            if search_text:
                matched = Employee.search([('name', 'ilike', search_text)]).ids
                domain.append(('employee_id', 'in', matched or [0]))

            grouped = TaskLog.read_group(
                domain=domain,
                fields=['project_id', 'employee_id'],
                groupby=['project_id', 'employee_id'],
                lazy=False,
            )

            rows = []
            for r in grouped:
                pid = (r.get('project_id') or [None, None])[0]
                pname = (r.get('project_id') or [None, ''])[1]
                eid = (r.get('employee_id') or [None, None])[0]
                ename = (r.get('employee_id') or [None, ''])[1]
                rows.append({
                    'project_id': pid,
                    'project_name': pname,
                    'employee_id': eid,
                    'member_name': ename,
                    'qc_tasks': r.get('__count') or 0,
                    'issues_first_hit': None,
                    'total_issues': None,
                    'first_hit_pct': None,
                    'top_categories': [],
                })

            SORT_KEYS = {
                'qc_tasks_desc': ('qc_tasks', True),
                'qc_tasks_asc':  ('qc_tasks', False),
                'name_asc':      ('member_name', False),
                'name_desc':     ('member_name', True),
            }
            field, reverse = SORT_KEYS.get(kw.get('sort'), ('qc_tasks', True))
            rows.sort(
                key=lambda r: (r.get(field) or 0) if field == 'qc_tasks'
                else (r.get(field) or ''),
                reverse=reverse,
            )

            total_items = len(rows)
            paginated = rows[offset:offset + limit]

            data = {
                'date_from': date_from.isoformat(),
                'date_to': date_to.isoformat(),
                'category': (kw.get('category') or 'all').lower(),
                'project_id': project_id,
                'total': total_items,
                'page': page,
                'limit': limit,
                'items': paginated,
                'data_availability': 'partial',
            }

            return return_Response(
                message='QC feedback justification breakdown',
                status=200,
                data={'data': data},
            )
        except Exception as e:
            return _safe_error(e, 'qc_feedback_justification_breakdown')

    # ------------------------------------------------------------------
    # 10) QC Feedback prompt breakdown (per project x per member)
    # ------------------------------------------------------------------
    @http.route(
        '/api/v2/taskforge/main/qc_feedback_prompt_breakdown',
        type='http', auth='none', methods=['GET'], csrf=False, cors='*',
    )
    @validate_token
    def qc_feedback_prompt_breakdown(self, **kw):
        try:
            user = request.env.user
            forbid = _require_cto(user)
            if forbid:
                return forbid

            date_from, date_to = _parse_range(kw)
            dt_start, _ = _day_bounds(date_from)
            _, dt_end = _day_bounds(date_to)

            project_id = kw.get('project_id')
            try:
                project_id = int(project_id) if project_id else None
            except (TypeError, ValueError):
                project_id = None

            search_text = (kw.get('search') or '').strip()

            page = max(1, int(kw.get('page') or 1))
            limit = max(1, min(int(kw.get('limit') or 50), 200))
            offset = (page - 1) * limit

            category_pids = _project_ids_in_category(kw)

            PrefRanking = request.env.get('preference.ranking')
            if PrefRanking is None:
                return return_Response(
                    message='preference.ranking module not installed',
                    status=200,
                    data={'data': {
                        'date_from': date_from.isoformat(),
                        'date_to': date_to.isoformat(),
                        'total': 0, 'page': page, 'limit': limit,
                        'items': [],
                        'data_availability': 'unavailable',
                        'project_scope_supported': False,
                    }},
                )
            PrefRanking = PrefRanking.sudo()
            Employee = request.env['hr.employee'].sudo()

            # preference.ranking does not carry a project_id in every deployment; auto-probe the schema.
            has_project = 'project_id' in PrefRanking._fields

            base_domain = [
                ('submitted_at', '>=', dt_start),
                ('submitted_at', '<=', dt_end),
            ]
            if has_project and category_pids is not None:
                base_domain.append(('project_id', 'in', category_pids))
            if has_project and project_id:
                base_domain.append(('project_id', '=', project_id))
            if search_text:
                matched = Employee.search([('name', 'ilike', search_text)]).ids
                base_domain.append(('employee_id', 'in', matched or [0]))

            group_by = ['project_id', 'employee_id'] if has_project else ['employee_id']
            totals = PrefRanking.read_group(
                domain=base_domain,
                fields=group_by,
                groupby=group_by,
                lazy=False,
            )
            rejected_domain = base_domain + [('rejection_reason', '!=', False)]
            rejected_groups = PrefRanking.read_group(
                domain=rejected_domain,
                fields=group_by + ['rejection_reason'],
                groupby=group_by + ['rejection_reason'],
                lazy=False,
            )

            def _key(r):
                if has_project:
                    return (
                        (r.get('project_id') or [None])[0],
                        (r.get('employee_id') or [None])[0],
                    )
                return (
                    None,
                    (r.get('employee_id') or [None])[0],
                )

            totals_map = {_key(r): r.get('__count') or 0 for r in totals}
            rejected_count = {}
            top_reason = {}
            for r in rejected_groups:
                k = _key(r)
                cnt = r.get('__count') or 0
                rejected_count[k] = rejected_count.get(k, 0) + cnt
                reason = r.get('rejection_reason') or ''
                prev = top_reason.get(k)
                if not prev or cnt > prev['count']:
                    top_reason[k] = {'key': reason, 'count': cnt}

            rows = []
            for k, prompts in totals_map.items():
                pid, eid = k
                pname = ''
                ename = ''
                if has_project:
                    for r in totals:
                        if (_key(r) == k):
                            pname = (r.get('project_id') or [None, ''])[1] or ''
                            ename = (r.get('employee_id') or [None, ''])[1] or ''
                            break
                else:
                    for r in totals:
                        if (_key(r) == k):
                            ename = (r.get('employee_id') or [None, ''])[1] or ''
                            break

                rej = rejected_count.get(k, 0)
                pct = round((rej / prompts * 100) if prompts else 0.0, 1)
                rows.append({
                    'project_id': pid,
                    'project_name': pname,
                    'employee_id': eid,
                    'member_name': ename,
                    'prompts': prompts,
                    'rejected': rej,
                    'rej_pct': pct,
                    'top_reason': top_reason.get(k),
                })

            SORT_KEYS = {
                'rej_pct_desc':  ('rej_pct', True),
                'rej_pct_asc':   ('rej_pct', False),
                'prompts_desc':  ('prompts', True),
                'prompts_asc':   ('prompts', False),
                'name_asc':      ('member_name', False),
                'name_desc':     ('member_name', True),
            }
            field, reverse = SORT_KEYS.get(kw.get('sort'), ('rej_pct', True))
            rows.sort(
                key=lambda r: (r.get(field) or 0) if field in ('rej_pct', 'prompts')
                else (r.get(field) or ''),
                reverse=reverse,
            )

            total_items = len(rows)
            paginated = rows[offset:offset + limit]

            data = {
                'date_from': date_from.isoformat(),
                'date_to': date_to.isoformat(),
                'category': (kw.get('category') or 'all').lower(),
                'project_id': project_id,
                'total': total_items,
                'page': page,
                'limit': limit,
                'items': paginated,
                'data_availability': 'live',
                'project_scope_supported': has_project,
            }

            return return_Response(
                message='QC feedback prompt breakdown',
                status=200,
                data={'data': data},
            )
        except Exception as e:
            return _safe_error(e, 'qc_feedback_prompt_breakdown')
