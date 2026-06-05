import base64
import io
import json
import re
import zipfile

from odoo import api, fields, models
from odoo.exceptions import UserError

from . import fenrir_generators as gen


def _slug(name):
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", name or "").strip("_") or "file"


class FenrirTask(models.Model):
    _name = "fenrir.task"
    _description = "Fenrir Task / Project Record"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "code"
    _rec_name = "code"

    CODE_PATTERN = re.compile(r"^(3D|GD|GDV|WD|SD)-\d{3,}$")

    code = fields.Char(string="Task Code", required=True, copy=False, tracking=True,
                       help="Unique project reference, e.g. GDV-002. "
                            "Format: <PREFIX>-<NNN> where prefix ∈ "
                            "{3D, GD, GDV, WD, SD}.")
    category_id = fields.Many2one(
        comodel_name="fenrir.category",
        string="Category",
        tracking=True,
        ondelete="restrict",
    )
    subcategory = fields.Char(
        string="Subcategory",
        help="Finer-grained category, e.g. 'Logo Design', '3D Modeling'.")
    recreation_notes = fields.Text(
        string="Recreation Notes",
        help="How the original gig concept was adapted, what was fictionalized "
             "(client name, brand, scope), and confirmation that no proprietary "
             "assets were used.")
    difficulty_estimate = fields.Selection(
        selection=[
            ("easy", "Easy"),
            ("medium", "Medium"),
            ("hard", "Hard"),
        ],
        string="Difficulty Estimate",
        help="How hard is this task for a seller?")
    estimated_completion_time_hours = fields.Float(
        string="Estimated Completion Time (hours)",
        help="Expected hours for a competent freelancer to complete the task.")
    tags = fields.Char(
        string="Tags",
        help="Comma-separated keywords, e.g. logo, vintage, emblem.")
    expected_deliverables = fields.Text(
        string="Expected Deliverables",
        help="One filename or pattern per line. Used to auto-generate "
             "validator stubs at submit (e.g. 'logo.svg').")
    environment_type = fields.Selection(
        selection=[
            ("non_dev", "Non-development (setup.sh)"),
            ("dev", "Development (Dockerfile)"),
        ],
        string="Environment Type",
        compute="_compute_environment_type",
        store=True,
        help="Derived from the task code prefix.")
    environment_base_runtime = fields.Char(
        string="Environment Base / Runtime",
        help="e.g. node:18, python:3.11, blender:3.6, nginx:1.25-alpine, "
             "or N/A for pure creative tasks.")
    key_dependencies = fields.Char(
        string="Key Dependencies / Tools",
        help="Comma-separated apt packages or tools required to validate, "
             "e.g. imagemagick, librsvg2-bin, file.")
    price_bracket = fields.Char(
        string="Price Bracket",
        help='Commissioned price band, e.g. "$0-$50", "$50-$100".')
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
        if not self.env.user.has_group("fenrir.group_fenrir_manager"):
            raise UserError("Only managers can approve tasks.")
        for rec in self:
            rec.status = "completed"

    def action_reject_task(self):
        if not self.env.user.has_group("fenrir.group_fenrir_manager"):
            raise UserError("Only managers can reject tasks.")
        for rec in self:
            rec.status = "completed"

    def action_submit_task(self):
        for rec in self:
            rec._validate_for_submit()
            rec._regenerate_task_package()
            rec.status = "pending_review"
            rec.submitted_at = fields.Datetime.now()

    # ── Submit-time validation ────────────────────────────────────────────
    _REQUIRED_TASK_FIELDS = (
        ("title", "Title"),
        ("category_id", "Category"),
        ("subcategory", "Subcategory"),
        ("price_bracket", "Price Bracket"),
        ("recreation_notes", "Recreation Notes"),
        ("difficulty_estimate", "Difficulty Estimate"),
        ("estimated_completion_time_hours", "Estimated Completion Time"),
        ("tags", "Tags"),
    )
    _REQUIRED_SELLER_FIELDS = (
        ("seller_username", "Seller Username"),
        ("seller_level", "Seller Level"),
        ("price_paid_usd", "Price Paid (USD)"),
        ("order_date", "Order Date"),
        ("delivery_date", "Delivery Date"),
        ("order_id", "Order ID"),
        ("seller_profile_url", "Seller Profile URL"),
    )

    def _validate_for_submit(self):
        self.ensure_one()
        missing = [
            label for field, label in self._REQUIRED_TASK_FIELDS
            if not self[field]
        ]
        accepted = self.seller_offer_ids.filtered(lambda o: o.accepted == "yes")
        if not accepted:
            missing.append("at least one accepted seller offer")
        for offer in accepted:
            for field, label in self._REQUIRED_SELLER_FIELDS:
                if not offer[field]:
                    missing.append(f"seller_{offer.seller_no}.{label}")
        if missing:
            raise UserError(
                "Cannot submit task — missing required fields:\n  • "
                + "\n  • ".join(missing))

    # ── Submit-time generation ────────────────────────────────────────────
    def _regenerate_task_package(self):
        """Wipe stale generated attachments and rebuild from current state."""
        self.ensure_one()
        self.attachment_ids.filtered("is_generated").unlink()

        Attachment = self.env["fenrir.task.attachment"]
        # task_metadata.json + licenses.json at root
        Attachment.create({
            "task_id": self.id,
            "file_name": "task_metadata.json",
            "folder": "root",
            "is_generated": True,
            "license": "self_created",
            "attachment": base64.b64encode(json.dumps(
                gen.build_task_metadata(self), indent=2).encode("utf-8")),
        })
        Attachment.create({
            "task_id": self.id,
            "file_name": "licenses.json",
            "folder": "root",
            "is_generated": True,
            "license": "self_created",
            "attachment": base64.b64encode(json.dumps(
                self._build_license_doc(), indent=2).encode("utf-8")),
        })

        # environment/<files>
        for filename, content in gen.build_environment_files(self):
            Attachment.create({
                "task_id": self.id,
                "file_name": filename,
                "folder": "environment",
                "is_generated": True,
                "license": "self_created",
                "attachment": base64.b64encode(content.encode("utf-8")),
            })

        # tests/test_deliverables.*
        test_filename, test_content = gen.build_validator_script(self)
        Attachment.create({
            "task_id": self.id,
            "file_name": test_filename,
            "folder": "tests",
            "is_generated": True,
            "license": "self_created",
            "attachment": base64.b64encode(test_content.encode("utf-8")),
        })

        # Per-seller metadata.json — stored on the offer's metadata_json field
        # so the existing _write_rich_export() flow picks it up as
        # submissions/seller_<n>/metadata.json.
        for offer in self.seller_offer_ids.filtered(lambda o: o.accepted == "yes"):
            offer.metadata_json = json.dumps(
                gen.build_seller_metadata(offer), indent=2)
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

    @api.depends("code")
    def _compute_environment_type(self):
        dev_prefixes = ("GDV", "WD", "SD")
        for rec in self:
            prefix = (rec.code or "").split("-", 1)[0]
            rec.environment_type = "dev" if prefix in dev_prefixes else "non_dev"

    @api.constrains("code")
    def _check_code_pattern(self):
        for rec in self:
            if rec.code and not self.CODE_PATTERN.match(rec.code):
                raise UserError(
                    f"Task code '{rec.code}' is invalid. "
                    "Expected format <PREFIX>-<NNN> where prefix ∈ "
                    "{3D, GD, GDV, WD, SD}, e.g. 'GDV-002'.")

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

        zf.writestr(f"{root}/instruction.md",
                   self._build_instruction_md(include_remarks=True))
        zf.writestr(f"{root}/rubrics.json",
                   json.dumps([
                       {"sequence": r.sequence,
                        "name": r.name or "",
                        "description": r.description or ""}
                       for r in self.rubric_ids.sorted("sequence")
                   ], indent=2))

        generated_env_names = set()
        generated_test_names = set()
        wrote_task_metadata = False
        wrote_licenses = False

        for att in self.attachment_ids:
            if not att.attachment:
                continue
            file_bytes = base64.b64decode(att.attachment)
            safe_name = _slug(att.file_name or f"attachment_{att.id}")
            folder = att.folder or "resources"
            if folder == "root":
                zf.writestr(f"{root}/{safe_name}", file_bytes)
                if safe_name == "task_metadata.json":
                    wrote_task_metadata = True
                elif safe_name == "licenses.json":
                    wrote_licenses = True
            else:
                zf.writestr(f"{root}/{folder}/{safe_name}", file_bytes)
                if folder == "environment" and att.is_generated:
                    generated_env_names.add(safe_name)
                elif folder == "tests" and att.is_generated:
                    generated_test_names.add(safe_name)

        # Fallbacks for legacy tasks that haven't been (re)submitted under the
        # new generator yet — emit a minimal task_metadata.json / licenses.json
        # so the export tree is never missing those top-level files.
        if not wrote_task_metadata:
            zf.writestr(f"{root}/task_metadata.json",
                       json.dumps(gen.build_task_metadata(self), indent=2))
        if not wrote_licenses:
            zf.writestr(f"{root}/licenses.json",
                       json.dumps(self._build_license_doc(), indent=2))

        # Legacy per-task binary uploads (Dockerfile, nginx.conf, …) — only
        # emit if the generator hasn't already produced a file by that name.
        for filename, content in self._environment_files():
            if filename in generated_env_names:
                continue
            zf.writestr(f"{root}/environment/{filename}", content)

        for filename, content in self._test_files():
            if filename in generated_test_names:
                continue
            zf.writestr(f"{root}/tests/{filename}", content)

        for offer in self.seller_offer_ids.sorted("seller_no"):
            seller_dir = f"{root}/submissions/seller_{offer.seller_no or offer.id}"
            if offer.metadata_json:
                # Generated schema-compliant payload (set at submit time).
                zf.writestr(f"{seller_dir}/metadata.json", offer.metadata_json)
            else:
                fallback = {
                    "task_id": self.code,
                    "seller_number": offer.seller_no,
                    "seller_username": offer.seller_username or offer.seller or "",
                    "received_custom_offer": offer.received_custom_offer,
                    "sellers_initial_ask": offer.sellers_initial_ask,
                    "negotiated_offer": offer.negotiated_offer or "",
                    "accepted": offer.accepted,
                    "price_paid_usd": offer.price_paid_usd or offer.final_payment_amount,
                    "currency": offer.final_payment_currency or "",
                    "delivery_received": offer.delivery_received,
                    "accepted_delivery": offer.accepted_delivery,
                    "deliverables_link": offer.deliverables_link or "",
                    "order_date": offer.order_date.isoformat() if offer.order_date else None,
                    "notes": offer.notes or "",
                }
                zf.writestr(f"{seller_dir}/metadata.json",
                           json.dumps(fallback, indent=2, default=str))
            zf.writestr(f"{seller_dir}/ratings.json",
                       json.dumps(self._build_ratings(offer), indent=2, default=str))
            if offer.conversation:
                zf.writestr(f"{seller_dir}/conversation.txt", offer.conversation)
            if offer.automated_checks:
                zf.writestr(f"{seller_dir}/automated_checks.txt", offer.automated_checks)

    def _build_license_doc(self):
        """licenses.json — annotator-supplied INPUT assets only.

        Per the requirements doc: lists files in instruction.md, data/, and
        resources/. Skips auto-generated artifacts (task_metadata.json,
        licenses.json, environment/*, tests/*) and seller deliverables.
        """
        self.ensure_one()
        assets = [{
            "file_name": "instruction.md",
            "location": "root",
            "license": "Self-created",
            "source_url": None,
            "notes": self.instruction_notes or f"Task instructions for {self.code}.",
        }]
        for att in self.attachment_ids:
            if att.is_generated:
                continue
            if att.folder in ("environment", "tests"):
                continue
            location = "root" if att.folder == "root" else f"{att.folder or 'resources'}/"
            assets.append({
                "file_name": att.file_name or f"attachment_{att.id}",
                "location": location,
                "license": att.license_label(),
                "source_url": att.source_url or None,
                "notes": att.notes or "",
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
