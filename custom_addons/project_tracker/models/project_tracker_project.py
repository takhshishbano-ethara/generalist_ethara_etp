# -*- coding: utf-8 -*-
from odoo import models, fields, _
from odoo.exceptions import ValidationError


class Kensei2TrackerProject(models.Model):
    """A project the Tracker allocates work under.

    Deliberately lightweight — a label to slice allocations, dashboards and
    the daily tracker by, NOT a copy of Odoo's project.project (the Tracker's
    pipeline is its own workflow; linking the stock Project app would drag in
    stages/tasks that mean something different here).
    """
    _name = 'project.tracker.project'
    _description = 'Project Tracker — Project'
    _order = 'name'

    name = fields.Char(string='Project', required=True, index=True)
    code = fields.Char(
        string='Code', index=True,
        help='Short reference used in lists and exports (e.g. KEN2, ARC).')
    project_type = fields.Selection(
        [('sft', 'SFT'), ('rlhf', 'RLHF'), ('other', 'Other')],
        string='Type', default='sft', required=True, index=True,
        help='Drives which persona attributes apply: SFT projects use '
             'Domain / Agent Type, RLHF projects use the L1/L2 taxonomy.')
    description = fields.Text(string='Description')
    active = fields.Boolean(
        default=True,
        help='Archive a finished project to hide it from selectors without '
             'losing the history on its allocations.')
    allocation_ids = fields.One2many(
        'project.tracker.allocation', 'project_id', string='Allocations')
    allocation_count = fields.Integer(
        string='Allocations', compute='_compute_allocation_count')
    persona_ids = fields.One2many(
        'kensei2.persona', 'pt_project_id', string='Personas')
    persona_count = fields.Integer(
        string='Personas', compute='_compute_persona_count')
    team_member_ids = fields.Many2many(
        'project.tracker.team.member',
        'project_tracker_project_team_member_rel',
        'project_id', 'member_id',
        string='Team Members')
    team_count = fields.Integer(
        string='Team', compute='_compute_team_count')

    _name_uniq = models.Constraint(
        'unique(name)', 'A project with this name already exists.')

    def _compute_allocation_count(self):
        counts = dict(self.env['project.tracker.allocation']._read_group(
            [('project_id', 'in', self.ids)], ['project_id'], ['__count']))
        for rec in self:
            rec.allocation_count = counts.get(rec, 0)

    def _compute_persona_count(self):
        counts = dict(self.env['kensei2.persona']._read_group(
            [('pt_project_id', 'in', self.ids)], ['pt_project_id'], ['__count']))
        for rec in self:
            rec.persona_count = counts.get(rec, 0)

    def _compute_team_count(self):
        for rec in self:
            rec.team_count = len(rec.team_member_ids)

    def action_view_allocations(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('%s — Allocations', self.name),
            'res_model': 'project.tracker.allocation',
            'domain': [('project_id', '=', self.id)],
            'view_mode': 'list,form',
            'context': {'default_project_id': self.id},
        }

    def action_open_dashboard(self):
        """Open the org dashboard scoped to this project (via context)."""
        self.ensure_one()
        return {
            'type': 'ir.actions.client',
            'tag': 'project_tracker_dashboard',
            'name': _('%s — Dashboard', self.name),
            'context': {'active_project_id': self.id},
        }

    def ondelete(self):
        for rec in self:
            if rec.allocation_ids:
                raise ValidationError(_(
                    'Project "%s" still has allocations — archive it instead '
                    'of deleting so the history stays intact.', rec.name))

    def unlink(self):
        self.ondelete()
        return super().unlink()
