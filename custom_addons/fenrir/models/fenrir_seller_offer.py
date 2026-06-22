import base64
import csv
import io

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
    seller_username = fields.Char(
        string="Seller Username",
        help='Marketplace handle, with leading "@", e.g. "@atanso".')
    seller_level = fields.Selection(
        selection=[
            ("new_seller", "New Seller"),
            ("level_1", "Level 1"),
            ("level_2", "Level 2"),
            ("top_rated", "Top Rated"),
        ],
        string="Seller Level",
        help="Marketplace seller tier.")
    seller_profile_url = fields.Char(
        string="Seller Profile URL",
        help="Canonical link to the seller's marketplace profile.")
    order_id = fields.Char(
        string="Order ID",
        help='Marketplace order ID, format "FO<hex>".')
    order_date = fields.Date(string="Order Date")
    delivery_date = fields.Date(string="Delivery Date")
    delivery_time_days = fields.Integer(
        string="Delivery Time (days)",
        compute="_compute_delivery_time_days",
        store=True,
        help="Whole days between order_date and delivery_date.")
    revisions_requested = fields.Integer(
        string="Revisions Requested",
        default=0,
        help="Count of revision rounds requested before acceptance.")
    price_paid_usd = fields.Float(
        string="Price Paid (USD)",
        help="Final amount paid for the order in USD. Used as the canonical "
             "value emitted in seller metadata.json.")
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
        selection=YES_NO_SELECTION + [
            ("refunded", "Cancelled & Refunded"),
            ("pending_overdue", "Pending (Overdue)"),
        ],
        string="Accepted Delivery",
        default="no",
        tracking=True,
    )

    deliverable_attachment_ids = fields.Many2many(
        comodel_name="ir.attachment",
        relation="fenrir_seller_offer_deliverable_rel",
        column1="offer_id", column2="attachment_id",
        string="Deliverables (legacy)",
        help="Legacy local-filestore uploads. New uploads go through "
             "the S3-backed Deliverables field. Kept for backward compat.")
    deliverable_file_ids = fields.One2many(
        comodel_name="fenrir.seller.deliverable",
        inverse_name="offer_id",
        string="Deliverables",
        copy=False,
        help="Files pushed directly to S3 via the upload controller; "
             "bytes never land in Odoo's local filestore.")
    deliverable_count = fields.Integer(
        string="Deliverables",
        compute="_compute_deliverable_count",
        store=False,
    )

    @api.depends("deliverable_file_ids")
    def _compute_deliverable_count(self):
        for rec in self:
            rec.deliverable_count = len(rec.deliverable_file_ids)

    metadata_json = fields.Text(string="Metadata.json")

    rubric_score_ids = fields.One2many(
        comodel_name="fenrir.rubric.score",
        inverse_name="seller_offer_id",
        string="Rubric Scores",
    )
    overall_rating = fields.Integer(string="Overall Rating", tracking=True)
    overall_justification = fields.Text(string="Overall Justification")

    notes = fields.Text(string="Internal Notes")

    @api.depends("seller_no", "seller")
    def _compute_display_name(self):
        for rec in self:
            label = f"Seller {rec.seller_no}" if rec.seller_no else "Seller —"
            rec.display_name = f"{label} — {rec.seller}" if rec.seller else label

    @api.depends("order_date", "delivery_date")
    def _compute_delivery_time_days(self):
        for rec in self:
            if rec.order_date and rec.delivery_date:
                rec.delivery_time_days = (rec.delivery_date - rec.order_date).days
            else:
                rec.delivery_time_days = 0

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

    @staticmethod
    def _strip_empty_score_cmds(vals):
        cmds = vals.get("rubric_score_ids")
        if not cmds:
            return vals
        cleaned = []
        for cmd in cmds:
            if isinstance(cmd, (list, tuple)) and len(cmd) >= 3 and cmd[0] == 0:
                if not (cmd[2] or {}).get("rubric_id"):
                    continue
            cleaned.append(cmd)
        new_vals = dict(vals)
        new_vals["rubric_score_ids"] = cleaned
        return new_vals

    def write(self, vals):
        return super().write(self._strip_empty_score_cmds(vals))

    @api.model_create_multi
    def create(self, vals_list):
        vals_list = [self._strip_empty_score_cmds(v) for v in vals_list]
        # Track next seller_no PER task across the batch so multi-create in
        # one save doesn't give every new offer seller_no = 1.
        next_no_per_task = {}
        for vals in vals_list:
            if not vals.get("seller_no") and vals.get("task_id"):
                task_id = vals["task_id"]
                if task_id not in next_no_per_task:
                    next_no_per_task[task_id] = self.search_count(
                        [("task_id", "=", task_id)]) + 1
                vals["seller_no"] = next_no_per_task[task_id]
                next_no_per_task[task_id] += 1
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

    # ── Rubric-score CSV export ──────────────────────────────────────────
    def action_export_rubric_scores(self):
        """Download a CSV of this seller's rubric scores.

        Columns: rubric_name (key on import), rubric_description (info-only),
        rating, justification. Annotators edit the rating + justification
        columns in Excel and re-upload via Import from CSV.
        """
        self.ensure_one()
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow([
            "rubric_name", "rubric_description", "rating", "justification"])
        for score in self.rubric_score_ids.sorted("rubric_sequence"):
            writer.writerow([
                score.rubric_name or "",
                score.rubric_description or "",
                score.rating or "",
                score.justification or "",
            ])
        # utf-8-sig so Excel opens with the right encoding
        csv_bytes = buf.getvalue().encode("utf-8-sig")
        filename = (
            f"{self.task_code or 'task'}_seller_{self.seller_no or self.id}"
            f"_rubric_scores.csv")
        attachment = self.env["ir.attachment"].create({
            "name": filename,
            "type": "binary",
            "datas": base64.b64encode(csv_bytes),
            "res_model": self._name,
            "res_id": self.id,
            "mimetype": "text/csv",
        })
        return {
            "type": "ir.actions.act_url",
            "url": f"/web/content/{attachment.id}?download=true",
            "target": "self",
        }
