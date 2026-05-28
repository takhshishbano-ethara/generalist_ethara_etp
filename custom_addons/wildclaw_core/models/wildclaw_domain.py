from odoo import fields, models


class WildclawDomain(models.Model):
    _name = "wildclaw.domain"
    _description = "WildClaw Domain (hierarchical L1/L2 classification)"
    _rec_name = "name"
    _order = "name"

    name = fields.Char(string="Name", required=True)
    parent_id = fields.Many2one("wildclaw.domain", string="Parent")
    child_ids = fields.One2many("wildclaw.domain", "parent_id", string="Children")
    md_file1 = fields.Char(string="MD File 1")
    md_file2 = fields.Char(string="MD File 2")
    md_file3 = fields.Char(string="MD File 3")
