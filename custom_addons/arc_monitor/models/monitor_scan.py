import csv
import json
import logging
import os
import shutil
import statistics
import subprocess
import tempfile
import time
from pathlib import Path

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Filesystem parsing utilities (ported from puzzle_monitor.py)
# ---------------------------------------------------------------------------


def _read_jsonl(path):
    """Read a JSONL file, skipping malformed lines."""
    if not path.exists():
        return []
    out = []
    try:
        with path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return out


def _read_json(path):
    """Read a JSON file, returning {} on failure."""
    if not path.exists():
        return {}
    try:
        with path.open() as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _read_csv_file(path):
    """Read a CSV file as list of dicts."""
    if not path.exists():
        return []
    try:
        with path.open() as f:
            return list(csv.DictReader(f))
    except OSError:
        return []


def _read_session_log(run_dir, max_bytes=512_000):
    """Read session log, tail if too large."""
    log_path = run_dir / "logs" / "session.log"
    if not log_path.exists():
        return ""
    try:
        size = log_path.stat().st_size
        if size <= max_bytes:
            return log_path.read_text(errors="replace")
        with log_path.open("rb") as f:
            f.seek(size - max_bytes)
            raw = f.read()
        text = raw.decode("utf-8", errors="replace")
        first_nl = text.find("\n")
        if first_nl >= 0:
            text = text[first_nl + 1:]
        return text
    except OSError:
        return ""


def _percentiles(values):
    """Return (p50, p95, max). Empty list returns zeros."""
    if not values:
        return 0.0, 0.0, 0.0
    s = sorted(values)
    p50 = statistics.median(s)
    p95_idx = max(0, int(round(0.95 * (len(s) - 1))))
    return p50, s[p95_idx], s[-1]


def _discover_runs(root):
    """
    Iterate data/puzzle-evals/<timestamp>/<game>/<model>/ directories.
    Yields dicts with run_dir, game_id, model_name, model_dir paths.
    """
    root = Path(root)
    if not root.exists():
        return
    for run_dir in sorted(root.iterdir()):
        if not run_dir.is_dir():
            continue
        game_meta = _read_json(run_dir / "game_metadata.json")
        for game_dir in sorted(run_dir.iterdir()):
            if not game_dir.is_dir() or game_dir.name == "logs":
                continue
            game_level_meta = _read_json(game_dir / "metadata.json")
            for model_dir in sorted(game_dir.iterdir()):
                if not model_dir.is_dir() or model_dir.name == "traces":
                    continue
                try:
                    if not any(model_dir.iterdir()):
                        continue
                except PermissionError:
                    continue
                yield {
                    'run_dir': run_dir,
                    'game_id': game_dir.name,
                    'model_name': model_dir.name,
                    'model_dir': model_dir,
                    'game_meta': game_meta,
                    'game_level_meta': game_level_meta,
                }


def _load_run_data(run_dir, game_id, model_name, model_dir, game_meta, game_level_meta):
    """Load all data files for a single run entry."""
    return {
        'run_dir': run_dir,
        'game_id': game_id,
        'model_name': model_name,
        'model_dir': model_dir,
        'game_meta': game_meta,
        'game_level_meta': game_level_meta,
        'runs': _read_jsonl(model_dir / "runs.jsonl"),
        'steps': _read_jsonl(model_dir / "steps.jsonl"),
        'timing': _read_jsonl(model_dir / "timing.jsonl"),
        'skips': _read_jsonl(model_dir / "skips.jsonl"),
        'summary_rows': _read_csv_file(model_dir / "token_usage_summary.csv"),
        'session_log': _read_session_log(run_dir),
    }


