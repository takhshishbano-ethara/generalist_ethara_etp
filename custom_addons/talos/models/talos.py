from odoo import models, fields, api


class Talos(models.Model):
    _name = 'talos.talos'
    _description = 'Talos'

    task_id = fields.Char(string="Task ID", readonly=True, copy=False)
    parsona = fields.Many2one('talos.domain', string='Parsona')
    task_status = fields.Selection([('Submitted', 'Submitted'), ('NotSubmitted', 'Not Submitted')])
    employee_id = fields.Many2one('hr.employee')
    user_id = fields.Many2one(related='employee_id.user_id')
    turn_ids = fields.One2many('talos.turn', 'talos_id', string='Turns')

class TalosTurn(models.Model):
    _name = 'talos.turn'
    _description = 'Talos Turn'

    talos_id = fields.Many2one('talos.talos', string='Talos')
    turn_number = fields.Integer(string='Turn Number')
    turn_status = fields.Selection([('Pending', 'Pending'), ('Completed', 'Completed')])
    prompt = fields.Text(string='Prompt')
