# -*- coding: utf-8 -*-
from odoo import fields, models


class MmTaskerRubric(models.Model):
    _name = 'mm.tasker.rubric'
    _description = 'MM Tasker Rubric Item (parsed from rubrics.jsonl)'
    _order = 'task_id, number, id'

    task_id = fields.Many2one(
        'mm.tasker.task',
        string='Task',
        required=True,
        ondelete='cascade',
        index=True,
    )
    number = fields.Integer(string='#', required=True)
    rubric_type = fields.Char(string='Type')
    category = fields.Char(string='Category')
    points = fields.Integer(string='Points')
    importance = fields.Char(string='Importance')
    criterion = fields.Text(string='Criterion')
    raw_json = fields.Text(string='Raw JSON', help='Original JSON line preserved for export.')