def _run_summary_dict(rd):
    """Compute summary stats for a single game/model run entry."""
    runs = rd['runs']
    n_total = len(runs)
    n_solved = sum(1 for r in runs if r.get("solved"))
    avg_pct = (
        sum(float(r.get("final_score_pct", 0)) for r in runs) / n_total
    ) if n_total else 0.0
    total_steps = sum(int(r.get("total_steps", 0)) for r in runs)
    total_cost = sum(float(r.get("cost_usd", 0)) for r in runs)
    total_in = sum(int(r.get("total_input_tokens", 0) or 0) for r in runs)
    total_out = sum(int(r.get("total_output_tokens", 0) or 0) for r in runs)
    total_elapsed = sum(float(r.get("elapsed_seconds", 0)) for r in runs)

    if n_total > 0 and n_solved == n_total:
        status = "pass"
    elif n_solved > 0 or avg_pct > 0:
        status = "partial"
    else:
        status = "fail"

    return {
        "run_dir": rd['run_dir'].name,
        "run_path": str(rd['run_dir']),
        "game_id": rd['game_id'],
        "model_name": rd['model_name'],
        "model_dir_path": str(rd['model_dir']),
        "session_id": rd['game_meta'].get("sessionId"),
        "session_status": rd['game_meta'].get("status"),
        "timestamp": rd['game_meta'].get("timestamp"),
        "status": status,
        "solved": n_solved,
        "total_runs": n_total,
        "avg_score_pct": round(avg_pct, 2),
        "total_steps": total_steps,
        "total_cost": round(total_cost, 6),
        "total_input_tokens": total_in,
        "total_output_tokens": total_out,
        "total_tokens": total_in + total_out,
        "elapsed_seconds": round(total_elapsed, 2),
        "max_steps": rd['game_level_meta'].get("maxSteps"),
        "num_runs_config": rd['game_level_meta'].get("numRuns"),
        "seed_base": rd['game_level_meta'].get("seedBase"),
        "flagged_low_steps": any(
            int(r.get("total_steps", 0)) < 200 for r in runs
        ),
        "levels_summary": (
            f"{sum(int(r.get('levels_completed', 0)) for r in runs)}/"
            f"{sum(int(r.get('total_levels', 0)) for r in runs)} levels "
            f"across {n_total} runs"
        ),
        "avg_levels_completed": (
            round(sum(int(r.get("levels_completed", 0)) for r in runs) / n_total, 2)
            if n_total else 0
        ),
        "total_levels": max(
            (int(r.get("total_levels", 0)) for r in runs), default=0
        ),
    }


def _run_detail_dict(rd):
    """Compute full detail payload for a single game/model run entry."""
    timing = rd['timing']
    summary_rows = rd['summary_rows']

    api_vals = [
        float(t.get("api_call_ms", 0))
        for t in timing if t.get("api_call_ms") is not None
    ]
    game_vals = [
        float(t.get("game_step_ms", 0))
        for t in timing if t.get("game_step_ms") is not None
    ]
    api_p50, api_p95, api_max = _percentiles(api_vals)
    g_p50, g_p95, g_max = _percentiles(game_vals)

    total_in = sum(int(r.get("total_input_tokens", 0) or 0) for r in summary_rows)
    total_out = sum(int(r.get("total_output_tokens", 0) or 0) for r in summary_rows)
    total_reason = sum(int(r.get("total_reasoning_tokens", 0) or 0) for r in summary_rows)
    total_cached = sum(int(r.get("total_cached_input_tokens", 0) or 0) for r in summary_rows)
    total_cw = sum(int(r.get("total_cache_write_tokens", 0) or 0) for r in summary_rows)
    total_cost = sum(float(r.get("total_cost_usd", 0) or 0) for r in summary_rows)
    total_steps_sum = sum(int(r.get("total_steps", 0) or 0) for r in summary_rows)

    result = _run_summary_dict(rd)
    result.update({
        "session_log": rd['session_log'],
        "game_meta": rd['game_meta'],
        "game_level_meta": rd['game_level_meta'],
        "runs": rd['runs'],
        "steps": rd['steps'],
        "timing": timing,
        "skips": rd['skips'],
        "summary_rows": summary_rows,
        "timing_stats": {
            "api_call_count": len(api_vals),
            "api_p50_ms": round(api_p50, 1),
            "api_p95_ms": round(api_p95, 1),
            "api_max_ms": round(api_max, 1),
            "api_total_s": round(sum(api_vals) / 1000.0, 2),
            "game_p50_ms": round(g_p50, 2),
            "game_p95_ms": round(g_p95, 2),
            "game_max_ms": round(g_max, 2),
            "game_total_s": round(sum(game_vals) / 1000.0, 3),
        },
        "token_totals": {
            "input": total_in,
            "cached_input": total_cached,
            "cache_write": total_cw,
            "output": total_out,
            "reasoning": total_reason,
            "cost_usd": round(total_cost, 6),
            "total_steps": total_steps_sum,
            "cost_per_step": (
                round(total_cost / total_steps_sum, 6) if total_steps_sum else 0.0
            ),
            "in_tokens_per_step": (
                round(total_in / total_steps_sum, 1) if total_steps_sum else 0.0
            ),
        },
    })
    return result


