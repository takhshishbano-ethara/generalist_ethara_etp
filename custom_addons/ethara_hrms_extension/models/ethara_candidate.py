from odoo import fields, models


class EtharaCandidate(models.Model):
    _name = 'ethara.candidate'
    _description = 'Ethara Candidate'
    _order = 'create_date desc, id desc'
    _rec_name = 'name'

    name = fields.Char(string='Name', required=True)
    email = fields.Char(string='Email', required=True, index=True)
    mobile = fields.Char(string='Mobile', required=True)
    linkedin_url = fields.Char(string='LinkedIn URL')
    portfolio_url = fields.Char(string='Portfolio URL')
    github_url = fields.Char(string='GitHub URL')
    resume_url = fields.Char(string='Resume URL')
