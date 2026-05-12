import json
import logging
from pathlib import Path

from odoo import http
from odoo.http import request

from ..models.monitor_scan import _collect_run_detail_payload

_logger = logging.getLogger(__name__)


class ArcMonitorController(http.Controller):
    """JSON controller providing data for the ARC Monitor OWL dashboard."""

    @http.route('/arc_monitor/dashboard/data', type='jsonrpc', auth='user')
    def dashboard_data(self, scan_id=None, **kwargs):
        """Return scan data for the dashboard.

        If *scan_id* is given, return that scan's data; otherwise return the
        latest completed scan.
        """
        Scan = request.env['arc.monitor.scan']

        if scan_id:
            target = Scan.browse(int(scan_id)).exists()
        else:
            target = Scan.search([('state', '=', 'done')], limit=1)

        # Currently scanning (for polling indicator)
        active = Scan.search([('state', '=', 'scanning')], limit=1)
        # Recent scans for session selector
        recent = Scan.search([], limit=20)

        runs = []
        models_list = []
        games_list = []
        totals = {
            'runs': 0, 'solved': 0, 'partial': 0, 'failed': 0,
            'total_cost': 0, 'total_tokens': 0, 'total_elapsed_s': 0,
        }

        if target and target.runs_payload:
            try:
                runs = json.loads(target.runs_payload)
            except (json.JSONDecodeError, TypeError):
                runs = []
            models_list = sorted({r.get('model_name', '') for r in runs})
            games_list = sorted({r.get('game_id', '') for r in runs})
            totals = {
                'runs': target.total_runs,
                'solved': target.pass_count,
                'partial': target.partial_count,
                'failed': target.fail_count,
                'total_cost': target.total_cost,
                'total_tokens': target.total_tokens,
                'total_elapsed_s': target.total_elapsed,
            }

        sessions = []
        for s in recent:
            sessions.append({
                'id': s.id,
                'name': s.name or '',
                'state': s.state,
                'trigger': s.trigger or '',
                'source_type': s.source_type or '',
                'session_path': s.session_path or '',
                'total_runs': s.total_runs,
                'pass_count': s.pass_count,
                'partial_count': s.partial_count,
                'fail_count': s.fail_count,
                'total_cost': s.total_cost,
                'duration_seconds': s.duration_seconds,
                'scan_date': s.scan_date.isoformat() if s.scan_date else '',
                'create_date': s.create_date.isoformat() if s.create_date else '',
            })

        return {
            'runs': runs,
            'models': models_list,
            'games': games_list,
            'totals': totals,
            'sessions': sessions,
            'active_scan': {
                'id': active.id,
                'name': active.name or 'Scanning...',
            } if active else None,
            'latest_scan_id': target.id if target else None,
            'latest_scan_name': target.name if target else '',
            'latest_scan_path': target.session_path if target else '',
            'latest_scan_date': target.scan_date.isoformat() if target and target.scan_date else '',
            'latest_scan_trigger': target.trigger if target else '',
            'latest_scan_source_type': target.source_type if target else '',
            'latest_scan_duration': target.duration_seconds if target else 0,
        }

    @http.route('/arc_monitor/dashboard/run_detail', type='jsonrpc', auth='user')
    def run_detail(self, run_path='', game_id='', model_name='', scan_id=None, **kwargs):
        """Return detailed data for a specific run directory, read from filesystem."""
        if not run_path:
            return {'error': 'missing run_path'}

        # Security: validate run_path is under the scan's session_path
        source_path = ''
        if scan_id:
            scan = request.env['arc.monitor.scan'].browse(int(scan_id)).exists()
            if scan:
                source_path = scan.session_path or ''

        if not source_path:
            ICP = request.env['ir.config_parameter'].sudo()
            source_path = ICP.get_param('arc_monitor.source_path', default='')

        if source_path:
            try:
                candidate = Path(run_path).resolve()
                root_resolved = Path(source_path).resolve()
                candidate.relative_to(root_resolved)
            except (ValueError, OSError):
                return {'error': 'path outside configured source root'}

        candidate = Path(run_path)
        if not candidate.is_dir():
            return {'error': 'not a directory'}

        return _collect_run_detail_payload(
            candidate,
            game_id=game_id or None,
            model_name=model_name or None,
        )