def _collect_dashboard_payload(root):
    """Discover all runs and build the summary payload."""
    root = Path(root)
    summaries = []
    for entry in _discover_runs(root):
        rd = _load_run_data(**entry)
        summaries.append(_run_summary_dict(rd))

    summaries.sort(
        key=lambda s: s.get("timestamp") or s["run_dir"], reverse=True
    )
    models_list = sorted({s["model_name"] for s in summaries})
    games_list = sorted({s["game_id"] for s in summaries})

    totals = {
        "runs": len(summaries),
        "solved": sum(1 for s in summaries if s["status"] == "pass"),
        "partial": sum(1 for s in summaries if s["status"] == "partial"),
        "failed": sum(1 for s in summaries if s["status"] == "fail"),
        "total_cost": round(sum(s["total_cost"] for s in summaries), 4),
        "total_tokens": sum(s["total_tokens"] for s in summaries),
        "total_elapsed_s": round(
            sum(s["elapsed_seconds"] for s in summaries), 1
        ),
    }

    return {
        "root": str(root),
        "runs": summaries,
        "models": models_list,
        "games": games_list,
        "totals": totals,
    }


def _collect_run_detail_payload(run_dir, game_id=None, model_name=None):
    """Load detail data for a single timestamped run directory."""
    run_dir = Path(run_dir)
    if not run_dir.is_dir():
        return {"error": "not a directory", "entries": []}

    entries = []
    game_meta = _read_json(run_dir / "game_metadata.json")
    for game_dir in sorted(run_dir.iterdir()):
        if not game_dir.is_dir() or game_dir.name == "logs":
            continue
        if game_id and game_dir.name != game_id:
            continue
        game_level_meta = _read_json(game_dir / "metadata.json")
        for model_dir in sorted(game_dir.iterdir()):
            if not model_dir.is_dir() or model_dir.name == "traces":
                continue
            if model_name and model_dir.name != model_name:
                continue
            try:
                if not any(model_dir.iterdir()):
                    continue
            except PermissionError:
                continue
            rd = _load_run_data(
                run_dir=run_dir,
                game_id=game_dir.name,
                model_name=model_dir.name,
                model_dir=model_dir,
                game_meta=game_meta,
                game_level_meta=game_level_meta,
            )
            entries.append(_run_detail_dict(rd))

    return {
        "run_path": str(run_dir),
        "run_dir": run_dir.name,
        "found": len(entries),
        "entries": entries,
    }


# ---------------------------------------------------------------------------
# Odoo Model
# ---------------------------------------------------------------------------


