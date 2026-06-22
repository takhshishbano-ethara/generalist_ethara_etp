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


def _norm_filename(name):
    return _slug(name).lower()


class FenrirTask(models.Model):
    _name = "fenrir.task"
    _description = "Fenrir Task / Project Record"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "code"
    _rec_name = "code"

    code = fields.Char(string="Task Code", required=True, copy=False, tracking=True,
                       help="Unique project reference, e.g. GDV-002.")
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
            # ("non_dev", "Non-development (setup.sh)"),
            ("dev", "Development (Dockerfile)"),
        ],
        default="dev",
        string="Environment Type",
        # compute="_compute_environment_type",
        # store=True,
        help="Derived from the task code prefix.")
    environment_base_runtime_ids = fields.Many2many(
        comodel_name="fenrir.environment.runtime",
        relation="fenrir_task_runtime_rel",
        column1="task_id", column2="runtime_id",
        string="Environment Base / Runtime",
        help="One or more base runtimes for the task. Key Dependencies "
             "auto-aggregates from these.")
    key_dependency_ids = fields.Many2many(
        comodel_name="fenrir.key.dependency",
        string="Key Dependencies / Tools",
        compute="_compute_key_dependency_ids",
        help="Auto-aggregated from the selected runtimes (read-only).")

    # Legacy free-text fields, kept hidden so existing data isn't lost.
    # The generator prefers the M2O/M2M fields above; these are only used
    # as a fallback when the master records aren't picked.
    environment_base_runtime = fields.Char(
        string="Environment Base / Runtime (legacy)")
    key_dependencies = fields.Char(
        string="Key Dependencies / Tools (legacy)")
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
    input_asset_license_ids = fields.One2many(
        comodel_name="fenrir.input.asset.license",
        inverse_name="task_id",
        string="Input Asset Licenses",
    )

    assets_url = fields.Char(string="Project Requirements Document (PRD)")
    assets_file = fields.Binary(string="Project Requirements Document (PRD)", attachment=True,
                                help="Optional file alternative to the assets URL.")
    assets_filename = fields.Char(string="Project Requirements Document (PRD) Filename")

    # rubrics_url = fields.Char(string="Rubrics URL",
    #                           help="External link to a rubric spec / doc")
    # rubrics_file = fields.Binary(string="Rubrics File", attachment=True,
    #                              help="Optional file alternative to the rubrics URL.")
    # rubrics_filename = fields.Char(string="Rubrics Filename")

    instruction_md_url = fields.Char(string="Instruction.md")
    instruction_md_file = fields.Binary(
        string="Instruction.md File", attachment=True,
        help="Optional uploaded markdown file. When set, it overrides the "
             "instruction.md auto-generated from the text fields.")
    instruction_md_filename = fields.Char(
        string="Instruction.md Filename", default="instruction.md")
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
    data_attachment_ids = fields.One2many(
        comodel_name="fenrir.task.attachment",
        inverse_name="task_id",
        domain=[("folder", "=", "data")],
        string="Data",
        help="Data files uploaded for this task. Land under data/ in the "
             "Drive export and the S3 mirror.",
    )

    show_environment_config = fields.Boolean(
        string="Show Environment Configuration",
        default=False,
        copy=False,
        help="Toggled True by the 'Create Dockerfile' button. When True, "
             "Environment Type / Base Runtime / Key Dependencies fields "
             "are shown on the form.",
    )
    show_environment_uploads = fields.Boolean(
        string="Show Environment File Uploads",
        default=False,
        copy=False,
        help="Toggled True by the 'Upload Environment Files' button. When "
             "True, an attachment list filtered to folder='environment' is "
             "shown on the form.",
    )
    environment_attachment_ids = fields.One2many(
        comodel_name="fenrir.task.attachment",
        inverse_name="task_id",
        domain=[("folder", "=", "environment")],
        string="Environment Files",
        help="Files uploaded via the 'Upload Environment Files' button. "
             "Each lands under environment/ in the Drive export and S3 "
             "mirror via the existing attachment_ids iteration in "
             "_regenerate_task_package (no extra export-pipeline code needed).",
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

    drive_folder_id = fields.Char(
        string="Drive Folder ID",
        readonly=True, copy=False, tracking=True,
        help="Google Drive folder ID where this task's package was uploaded.")
    drive_folder_url = fields.Char(
        string="Open in Drive",
        compute="_compute_drive_folder_url")
    drive_last_uploaded_at = fields.Datetime(
        string="Last Uploaded to Drive",
        readonly=True, copy=False, tracking=True)

    @api.depends("drive_folder_id")
    def _compute_drive_folder_url(self):
        for rec in self:
            rec.drive_folder_url = (
                f"https://drive.google.com/drive/folders/{rec.drive_folder_id}"
                if rec.drive_folder_id else False)

    def read(self, fields=None, load="_classic_read"):
        # Defensive: if a Selection field has a stored value that is no
        # longer in its selection list (schema changed after data was
        # written), substitute the field's default so the web client does
        # not crash in SelectionField.template (orphan value → undefined
        # lookup → "Cannot read properties of undefined (reading '1')").
        result = super().read(fields=fields, load=load)
        sel_fields = []
        for name, field in self._fields.items():
            if field.type != "selection":
                continue
            if fields and name not in fields:
                continue
            try:
                valid = {opt[0] for opt in field._description_selection(self.env)}
            except Exception:
                continue
            default = field.default(self) if callable(field.default) else (field.default or False)
            sel_fields.append((name, valid, default))
        for rec in result:
            for name, valid, default in sel_fields:
                value = rec.get(name)
                if value and value not in valid:
                    rec[name] = default
        return result

    def action_approve_task(self):
        if not self.env.user.has_group("fenrir.group_fenrir_manager"):
            raise UserError("Only managers can approve tasks.")
        drive = self.env["fenrir.drive.service"]
        for rec in self:
            drive.upload_task(rec)
            rec.status = "approved"

    def action_reapprove_task(self):
        if not self.env.user.has_group("fenrir.group_fenrir_manager"):
            raise UserError("Only managers can re-approve tasks.")
        drive = self.env["fenrir.drive.service"]
        for rec in self:
            drive.upload_task(rec)
            rec.message_post(
                body="Task re-approved — files regenerated and overwritten "
                     "in Google Drive."
            )

    def action_reject_task(self):
        if not self.env.user.has_group("fenrir.group_fenrir_manager"):
            raise UserError("Only managers can reject tasks.")
        for rec in self:
            rec.status = "rejected"

    def action_complete_task(self):
        if not self.env.user.has_group("fenrir.group_fenrir_manager"):
            raise UserError("Only managers can complete tasks.")
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
            "file_name": "license.json",
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
    entrypoint_sh_attachment = fields.Binary(string="setup.sh", attachment=True)
    entrypoint_sh_filename = fields.Char(default="setup.sh")

    test_deliverables_attachment = fields.Binary(
        string="test_deliverables.sh", attachment=True)
    test_deliverables_filename = fields.Char(default="test_deliverables.sh")

    buyer_id = fields.Many2one(
        comodel_name="res.users",
        string="Buyer",
        tracking=True,
    )
    # pricing = fields.Float(string="Pricing", tracking=True,
    #                        help="Buyer-side pricing")
    price_tier = fields.Selection(
        selection=[
            ("$0-$50", "$0-$50"),
            ("$50-$100", "$50-$100"),
            ("$100-$150", "$100-$150"),
            ("$150-$200", "$150-$200"),
        ],
        default="$0-$50",
        string="Price Tier",
        tracking=True,
    )
    # price_tier = fields.Char(string="Price Tier")
    delivery_time = fields.Date(string="Expected Delivery Date", tracking=True)
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

    # @api.depends("code")
    # def _compute_environment_type(self):
    #     dev_prefixes = ("GDV", "WD", "SD")
    #     for rec in self:
    #         prefix = (rec.code or "").split("-", 1)[0]
    #         rec.environment_type = "dev" if prefix in dev_prefixes else "non_dev"

    @api.depends("environment_base_runtime_ids",
                 "environment_base_runtime_ids.key_dependency_ids")
    def _compute_key_dependency_ids(self):
        for rec in self:
            rec.key_dependency_ids = (
                rec.environment_base_runtime_ids.key_dependency_ids)

    # Action methods for the previous button-reveal UI (Create Dockerfile /
    # Upload Environment Files + Hide buttons). Replaced by the tab notebook
    # in views/fenrir_task_views.xml. Kept (commented) per user request to
    # preserve all code.
    # def action_show_environment_config(self):
    #     for rec in self:
    #         rec.show_environment_config = True
    #     return True
    #
    # def action_show_environment_uploads(self):
    #     for rec in self:
    #         rec.show_environment_uploads = True
    #     return True
    #
    # def action_hide_environment_config(self):
    #     for rec in self:
    #         rec.show_environment_config = False
    #     return True
    #
    # def action_hide_environment_uploads(self):
    #     for rec in self:
    #         rec.show_environment_uploads = False
    #     return True

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

    def action_renumber_sellers(self):
        """One-shot fix: reassign seller_no sequentially (1, 2, 3, …) to
        every offer on this task, ordered by creation. Use after fixing
        duplicate seller_no values left over from earlier bug."""
        self.ensure_one()
        for idx, offer in enumerate(
                self.seller_offer_ids.sorted(lambda o: o.id), 1):
            if offer.seller_no != idx:
                offer.seller_no = idx
        return True

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

    # Standard package sub-folders, always present in an export even when empty.
    _EXPORT_BASE_DIRS = ("resources", "data", "environment", "tests", "submissions")

    def _write_rich_export(self, zf, root):
        self.ensure_one()
        # Always materialise the standard package sub-folders, even when empty,
        # via explicit zero-byte directory entries.
        for base in self._EXPORT_BASE_DIRS:
            zf.writestr(f"{root}/{base}/", b"")
        # ZIP includes the actual binary content (no S3 indirection); just
        # ignore the is_binary_upload / existing_s3_key flags.
        for rel_path, content, _mime, _is_binary, _s3 in self._collect_export_files():
            zf.writestr(f"{root}/{rel_path}", content)

    def _collect_export_files(self):
        """Return [(rel_path, bytes, mime, is_binary_upload, existing_s3_key), ...].

        is_binary_upload = True for files that came in as uploads (Binary
        fields, ir.attachment). The Drive uploader sends these to S3 and
        replaces them with .url.txt pointers in Drive. ZIP export keeps the
        actual content regardless.

        existing_s3_key is the S3 object key when the file was already
        pushed at attach time (see fenrir.task.attachment._maybe_push_to_s3);
        None otherwise. The Drive uploader uses it to skip the redundant
        S3 mirror.
        """
        self.ensure_one()
        import mimetypes
        files = []
        GENERATED = False
        UPLOADED = True

        # instruction.md — annotator-uploaded file wins; otherwise build from text.
        if self.instruction_md_file:
            files.append(("instruction.md",
                          base64.b64decode(self.instruction_md_file),
                          "text/markdown", UPLOADED, None))
        else:
            files.append(("instruction.md",
                          self._build_instruction_md(include_remarks=True).encode("utf-8"),
                          "text/markdown", GENERATED, None))

        # if self.rubrics_file:
        #     name = _slug(self.rubrics_filename or "rubrics_source")
        #     mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
        #     files.append((name, base64.b64decode(self.rubrics_file), mime, UPLOADED, None))

        if self.assets_file:
            name = _norm_filename(self.assets_filename or "assets")
            mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
            files.append((f"resources/{name}",
                          base64.b64decode(self.assets_file), mime, UPLOADED, None))

        generated_env_names = set()
        generated_test_names = set()
        wrote_task_metadata = False
        wrote_licenses = False

        for att in self.attachment_ids:
            if not att.has_content():
                continue
            file_bytes = att._fetch_bytes()
            raw_name = att.file_name or f"attachment_{att.id}"
            safe_name = (_slug(raw_name)
                         if (att.folder or "resources") in ("environment", "tests")
                         else _norm_filename(raw_name))
            folder = att.folder or "resources"
            mime = mimetypes.guess_type(safe_name)[0] or "application/octet-stream"
            if folder == "root":
                rel = safe_name
                if safe_name == "task_metadata.json":
                    wrote_task_metadata = True
                elif safe_name == "license.json":
                    wrote_licenses = True
            else:
                rel = f"{folder}/{safe_name}"
                if folder == "environment" and att.is_generated:
                    generated_env_names.add(safe_name)
                elif folder == "tests" and att.is_generated:
                    generated_test_names.add(safe_name)
            # Auto-generated attachments (env/tests/json files we created)
            # are generated content. User-uploaded attachments go to S3.
            tag = GENERATED if att.is_generated else UPLOADED
            files.append((rel, file_bytes, mime, tag, att.s3_key or None))

        if not wrote_task_metadata:
            files.append(("task_metadata.json",
                          json.dumps(gen.build_task_metadata(self), indent=2).encode("utf-8"),
                          "application/json", GENERATED, None))
        if not wrote_licenses:
            files.append(("license.json",
                          json.dumps(self._build_license_doc(), indent=2).encode("utf-8"),
                          "application/json", GENERATED, None))

        # Legacy per-task binary uploads (user-uploaded Dockerfile etc.).
        for filename, content in self._environment_files():
            if filename in generated_env_names:
                continue
            mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
            files.append((f"environment/{filename}", content, mime, UPLOADED, None))

        for filename, content in self._test_files():
            if filename in generated_test_names:
                continue
            mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
            files.append((f"tests/{filename}", content, mime, UPLOADED, None))

        for offer in self.seller_offer_ids.sorted("seller_no"):
            seller_dir = f"submissions/seller_{offer.seller_no or offer.id}"
            if offer.metadata_json:
                meta_bytes = offer.metadata_json.encode("utf-8")
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
                    "order_date": offer.order_date.isoformat() if offer.order_date else None,
                    "notes": offer.notes or "",
                }
                meta_bytes = json.dumps(fallback, indent=2, default=str).encode("utf-8")
            files.append((f"{seller_dir}/metadata.json",
                          meta_bytes, "application/json", GENERATED, None))
            files.append((f"{seller_dir}/ratings.json",
                          json.dumps(self._build_ratings(offer), indent=2, default=str).encode("utf-8"),
                          "application/json", GENERATED, None))

            for att in offer.deliverable_attachment_ids:
                if not att.datas:
                    continue
                content = base64.b64decode(att.datas)
                safe_name = _norm_filename(att.name or f"deliverable_{att.id}")
                mime = att.mimetype or mimetypes.guess_type(safe_name)[0] \
                    or "application/octet-stream"
                files.append((f"{seller_dir}/deliverables/{safe_name}",
                              content, mime, UPLOADED, None))

            # S3-backed deliverables (uploaded via the new controller).
            # Bytes are streamed back from S3 only for the Drive/ZIP export;
            # the S3 mirror is skipped in the Drive uploader because
            # existing_s3_key is set.
            for deliv in offer.deliverable_file_ids:
                if not deliv.s3_key:
                    continue
                safe_name = _norm_filename(deliv.file_name or f"deliverable_{deliv.id}")
                mime = (deliv.mime_type
                        or mimetypes.guess_type(safe_name)[0]
                        or "application/octet-stream")
                files.append((f"{seller_dir}/deliverables/{safe_name}",
                              deliv.fetch_bytes(), mime, UPLOADED, deliv.s3_key))

        return files

    def _build_license_doc(self):
        """license.json — annotator-supplied INPUT assets only.

        Per the requirements doc: lists files in instruction.md, data/, and
        resources/. Skips auto-generated artifacts (task_metadata.json,
        license.json, environment/*, tests/*) and seller deliverables.
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
                "file_name": _norm_filename(att.file_name or f"attachment_{att.id}"),
                "location": location,
                "license": att.license_label(),
                "source_url": att.source_url or None,
                "notes": att.notes or "",
            })
        # if self.rubrics_file:
        #     assets.append({
        #         "file_name": self.rubrics_filename or "rubrics_source",
        #         "location": "root",
        #         "license": "Self-created",
        #         "source_url": self.rubrics_url or None,
        #         "notes": "",
        #     })
        if self.assets_file:
            assets.append({
                "file_name": _norm_filename(self.assets_filename or "assets"),
                "location": "resources/",
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
            (self.entrypoint_sh_attachment, self.entrypoint_sh_filename, "setup.sh"),
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
            "rater_id": f"rater_{offer.write_uid.id:03d}" if offer.write_uid else "",
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
