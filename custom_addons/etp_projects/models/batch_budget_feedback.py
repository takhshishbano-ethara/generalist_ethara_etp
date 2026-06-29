from odoo import fields, models


RATING_SELECTION = [
    ("positive", "Positive"),
    ("neutral", "Neutral"),
    ("negative", "Negative"),
]


class EtpBatchBudgetFeedback(models.Model):
    _name = "etp.batch.budget.feedback"
    _description = "Phase Budget Client Feedback"
    _order = "date desc, id desc"

    batch_id = fields.Many2one(
        "etp.batch.budget",
        string="Phase Budget",
        required=True,
        ondelete="cascade",
        index=True,
    )
    author_id = fields.Many2one(
        "res.users",
        string="Author",
        default=lambda self: self.env.user,
    )
    date = fields.Date(
        string="Date",
        default=fields.Date.context_today,
        required=True,
    )
    rating = fields.Selection(
        RATING_SELECTION,
        string="Rating",
    )
    note = fields.Text(string="Note")
