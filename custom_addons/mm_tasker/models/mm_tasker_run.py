# -*- coding: utf-8 -*-
import logging

from odoo import api, fields, models, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class MmTaskerRun(models.Model):
    _name = 'mm.tasker.run'
    _description = 'MM Tasker Reference Model Run'
    _order = 'task_id, model_key, run_index'
    _rec_name = 'display_name'

    task_id = fields.Many2one(
        'mm.tasker.task',
        string='Task',
        required=True,
        ondelete='cascade',
        index=True,
    )
    model_key = fields.Char(string='Model', required=True, index=True)
    # 1-indexed to mirror goku's on-disk layout (run_1/, run_2/, ...).
    run_index = fields.Integer(
        string='Run #',
        default=1,
        required=True,
        index=True,
    )
    model_label = fields.Char(compute='_compute_model_label', store=True)
    display_name = fields.Char(compute='_compute_display_name', store=True)

    state = fields.Selection(
        [
            ('queued', 'Queued'),
            ('running', 'Running'),
            ('judging', 'Judging'),
            ('scored', 'Scored'),
            ('error', 'Error'),
        ],
        default='queued',
        required=True,
        index=True,
    )

    response_text = fields.Text(string='Response')
    output_file_ids = fields.One2many('mm.tasker.run.output', 'run_id', string='Output Files')
    score_ids = fields.One2many('mm.tasker.run.score', 'run_id', string='Per-rubric Scores')

    awarded = fields.Integer(readonly=True)
    max_total = fields.Integer(readonly=True)
    raw_score = fields.Float(readonly=True, digits=(8, 4))
    per_task_score = fields.Float(readonly=True, digits=(8, 4))
    passed = fields.Boolean(readonly=True, index=True)

    started_at = fields.Datetime(readonly=True)
    finished_at = fields.Datetime(readonly=True)
    duration_s = fields.Float(compute='_compute_duration', store=True, digits=(10, 2))

    tokens_in = fields.Integer(readonly=True)
    tokens_out = fields.Integer(readonly=True)

    error_message = fields.Text(readonly=True)

    llm_judge_count = fields.Integer(
        compute='_compute_llm_judge_counts',
        store=True,
        help='Number of rubrics on this run that were graded by the LLM judge.',
    )
    llm_judge_passed_count = fields.Integer(
        compute='_compute_llm_judge_counts',
        store=True,
        help='Number of LLM-judged rubrics where the agent passed.',
    )

    _uniq_task_model_run = models.Constraint(
        'UNIQUE(task_id, model_key, run_index)',
        'Each (task, model, run_index) combination must be unique.',
    )

    @api.model
    def _read_label_map(self):
        raw = self.env['ir.config_parameter'].sudo().get_param(
            'mm_tasker.model_labels', default='',
        ) or ''
        labels = {}
        for pair in raw.split(','):
            if ':' in pair:
                k, v = pair.split(':', 1)
                k, v = k.strip(), v.strip()
                if k:
                    labels[k] = v
        return labels

    @api.depends('model_key')
    def _compute_model_label(self):
        labels = self._read_label_map()
        for rec in self:
            key = rec.model_key or ''
            rec.model_label = labels.get(key) or key.replace('_', ' ').title()

    @api.depends('task_id', 'model_label', 'run_index')
    def _compute_display_name(self):
        for rec in self:
            task_name = rec.task_id.name or '?'
            suffix = f' · run {rec.run_index}' if rec.run_index and rec.run_index > 1 else ''
            rec.display_name = f'{task_name} · {rec.model_label}{suffix}'

    @api.depends('score_ids', 'score_ids.judged_by', 'score_ids.passed')
    def _compute_llm_judge_counts(self):
        for rec in self:
            judges = rec.score_ids.filtered(lambda s: s.judged_by == 'llm_judge')
            rec.llm_judge_count = len(judges)
            rec.llm_judge_passed_count = sum(1 for s in judges if s.passed)

    @api.depends('started_at', 'finished_at')
    def _compute_duration(self):
        for rec in self:
            if rec.started_at and rec.finished_at:
                delta = fields.Datetime.from_string(rec.finished_at) - fields.Datetime.from_string(rec.started_at)
                rec.duration_s = delta.total_seconds()
            else:
                rec.duration_s = 0.0

    def action_rerun(self):
        """Reset and re-dispatch a single run. Manager-only via record rule."""
        for rec in self:
            rec.write({
                'state': 'queued',
                'response_text': False,
                'awarded': 0,
                'max_total': 0,
                'raw_score': 0.0,
                'per_task_score': 0.0,
                'passed': False,
                'started_at': False,
                'finished_at': False,
                'tokens_in': 0,
                'tokens_out': 0,
                'error_message': False,
            })
            rec.output_file_ids.unlink()
            rec.score_ids.unlink()
        self.env['mm.tasker.agent.dispatcher'].dispatch_runs(self)
        return True

    # Rubric types graded by the LLM judge (separate /judge endpoint on
    # the backend, separate Run Judge button up in the task header).
    _JUDGE_TYPES = ('response_criteria', 'response_not_criteria')

    def action_regrade(self):
        """Re-grade this run via the goku /regrade endpoint.

        Goku re-scores the current rubrics against the persisted
        workspace + response on disk and returns fresh scores. We
        replace all non-judge scores in place and recompute the
        aggregate. The LLM judge is a separate endpoint — for
        response_criteria / response_not_criteria rubrics the user
        still uses Run Judge in the task header (or it auto-runs on
        the next dispatch).

        Cost: cheap. No agent execution, no LLM tokens. Just goku
        re-running deterministic scorers against files that already
        exist on its disk.
        """
        dispatcher = self.env['mm.tasker.agent.dispatcher']
        Score = self.env['mm.tasker.run.score']

        for rec in self:
            if rec.state != 'scored':
                raise UserError(_(
                    'Re-grade is only available for runs in the Scored state. '
                    'This run is %s.'
                ) % (rec.state or '?'))
            if not rec.task_id.rubric_ids:
                raise UserError(_('No rubrics to grade — upload rubrics.jsonl on the Rubrics tab first.'))

            new_scores = dispatcher.call_regrade(rec, rec.task_id.rubric_ids)

            # Index backend scores by rubric number for O(1) lookup
            # (same pattern as _grade_run on the dispatcher).
            scores_by_num = {
                int(s.get('rubric_number')): s
                for s in new_scores
                if s.get('rubric_number') is not None
            }

            # Wipe all NON-judge scores. The LLM judge scores survive
            # because /regrade doesn't touch response_criteria — that's
            # what Run Judge is for. Without this carve-out, regrade
            # would erase the judge's verdicts and the user would have
            # to rerun the judge after every regrade.
            (rec.score_ids
                .filtered(lambda s: s.judged_by != 'llm_judge')
                .unlink())

            for rubric in rec.task_id.rubric_ids:
                if rubric.rubric_type in self._JUDGE_TYPES:
                    continue
                bs = scores_by_num.get(rubric.number)
                if not bs:
                    continue
                Score.create(
                    dispatcher._row_from_backend_score(rec, rubric, bs)
                )

            dispatcher._compute_aggregate(rec)

            # mm.tasker.run doesn't inherit mail.thread — post on the
            # parent task's chatter.
            rec.task_id.message_post(
                body=_('Re-graded %(model)s · run %(idx)s via /regrade — %(n)s rubric verdict(s) refreshed.') % {
                    'model': rec.model_label or rec.model_key,
                    'idx': rec.run_index,
                    'n': len(new_scores),
                },
                message_type='comment',
                subtype_xmlid='mail.mt_note',
            )
        return True