class ArcMonitorScan(models.Model):
    _name = 'arc.monitor.scan'
    _description = 'ARC Monitor Scan'
    _inherit = ['mail.thread']
    _order = 'scan_date desc, id desc'

    name = fields.Char(
        string='Reference',
        compute='_compute_name',
        store=True,
    )
    state = fields.Selection([
        ('draft', 'Draft'),
        ('scanning', 'Scanning'),
        ('done', 'Done'),
        ('error', 'Error'),
    ], default='draft', readonly=True, tracking=True)

    # --- Source ---
    source_type = fields.Selection(
        selection=[
            ('local', 'Local Directory'),
            ('git', 'Git Repository'),
        ],
        default='local',
        required=True,
        string='Source Type',
    )
    session_path = fields.Char(
        string='Session Directory',
        help='Absolute path to the arc-explainer eval session directory '
             '(e.g. data/puzzle-evals/20260427_120000_001/).',
    )
    repo_url = fields.Char(
        string='Repository URL',
        help='HTTPS URL of the Git repository containing trajectory data.',
    )
    repo_token = fields.Char(
        string='Access Token',
        help='Personal access token for private repositories. '
             'Injected into the clone URL as https://<token>@host/...',
    )
    repo_branch = fields.Char(
        string='Branch',
        default='main',
        help='Branch to checkout after cloning.',
    )
    clone_path = fields.Char(
        string='Clone Directory',
        readonly=True,
        help='Temporary directory where the repo was cloned (auto-managed).',
    )

    scan_date = fields.Datetime(string="Scan Completed", readonly=True)
    trigger = fields.Selection([
        ('manual', 'Manual'),
        ('cron', 'Cron'),
    ], readonly=True)
    duration_seconds = fields.Float(string="Duration (s)", readonly=True)

    # Aggregated totals
    total_runs = fields.Integer(readonly=True)
    total_games = fields.Integer(readonly=True)
    total_models = fields.Integer(readonly=True)
    pass_count = fields.Integer(string="Passed", readonly=True)
    partial_count = fields.Integer(string="Partial", readonly=True)
    fail_count = fields.Integer(string="Failed", readonly=True)
    total_cost = fields.Float(string="Total Cost ($)", readonly=True, digits=(12, 4))
    total_tokens = fields.Integer(readonly=True)
    total_elapsed = fields.Float(
        string="Total Elapsed (s)", readonly=True, digits=(12, 1)
    )

    # Full payload stored as JSON
    runs_payload = fields.Text(
        string="Runs Payload (JSON)", readonly=True,
        help="JSON array of run summary dicts from last scan",
    )
    error_message = fields.Text(string="Error", readonly=True)

    # --- Computed ---

    @api.depends('session_path', 'repo_url', 'source_type', 'create_date')
    def _compute_name(self):
        for rec in self:
            if rec.source_type == 'git' and rec.repo_url:
                repo_name = rec.repo_url.rstrip('/').rsplit('/', 1)[-1].replace('.git', '')
                branch = rec.repo_branch or 'main'
                rec.name = f'{repo_name}:{branch}'
            elif rec.session_path:
                rec.name = os.path.basename(rec.session_path.rstrip('/'))
            elif rec.create_date:
                rec.name = _('Scan @ %s') % fields.Datetime.to_string(rec.create_date)
            else:
                rec.name = _('New Scan')

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def action_scan(self):
        """Trigger a manual scan."""
        self.ensure_one()
        if self.state == 'scanning':
            raise UserError(_('Scan is already running.'))

        if self.source_type == 'local':
            if not self.session_path or not os.path.isdir(self.session_path):
                raise UserError(
                    _('Session directory does not exist: %s') % self.session_path
                )
        elif self.source_type == 'git':
            if not self.repo_url:
                raise UserError(_('Repository URL is required for Git source.'))
        else:
            raise UserError(_('Unknown source type: %s') % self.source_type)

        self.write({
            'state': 'scanning',
            'error_message': False,
            'trigger': 'manual',
            'total_runs': 0,
            'total_games': 0,
            'total_models': 0,
            'pass_count': 0,
            'partial_count': 0,
            'fail_count': 0,
            'total_cost': 0,
            'total_tokens': 0,
            'total_elapsed': 0,
            'runs_payload': False,
        })
        self._run_scan()

    def action_reset_draft(self):
        """Reset to draft state."""
        self.ensure_one()
        self.write({
            'state': 'draft',
            'scan_date': False,
            'duration_seconds': 0,
            'total_runs': 0,
            'total_games': 0,
            'total_models': 0,
            'pass_count': 0,
            'partial_count': 0,
            'fail_count': 0,
            'total_cost': 0,
            'total_tokens': 0,
            'total_elapsed': 0,
            'runs_payload': False,
            'error_message': False,
        })

    # ------------------------------------------------------------------
    # Git clone
    # ------------------------------------------------------------------

    @staticmethod
    def _clone_repo(repo_url, token, branch):
        """Clone a Git repository to a temporary directory.

        Returns the path to the cloned directory.
        """
        clone_dir = tempfile.mkdtemp(prefix='arc_monitor_clone_')
        try:
            if token:
                if repo_url.startswith('https://'):
                    auth_url = repo_url.replace('https://', f'https://{token}@', 1)
                elif repo_url.startswith('http://'):
                    auth_url = repo_url.replace('http://', f'http://{token}@', 1)
                else:
                    auth_url = repo_url
            else:
                auth_url = repo_url

            cmd = [
                'git', 'clone',
                '--depth', '1',
                '--branch', branch,
                '--single-branch',
                auth_url,
                clone_dir,
            ]
            _logger.info('Cloning %s (branch=%s) into %s', repo_url, branch, clone_dir)
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=300,
            )
            if result.returncode != 0:
                stderr = result.stderr.strip()
                if token:
                    stderr = stderr.replace(token, '***')
                raise RuntimeError(
                    f'Git clone failed (exit {result.returncode}): {stderr}'
                )
            _logger.info('Clone complete: %s', clone_dir)
            return clone_dir
        except Exception:
            if os.path.isdir(clone_dir):
                shutil.rmtree(clone_dir, ignore_errors=True)
            raise

    # ------------------------------------------------------------------
    # Scan logic
    # ------------------------------------------------------------------

    def _run_scan(self):
        """Execute the filesystem scan and populate results."""
        self.ensure_one()
        t0 = time.time()
        clone_dir = None
        try:
            # Resolve source path
            if self.source_type == 'git':
                clone_dir = self._clone_repo(
                    self.repo_url,
                    self.repo_token,
                    self.repo_branch or 'main',
                )
                scan_path = clone_dir
            else:
                scan_path = self.session_path

            payload = _collect_dashboard_payload(scan_path)
            totals = payload['totals']
            write_vals = {
                'state': 'done',
                'scan_date': fields.Datetime.now(),
                'duration_seconds': round(time.time() - t0, 2),
                'total_runs': totals['runs'],
                'total_games': len(payload['games']),
                'total_models': len(payload['models']),
                'pass_count': totals['solved'],
                'partial_count': totals['partial'],
                'fail_count': totals['failed'],
                'total_cost': totals['total_cost'],
                'total_tokens': totals['total_tokens'],
                'total_elapsed': totals['total_elapsed_s'],
                'runs_payload': json.dumps(payload['runs'], default=str),
            }
            if clone_dir:
                write_vals['clone_path'] = clone_dir
                write_vals['session_path'] = clone_dir
            self.write(write_vals)
            self.message_post(
                body=f"Scan completed: {totals['runs']} runs, "
                     f"{totals['solved']} pass / {totals['partial']} partial / "
                     f"{totals['failed']} fail",
            )
        except Exception as e:
            _logger.exception("ARC Monitor scan failed")
            self.write({
                'state': 'error',
                'error_message': str(e),
                'duration_seconds': round(time.time() - t0, 2),
            })
        finally:
            if clone_dir and os.path.isdir(clone_dir):
                try:
                    shutil.rmtree(clone_dir)
                    _logger.info('Cleaned up clone dir: %s', clone_dir)
                except Exception:
                    _logger.warning(
                        'Failed to clean up clone dir: %s', clone_dir, exc_info=True
                    )

    # ------------------------------------------------------------------
    # Cron
    # ------------------------------------------------------------------

    @api.model
    def _cron_auto_scan(self):
        """Auto-scan cron: re-scans the most recent completed scan's source path if filesystem changed."""
        # Skip if a scan is already running
        running = self.search([('state', '=', 'scanning')], limit=1)
        if running:
            _logger.info("arc_monitor: scan already in progress, skipping")
            return

        # Use the last completed scan's source path
        last_scan = self.search(
            [('state', '=', 'done'), ('session_path', '!=', False)],
            order='scan_date desc',
            limit=1,
        )
        if not last_scan or not last_scan.session_path:
            _logger.info("arc_monitor: no previous scan found, skipping")
            return

        source_path = last_scan.session_path
        root = Path(source_path)
        if not root.is_dir():
            _logger.warning("arc_monitor: source path not a directory: %s", source_path)
            return

        # Check mtime vs last scan
        try:
            current_mtime = root.stat().st_mtime
        except OSError:
            return

        if last_scan.scan_date:
            last_ts = last_scan.scan_date.timestamp()
            if current_mtime <= last_ts:
                return

        # Create and run scan using same source config
        vals = {
            'source_type': last_scan.source_type,
            'session_path': source_path,
            'state': 'scanning',
            'trigger': 'cron',
        }
        if last_scan.source_type == 'git':
            vals.update({
                'repo_url': last_scan.repo_url,
                'repo_token': last_scan.repo_token,
                'repo_branch': last_scan.repo_branch,
            })
        scan = self.create(vals)
        scan._run_scan()
