# -*- coding: utf-8 -*-
from odoo import models, fields, api, _
from odoo.exceptions import AccessError, ValidationError


class KenseiTrackerStageHandoff(models.TransientModel):
    """Allocate the NEXT stage of a task once the current stage is Deliverable.

    A task is worked in stages, and each stage is its own allocation record (see
    kensei.tracker.allocation.stage_no / parent_id). This wizard creates the next
    link in that chain: same Task ID and Persona, one stage further, assigned to
    the chosen tasker — with every stage input left BLANK so the existing
    pipeline replays from the start (``_compute_status`` derives 'In Progress'
    from the empty stage fields on its own).
    """
    _name = 'kensei.tracker.stage.handoff'
    _description = 'Kensei Tracker — Hand off to Next Stage'

    allocation_id = fields.Many2one(
        'kensei.tracker.allocation', string='Completed Stage',
        required=True, readonly=True, ondelete='cascade',
    )
    task_id = fields.Char(related='allocation_id.task_id', readonly=True)
    persona_id = fields.Many2one(
        related='allocation_id.persona_id', readonly=True)
    current_stage_no = fields.Integer(
        related='allocation_id.stage_no', readonly=True)
    next_stage_no = fields.Integer(
        string='Next Stage', compute='_compute_next_stage_no')
    current_tasker_id = fields.Many2one(
        related='allocation_id.tasker_member_id', string='Current Tasker',
        readonly=True,
    )
    # Defaults to the same tasker (a stage often continues with the same person),
    # but a QL/PL can hand the next stage to anyone on the roster.
    tasker_member_id = fields.Many2one(
        'kensei.tracker.team.member', string='Next Tasker', required=True,
        default=lambda self: self._default_tasker(),
        help='Who works the next stage. Defaults to the current tasker; pick '
             'another team member to hand the task over.',
    )
    assigned_date = fields.Date(
        string='Assignment Date', required=True,
        default=fields.Date.context_today,
    )

    @api.model
    def _default_tasker(self):
        alloc = self.env['kensei.tracker.allocation'].browse(
            self.env.context.get('default_allocation_id', []))
        return alloc.tasker_member_id.id if alloc else False

    @api.depends('allocation_id.stage_no')
    def _compute_next_stage_no(self):
        for wiz in self:
            wiz.next_stage_no = wiz.allocation_id.stage_no + 1

    def action_confirm(self):
        self.ensure_one()
        alloc = self.allocation_id

        # Only a QL/PL (or admin) may allocate work — mirrors the guard on the
        # allocation's own core fields.
        if not (self.env.su
                or self.env.user.has_group('kensei.group_kensei_ql')
                or self.env.user.has_group('base.group_system')):
            raise AccessError(_(
                "Only a Project Lead / QL can hand a task off to the next stage."))

        # Re-check server-side: the button may have been rendered from stale data,
        # and a stage must never be handed off twice (the unique(task_id, stage_no)
        # constraint would catch it, but this gives a readable error).
        if not alloc.can_hand_off:
            raise ValidationError(_(
                "Stage %(stage)s of task %(task)s cannot be handed off: it has "
                "not reached 'Ready for Next Stage' yet, it is the final stage, "
                "or the next stage already exists.",
                stage=alloc.stage_no, task=alloc.task_id,
            ))

        # Same task + persona, one stage further, new tasker. Every stage input
        # (drive links, QC statuses, scores, notes) is deliberately omitted so the
        # new record starts clean and _compute_status puts it back at In Progress.
        nxt = self.env['kensei.tracker.allocation'].create({
            'task_id': alloc.task_id,
            'stage_no': alloc.stage_no + 1,
            # carried forward so the chain knows which stage is the last one —
            # only that stage may reach 'Deliverable'.
            'total_stages': alloc.total_stages,
            'parent_id': alloc.id,
            'persona_id': alloc.persona_id.id,
            'tasker_member_id': self.tasker_member_id.id,
            'assigned_date': self.assigned_date,
        })

        alloc.message_post(body=_(
            "Handed off to stage %(stage)s — assigned to %(tasker)s.",
            stage=nxt.stage_no, tasker=nxt.tasker_member_id.display_name,
        ))

        return {
            'type': 'ir.actions.act_window',
            'name': _('Task %(task)s — Stage %(stage)s',
                      task=nxt.task_id, stage=nxt.stage_no),
            'res_model': 'kensei.tracker.allocation',
            'res_id': nxt.id,
            'view_mode': 'form',
            'target': 'current',
        }