class MmTaskerRunOutput(models.Model):
    _name = 'mm.tasker.run.output'
    _description = 'MM Tasker Run Output File'
    _order = 'run_id, name'

    run_id = fields.Many2one('mm.tasker.run', required=True, ondelete='cascade', index=True)
    name = fields.Char(required=True)
    file = fields.Binary(attachment=True)
    sha256 = fields.Char(string='SHA-256', readonly=True)
    size_bytes = fields.Integer(readonly=True)


class MmTaskerRunScore(models.Model):
    _name = 'mm.tasker.run.score'
    _description = 'MM Tasker Per-Rubric Score'
    _order = 'run_id, rubric_id'

    run_id = fields.Many2one('mm.tasker.run', required=True, ondelete='cascade', index=True)
    rubric_id = fields.Many2one('mm.tasker.rubric', required=True, ondelete='cascade')

    rubric_number = fields.Integer(related='rubric_id.number', store=True)
    rubric_type = fields.Char(related='rubric_id.rubric_type', store=True)
    rubric_category = fields.Char(related='rubric_id.category', store=True)
    rubric_importance = fields.Char(related='rubric_id.importance', store=True)
    rubric_points = fields.Integer(related='rubric_id.points', store=True)
    rubric_criterion = fields.Text(related='rubric_id.criterion')

    passed = fields.Boolean(string='Passed')
    triggered = fields.Boolean(string='Triggered (negation)')
    awarded_points = fields.Integer(readonly=True)
    judged_by = fields.Selection(
        [('probe', 'Probe'), ('regex', 'Regex'), ('llm_judge', 'LLM Judge'), ('not_applicable', 'Not Applicable')],
        readonly=True,
    )
    judge_rationale = fields.Text(readonly=True)
    judge_confidence = fields.Float(
        string='Confidence',
        digits=(3, 2),
        readonly=True,
        help='LLM judge self-reported confidence (0-1). Set only for llm_judge scores.',
    )
    judge_raw_response = fields.Text(
        string='Raw Judge Response',
        readonly=True,
        help='The literal text the judge model returned, before JSON parsing.',
    )
    judge_tokens_in = fields.Integer(readonly=True)
    judge_tokens_out = fields.Integer(readonly=True)
