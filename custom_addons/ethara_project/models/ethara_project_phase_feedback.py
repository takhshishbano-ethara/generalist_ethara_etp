from odoo import api, fields, models


RATING_SELECTION = [
    ("positive", "Positive"),
    ("neutral", "Neutral"),
    ("negative", "Negative"),
]


class EtharaProjectPhaseFeedback(models.Model):
    _name = "ethara.project.phase.feedback"
    _description = "Ethara Project Phase Client Feedback"
    _order = "date desc, id desc"

    phase_id = fields.Many2one(
        "ethara.project.phase",
        string="Phase",
        required=True,
        ondelete="cascade",
        index=True,
    )
    ethara_project_id = fields.Many2one(
        "ethara.project",
        related="phase_id.ethara_project_id",
        store=True,
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

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        for rec in records:
            phase = rec.phase_id
            if phase:
                rating_label = dict(RATING_SELECTION).get(rec.rating) or "n/a"
                phase._post_project_thread(
                    "<p><strong>Phase feedback recorded:</strong> "
                    "%s (rating: %s)</p>" % (
                        phase.name or "",
                        rating_label,
                    )
                )
        return records
