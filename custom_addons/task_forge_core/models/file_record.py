from odoo import models, fields


class TaskForgeFile(models.Model):
    _name = 'task.forge.file'
    _description = 'Task Forge File Record'
    _order = 'create_date desc'

    name = fields.Char(string='Original Filename', required=True)
    storage_path = fields.Char(string='Storage Path')
    content_type = fields.Char(string='Content Type')
    size = fields.Integer(string='Size (bytes)')
    uploaded_by_id = fields.Many2one('hr.employee', string='Uploaded By')
    is_deleted = fields.Boolean(string='Deleted', default=False)
