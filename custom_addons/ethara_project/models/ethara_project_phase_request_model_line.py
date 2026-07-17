from odoo import api, fields, models

from .ethara_project_budget_model_line import COST_TYPE_SELECTION


class EtharaProjectPhaseRequestModelLine(models.Model):
    _name = 'ethara.project.phase.request.model.line'
    _description = 'Ethara Project Phase Request Model Line'
    _order = 'id'

    request_id = fields.Many2one(
        comodel_name='ethara.project.phase.request',
        string='Request',
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
    requested_amount = fields.Float(string='Requested (USD)')
    approved_amount = fields.Float(string='Approved (USD)')

    @api.onchange('cost_type', 'per_trajectory_cost', 'iterations')
    def _onchange_trajectory_inputs(self):
        for line in self:
            if line.cost_type == 'per_trajectory':
                line.per_task_cost = (
                    (line.per_trajectory_cost or 0.0)
                    * (line.iterations or 0)
                )
