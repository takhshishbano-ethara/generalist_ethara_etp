from odoo import api, fields, models


COST_TYPE_SELECTION = [
    ('per_task', 'Per Task'),
    ('per_trajectory', 'Per Trajectory'),
]


class EtharaProjectBudgetModelLine(models.Model):
    _name = 'ethara.project.budget.model.line'
    _description = 'Ethara Project Budget Model Line'
    _order = 'id'

    budget_id = fields.Many2one(
        comodel_name='ethara.project.budget',
        string='Project Budget',
        required=True,
        ondelete='cascade',
        index=True,
    )
    ai_model_id = fields.Many2one(
        comodel_name='ethara.project.ai.model',
        string='Provider',
        required=True,
    )
    ai_model_name = fields.Char(string='Model')
    cost_type = fields.Selection(
        selection=COST_TYPE_SELECTION,
        string='Cost Type',
        default='per_task',
        required=True,
    )
    per_trajectory_cost = fields.Float(string='Per Trajectory Cost (USD)')
    iterations = fields.Integer(string='No. of Trajectories per Task')
    per_task_cost = fields.Float(string='Per Task Cost (USD)')

    @api.onchange('cost_type', 'per_trajectory_cost', 'iterations')
    def _onchange_trajectory_inputs(self):
        for line in self:
            if line.cost_type == 'per_trajectory':
                line.per_task_cost = (
                    (line.per_trajectory_cost or 0.0)
                    * (line.iterations or 0)
                )
