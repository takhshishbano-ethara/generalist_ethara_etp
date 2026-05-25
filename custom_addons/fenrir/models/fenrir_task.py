from odoo import api, fields, models


class FenrirTask(models.Model):
    _name = "fenrir.task"
    _description = "Fenrir Task / Project Record"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "sequence, id"
    _rec_name = "code"

    sequence = fields.Integer(string="#", default=1, help="Row number (column 'x' in source sheet)")
    code = fields.Char(string="Task Code", required=True, copy=False, tracking=True,
                       help="Unique project reference, e.g. GDV-002")
    category = fields.Char(string="Category", tracking=True)
    name = fields.Char(string="Name", required=True, tracking=True,
                      help="Project lead / contact name")
    title = fields.Char(string="Title", tracking=True)
    overview = fields.Text(string="Overview")
    scope_of_work = fields.Text(string="Scope of Work")
    company_details = fields.Text(string="Company Details")

    assets_url = fields.Char(string="Assets")
    rubrics_url = fields.Char(string="Rubrics")
    instruction_md_url = fields.Char(string="Instruction.md")

    reviewer = fields.Char(string="Reviewer", tracking=True)
    status = fields.Selection(
        selection=[
            ("draft", "Draft"),
            ("pending_review", "Pending Review"),
            ("approved", "Approved"),
            ("in_progress", "In Progress"),
            ("completed", "Completed"),
            ("cancelled", "Cancelled"),
        ],
        string="Status",
        default="draft",
        tracking=True,
    )
    remarks = fields.Text(string="Remarks")

    buyer = fields.Char(string="Buyer", tracking=True)
    pricing = fields.Char(string="Pricing",
                          help="Buyer-side pricing, e.g. '$150' or '$150-$200'")
    price_tier = fields.Char(string="Price Tier")
    delivery_time = fields.Char(string="Delivery Time")

    seller_offer_ids = fields.One2many(
        comodel_name="fenrir.seller.offer",
        inverse_name="task_id",
        string="Seller Offers",
    )
    seller_offer_count = fields.Integer(
        string="Sellers", compute="_compute_seller_offer_count")
    accepted_offer_count = fields.Integer(
        string="Accepted", compute="_compute_seller_offer_count")

    _sql_constraints = [
        ("fenrir_task_code_unique", "unique(code)", "Task Code must be unique."),
    ]

    @api.depends("seller_offer_ids", "seller_offer_ids.accepted")
    def _compute_seller_offer_count(self):
        for rec in self:
            rec.seller_offer_count = len(rec.seller_offer_ids)
            rec.accepted_offer_count = len(
                rec.seller_offer_ids.filtered(lambda o: o.accepted == "yes"))

    def action_open_seller_offers(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": f"Seller Offers — {self.code}",
            "res_model": "fenrir.seller.offer",
            "view_mode": "list,form",
            "domain": [("task_id", "=", self.id)],
            "context": {"default_task_id": self.id},
        }
