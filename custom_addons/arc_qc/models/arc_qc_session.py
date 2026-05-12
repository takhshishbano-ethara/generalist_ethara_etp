import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.parse

from markupsafe import Markup, escape

from ._constants import SEV_COLORS

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from ..engine import run_qc, QcConfig, QcResult, GameResult, Verdict

_logger = logging.getLogger(__name__)


class ArcQcSession(models.Model):
    """Top-level QC validation session.

    Each record represents one QC run over a session directory produced
    by the arc-explainer eval harness.
    """

    _name = 'arc.qc.session'
    _description = 'ARC QC Session'
    _order = 'create_date desc'
    _inherit = ['mail.thread']

    name = fields.Char(
        string='Reference',
        compute='_compute_name',
        store=True,
    )
    state = fields.Selection(
        selection=[
            ('draft', 'Draft'),
            ('running', 'Running'),
            ('ship', 'SHIP'),
            ('conditional_ship', 'CONDITIONAL SHIP'),
            ('blocked', 'BLOCKED'),
            ('error', 'Error'),
        ],
        default='draft',
        required=True,
    )

    # --- Input ---
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
        help='HTTPS URL of the Git repository containing trajectory data '
             '(e.g. https://github.com/org/arc-trajectories).',
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
    # --- Config ---
    expected_runs = fields.Integer(
        string='Expected Runs per Model',
        default=3,
    )
    max_steps = fields.Integer(
        string='Max Steps',
        default=200,
    )
    skip_content_safety = fields.Boolean(
        string='Skip Content Safety',
        default=False,
    )
    skip_smell_tests = fields.Boolean(
        string='Skip Smell Tests',
        default=False,
    )

    # --- Results ---
    games_checked = fields.Integer(readonly=True)
    models_checked = fields.Integer(readonly=True)
    runs_checked = fields.Integer(readonly=True)
    steps_checked = fields.Integer(readonly=True)
    duration_seconds = fields.Float(readonly=True, digits=(10, 3))

    critical_count = fields.Integer(string='Critical', readonly=True)
    high_count = fields.Integer(string='High', readonly=True)
    medium_count = fields.Integer(string='Medium', readonly=True)
    low_count = fields.Integer(string='Low', readonly=True)
    total_findings = fields.Integer(
        compute='_compute_total_findings',
        store=True,
    )

    finding_ids = fields.One2many(
        comodel_name='arc.qc.finding',
        inverse_name='session_id',
        string='Findings',
        readonly=True,
    )
    game_result_ids = fields.One2many(
        comodel_name='arc.qc.game.result',
        inverse_name='session_id',
        string='Game Results',
        readonly=True,
    )

    summary_html = fields.Html(
        string='Summary',
        readonly=True,
        sanitize=False,
    )

    error_message = fields.Text(readonly=True)

    # --- Computed ---

    @api.depends('session_path', 'repo_url', 'source_type', 'create_date')
    def _compute_name(self):
        for rec in self:
            if rec.source_type == 'git' and rec.repo_url:
                # Use repo name + branch as reference
                repo_name = rec.repo_url.rstrip('/').rsplit('/', 1)[-1].replace('.git', '')
                branch = rec.repo_branch or 'main'
                rec.name = f'{repo_name}:{branch}'
            elif rec.session_path:
                rec.name = os.path.basename(rec.session_path.rstrip('/'))
            elif rec.create_date:
                rec.name = _('QC @ %s') % fields.Datetime.to_string(rec.create_date)
            else:
                rec.name = _('New QC Session')

    @api.depends('critical_count', 'high_count', 'medium_count', 'low_count')
    def _compute_total_findings(self):
        for rec in self:
            rec.total_findings = (
                rec.critical_count + rec.high_count
                + rec.medium_count + rec.low_count
            )

    # --- Actions ---

    def action_run_qc(self):
        """Launch QC validation in a background thread."""
        self.ensure_one()
        if self.state == 'running':
            raise UserError(_('QC is already running.'))

        if self.source_type == 'local':
            if not self.session_path or not os.path.isdir(self.session_path):
                raise UserError(_('Session directory does not exist: %s') % self.session_path)
        elif self.source_type == 'git':
            if not self.repo_url:
                raise UserError(_('Repository URL is required for Git source.'))
        else:
            raise UserError(_('Unknown source type: %s') % self.source_type)

        self.write({
            'state': 'running',
            'error_message': False,
            'critical_count': 0,
            'high_count': 0,
            'medium_count': 0,
            'low_count': 0,
            'games_checked': 0,
            'models_checked': 0,
            'runs_checked': 0,
            'steps_checked': 0,
            'duration_seconds': 0,
        })

        # Fire and forget in a thread
        thread = threading.Thread(
            target=self._run_qc_thread,
            args=(self.id,),
            daemon=True,
        )
        thread.start()

    def _run_qc_thread(self, session_id):
        """Execute QC engine in a background thread and write results back."""
        # Wait for the main HTTP transaction to commit and release locks
        time.sleep(2)
        with self.pool.cursor() as cr:
            env = api.Environment(cr, self.env.uid, self.env.context)
            rec = env['arc.qc.session'].browse(session_id)
            clone_dir = None
            try:
                rec.finding_ids.unlink()
                rec.game_result_ids.unlink()

                # Re-read fresh values from DB
                source_type = rec.source_type
                repo_url = rec.repo_url
                repo_token = rec.repo_token
                repo_branch = rec.repo_branch or 'main'
                session_path = rec.session_path

                # Resolve the session_path
                if source_type == 'git':
                    clone_dir = self._clone_repo(repo_url, repo_token, repo_branch)
                    session_path = clone_dir

                config = QcConfig(
                    expected_runs_per_model=rec.expected_runs,
                    max_steps=rec.max_steps,
                    skip_content_safety=rec.skip_content_safety,
                    skip_smell_tests=rec.skip_smell_tests,
                )
                result = run_qc(session_path, config)
                _logger.info('QC engine completed for session %s: %s findings, verdict=%s',
                             session_id, len(result.findings), result.verdict.value)
                rec._apply_result(result)
                _logger.info('Applied QC result for session %s', session_id)
                # Store clone_path for reference
                if clone_dir:
                    rec.write({'clone_path': clone_dir, 'session_path': clone_dir})
                cr.commit()
            except Exception as exc:
                cr.rollback()
                _logger.exception('QC engine error for session %s', session_id)
                try:
                    rec.write({
                        'state': 'error',
                        'error_message': str(exc),
                    })
                    rec.message_post(
                        body=Markup(f'<strong>QC Error</strong><br/>{str(exc)[:500]}'),
                        message_type='comment',
                        subtype_xmlid='mail.mt_note',
                    )
                    cr.commit()
                except Exception:
                    _logger.exception('Failed to write error state for session %s', session_id)
            finally:
                # Clean up clone directory
                if clone_dir and os.path.isdir(clone_dir):
                    try:
                        shutil.rmtree(clone_dir)
                        _logger.info('Cleaned up clone dir: %s', clone_dir)
                    except Exception:
                        _logger.warning('Failed to clean up clone dir: %s', clone_dir, exc_info=True)

    @staticmethod
    def _clone_repo(repo_url, token, branch):
        """Clone a Git repository to a temporary directory.

        Returns the path to the cloned directory.
        """
        clone_dir = tempfile.mkdtemp(prefix='arc_qc_clone_')
        try:
            # Inject token into URL for authentication
            if token:
                # https://github.com/org/repo → https://<token>@github.com/org/repo
                safe_token = urllib.parse.quote(token, safe='')
                if repo_url.startswith('https://'):
                    auth_url = repo_url.replace('https://', f'https://{safe_token}@', 1)
                elif repo_url.startswith('http://'):
                    auth_url = repo_url.replace('http://', f'http://{safe_token}@', 1)
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
                timeout=300,  # 5 minute timeout
                env={
                    'PATH': os.environ.get('PATH', '/usr/bin:/bin'),
                    'GIT_TERMINAL_PROMPT': '0',
                },
            )
            if result.returncode != 0:
                stderr = result.stderr.strip()
                # Redact token from error messages
                if token:
                    stderr = stderr.replace(token, '***')
                raise RuntimeError(
                    f'Git clone failed (exit {result.returncode}): {stderr}'
                )
            _logger.info('Clone complete: %s', clone_dir)
            return clone_dir
        except Exception:
            # Clean up on failure
            if os.path.isdir(clone_dir):
                shutil.rmtree(clone_dir, ignore_errors=True)
            raise

    def _apply_result(self, result: QcResult):
        """Store QcResult into Odoo records."""
        self.ensure_one()

        # Create game results FIRST so we have IDs for finding linkage
        GameResult = self.env['arc.qc.game.result']
        game_result_map = {}  # engine game_id -> Odoo record id
        for gr in result.game_results:
            rec = GameResult.create({
                'session_id': self.id,
                'game_id': gr.game_id,
                'game_path': gr.game_path,
                'verdict': gr.verdict.value,
                'models_found': gr.models_found,
                'models_expected': gr.models_expected,
                'runs_checked': gr.runs_checked,
                'steps_checked': gr.steps_checked,
                'critical_count': gr.critical_count,
                'high_count': gr.high_count,
                'medium_count': gr.medium_count,
                'low_count': gr.low_count,
            })
            game_result_map[gr.game_id] = rec.id

        # Create findings in batches to avoid ORM overload
        BATCH_SIZE = 1000
        finding_vals = []
        for f in result.findings:
            finding_vals.append({
                'session_id': self.id,
                'game_result_id': game_result_map.get(f.game_id, False),
                'severity': f.severity.value,
                'phase': f.phase,
                'code': f.code,
                'game_id': f.game_id,
                'message': f.message,
                'file_path': f.file_path or '',
                'line_number': f.line_number or 0,
                'field_name': f.field_name or '',
                'expected': f.expected or '',
                'actual': f.actual or '',
                'spec_ref': f.spec_ref or '',
            })

        Finding = self.env['arc.qc.finding']
        for i in range(0, len(finding_vals), BATCH_SIZE):
            batch = finding_vals[i:i + BATCH_SIZE]
            Finding.create(batch)
        _logger.info('Created %d findings in %d batches', len(finding_vals), (len(finding_vals) + BATCH_SIZE - 1) // BATCH_SIZE)

        # Build summary HTML
        summary = self._build_summary_html(result)

        # Map engine verdict to state
        verdict_to_state = {
            'ship': 'ship',
            'conditional_ship': 'conditional_ship',
            'block': 'blocked',
        }
        final_state = verdict_to_state[result.verdict.value]

        self.write({
            'state': final_state,
            'summary_html': summary,
            'games_checked': result.games_checked,
            'models_checked': result.models_checked,
            'runs_checked': result.runs_checked,
            'steps_checked': result.steps_checked,
            'duration_seconds': result.duration_seconds,
            'critical_count': result.critical_count,
            'high_count': result.high_count,
            'medium_count': result.medium_count,
            'low_count': result.low_count,
        })

        # Post a clean chatter message with the result
        state_labels = {'ship': 'SHIP ✓', 'conditional_ship': 'CONDITIONAL SHIP ⚠', 'blocked': 'BLOCKED ✗'}
        label = state_labels.get(final_state, final_state)
        body = (
            f'<strong>QC Complete — {label}</strong><br/>'
            f'{result.games_checked} games · {result.runs_checked} runs · '
            f'{result.steps_checked:,} steps · {result.duration_seconds:.1f}s<br/>'
        )
        if result.critical_count:
            body += f'<span style="color:#dc3545;font-weight:600;">{result.critical_count:,} critical</span> '
        if result.high_count:
            body += f'<span style="color:#fd7e14;font-weight:600;">{result.high_count:,} high</span> '
        if result.medium_count:
            body += f'<span style="color:#ffc107;font-weight:600;">{result.medium_count:,} medium</span> '
        if result.low_count:
            body += f'<span style="color:#6c757d;">{result.low_count:,} low</span>'
        if not (result.critical_count or result.high_count or result.medium_count or result.low_count):
            body += '<span style="color:#28a745;font-weight:600;">No findings</span>'
        self.message_post(body=Markup(body), message_type='comment', subtype_xmlid='mail.mt_note')

    @staticmethod
    def _build_summary_html(result: QcResult) -> str:
        """Build a rich HTML summary of the QC run."""
        from collections import Counter

        # Severity breakdown
        total = (
            result.critical_count + result.high_count
            + result.medium_count + result.low_count
        )

        # Top finding codes
        code_counter = Counter()
        for f in result.findings:
            code_counter[(f.code, f.severity.value)] += 1

        # Per-game pass rate
        games_passed = sum(1 for gr in result.game_results if gr.verdict.value == 'ship')
        games_total = len(result.game_results)
        pass_pct = round(100 * games_passed / games_total) if games_total else 0

        # Unique model names (from game results model_dirs)
        unique_models = set()
        for gr in result.game_results:
            for mdir in gr.model_dirs:
                unique_models.add(mdir.model_name)

        # Build HTML
        lines = []
        lines.append('<div class="arc-qc-summary">')

        # Stats bar
        lines.append('<div style="display:flex;gap:24px;margin-bottom:16px;">')
        lines.append(f'<div><strong>{result.games_checked}</strong> games</div>')
        lines.append(f'<div><strong>{len(unique_models)}</strong> models</div>')
        lines.append(f'<div><strong>{result.runs_checked}</strong> runs</div>')
        lines.append(f'<div><strong>{result.steps_checked:,}</strong> steps</div>')
        lines.append(f'<div><strong>{result.duration_seconds:.1f}s</strong> duration</div>')
        lines.append('</div>')

        # Severity badges
        lines.append('<div style="margin-bottom:16px;">')
        if result.critical_count:
            lines.append(
                f'<span style="background:#dc3545;color:#fff;padding:2px 8px;'
                f'border-radius:4px;margin-right:8px;font-weight:bold;">'
                f'{result.critical_count:,} Critical</span>'
            )
        if result.high_count:
            lines.append(
                f'<span style="background:#fd7e14;color:#fff;padding:2px 8px;'
                f'border-radius:4px;margin-right:8px;font-weight:bold;">'
                f'{result.high_count:,} High</span>'
            )
        if result.medium_count:
            lines.append(
                f'<span style="background:#ffc107;color:#000;padding:2px 8px;'
                f'border-radius:4px;margin-right:8px;font-weight:bold;">'
                f'{result.medium_count:,} Medium</span>'
            )
        if result.low_count:
            lines.append(
                f'<span style="background:#6c757d;color:#fff;padding:2px 8px;'
                f'border-radius:4px;margin-right:8px;">'
                f'{result.low_count:,} Low</span>'
            )
        if not total:
            lines.append('<span style="color:#28a745;font-weight:bold;">No findings</span>')
        lines.append('</div>')

        # Pass rate
        lines.append('<div style="margin-bottom:16px;">')
        bar_color = '#28a745' if pass_pct == 100 else '#dc3545' if pass_pct == 0 else '#fd7e14'
        lines.append(
            f'<div style="background:#e9ecef;border-radius:4px;height:20px;width:300px;'
            f'position:relative;display:inline-block;vertical-align:middle;">'
            f'<div style="background:{bar_color};height:100%;width:{pass_pct}%;'
            f'border-radius:4px;"></div></div>'
            f' <strong>{games_passed}/{games_total}</strong> games passed ({pass_pct}%)'
        )
        lines.append('</div>')

        # Per-game verdict grid with checkmarks
        if result.game_results:
            lines.append('<h4 style="margin-top:20px;margin-bottom:8px;">Per-Game Verdicts</h4>')
            lines.append(
                '<div style="display:flex;flex-wrap:wrap;gap:6px;margin-bottom:16px;">'
            )
            sorted_games = sorted(result.game_results, key=lambda g: (g.verdict.value != 'ship', g.game_id))
            for gr in sorted_games:
                if gr.verdict.value == 'ship':
                    icon = '<span style="color:#28a745;font-weight:bold;">&#10003;</span>'
                    bg = '#d4edda'
                    border = '#28a745'
                elif gr.verdict.value == 'conditional_ship':
                    icon = '<span style="color:#fd7e14;font-weight:bold;">&#9888;</span>'
                    bg = '#fff3cd'
                    border = '#fd7e14'
                else:
                    icon = '<span style="color:#dc3545;font-weight:bold;">&#10007;</span>'
                    bg = '#f8d7da'
                    border = '#dc3545'
                lines.append(
                    f'<div style="background:{bg};border:1px solid {border};'
                    f'border-radius:4px;padding:3px 8px;font-size:12px;'
                    f'display:inline-flex;align-items:center;gap:4px;">'
                    f'{icon} <span style="font-family:monospace;">{escape(gr.game_id)}</span>'
                    f'</div>'
                )
            lines.append('</div>')

        # Phase breakdown
        phase_counter = Counter()
        for f in result.findings:
            phase_counter[f.phase] += 1
        if phase_counter:
            lines.append('<h4 style="margin-top:20px;margin-bottom:8px;">Findings by Phase</h4>')
            lines.append('<div style="display:flex;flex-wrap:wrap;gap:8px;margin-bottom:16px;">')
            for phase, count in sorted(phase_counter.items(), key=lambda x: -x[1]):
                lines.append(
                    f'<div style="background:#f8f9fa;border:1px solid #dee2e6;'
                    f'border-radius:4px;padding:4px 12px;font-size:13px;">'
                    f'<strong>{escape(phase)}</strong>: {count:,}'
                    f'</div>'
                )
            lines.append('</div>')

        # Top findings table
        if code_counter:
            top_codes = code_counter.most_common(15)
            lines.append('<h4 style="margin-top:16px;margin-bottom:8px;">Top Finding Codes</h4>')
            lines.append('<table style="border-collapse:collapse;width:100%;margin-top:8px;">')
            lines.append(
                '<thead><tr style="border-bottom:2px solid #dee2e6;">'
                '<th style="text-align:left;padding:4px 8px;">Code</th>'
                '<th style="text-align:left;padding:4px 8px;">Severity</th>'
                '<th style="text-align:right;padding:4px 8px;">Count</th>'
                '</tr></thead><tbody>'
            )
            for (code, sev), count in top_codes:
                color = SEV_COLORS.get(sev, '#000')
                lines.append(
                    f'<tr style="border-bottom:1px solid #dee2e6;">'
                    f'<td style="padding:4px 8px;font-family:monospace;">{escape(code)}</td>'
                    f'<td style="padding:4px 8px;color:{color};font-weight:bold;">'
                    f'{sev.upper()}</td>'
                    f'<td style="padding:4px 8px;text-align:right;">{count:,}</td>'
                    f'</tr>'
                )
            lines.append('</tbody></table>')

        lines.append('</div>')
        return '\n'.join(lines)

    def action_reset_draft(self):
        """Reset to draft so the user can re-run."""
        self.ensure_one()
        self.finding_ids.unlink()
        self.game_result_ids.unlink()
        vals = {
            'state': 'draft',
            'error_message': False,
            'summary_html': False,
            'critical_count': 0,
            'high_count': 0,
            'medium_count': 0,
            'low_count': 0,
            'games_checked': 0,
            'models_checked': 0,
            'runs_checked': 0,
            'steps_checked': 0,
            'duration_seconds': 0,
            'clone_path': False,
        }
        # For Git source, clear derived session_path so it's re-derived on next run
        if self.source_type == 'git':
            vals['session_path'] = False
        self.write(vals)
