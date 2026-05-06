from odoo import models, fields, api
from odoo.exceptions import UserError


class TaskForgeRubricRating(models.Model):
    _name = 'task.forge.rubric.rating'
    _description = 'Task Forge Rubric Rating'
    _order = 'log_id, dimension_id'

    log_id = fields.Many2one(
        'task.forge.log', string='Task Log',
        required=True, ondelete='cascade', index=True,
    )
    dimension_id = fields.Many2one(
        'rubric.dimension', string='Dimension',
        required=True, ondelete='restrict',
    )
    option_id = fields.Many2one(
        'rubric.category.option', string='Selected Option',
        required=True, ondelete='restrict',
    )

    category_id = fields.Many2one(
        related='dimension_id.category_id', store=True, string='Category',
    )
    project_id = fields.Many2one(
        related='log_id.project_id', store=True, string='Project',
    )
    employee_id = fields.Many2one(
        related='log_id.employee_id', store=True, string='Employee',
    )

    dimension_name_snapshot = fields.Char(string='Dimension (Snapshot)')
    option_name_snapshot = fields.Char(string='Option (Snapshot)')
    option_value_snapshot = fields.Integer(string='Option Value (Snapshot)')

    _uniq_log_dimension = models.Constraint(
        'UNIQUE(log_id, dimension_id)',
        'A dimension can only be rated once per task.',
    )

    @api.model_create_multi
    def create(self, vals_list):
        Dimension = self.env['rubric.dimension'].sudo()
        Option = self.env['rubric.category.option'].sudo()
        for vals in vals_list:
            dim = Dimension.browse(vals.get('dimension_id'))
            opt = Option.browse(vals.get('option_id'))
            if dim and dim.exists():
                vals.setdefault('dimension_name_snapshot', dim.name or '')
            if opt and opt.exists():
                vals.setdefault('option_name_snapshot', opt.name or '')
                vals.setdefault('option_value_snapshot', opt.value or 0)
        return super().create(vals_list)

    def _ensure_editable(self):
        for rec in self:
            if rec.log_id and rec.log_id.state == 'completed':
                raise UserError(
                    'Rubric ratings are locked after task completion.'
                )

    def write(self, vals):
        self._ensure_editable()
        return super().write(vals)

    def unlink(self):
        self._ensure_editable()
        return super().unlink()
