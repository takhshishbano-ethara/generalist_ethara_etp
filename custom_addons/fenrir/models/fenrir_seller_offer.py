from odoo import fields, models


YES_NO_SELECTION = [
    ("yes", "Yes"),
    ("no", "No"),
    ("cancelled", "Cancelled"),
]


class FenrirSellerOffer(models.Model):
    _name = "fenrir.seller.offer"
    _description = "Fenrir Seller Offer / Negotiation"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "task_id, id"
    _rec_name = "seller"

    task_id = fields.Many2one(
        comodel_name="fenrir.task",
        string="Task",
        required=True,
        ondelete="cascade",
        index=True,
    )
    task_code = fields.Char(related="task_id.code", string="Task Code", store=True)
    category = fields.Char(related="task_id.category", string="Category", store=True)

    seller = fields.Char(string="Seller", required=True, tracking=True)
    seller_profile = fields.Char(string="Seller Profile",
                                 help="Public profile URL (Fiverr / Upwork / etc.)")

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

    notes = fields.Text(string="Internal Notes")
