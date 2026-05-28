from odoo import fields, models


class KenseiWildclawAttachment(models.Model):
    _name = "kensei_wildclaw.attachment"
    _description = "Kensei WildClaw Attachment Link"
    _inherit = "wildclaw.media.attachment"
