import logging

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)

# States that represent a completed QC run (verdict is now the state itself)
_COMPLETED_STATES = ('ship', 'conditional_ship', 'blocked')


class ArcQcDashboardController(http.Controller):
    """JSON controller providing data for the ARC QC OWL dashboard."""

    @http.route('/arc_qc/dashboard/data', type='json', auth='user')
    def dashboard_data(self, **kwargs):
        """Return aggregated QC dashboard statistics."""
        Session = request.env['arc.qc.session']
        GameResult = request.env['arc.qc.game.result']

        # All completed sessions
        done_sessions = Session.search([('state', 'in', _COMPLETED_STATES)])
        all_sessions = Session.search([], order='create_date desc', limit=20)

        # --- KPIs ---
        total_sessions = len(done_sessions)
        total_games = sum(s.games_checked for s in done_sessions)
        total_runs = sum(s.runs_checked for s in done_sessions)
        total_steps = sum(s.steps_checked for s in done_sessions)

        ship_count = len(done_sessions.filtered(lambda s: s.state == 'ship'))
        block_count = len(done_sessions.filtered(lambda s: s.state == 'blocked'))
        conditional_count = len(done_sessions.filtered(lambda s: s.state == 'conditional_ship'))

        # Overall pass rate (sessions)
        session_pass_rate = round(100 * ship_count / total_sessions) if total_sessions else 0

        # Game-level pass rate from latest session
        latest_done = Session.search(
            [('state', 'in', _COMPLETED_STATES)], order='create_date desc', limit=1
        )
        latest_game_pass_rate = 0
        latest_games_total = 0
        latest_games_passed = 0
        game_results_all = GameResult  # empty recordset
        if latest_done:
            game_results_all = GameResult.search([('session_id', '=', latest_done.id)])
            latest_games_total = len(game_results_all)
            latest_games_passed = len(game_results_all.filtered(lambda g: g.verdict == 'ship'))
            latest_game_pass_rate = round(
                100 * latest_games_passed / latest_games_total
            ) if latest_games_total else 0

        kpis = {
            'total_sessions': total_sessions,
            'total_games': total_games,
            'total_runs': total_runs,
            'total_steps': total_steps,
            'ship_count': ship_count,
            'block_count': block_count,
            'conditional_count': conditional_count,
            'session_pass_rate': session_pass_rate,
            'latest_game_pass_rate': latest_game_pass_rate,
            'latest_games_total': latest_games_total,
            'latest_games_passed': latest_games_passed,
        }

        # --- Recent Sessions ---
        sessions_data = []
        for s in all_sessions:
            sessions_data.append({
                'id': s.id,
                'name': s.name or '',
                'state': s.state,
                'games_checked': s.games_checked,
                'critical_count': s.critical_count,
                'high_count': s.high_count,
                'medium_count': s.medium_count,
                'low_count': s.low_count,
                'duration_seconds': s.duration_seconds,
                'create_date': s.create_date.isoformat() if s.create_date else '',
            })

        # --- Per-Game Verdicts (from latest session) ---
        game_verdicts = []
        if game_results_all:
            for gr in game_results_all.sorted('game_id'):
                game_verdicts.append({
                    'game_id': gr.game_id,
                    'verdict': gr.verdict,
                    'critical': gr.critical_count,
                    'high': gr.high_count,
                    'medium': gr.medium_count,
                    'low': gr.low_count,
                })

        # --- Verdict Breakdown (from latest session) ---
        verdict_breakdown = []
        if game_results_all:
            v_ship = len(game_results_all.filtered(lambda g: g.verdict == 'ship'))
            v_cond = len(game_results_all.filtered(lambda g: g.verdict == 'conditional_ship'))
            v_block = len(game_results_all.filtered(lambda g: g.verdict == 'block'))
            verdict_breakdown = [
                {'label': 'SHIP', 'value': v_ship, 'color': '#28a745'},
                {'label': 'CONDITIONAL', 'value': v_cond, 'color': '#fd7e14'},
                {'label': 'BLOCK', 'value': v_block, 'color': '#dc3545'},
            ]

        # --- Severity Totals (from latest session) ---
        severity_totals = {'critical': 0, 'high': 0, 'medium': 0, 'low': 0}
        if latest_done:
            severity_totals = {
                'critical': latest_done.critical_count,
                'high': latest_done.high_count,
                'medium': latest_done.medium_count,
                'low': latest_done.low_count,
            }

        # --- Top Finding Codes (from latest session) ---
        top_findings = []
        if latest_done:
            Finding = request.env['arc.qc.finding']
            grouped = Finding.read_group(
                domain=[('session_id', '=', latest_done.id)],
                fields=['code', 'severity'],
                groupby=['code', 'severity'],
                lazy=False,
            )
            sorted_groups = sorted(grouped, key=lambda g: g['__count'], reverse=True)[:10]
            for g in sorted_groups:
                top_findings.append({
                    'code': g['code'],
                    'severity': g['severity'],
                    'count': g['__count'],
                })

        # --- Model Health (from latest session) ---
        model_health = []
        if latest_done:
            ModelResult = request.env['arc.qc.model.result']
            model_results = ModelResult.search([('session_id', '=', latest_done.id)])
            model_data = {}
            for mr in model_results:
                key = mr.model_dir
                if key not in model_data:
                    model_data[key] = {'total': 0, 'passing': 0}
                model_data[key]['total'] += 1
                if mr.game_result_id.verdict == 'ship':
                    model_data[key]['passing'] += 1
            for model_dir in sorted(model_data):
                d = model_data[model_dir]
                model_health.append({
                    'model_dir': model_dir,
                    'total_games': d['total'],
                    'passing': d['passing'],
                    'pass_rate': round(100 * d['passing'] / d['total']) if d['total'] else 0,
                })

        # --- Session Trend (last 5 completed) ---
        session_trend = []
        trend_sessions = Session.search(
            [('state', 'in', _COMPLETED_STATES)],
            order='create_date desc',
            limit=5,
        )
        for s in reversed(trend_sessions):  # chronological order
            gr_count = GameResult.search_count([('session_id', '=', s.id)])
            gr_passed = GameResult.search_count([
                ('session_id', '=', s.id), ('verdict', '=', 'ship')
            ])
            session_trend.append({
                'id': s.id,
                'name': s.name or '',
                'state': s.state,
                'games_checked': s.games_checked,
                'games_passed': gr_passed,
                'create_date': s.create_date.strftime('%b %d') if s.create_date else '',
            })

        # --- Findings by Phase (from latest session) ---
        phase_findings = []
        if latest_done:
            FindingModel = request.env['arc.qc.finding']
            grouped = FindingModel.read_group(
                domain=[('session_id', '=', latest_done.id)],
                fields=['phase', 'severity'],
                groupby=['phase', 'severity'],
                lazy=False,
            )
            phase_data = {}
            for g in grouped:
                phase = g['phase'] or 'unknown'
                sev = g['severity']
                count = g['__count']
                if phase not in phase_data:
                    phase_data[phase] = {'critical': 0, 'standard': 0, 'advisory': 0, 'total': 0}
                phase_data[phase][sev] = count
                phase_data[phase]['total'] += count
            for phase in sorted(phase_data, key=lambda p: phase_data[p]['total'], reverse=True):
                d = phase_data[phase]
                phase_findings.append({
                    'phase': phase,
                    'critical': d['critical'],
                    'standard': d['standard'],
                    'advisory': d['advisory'],
                    'total': d['total'],
                })

        return {
            'kpis': kpis,
            'sessions': sessions_data,
            'game_verdicts': game_verdicts,
            'verdict_breakdown': verdict_breakdown,
            'severity_totals': severity_totals,
            'top_findings': top_findings,
            'model_health': model_health,
            'session_trend': session_trend,
            'phase_findings': phase_findings,
        }
