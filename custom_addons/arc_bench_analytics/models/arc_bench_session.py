import base64
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.parse

from markupsafe import Markup

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from ..engine import run_analysis, AnalysisConfig, AnalysisResult

_logger = logging.getLogger(__name__)


class ArcBenchSession(models.Model):
    """Top-level benchmark analytics session.

    Each record represents one analysis run over a session directory
    containing ARC-AGI-3 evaluation trajectory data.
    """

    _name = 'arc.bench.session'
    _description = 'ARC Bench Analytics Session'
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
            ('done', 'Done'),
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
        help='Absolute path to the directory containing game folders '
             '(e.g. /data/25_batch2_with_notepad/).',
    )
    repo_url = fields.Char(
        string='Repository URL',
        help='HTTPS URL of the Git repository containing trajectory data.',
    )
    repo_token = fields.Char(
        string='Access Token',
        help='Personal access token for private repositories.',
    )
    repo_branch = fields.Char(
        string='Branch',
        default='main',
    )
    clone_path = fields.Char(
        string='Clone Directory',
        readonly=True,
    )

    # --- Config ---
    script_path = fields.Char(
        string='Plot Script',
        help='Absolute path to the Python plotting script. '
             'Leave blank to use the bundled default script.',
    )
    expected_runs = fields.Integer(
        string='Expected Runs per Model',
        default=3,
    )
    max_steps = fields.Integer(
        string='Max Steps',
        default=200,
    )

    # --- Results ---
    game_count = fields.Integer(readonly=True)
    model_count = fields.Integer(readonly=True)
    duration_seconds = fields.Float(readonly=True, digits=(10, 3))

    game_result_ids = fields.One2many(
        comodel_name='arc.bench.game.result',
        inverse_name='session_id',
        string='Game Results',
        readonly=True,
    )

    error_message = fields.Text(readonly=True)
    script_log = fields.Text(
        string='Script Output',
        readonly=True,
        help='Combined stdout/stderr from the plot script execution.',
    )

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
                rec.name = _('Analysis @ %s') % fields.Datetime.to_string(rec.create_date)
            else:
                rec.name = _('New Analysis')

    # --- Actions ---

    def action_run_analysis(self):
        """Launch analysis in a background thread."""
        self.ensure_one()
        if self.state == 'running':
            raise UserError(_('Analysis is already running.'))

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
            'state': 'running',
            'error_message': False,
            'game_count': 0,
            'model_count': 0,
            'duration_seconds': 0,
        })

        thread = threading.Thread(
            target=self._run_analysis_thread,
            args=(self.id,),
            daemon=True,
        )
        thread.start()

    def action_reset_draft(self):
        """Reset session back to draft state."""
        self.ensure_one()
        self.game_result_ids.unlink()
        self.write({
            'state': 'draft',
            'error_message': False,
            'script_log': False,
            'game_count': 0,
            'model_count': 0,
            'duration_seconds': 0,
        })

    def _run_analysis_thread(self, session_id):
        """Execute analysis engine in a background thread."""
        time.sleep(2)
        with self.pool.cursor() as cr:
            env = api.Environment(cr, self.env.uid, self.env.context)
            rec = env['arc.bench.session'].browse(session_id)
            clone_dir = None
            try:
                rec.game_result_ids.unlink()

                source_type = rec.source_type
                repo_url = rec.repo_url
                repo_token = rec.repo_token
                repo_branch = rec.repo_branch or 'main'
                session_path = rec.session_path

                if source_type == 'git':
                    clone_dir = self._clone_repo(repo_url, repo_token, repo_branch)
                    session_path = clone_dir

                config = AnalysisConfig(
                    expected_runs_per_model=rec.expected_runs,
                    max_steps=rec.max_steps,
                )
                result = run_analysis(
                    session_path,
                    config,
                    script_path=rec.script_path or None,
                )

                if result.error:
                    raise RuntimeError(result.error)

                _logger.info(
                    'Analysis completed for session %s: %d games, %.1fs',
                    session_id, result.game_count, result.duration_seconds,
                )
                rec._apply_result(result)

                # For git source: commit and push plots back
                if source_type == 'git' and clone_dir:
                    self._git_push_plots(clone_dir, repo_url, repo_token, repo_branch)
                    rec.write({'clone_path': clone_dir, 'session_path': clone_dir})

                cr.commit()

                rec.message_post(
                    body=Markup(
                        f'<strong>Analysis Complete</strong><br/>'
                        f'{result.game_count} games analysed in '
                        f'{result.duration_seconds:.1f}s'
                    ),
                    message_type='comment',
                    subtype_xmlid='mail.mt_note',
                )
                cr.commit()

            except Exception as exc:
                cr.rollback()
                _logger.exception('Analysis error for session %s', session_id)
                try:
                    rec.write({
                        'state': 'error',
                        'error_message': str(exc),
                    })
                    rec.message_post(
                        body=Markup(
                            f'<strong>Analysis Error</strong><br/>'
                            f'{str(exc)[:500]}'
                        ),
                        message_type='comment',
                        subtype_xmlid='mail.mt_note',
                    )
                    cr.commit()
                except Exception:
                    _logger.exception(
                        'Failed to write error state for session %s', session_id
                    )
            finally:
                # For git: clean up clone dir AFTER push
                if clone_dir and os.path.isdir(clone_dir):
                    try:
                        shutil.rmtree(clone_dir)
                        _logger.info('Cleaned up clone dir: %s', clone_dir)
                    except Exception:
                        _logger.warning(
                            'Failed to clean up clone dir: %s',
                            clone_dir, exc_info=True,
                        )

    @staticmethod
    def _clone_repo(repo_url, token, branch):
        """Clone a Git repository to a temporary directory."""
        clone_dir = tempfile.mkdtemp(prefix='arc_bench_clone_')
        try:
            if token:
                safe_token = urllib.parse.quote(token, safe='')
                if repo_url.startswith('https://'):
                    auth_url = repo_url.replace(
                        'https://', f'https://{safe_token}@', 1
                    )
                elif repo_url.startswith('http://'):
                    auth_url = repo_url.replace(
                        'http://', f'http://{safe_token}@', 1
                    )
                else:
                    auth_url = repo_url
            else:
                auth_url = repo_url

            cmd = [
                'git', 'clone',
                '--branch', branch,
                '--single-branch',
                auth_url,
                clone_dir,
            ]
            _logger.info(
                'Cloning %s (branch=%s) into %s', repo_url, branch, clone_dir
            )
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=600,
                env={
                    'PATH': os.environ.get('PATH', '/usr/bin:/bin'),
                    'GIT_TERMINAL_PROMPT': '0',
                },
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

    @staticmethod
    def _git_push_plots(clone_dir, repo_url, token, branch):
        """Stage generated plots, commit, and push back to the repository."""
        git_env = {
            'PATH': os.environ.get('PATH', '/usr/bin:/bin'),
            'GIT_TERMINAL_PROMPT': '0',
            'GIT_AUTHOR_NAME': 'ARC Bench Analytics',
            'GIT_AUTHOR_EMAIL': 'arc-bench@ethara.com',
            'GIT_COMMITTER_NAME': 'ARC Bench Analytics',
            'GIT_COMMITTER_EMAIL': 'arc-bench@ethara.com',
        }

        def _run_git(*args):
            result = subprocess.run(
                ['git'] + list(args),
                cwd=clone_dir,
                capture_output=True,
                text=True,
                timeout=120,
                env=git_env,
            )
            if result.returncode != 0:
                stderr = result.stderr.strip()
                if token:
                    stderr = stderr.replace(token, '***')
                raise RuntimeError(
                    f'git {args[0]} failed (exit {result.returncode}): {stderr}'
                )
            return result.stdout.strip()

        # Stage all generated plot files
        _run_git('add', '--all')

        # Check if there are staged changes
        status = subprocess.run(
            ['git', 'diff', '--cached', '--quiet'],
            cwd=clone_dir,
            capture_output=True,
            env=git_env,
        )
        if status.returncode == 0:
            _logger.info('No plot changes to commit.')
            return

        _run_git('commit', '-m', 'chore: generate benchmark analytics plots')

        # Push — construct auth URL for push
        if token:
            safe_token = urllib.parse.quote(token, safe='')
            if repo_url.startswith('https://'):
                push_url = repo_url.replace(
                    'https://', f'https://{safe_token}@', 1
                )
            elif repo_url.startswith('http://'):
                push_url = repo_url.replace(
                    'http://', f'http://{safe_token}@', 1
                )
            else:
                push_url = repo_url
        else:
            push_url = repo_url

        _run_git('push', push_url, branch)
        _logger.info('Pushed plots to %s branch %s', repo_url, branch)

    def _apply_result(self, result: AnalysisResult):
        """Store AnalysisResult into Odoo records."""
        self.ensure_one()

        GameResult = self.env['arc.bench.game.result']
        ModelResult = self.env['arc.bench.model.result']
        RunModel = self.env['arc.bench.run']

        for game in result.games:
            game_rec = GameResult.create({
                'session_id': self.id,
                'game_id': game.game_id,
                'game_path': game.game_path,
                'model_count': len(game.models),
                'plot1_image': base64.b64encode(game.plot1_png) if game.plot1_png else False,
                'plot2_image': base64.b64encode(game.plot2_png) if game.plot2_png else False,
            })

            for model in game.models:
                model_rec = ModelResult.create({
                    'game_result_id': game_rec.id,
                    'model_dir': model.model_dir,
                    'model_name': model.model_name,
                    'run_count': model.run_count,
                    'mean_score_pct': model.mean_score_pct,
                    'mean_cost_usd': model.mean_cost_usd,
                    'total_steps': model.total_steps,
                    'solved_count': model.solved_count,
                    'mean_elapsed_seconds': model.mean_elapsed_seconds,
                })

                # Store individual runs
                for run in model.runs:
                    RunModel.create({
                        'model_result_id': model_rec.id,
                        'run_id': run.run_id,
                        'run_number': run.run_number,
                        'final_score_pct': run.final_score_pct,
                        'cost_usd': run.cost_usd,
                        'total_steps': run.total_steps,
                        'solved': run.solved,
                        'levels_completed': run.levels_completed,
                        'total_levels': run.total_levels,
                        'total_input_tokens': run.total_input_tokens,
                        'total_output_tokens': run.total_output_tokens,
                        'total_reasoning_tokens': run.total_reasoning_tokens,
                        'elapsed_seconds': run.elapsed_seconds,
                        'error': run.error or '',
                    })

        script_log_parts = []
        if result.script_stdout:
            script_log_parts.append(result.script_stdout)
        if result.script_stderr:
            script_log_parts.append(result.script_stderr)

        self.write({
            'state': 'done',
            'game_count': result.game_count,
            'model_count': result.model_count,
            'duration_seconds': result.duration_seconds,
            'script_log': '\n'.join(script_log_parts) or False,
        })
