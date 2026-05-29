import base64
import io
import json
import re
import zipfile

from odoo import api, fields, models
from odoo.exceptions import UserError


def _slug(name):
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", name or "").strip("_") or "file"


class FenrirTask(models.Model):
    _name = "fenrir.task"
    _description = "Fenrir Task / Project Record"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "code"
    _rec_name = "code"

    code = fields.Char(string="Task Code", required=True, copy=False, tracking=True,
                       help="Unique project reference, e.g. GDV-002")
    category_id = fields.Many2one(
        comodel_name="fenrir.category",
        string="Category",
        tracking=True,
        ondelete="restrict",
    )
    lead_user_id = fields.Many2one(
        comodel_name="res.users",
        string="Name",
        default=lambda self: self.env.user,
        readonly=True,
        tracking=True,
        help="Auto-filled with the user who created the record",
    )
    title = fields.Char(string="Title", tracking=True)
    overview = fields.Text(string="Overview")
    scope_of_work = fields.Text(string="Scope of Work")
    company_details = fields.Text(string="Company Details")

    assets_url = fields.Char(string="Assets")
    rubrics_url = fields.Char(string="Rubrics URL",
                              help="External link to a rubric spec / doc")
    instruction_md_url = fields.Char(string="Instruction.md")
    instruction_notes = fields.Text(
        string="Instruction.md Notes",
        help="Notes about instruction.md; emitted as the 'notes' field for "
             "the instruction.md entry in license.json.",
    )

    rubric_ids = fields.One2many(
        comodel_name="fenrir.rubric",
        inverse_name="task_id",
        string="Rubrics",
    )
    attachment_ids = fields.One2many(
        comodel_name="fenrir.task.attachment",
        inverse_name="task_id",
        string="Attachments",
    )

    reviewer_id = fields.Many2one(
        comodel_name="res.users",
        string="Reviewer",
        tracking=True,
    )
    status = fields.Selection(
        selection=[
            ("draft", "Draft"),
            ("pending_review", "Pending Review"),
            ("approved", "Approved"),
            ("rejected", "Rejected"),
            ("completed", "Completed"),
            ("cancelled", "Cancelled"),
        ],
        string="Status",
        default="draft",
        tracking=True,
    )

    def action_approve_task(self):
        for rec in self:
            rec.status = "approved"

    def action_reject_task(self):
        for rec in self:
            rec.status = "rejected"

    def action_submit_task(self):
        for rec in self:
            rec.status = "completed"
            rec.submitted_at = fields.Datetime.now()
    remarks = fields.Text(string="Remarks")
    submitted_at = fields.Datetime(string="Submitted At", readonly=True, tracking=True)

    dockerfile_attachment = fields.Binary(string="Dockerfile", attachment=True)
    dockerfile_filename = fields.Char(default="Dockerfile")
    dockerignore_attachment = fields.Binary(string=".dockerignore", attachment=True)
    dockerignore_filename = fields.Char(default=".dockerignore")
    nginx_conf_attachment = fields.Binary(string="nginx.conf", attachment=True)
    nginx_conf_filename = fields.Char(default="nginx.conf")
    entrypoint_sh_attachment = fields.Binary(string="entrypoint.sh", attachment=True)
    entrypoint_sh_filename = fields.Char(default="entrypoint.sh")

    test_deliverables_attachment = fields.Binary(
        string="test_deliverables.sh", attachment=True)
    test_deliverables_filename = fields.Char(default="test_deliverables.sh")

    buyer_id = fields.Many2one(
        comodel_name="res.users",
        string="Buyer",
        tracking=True,
    )
    pricing = fields.Float(string="Pricing", tracking=True,
                           help="Buyer-side pricing")
    price_tier = fields.Char(string="Price Tier")
    delivery_time = fields.Date(string="Delivery Time", tracking=True)
    order_accepted_date = fields.Date(string="Order Accepted Date", tracking=True)

    seller_offer_ids = fields.One2many(
        comodel_name="fenrir.seller.offer",
        inverse_name="task_id",
        string="Seller Offers",
    )
    all_rubric_score_ids = fields.One2many(
        comodel_name="fenrir.rubric.score",
        inverse_name="task_id",
        string="Per-Seller Rubric Scoring",
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

    def action_export_task(self):
        tasks = self._exportable_tasks()
        zip_buf = io.BytesIO()
        with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for task in tasks:
                task._write_rich_export(zf, _slug(task.code))
        return self._build_zip_download(zip_buf.getvalue(),
                                        self._zip_name(tasks, "fenrir_tasks"))

    def _exportable_tasks(self):
        tasks = self.filtered("code")
        if not tasks:
            raise UserError("Select at least one task with a code to export.")
        return tasks

    @staticmethod
    def _zip_name(tasks, fallback):
        if len(tasks) == 1:
            return f"{_slug(tasks.code)}.zip"
        return f"{fallback}_{len(tasks)}.zip"

    def _write_rich_export(self, zf, root):
        self.ensure_one()
        task_meta = {
            "task_id": self.code,
            "title": self.title or "",
            "category": self.category_id.name or "",
            "price_bracket": self.price_tier or "",
            "pricing": self.pricing,
            "delivery_time": self.delivery_time.isoformat() if self.delivery_time else None,
            "status": self.status,
            "lead_user": self.lead_user_id.name or "",
            "reviewer": self.reviewer_id.name or "",
            "buyer": self.buyer_id.name or "",
            "recreation_notes": self.scope_of_work or "",
            "assets_url": self.assets_url or "",
            "rubrics_url": self.rubrics_url or "",
            "instruction_md_url": self.instruction_md_url or "",
        }
        zf.writestr(f"{root}/task_metadata.json",
                   json.dumps(task_meta, indent=2, default=str))
        zf.writestr(f"{root}/instruction.md", self._build_instruction_md(include_remarks=True))
        zf.writestr(f"{root}/rubrics.json",
                   json.dumps([
                       {"sequence": r.sequence,
                        "name": r.name or "",
                        "description": r.description or ""}
                       for r in self.rubric_ids.sorted("sequence")
                   ], indent=2))
        zf.writestr(f"{root}/license.json",
                   json.dumps(self._build_license_doc(), indent=2))

        for att in self.attachment_ids:
            if not att.attachment:
                continue
            file_bytes = base64.b64decode(att.attachment)
            safe_name = _slug(att.file_name or f"attachment_{att.id}")
            folder = att.folder or "resources"
            zf.writestr(f"{root}/{folder}/{safe_name}", file_bytes)

        for filename, content in self._environment_files():
            zf.writestr(f"{root}/environment/{filename}", content)

        for filename, content in self._test_files():
            zf.writestr(f"{root}/tests/{filename}", content)

        for offer in self.seller_offer_ids.sorted("seller_no"):
            seller_dir = f"{root}/submissions/seller_{offer.seller_no or offer.id}"
            meta = {
                "task_id": self.code,
                "seller_number": offer.seller_no,
                "seller_username": offer.seller or "",
                "received_custom_offer": offer.received_custom_offer,
                "sellers_initial_ask": offer.sellers_initial_ask,
                "negotiated_offer": offer.negotiated_offer or "",
                "accepted": offer.accepted,
                "price_paid_usd": offer.final_payment_amount,
                "currency": offer.final_payment_currency or "",
                "delivery_received": offer.delivery_received,
                "accepted_delivery": offer.accepted_delivery,
                "deliverables_link": offer.deliverables_link or "",
                "order_date": offer.create_date.isoformat() if offer.create_date else None,
                "notes": offer.notes or "",
            }
            zf.writestr(f"{seller_dir}/metadata.json",
                       json.dumps(meta, indent=2, default=str))
            zf.writestr(f"{seller_dir}/ratings.json",
                       json.dumps(self._build_ratings(offer), indent=2, default=str))
            if offer.conversation:
                zf.writestr(f"{seller_dir}/conversation.txt", offer.conversation)
            if offer.automated_checks:
                zf.writestr(f"{seller_dir}/automated_checks.txt", offer.automated_checks)
            if offer.metadata_json:
                zf.writestr(f"{seller_dir}/extra_metadata.json", offer.metadata_json)

    def _build_license_doc(self):
        self.ensure_one()
        assets = [{
            "file_name": "instruction.md",
            "location": "root",
            "license": "Self-created",
            "source_url": None,
            "notes": self.instruction_notes or f"Task instructions for {self.code}.",
        }]
        for att in self.attachment_ids:
            assets.append({
                "file_name": att.file_name or f"attachment_{att.id}",
                "location": f"{att.folder or 'resources'}/",
                "license": att.license_label(),
                "source_url": att.source_url or None,
                "notes": att.notes or "",
            })
        for filename, _content in self._environment_files():
            assets.append({
                "file_name": filename,
                "location": "environment/",
                "license": "Self-created",
                "source_url": None,
                "notes": "",
            })
        for filename, _content in self._test_files():
            assets.append({
                "file_name": filename,
                "location": "tests/",
                "license": "Self-created",
                "source_url": None,
                "notes": "",
            })
        return {"task_id": self.code, "assets": assets}

    def _environment_files(self):
        self.ensure_one()
        return self._collect_uploads([
            (self.dockerfile_attachment, self.dockerfile_filename, "Dockerfile"),
            (self.dockerignore_attachment, self.dockerignore_filename, ".dockerignore"),
            (self.nginx_conf_attachment, self.nginx_conf_filename, "nginx.conf"),
            (self.entrypoint_sh_attachment, self.entrypoint_sh_filename, "entrypoint.sh"),
        ])

    def _test_files(self):
        self.ensure_one()
        return self._collect_uploads([
            (self.test_deliverables_attachment,
             self.test_deliverables_filename, "test_deliverables.sh"),
        ])

    @staticmethod
    def _collect_uploads(slots):
        files = []
        for blob, name, default_name in slots:
            if not blob:
                continue
            files.append((_slug(name or default_name), base64.b64decode(blob)))
        return files

    def _build_instruction_md(self, include_remarks=False):
        self.ensure_one()
        parts = [f"# {self.title or self.code}\n"]
        if self.overview:
            parts.append("## Overview\n\n" + self.overview)
        if self.scope_of_work:
            parts.append("## Scope of Work\n\n" + self.scope_of_work)
        if self.company_details:
            parts.append("## Company Details\n\n" + self.company_details)
        if include_remarks and self.remarks:
            parts.append("## Remarks\n\n" + self.remarks)
        return "\n\n".join(parts) + "\n"

    @staticmethod
    def _build_ratings(offer):
        return {
            "overall_score": offer.overall_rating,
            "justification": offer.overall_justification or "",
            "rubric_evaluation": [
                {
                    "rubric_name": s.rubric_name or "",
                    "rubric_description": s.rubric_description or "",
                    "score": s.rating,
                    "justification": s.justification or "",
                }
                for s in offer.rubric_score_ids.sorted("rubric_sequence")
            ],
            "rater_id": offer.write_uid.login or "",
            "rating_date": offer.write_date.date().isoformat() if offer.write_date else None,
        }

    def _build_zip_download(self, zip_bytes, filename):
        attachment = self.env["ir.attachment"].create({
            "name": filename,
            "type": "binary",
            "datas": base64.b64encode(zip_bytes),
            "res_model": self._name,
            "res_id": self[:1].id or False,
            "mimetype": "application/zip",
        })
        return {
            "type": "ir.actions.act_url",
            "url": f"/web/content/{attachment.id}?download=true",
            "target": "self",
        }
