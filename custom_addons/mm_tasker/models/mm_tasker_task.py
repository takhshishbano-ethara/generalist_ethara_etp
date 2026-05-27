# -*- coding: utf-8 -*-
import base64
import json
import logging

from odoo import api, fields, models, _
from odoo.exceptions import ValidationError, UserError

_logger = logging.getLogger(__name__)


class MmTaskerTask(models.Model):
    _name = 'mm.tasker.task'
    _description = 'MM Tasker Task'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'task_date desc, id desc'

    name = fields.Char(
        string='Reference',
        compute='_compute_name',
        store=True,
    )

    tasker_id = fields.Many2one(
        'res.users',
        string='Tasker',
        default=lambda self: self.env.user,
        readonly=True,
        required=True,
        tracking=True,
        index=True,
    )

    task_date = fields.Datetime(
        string='Task Date',
        default=fields.Datetime.now,
        readonly=True,
        required=True,
    )

    default_prompt = fields.Text(string='Default Prompt', tracking=True)
    human_prompt = fields.Text(string='Human Prompt', tracking=True)
    final_prompt = fields.Text(string='Final Prompt', tracking=True)

    rubrics_file = fields.Binary(string='Rubrics JSONL', attachment=True)
    rubrics_filename = fields.Char(string='Rubrics Filename')

    rubric_ids = fields.One2many(
        'mm.tasker.rubric',
        'task_id',
        string='Parsed Rubrics',
    )
    rubric_count = fields.Integer(compute='_compute_rubric_count', store=True)

    media_ids = fields.One2many(
        'mm.tasker.media',
        'task_id',
        string='Media',
    )
    media_count = fields.Integer(compute='_compute_media_count', store=True)
    image_count = fields.Integer(compute='_compute_media_count', store=True)
    pdf_count = fields.Integer(compute='_compute_media_count', store=True)
    video_count = fields.Integer(compute='_compute_media_count', store=True)

    run_ids = fields.One2many(
        'mm.tasker.run',
        'task_id',
        string='Reference Model Runs',
    )
    run_count = fields.Integer(compute='_compute_run_aggregates', store=True)
    runs_scored = fields.Integer(compute='_compute_run_aggregates', store=True)
    runs_passed = fields.Integer(compute='_compute_run_aggregates', store=True)
    runs_in_flight = fields.Integer(compute='_compute_run_aggregates', store=True)

    state = fields.Selection(
        [
            ('draft', 'Draft'),
            ('ready_for_eval', 'Ready for Eval'),
            ('dispatched', 'Dispatched'),
            ('evaluated', 'Evaluated'),
            ('qc_passed', 'QC Passed'),
            ('qc_failed', 'QC Failed'),
        ],
        default='draft',
        required=True,
        tracking=True,
        index=True,
    )

    active_models_display = fields.Char(
        compute='_compute_active_models_display',
        string='Active Models',
    )
    backend_status_display = fields.Char(
        compute='_compute_backend_status_display',
        string='Backend',
    )
    judge_model_display = fields.Char(
        compute='_compute_judge_model_display',
        string='Judge Model',
    )
    judge_backend_display = fields.Char(
        compute='_compute_judge_backend_display',
        string='Judge Backend',
    )

    judge_tokens_in = fields.Integer(
        compute='_compute_judge_metrics',
        store=True,
        string='Judge Tokens In',
    )
    judge_tokens_out = fields.Integer(
        compute='_compute_judge_metrics',
        store=True,
        string='Judge Tokens Out',
    )
    judge_cost_usd = fields.Float(
        compute='_compute_judge_metrics',
        store=True,
        string='Judge Cost (USD)',
        digits=(10, 4),
    )
    llm_judge_score_ids = fields.Many2many(
        'mm.tasker.run.score',
        compute='_compute_llm_judge_scores',
        string='LLM Judge Scores',
    )

    prompts_submitted = fields.Boolean(
        string='Prompts Submitted',
        default=False,
        copy=False,
        tracking=True,
    )
    media_submitted = fields.Boolean(
        string='Media Submitted',
        default=False,
        copy=False,
        tracking=True,
    )
    rubrics_submitted = fields.Boolean(
        string='Rubrics Submitted',
        default=False,
        copy=False,
        tracking=True,
    )
    verdict_ready = fields.Boolean(
        string='Verdict',
        compute='_compute_verdict_ready',
    )

    # Per-section QC gates. Each Submit button on the Prompts / Media /
    # Rubrics tab invokes its scripts/qc_*.py subprocess. A "pass" verdict
    # is what flips the corresponding *_submitted flag, so locking the
    # section is the reward for passing QC. "fail" leaves the section
    # editable so the tasker can fix and re-submit. Run Models on the
    # Verdict tab requires all three to be "pass".
    _QC_VERDICT_SELECTION = [
        ('not_run', 'Not run'),
        ('pass', 'Pass'),
        ('fail', 'Fail'),
    ]
    prompt_qc_verdict = fields.Selection(
        _QC_VERDICT_SELECTION, string='Prompt QC',
        default='not_run', copy=False, tracking=True,
    )
    prompt_qc_message = fields.Text(string='Prompt QC Message', readonly=True, copy=False)
    prompt_qc_at = fields.Datetime(string='Prompt QC At', readonly=True, copy=False)

    media_qc_verdict = fields.Selection(
        _QC_VERDICT_SELECTION, string='Media QC',
        default='not_run', copy=False, tracking=True,
    )
    media_qc_message = fields.Text(string='Media QC Message', readonly=True, copy=False)
    media_qc_at = fields.Datetime(string='Media QC At', readonly=True, copy=False)

    rubrics_qc_verdict = fields.Selection(
        _QC_VERDICT_SELECTION, string='Rubrics QC',
        default='not_run', copy=False, tracking=True,
    )
    rubrics_qc_message = fields.Text(string='Rubrics QC Message', readonly=True, copy=False)
    rubrics_qc_at = fields.Datetime(string='Rubrics QC At', readonly=True, copy=False)

    qc_verdict = fields.Selection(
        [('pass', 'Pass'), ('fail', 'Fail')],
        string='QC Verdict',
        tracking=True,
        copy=False,
    )
    qc_fail_reason = fields.Text(string='QC Fail Reason', copy=False)
    qc_user_id = fields.Many2one(
        'res.users',
        string='QC By',
        readonly=True,
        copy=False,
    )
    qc_date = fields.Datetime(string='QC Date', readonly=True, copy=False)

    @api.depends('tasker_id', 'task_date')
    def _compute_name(self):
        for rec in self:
            uname = rec.tasker_id.name or 'unassigned'
            dstr = fields.Datetime.to_string(rec.task_date) if rec.task_date else ''
            rec.name = f'Task-{rec.id or "new"} · {uname} · {dstr}'

    @api.depends('rubric_ids')
    def _compute_rubric_count(self):
        for rec in self:
            rec.rubric_count = len(rec.rubric_ids)

    @api.depends('media_ids', 'media_ids.kind')
    def _compute_media_count(self):
        for rec in self:
            rec.media_count = len(rec.media_ids)
            rec.image_count = sum(1 for m in rec.media_ids if m.kind == 'image')
            rec.pdf_count = sum(1 for m in rec.media_ids if m.kind == 'pdf')
            rec.video_count = sum(1 for m in rec.media_ids if m.kind == 'video')

    @api.depends('run_ids', 'run_ids.state', 'run_ids.passed')
    def _compute_run_aggregates(self):
        for rec in self:
            rec.run_count = len(rec.run_ids)
            rec.runs_scored = sum(1 for r in rec.run_ids if r.state == 'scored')
            rec.runs_passed = sum(1 for r in rec.run_ids if r.state == 'scored' and r.passed)
            rec.runs_in_flight = sum(
                1 for r in rec.run_ids if r.state in ('queued', 'running', 'judging')
            )

    @api.onchange('default_prompt', 'human_prompt')
    def _onchange_compose_final_prompt(self):
        """Auto-build final_prompt = default + human (blank-line separated).

        Re-runs every time the tasker edits default or human, so the final
        stays in sync. After both inputs are stable the tasker can still
        edit final_prompt directly — that manual tweak persists until
        default or human changes again.
        """
        for rec in self:
            parts = []
            if rec.default_prompt and rec.default_prompt.strip():
                parts.append(rec.default_prompt.strip())
            if rec.human_prompt and rec.human_prompt.strip():
                parts.append(rec.human_prompt.strip())
            rec.final_prompt = '\n\n'.join(parts) if parts else False

    @api.onchange('rubrics_file', 'rubrics_filename')
    def _onchange_rubrics_file(self):
        """Parse the uploaded .jsonl into child rubric rows.

        Upserts by ``number`` rather than wholesale replace, so post-
        dispatch re-uploads don't cascade-delete the existing
        mm.tasker.run.score rows that probe/shell rubrics depend on
        (those scores can't be recreated locally — we'd lose them
        forever). Side benefit: minor edits to an already-uploaded file
        keep stable rubric IDs.
        """
        for rec in self:
            if not rec.rubrics_file:
                rec.rubric_ids = [(5, 0, 0)]
                continue
            try:
                raw = base64.b64decode(rec.rubrics_file).decode('utf-8')
            except Exception as e:
                raise UserError(_('Could not decode uploaded file as UTF-8: %s') % e)
            parsed = self._parse_jsonl(raw)
            rec.rubric_ids = rec._build_rubric_upsert_commands(parsed)

    def _build_rubric_upsert_commands(self, parsed_rows):
        """Return Odoo write commands that upsert rubric rows by ``number``.

        - existing row with same number  -> (1, id, vals)  update in place
        - new number                     -> (0, 0, vals)   create
        - existing number not in parse   -> (2, id, 0)     delete (cascades)
        """
        self.ensure_one()
        existing_by_num = {r.number: r for r in self.rubric_ids if r.number}
        seen_numbers = set()
        commands = []
        for row in parsed_rows:
            num = row.get('number')
            seen_numbers.add(num)
            existing = existing_by_num.get(num)
            if existing:
                commands.append((1, existing.id, row))
            else:
                commands.append((0, 0, row))
        for num, rec in existing_by_num.items():
            if num not in seen_numbers:
                commands.append((2, rec.id, 0))
        return commands

    @staticmethod
    def _parse_jsonl(raw_text):
        """Return a list of dicts suitable for mm.tasker.rubric.create.

        Raises ValidationError with the offending line number on bad input.
        """
        rows = []
        for idx, line in enumerate(raw_text.splitlines(), start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                obj = json.loads(stripped)
            except json.JSONDecodeError as e:
                raise ValidationError(
                    _('Invalid JSON on line %(line)s: %(err)s') % {'line': idx, 'err': e.msg}
                )
            if not isinstance(obj, dict):
                raise ValidationError(
                    _('Line %s must be a JSON object, got %s') % (idx, type(obj).__name__)
                )
            rows.append({
                'number': obj.get('number') or idx,
                'rubric_type': (obj.get('type') or '')[:64],
                'category': (obj.get('category') or '')[:32],
                'points': obj.get('points') or 0,
                'importance': (obj.get('importance') or '')[:16],
                'criterion': obj.get('criterion') or '',
                'raw_json': json.dumps(obj, ensure_ascii=False),
            })
        return rows

    @api.model
    def _get_reference_models(self):
        """Return the list of active model keys from System Parameters."""
        raw = self.env['ir.config_parameter'].sudo().get_param(
            'mm_tasker.active_models',
            default='claude_opus_4_7,gpt_5_5,gemini_3_1',
        )
        return [k.strip() for k in (raw or '').split(',') if k.strip()]

    @api.depends('state')
    def _compute_active_models_display(self):
        models_list = self._get_reference_models()
        labels = self.env['mm.tasker.run']._read_label_map()
        pretty = [labels.get(k) or k.replace('_', ' ').title() for k in models_list]
        text = ', '.join(pretty) if pretty else _('(none configured)')
        for rec in self:
            rec.active_models_display = text

    @api.depends('state')
    def _compute_backend_status_display(self):
        Param = self.env['ir.config_parameter'].sudo()
        test_mode = self.env['mm.tasker.agent.dispatcher']._param_truthy(
            Param.get_param('mm_tasker.test_mode', default='true')
        )
        url = (Param.get_param('mm_tasker.backend_url') or '').strip()
        if test_mode:
            text = _('Test mode (responses are mocked)')
        elif url:
            text = _('Live → %s') % url
        else:
            text = _('Live mode but mm_tasker.backend_url is empty — calls will fail.')
        for rec in self:
            rec.backend_status_display = text

    @api.depends('state')
    def _compute_judge_model_display(self):
        Param = self.env['ir.config_parameter'].sudo()
        key = (Param.get_param('mm_tasker.judge_model_key', default='claude_opus_4_7') or '').strip()
        labels = self.env['mm.tasker.run']._read_label_map()
        label = labels.get(key) or key.replace('_', ' ').title() if key else _('(not configured)')
        for rec in self:
            rec.judge_model_display = label

    @api.depends('state')
    def _compute_judge_backend_display(self):
        Param = self.env['ir.config_parameter'].sudo()
        test_mode = self.env['mm.tasker.agent.dispatcher']._param_truthy(
            Param.get_param('mm_tasker.test_mode', default='true')
        )
        judge_url = (Param.get_param('mm_tasker.judge_backend_url') or '').strip()
        fallback = (Param.get_param('mm_tasker.backend_url') or '').strip()
        if test_mode:
            text = _('Test mode (judge verdicts are mocked)')
        elif judge_url:
            text = _('Live → %s') % judge_url
        elif fallback:
            text = _('Live → %s (fallback from backend_url)') % fallback
        else:
            text = _('Live mode but no judge URL configured — calls will fail.')
        for rec in self:
            rec.judge_backend_display = text

    @api.depends('run_ids.score_ids.judged_by',
                 'run_ids.score_ids.judge_tokens_in',
                 'run_ids.score_ids.judge_tokens_out')
    def _compute_judge_metrics(self):
        Param = self.env['ir.config_parameter'].sudo()
        try:
            cost_in = float(Param.get_param('mm_tasker.judge_cost_per_1m_in', default='15.0') or 15.0)
        except (TypeError, ValueError):
            cost_in = 15.0
        try:
            cost_out = float(Param.get_param('mm_tasker.judge_cost_per_1m_out', default='75.0') or 75.0)
        except (TypeError, ValueError):
            cost_out = 75.0

        for rec in self:
            judge_scores = rec.run_ids.score_ids.filtered(lambda s: s.judged_by == 'llm_judge')
            tin = sum(s.judge_tokens_in for s in judge_scores)
            tout = sum(s.judge_tokens_out for s in judge_scores)
            rec.judge_tokens_in = tin
            rec.judge_tokens_out = tout
            rec.judge_cost_usd = (tin / 1_000_000.0) * cost_in + (tout / 1_000_000.0) * cost_out

    @api.depends('prompt_qc_verdict', 'media_qc_verdict', 'rubrics_qc_verdict',
                 'prompts_submitted', 'media_submitted', 'rubrics_submitted',
                 'final_prompt', 'media_count', 'rubric_count')
    def _compute_verdict_ready(self):
        """All three per-section QCs must be Pass — and the underlying
        data must still be present. The per-section submit actions reset
        their QC verdict if the user unlocks for edits, so this naturally
        flips back when anything regresses."""
        for rec in self:
            rec.verdict_ready = (
                rec.prompt_qc_verdict == 'pass'
                and rec.media_qc_verdict == 'pass'
                and rec.rubrics_qc_verdict == 'pass'
                and rec.prompts_submitted
                and rec.media_submitted
                and rec.rubrics_submitted
                and bool(rec.final_prompt and rec.final_prompt.strip())
                and rec.media_count > 0
                and rec.rubric_count > 0
            )

    @api.depends('run_ids.score_ids.judged_by')
    def _compute_llm_judge_scores(self):
        for rec in self:
            rec.llm_judge_score_ids = rec.run_ids.score_ids.filtered(
                lambda s: s.judged_by == 'llm_judge'
            )

    def _ensure_rubrics_parsed(self):
        """Server-side parse of rubrics_file → rubric_ids.

        The onchange version handles the UI happy path, but it can miss in
        edge cases (file uploaded before first save, attachment-stored
        binary field not surfaced into the onchange context, etc.). This
        runs at action time as a safety net so the user never sees
        "no rubrics" when the file is sitting right there.
        """
        for rec in self:
            if rec.rubric_ids or not rec.rubrics_file:
                continue
            try:
                raw = base64.b64decode(rec.rubrics_file).decode('utf-8')
            except Exception as e:
                raise UserError(_('Could not decode rubrics file as UTF-8: %s') % e)
            parsed = self._parse_jsonl(raw)
            if not parsed:
                continue
            rec.rubric_ids = rec._build_rubric_upsert_commands(parsed)

    # ---- Per-section QC plumbing ----------------------------------------

    _QC_SCRIPTS = {
        'prompt': 'qc_prompt.py',
        'media': 'qc_media.py',
        'rubrics': 'qc_rubrics.py',
    }
    _QC_LABEL = {
        'prompt': 'Prompt',
        'media': 'Media',
        'rubrics': 'Rubrics',
    }

    def _qc_script_path(self, section):
        """Resolve scripts/qc_<section>.py inside the installed module."""
        import os
        from odoo.modules.module import get_module_path
        module_root = get_module_path('mm_tasker')
        if not module_root:
            return ''
        return os.path.join(module_root, 'scripts', self._QC_SCRIPTS[section])

    @staticmethod
    def _media_size_bytes(media):
        """file_size is the source of truth, but fall back to the binary
        if it's 0/empty (covers rows uploaded before the stored compute
        was added)."""
        if media.file_size:
            return media.file_size
        if not media.file:
            return 0
        try:
            return len(base64.b64decode(media.file))
        except Exception:
            return len(media.file) if isinstance(media.file, (bytes, bytearray)) else 0

    def _qc_payload(self, section):
        """Build the stdin payload for the section's QC script.

        Prompt-only checks get just the prompt. Media checks get prompt
        + media (to cross-reference filenames). Rubric checks get all
        three (script_qc + prompt + media coherence)."""
        payload = {'task_id': self.id, 'final_prompt': self.final_prompt or ''}
        if section in ('media', 'rubrics'):
            payload['media'] = [
                {
                    'filename': m.filename or m.name or '',
                    'kind': m.kind or 'other',
                    'size_bytes': self._media_size_bytes(m),
                }
                for m in self.media_ids
            ]
        if section == 'rubrics':
            payload['rubrics'] = [
                {
                    'number': r.number,
                    'type': r.rubric_type or '',
                    'category': r.category or '',
                    'points': r.points or 0,
                    'importance': r.importance or '',
                    'criterion': r.criterion or '',
                    'raw_json': r.raw_json or '',
                }
                for r in self.rubric_ids
            ]
        return payload

    def _run_qc(self, section):
        """Run the QC script for ``section`` and return (verdict, message).

        Never raises for script-level issues — those become a Fail verdict
        with the failure detail in the message, so the UI can render them
        the same way as a regular Fail.
        """
        import json
        import os
        import subprocess
        import sys

        self.ensure_one()
        Param = self.env['ir.config_parameter'].sudo()
        try:
            timeout = int(Param.get_param('mm_tasker.qc_timeout', default='30') or 30)
        except (TypeError, ValueError):
            timeout = 30

        script_path = self._qc_script_path(section)
        if not script_path or not os.path.exists(script_path):
            return 'fail', _('QC script not found at %s.') % (script_path or '(unresolved)')

        payload = self._qc_payload(section)
        try:
            result = subprocess.run(
                [sys.executable, script_path],
                input=json.dumps(payload),
                text=True,
                capture_output=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return 'fail', f'QC script timed out after {timeout}s.'
        except OSError as e:
            return 'fail', f'Failed to invoke QC script: {e}'

        if result.returncode != 0:
            tail = (result.stderr or '').strip()[-500:]
            return 'fail', f'QC script exited {result.returncode}. stderr: {tail}'

        try:
            parsed = json.loads((result.stdout or '').strip())
            verdict = (parsed.get('verdict') or '').strip().lower()
            message = str(parsed.get('message') or '')
        except (ValueError, TypeError) as e:
            return 'fail', f'Bad JSON from QC script: {e}. stdout head: {(result.stdout or "")[:200]!r}'

        if verdict not in ('pass', 'fail'):
            return 'fail', f'QC script returned unknown verdict {verdict!r}; expected pass/fail.'
        return verdict, message

    def _record_qc(self, section, verdict, message):
        """Persist + chatter-post a per-section QC verdict."""
        self.ensure_one()
        self.write({
            f'{section}_qc_verdict': verdict,
            f'{section}_qc_message': message,
            f'{section}_qc_at': fields.Datetime.now(),
        })
        self.message_post(
            body=_('%(section)s QC: %(verdict)s — %(msg)s') % {
                'section': self._QC_LABEL[section],
                'verdict': verdict.upper(),
                'msg': message or '(no message)',
            },
            message_type='comment',
            subtype_xmlid='mail.mt_note',
        )

    def _reset_qc(self, section):
        """Clear a section's QC verdict (called when the user unlocks it)."""
        self.ensure_one()
        if self[f'{section}_qc_verdict'] != 'not_run':
            self.write({
                f'{section}_qc_verdict': 'not_run',
                f'{section}_qc_at': False,
            })

    def action_submit_prompts(self):
        """Run prompt QC; lock the section only if it passes."""
        for rec in self:
            if rec.state != 'draft':
                raise UserError(_('Prompts can only be submitted while the task is in Draft.'))
            if not rec.final_prompt or not rec.final_prompt.strip():
                raise UserError(_('Final Prompt is required before submitting Prompts.'))
            verdict, message = rec._run_qc('prompt')
            rec._record_qc('prompt', verdict, message)
            rec.prompts_submitted = (verdict == 'pass')
        return True

    def action_unlock_prompts(self):
        for rec in self:
            if rec.state != 'draft':
                raise UserError(_('Only Draft tasks can be unlocked.'))
            rec.prompts_submitted = False
            rec._reset_qc('prompt')
            # Downstream verdicts were computed against the old prompt.
            rec.media_submitted = False
            rec.rubrics_submitted = False
            rec._reset_qc('media')
            rec._reset_qc('rubrics')
        return True

    def action_submit_media(self):
        """Run media QC (also checks coherence with the prompt); lock on pass."""
        for rec in self:
            if rec.state != 'draft':
                raise UserError(_('Media can only be submitted while the task is in Draft.'))
            if not rec.prompts_submitted or rec.prompt_qc_verdict != 'pass':
                raise UserError(_('Submit the Prompts tab and pass its QC first.'))
            if not rec.media_ids:
                raise UserError(_('Attach at least one media file before submitting Media.'))
            verdict, message = rec._run_qc('media')
            rec._record_qc('media', verdict, message)
            rec.media_submitted = (verdict == 'pass')
        return True

    def action_unlock_media(self):
        for rec in self:
            if rec.state != 'draft':
                raise UserError(_('Only Draft tasks can be unlocked.'))
            rec.media_submitted = False
            rec._reset_qc('media')
            rec.rubrics_submitted = False
            rec._reset_qc('rubrics')
        return True

    def action_submit_rubrics(self):
        """Run rubric QC against current rubrics (also checks coherence
        with prompt + media). Allowed in draft AND post-dispatch states
        (dispatched / evaluated) so the user can update rubrics after a
        run and re-trigger QC without unlocking back to Draft.

        Locked: ready_for_eval (mid-dispatch hand-off), qc_passed,
        qc_failed (QC verdict finalized).
        """
        editable_states = ('draft', 'dispatched', 'evaluated')
        for rec in self:
            if rec.state not in editable_states:
                raise UserError(_(
                    'Rubrics can only be submitted while the task is in Draft, '
                    'Dispatched, or Evaluated. Current state: %s.'
                ) % rec.state)
            if rec.state == 'draft':
                # Upstream gates only apply pre-dispatch. Post-dispatch
                # the prompt/media are already frozen on disk in goku;
                # re-uploading rubrics is a post-hoc edit.
                if not rec.media_submitted or rec.media_qc_verdict != 'pass':
                    raise UserError(_('Submit the Media tab and pass its QC first.'))
            rec._ensure_rubrics_parsed()
            if not rec.rubric_ids:
                raise UserError(_(
                    'No rubric rows were parsed from %s. '
                    'Each line of the .jsonl must be a JSON object.'
                ) % (rec.rubrics_filename or 'the uploaded file'))
            verdict, message = rec._run_qc('rubrics')
            rec._record_qc('rubrics', verdict, message)
            rec.rubrics_submitted = (verdict == 'pass')
        return True

    def action_unlock_rubrics(self):
        for rec in self:
            if rec.state != 'draft':
                raise UserError(_('Only Draft tasks can be unlocked.'))
            rec.rubrics_submitted = False
            rec._reset_qc('rubrics')
        return True

    def action_ready_for_eval(self):
        """Lock the form for editing. User can now click Run Models."""
        self._ensure_rubrics_parsed()
        for rec in self:
            if rec.state != 'draft':
                raise UserError(_('Only Draft tasks can be marked Ready for Eval.'))
            if not rec.final_prompt or not rec.final_prompt.strip():
                raise UserError(_('Final Prompt is required.'))
            if not rec.media_ids:
                raise UserError(_('Attach at least one media file (image / PDF / video) before continuing.'))
            if not rec.rubric_ids:
                raise UserError(_(
                    'No rubric rows were parsed from %s. '
                    'Each line must be a JSON object — check the file format.'
                ) % (rec.rubrics_filename or 'the uploaded file'))
            rec.state = 'ready_for_eval'
            rec.message_post(
                body=_('Marked Ready for Eval — form is now locked.'),
                message_type='comment',
                subtype_xmlid='mail.mt_note',
            )
        return True

    def action_back_to_draft(self):
        """Manager-only: re-open the form for editing and discard model runs."""
        is_manager = (
            self.env.user.has_group('mm_tasker.group_mm_manager')
            or self.env.user._is_admin()
        )
        if not is_manager:
            raise UserError(_('Only managers can unlock a task back to Draft.'))
        for rec in self:
            if rec.state in ('qc_passed', 'qc_failed'):
                raise UserError(_("QC'd tasks cannot be returned to Draft."))
            rec.run_ids.unlink()
            rec.state = 'draft'
            rec.message_post(
                body=_('Unlocked back to Draft by %s. Existing model runs were discarded.')
                % self.env.user.name,
                message_type='comment',
                subtype_xmlid='mail.mt_note',
            )
        return True

    def action_run_judge(self):
        """Grade response_criteria / response_not_criteria rubrics with an LLM judge.

        Available in `dispatched` (first pass) and `evaluated` (re-judge).
        Idempotent — clears prior llm_judge scores before re-creating.
        Skips runs in `error` state.
        """
        for rec in self:
            if rec.state not in ('dispatched', 'evaluated'):
                raise UserError(_('Run Judge is only available after models have been dispatched.'))
            runs = rec.run_ids.filtered(lambda r: r.state != 'error')
            if not runs:
                raise UserError(_('No completed runs to judge. Run Models first.'))
            self.env['mm.tasker.agent.dispatcher'].judge_runs(runs)
            rec.state = 'evaluated'
            rec.message_post(
                body=_('LLM judge completed across %s run(s).') % len(runs),
                message_type='comment',
                subtype_xmlid='mail.mt_note',
            )
        return True

    def action_run_models(self):
        """Open the Run Models wizard.

        The wizard lets the tasker pick a runs-per-model count for each
        active reference model, then performs the dispatch itself. We
        validate the readiness gate here so the wizard never opens for
        a task that's still missing QC.
        """
        self.ensure_one()
        if self.state == 'draft' and not self.verdict_ready:
            raise UserError(_(
                'All three sections must be submitted and pass their QC '
                'before running models.'
            ))
        if not self.final_prompt or not self.rubric_ids or not self.media_ids:
            raise UserError(_('Final prompt, media, and rubrics must all be present.'))
        if not self._get_reference_models():
            raise UserError(_(
                'No active models configured. Set mm_tasker.active_models in '
                'Settings → Technical → System Parameters.'
            ))
        return {
            'type': 'ir.actions.act_window',
            'name': _('Run Models'),
            'res_model': 'mm.tasker.run.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_task_id': self.id},
        }

    @api.constrains('qc_verdict', 'qc_fail_reason')
    def _check_fail_reason(self):
        for rec in self:
            if rec.qc_verdict == 'fail' and not (rec.qc_fail_reason or '').strip():
                raise ValidationError(_('A QC Fail Reason is required when verdict is Fail.'))

    def write(self, vals):
        """Stamp QC user/date and advance state whenever qc_verdict is set."""
        if 'qc_verdict' in vals and vals['qc_verdict']:
            verdict = vals['qc_verdict']
            vals.setdefault('qc_user_id', self.env.user.id)
            vals.setdefault('qc_date', fields.Datetime.now())
            vals.setdefault('state', 'qc_passed' if verdict == 'pass' else 'qc_failed')
        elif 'qc_verdict' in vals and not vals['qc_verdict']:
            vals.setdefault('qc_user_id', False)
            vals.setdefault('qc_date', False)
        return super().write(vals)
