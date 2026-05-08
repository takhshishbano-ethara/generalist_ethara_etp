from odoo import models, fields, api
from odoo.exceptions import UserError


class RubricCategory(models.Model):
    _name = 'rubric.category'
    _description = 'Rubric Category'
    _order = 'sequence, id'

    name = fields.Char(string='Category Name', required=True)
    description = fields.Text(string='Description')
    sequence = fields.Integer(default=10)
    project_id = fields.Many2one('project.project', string='Project', ondelete='cascade', index=True)
    option_ids = fields.One2many('rubric.category.option', 'category_id', string='Options')
    dimension_ids = fields.One2many('rubric.dimension', 'category_id', string='Dimensions')

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        for rec in records:
            for dim in rec.dimension_ids:
                dim._sync_options_from_category()
        return records


class RubricCategoryOption(models.Model):
    _name = 'rubric.category.option'
    _description = 'Rubric Category Option'
    _order = 'sequence, id'

    name = fields.Char(string='Option Label', required=True)
    value = fields.Integer(string='Score Value', default=0)
    sequence = fields.Integer(default=10)
    category_id = fields.Many2one('rubric.category', string='Category', ondelete='cascade', required=True)
    is_temp = fields.Boolean(string='Temporary', default=False)
    active = fields.Boolean(string='Active', default=True)

    def action_approve_temp(self):
        for rec in self:
            if not rec.is_temp:
                raise UserError(f"Option '{rec.name}' is already approved.")
            rec.is_temp = False

    def action_reject_temp(self):
        self.filtered('is_temp').write({'active': False})

    def write(self, vals):
        res = super().write(vals)
        for opt in self:
            for dim in opt.category_id.dimension_ids:
                dim._sync_options_from_category()
        return res

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        categories = records.mapped('category_id')
        for cat in categories:
            for dim in cat.dimension_ids:
                dim._sync_options_from_category()
        return records

    def unlink(self):
        categories = self.mapped('category_id')
        res = super().unlink()
        for cat in categories:
            for dim in cat.dimension_ids:
                dim._sync_options_from_category()
        return res


class RubricDimension(models.Model):
    _name = 'rubric.dimension'
    _description = 'Rubric Dimension'
    _order = 'sequence, id'

    name = fields.Char(string='Dimension Name', required=True)
    description = fields.Text(string='Description')
    is_required = fields.Boolean(string='Required', default=True)
    sequence = fields.Integer(default=10)
    category_id = fields.Many2one('rubric.category', string='Category', ondelete='cascade', required=True)
    project_id = fields.Many2one(related='category_id.project_id', store=True)
    option_ids = fields.Many2many(
        'rubric.category.option',
        'rubric_dimension_option_rel',
        'dimension_id',
        'option_id',
        string='Options',
    )
    is_temp = fields.Boolean(string='Temporary', default=False)
    active = fields.Boolean(string='Active', default=True)

    def action_approve_temp(self):
        for rec in self:
            if not rec.is_temp:
                raise UserError(f"Dimension '{rec.name}' is already approved.")
            rec.is_temp = False

    def action_reject_temp(self):
        self.filtered('is_temp').write({'active': False})

    def _sync_options_from_category(self):
        for dim in self:
            dim.option_ids = [(6, 0, dim.category_id.option_ids.ids)]

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        for rec in records:
            rec._sync_options_from_category()
        return records
