from odoo import fields, models


class FenrirRubricScore(models.Model):
    _name = "fenrir.rubric.score"
    _description = "Fenrir Per-Seller Rubric Score"
    _order = "seller_offer_id, rubric_sequence, rubric_id"
    _rec_name = "rubric_name"

    seller_offer_id = fields.Many2one(
        comodel_name="fenrir.seller.offer",
        string="Seller",
        required=True,
        ondelete="cascade",
        index=True,
    )
    task_id = fields.Many2one(
        comodel_name="fenrir.task",
        related="seller_offer_id.task_id",
        store=True,
        index=True,
        readonly=True,
    )
    seller_no = fields.Integer(related="seller_offer_id.seller_no",
                               store=True, readonly=True)
    rubric_id = fields.Many2one(
        comodel_name="fenrir.rubric",
        string="Rubric",
        required=True,
        ondelete="cascade",
        index=True,
    )
    rubric_name = fields.Char(related="rubric_id.name",
                              string="Rubric", store=True, readonly=True)
    rubric_description = fields.Text(related="rubric_id.description",
                                     string="Description", readonly=True)
    rubric_sequence = fields.Integer(related="rubric_id.sequence",
                                     store=True, readonly=True)

    rating = fields.Integer(string="Rating")
    justification = fields.Text(string="Justification")

    _sql_constraints = [
        ("fenrir_rubric_score_unique",
         "unique(seller_offer_id, rubric_id)",
         "A rubric can only be scored once per seller."),
    ]
