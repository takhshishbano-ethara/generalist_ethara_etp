# -*- coding: utf-8 -*-
from odoo import fields, models


class HrEmployee(models.Model):
    """Inherited to show Slack info on employee profile."""
    _inherit = 'hr.employee'

    slack_user_ref = fields.Char(
        related='user_id.slack_user_ref',
        string="Slack User ID",
        readonly=True,
        store=False,
        help="Slack member ID (read from linked Odoo user)")
    slack_exclude = fields.Boolean(
        related='user_id.slack_exclude',
        string="Exclude from Slack",
        readonly=False,
        store=False,
        help="If checked, this employee will be skipped during Slack sync")
