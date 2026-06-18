from odoo import api, fields, models


class EtpProjectAwsBudgetTag(models.Model):
    _name = "etp.project.aws.budget.tag"
    _description = "Project AWS Budget Tag Filter"
    _order = "sequence, id"
    _rec_name = "display_name"

    budget_id = fields.Many2one(
        "etp.project.aws.budget", required=True, ondelete="cascade", index=True,
    )
    sequence = fields.Integer(default=10)
    tag_key = fields.Char(
        required=True, string="Tag Key",
        help="AWS cost-allocation tag key, e.g. 'team' or 'project'.",
    )
    tag_value = fields.Char(
        required=True, string="Tag Value",
        help="AWS cost-allocation tag value, e.g. 'alpha' or 'apollo'.",
    )
    active = fields.Boolean(default=True)
    last_fetched_at = fields.Datetime(
        readonly=True, string="Last Fetched At",
        help="Timestamp of the most recent AWS Cost Explorer call that filtered "
             "on this tag.",
    )
    display_name = fields.Char(compute="_compute_display_name", store=True)

    _uniq_budget_tag = models.Constraint(
        "unique(budget_id, tag_key, tag_value)",
        "Each (Tag Key, Tag Value) pair must be unique per budget.",
    )

    @api.depends("tag_key", "tag_value")
    def _compute_display_name(self):
        for rec in self:
            key = (rec.tag_key or "").strip()
            value = (rec.tag_value or "").strip()
            if key and value:
                rec.display_name = "%s=%s" % (key, value)
            elif key:
                rec.display_name = key
            else:
                rec.display_name = value or ""
