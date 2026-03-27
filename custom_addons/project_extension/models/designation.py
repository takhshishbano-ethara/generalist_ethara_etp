from odoo import models, fields, api, _

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
    is_qc_review = fields.Boolean(default=False)
    is_tasker = fields.Boolean(default=False)
    experience_years = fields.Float(string="Total Experience")

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('designation_id'):
                if self.env.ref('designation_jr_software_engineer').id == int(vals.get('designation_id')):
                    vals['is_tasker'] = True
        employees = super(HrEmployee, self).create(vals_list)
        for emp in employees:
            if emp.whatsapp_number:
                self.env['whatsapp.group.members'].sudo().create({
                    'name': emp.name,
                    'email': emp.work_email,
                    'country_code': '+91',
                    'phone_number': emp.whatsapp_number
                })
        return employees


    def write(self, vals):
        result = super(HrEmployee, self).write(vals)
        if vals.get('whatsapp_number'):
            if not self.env['whatsapp.group.members'].sudo().search_count(
                    [('phone_number', '=', vals.get('whatsapp_number'))]):
                self.env['whatsapp.group.members'].sudo().create({
                    'name': self.name,
                    'email': self.work_email,
                    'country_code': '+91',
                    'phone_number': self.whatsapp_number
                })
        return result

class HrJob(models.Model):
    _inherit = 'hr.job'

    designation_id = fields.Many2one('hr.employee.designation', string="Default Designation")
