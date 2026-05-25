from odoo import api, fields, models


YES_NO_SELECTION = [
    ("yes", "Yes"),
    ("no", "No"),
    ("cancelled", "Cancelled"),
]


class FenrirSellerOffer(models.Model):
    _name = "fenrir.seller.offer"
    _description = "Fenrir Seller Offer / Negotiation"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "task_id, seller_no, id"
    _rec_name = "display_name"

    task_id = fields.Many2one(
        comodel_name="fenrir.task",
        string="Task",
        required=True,
        ondelete="cascade",
        index=True,
    )
    task_code = fields.Char(related="task_id.code", string="Task Code", store=True)
    category_id = fields.Many2one(
        comodel_name="fenrir.category",
        related="task_id.category_id",
        string="Category",
        store=True,
    )

    seller_no = fields.Integer(string="Seller #", copy=False)
    seller = fields.Char(string="Seller Name",
                         help="Optional freelancer name / Fiverr handle",
                         tracking=True)
    display_name = fields.Char(compute="_compute_display_name", store=True)

    received_custom_offer = fields.Selection(
        selection=YES_NO_SELECTION,
        string="Received Custom Offer",
        default="no",
        tracking=True,
    )
    sellers_initial_ask = fields.Float(string="Seller's Initial Ask")
    negotiated_offer = fields.Char(
        string="Negotiated Offer",
        help="May be a single value or range, e.g. '$150-$200'",
    )
    conversation = fields.Text(string="Conversation",
                               help="Pasted Fiverr / chat transcript")

    accepted = fields.Selection(
        selection=YES_NO_SELECTION,
        string="Accepted Offer",
        default="no",
        tracking=True,
    )

    final_payment_amount = fields.Float(string="Final Payment Amount")
    final_payment_currency = fields.Selection(
        selection=[
            ("USD", "USD"),
            ("INR", "INR"),
            ("EUR", "EUR"),
            ("GBP", "GBP"),
            ("OTHER", "Other"),
        ],
        string="Currency",
        default="USD",
    )

    delivery_received = fields.Selection(
        selection=YES_NO_SELECTION,
        string="Delivery Received",
        default="no",
        tracking=True,
    )
    accepted_delivery = fields.Selection(
        selection=YES_NO_SELECTION + [("refunded", "Cancelled & Refunded")],
        string="Accepted Delivery",
        default="no",
        tracking=True,
    )

    deliverables_link = fields.Char(string="Deliverables Link")
    data_media = fields.Char(string="Data (Media)")
    resources = fields.Char(string="Resources",
                            help="References and supporting documents")
    environment = fields.Char(string="Environment")
    test_unit_tests = fields.Char(string="Test (Unit Tests)")
    license_ref = fields.Char(string="License")
    metadata_json = fields.Text(string="Metadata.json")
    automated_checks = fields.Text(string="Automated Checks")

    rubric_score_ids = fields.One2many(
        comodel_name="fenrir.rubric.score",
        inverse_name="seller_offer_id",
        string="Rubric Scores",
    )
    overall_rating = fields.Float(string="Overall Rating", tracking=True)
    overall_justification = fields.Text(string="Overall Justification")

    notes = fields.Text(string="Internal Notes")

    @api.depends("seller_no", "seller")
    def _compute_display_name(self):
        for rec in self:
            label = f"Seller {rec.seller_no}" if rec.seller_no else "Seller —"
            rec.display_name = f"{label} — {rec.seller}" if rec.seller else label

    @api.onchange("task_id")
    def _onchange_task_id_populate_rubric_scores(self):
        Score = self.env["fenrir.rubric.score"]
        for rec in self:
            existing_rubric_ids = rec.rubric_score_ids.mapped("rubric_id")
            missing = rec.task_id.rubric_ids - existing_rubric_ids
            for rubric in missing:
                rec.rubric_score_ids |= Score.new({"rubric_id": rubric.id})

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        task_id = self.env.context.get("default_task_id") or res.get("task_id")
        if task_id and "rubric_score_ids" in fields_list:
            task = self.env["fenrir.task"].browse(task_id)
            if task.exists() and task.rubric_ids:
                res["rubric_score_ids"] = [
                    (0, 0, {"rubric_id": r.id}) for r in task.rubric_ids
                ]
        return res

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get("seller_no") and vals.get("task_id"):
                existing = self.search_count([("task_id", "=", vals["task_id"])])
                vals["seller_no"] = existing + 1
        records = super().create(vals_list)
        Score = self.env["fenrir.rubric.score"]
        for rec in records:
            for rubric in rec.task_id.rubric_ids:
                exists = Score.search_count([
                    ("seller_offer_id", "=", rec.id),
                    ("rubric_id", "=", rubric.id),
                ])
                if not exists:
                    Score.create({
                        "seller_offer_id": rec.id,
                        "rubric_id": rubric.id,
                    })
        return records
