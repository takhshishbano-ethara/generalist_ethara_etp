# from odoo import models, fields, api


# class etp_user_roles(models.Model):
#     _name = 'etp_user_roles.etp_user_roles'
#     _description = 'etp_user_roles.etp_user_roles'

#     name = fields.Char()
#     value = fields.Integer()
#     value2 = fields.Float(compute="_value_pc", store=True)
#     description = fields.Text()
#
#     @api.depends('value')
#     def _value_pc(self):
#         for record in self:
#             record.value2 = float(record.value) / 100

