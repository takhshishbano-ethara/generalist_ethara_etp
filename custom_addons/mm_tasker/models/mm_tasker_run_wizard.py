# -*- coding: utf-8 -*-
"""Wizard that opens before "Run Models" — lets the tasker pick a
runs-per-model count for each of the (up to 3) active reference models.

Why a wizard instead of inline fields on the task:
    The runs-per-model value is a *dispatch parameter*, not task data.
    Storing it on the task would pollute the task model with values
    that are meaningful only at the moment of dispatch and would
    survive long after the runs were created.
"""

from odoo import api, fields, models, _
from odoo.exceptions import UserError


_MAX_RUNS_PER_MODEL = 10  # safety cap; bump if the team needs more


class MmTaskerRunWizard(models.TransientModel):
    _name = 'mm.tasker.run.wizard'
    _description = 'Run Models Wizard'

    task_id = fields.Many2one(
        'mm.tasker.task',
        string='Task',
        required=True,
        readonly=True,
        ondelete='cascade',
    )

    # Three fixed model slots. The view hides any slot whose key didn't
    # come back from default_get (e.g. if active_models only has 2
    # entries). Going with fixed slots — instead of a one2many — keeps
    # the wizard layout exactly what the spec asked for: 6 visible
    # fields, no add/remove rows.
    model_1_key = fields.Char(readonly=True)
    model_1_label = fields.Char(readonly=True)
    model_1_runs = fields.Integer(string='Runs', default=2, required=True)

    model_2_key = fields.Char(readonly=True)
    model_2_label = fields.Char(readonly=True)
    model_2_runs = fields.Integer(string='Runs', default=2, required=True)

    model_3_key = fields.Char(readonly=True)
    model_3_label = fields.Char(readonly=True)
    model_3_runs = fields.Integer(string='Runs', default=2, required=True)

    @api.model
    def default_get(self, fields_list):
        """Populate model_N_key / model_N_label from the active_models
        system parameter so the form opens with the right labels."""
        vals = super().default_get(fields_list)

        Task = self.env['mm.tasker.task']
        active_keys = Task._get_reference_models()
        labels = self.env['mm.tasker.run']._read_label_map()

        for idx in range(3):
            slot = idx + 1
            key = active_keys[idx] if idx < len(active_keys) else ''
            vals[f'model_{slot}_key'] = key
            vals[f'model_{slot}_label'] = (
                labels.get(key) or (key.replace('_', ' ').title() if key else '')
            )

        return vals

    def _collect_dispatch_plan(self):
        """Return [(model_key, runs_count), ...] for non-empty slots."""
        self.ensure_one()
        plan = []
        for slot in (1, 2, 3):
            key = (self[f'model_{slot}_key'] or '').strip()
            runs = int(self[f'model_{slot}_runs'] or 0)
            if not key:
                continue
            if runs < 1:
                raise UserError(_(
                    '%(label)s: runs must be at least 1.'
                ) % {'label': self[f'model_{slot}_label'] or key})
            if runs > _MAX_RUNS_PER_MODEL:
                raise UserError(_(
                    '%(label)s: %(runs)s runs exceeds the safety cap of %(cap)s.'
                ) % {
                    'label': self[f'model_{slot}_label'] or key,
                    'runs': runs,
                    'cap': _MAX_RUNS_PER_MODEL,
                })
            plan.append((key, runs))
        if not plan:
            raise UserError(_(
                'No models configured. Set mm_tasker.active_models in '
                'System Parameters before dispatching.'
            ))
        return plan

    def action_dispatch(self):
        """Wipe any existing runs for the task and dispatch fresh ones
        according to the wizard's per-model run counts."""
        self.ensure_one()
        task = self.task_id
        if not task:
            raise UserError(_('Wizard is missing a task reference.'))

        plan = self._collect_dispatch_plan()

        # Re-check the readiness gate. The user could have cracked open
        # a section between clicking Run Models and confirming the
        # wizard, and we don't want to silently dispatch a stale task.
        if task.state == 'draft' and not task.verdict_ready:
            raise UserError(_(
                'Task is no longer ready — all three section QCs must be Pass.'
            ))

        # Replace, don't merge: discarding prior runs avoids weird
        # mixes of old + new index ranges. If the user wants to keep
        # history they should re-open the task after evaluation.
        task.run_ids.unlink()

        Run = self.env['mm.tasker.run']
        runs_to_dispatch = Run
        for model_key, count in plan:
            for run_index in range(1, count + 1):
                runs_to_dispatch |= Run.create({
                    'task_id': task.id,
                    'model_key': model_key,
                    'run_index': run_index,
                })

        if task.state == 'draft':
            task.action_ready_for_eval()

        if runs_to_dispatch:
            self.env['mm.tasker.agent.dispatcher'].dispatch_runs(runs_to_dispatch)
        task.state = 'dispatched'

        summary = ', '.join(f'{k} × {c}' for k, c in plan)
        task.message_post(
            body=_('Dispatched: %s') % summary,
            message_type='comment',
            subtype_xmlid='mail.mt_note',
        )
        return {'type': 'ir.actions.act_window_close'}
