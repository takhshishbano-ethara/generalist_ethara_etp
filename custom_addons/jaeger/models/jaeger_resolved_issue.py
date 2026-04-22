from odoo import fields, models


class JaegerResolvedIssue(models.Model):
    _name = "jaeger.resolved.issue"
    _description = "Jaeger Resolved Issue"

    instance_id = fields.Many2one(
        "jaeger.instance",
        string="Instance",
        required=True,
        ondelete="cascade",
        index=True,
    )
    issue_number = fields.Integer(string="Issue Number")
    issue_title = fields.Char(string="Issue Title")
    issue_body = fields.Text(string="Issue Body")
