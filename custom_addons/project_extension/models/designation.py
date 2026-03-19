from odoo import models, fields

class EmployeeDesignation(models.Model):
    _name = 'hr.employee.designation'
    _description = 'Employee Designation'

    name = fields.name = fields.Char(string="Designation Name", required=True)
    code = fields.Char(string="Code")
    active = fields.Boolean(default=True)

class HrEmployee(models.Model):
    _inherit = 'hr.employee'

    designation_id = fields.Many2one('hr.employee.designation', string="Designation")
    whatsapp_number = fields.Char(string="Whatsapp Number")

class HrJob(models.Model):
    _inherit = 'hr.job'

    designation_id = fields.Many2one('hr.employee.designation', string="Default Designation")