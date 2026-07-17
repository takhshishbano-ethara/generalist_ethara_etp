from odoo import api, fields, models


class EtharaProjectBudgetTagLine(models.Model):
    _name = 'ethara.project.budget.tag.line'
    _description = 'Ethara Project Budget AWS Tag Filter'
    _order = 'sequence, id'
    _rec_name = 'tag_key'

    budget_id = fields.Many2one(
        comodel_name='ethara.project.budget',
        required=True,
        ondelete='cascade',
        index=True,
    )
    tag_key = fields.Char(required=True, string='Tag Key')
    tag_value = fields.Char(required=True, string='Tag Value')
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    display_name = fields.Char(compute='_compute_display_name', store=False)

    _sql_constraints = [
        (
            'uniq_tag_per_budget',
            'unique(budget_id, tag_key, tag_value)',
            'Each tag pair must be unique per budget.',
        ),
    ]

    @api.depends('tag_key', 'tag_value')
    def _compute_display_name(self):
        for rec in self:
            if rec.tag_key and rec.tag_value:
                rec.display_name = '%s=%s' % (rec.tag_key, rec.tag_value)
            elif rec.tag_key:
                rec.display_name = rec.tag_key
            else:
                rec.display_name = ''
