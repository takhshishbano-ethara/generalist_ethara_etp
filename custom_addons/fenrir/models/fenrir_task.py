import base64
import io
import json
import logging
import re
import zipfile

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tools.sql import table_exists

from . import fenrir_generators as gen


_logger = logging.getLogger(__name__)


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

    code = fields.Char(string="Task Code", copy=False, tracking=True,
                       help="Unique project reference. Left blank, it is "
                            "auto-generated as <CATEGORY_CODE>-<PHASE_NO>"
                            "<SERIAL>, e.g. AI-0201.")
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

    # Resource links imported from the task-import spreadsheet. These simply
    # store external URLs (Drive/Docs) for reference; they are not uploaded.
    prd_link = fields.Char(
        string="PRD Link",
        help="External link to the PRD (e.g. Google Drive). Imported from "
             "the task spreadsheet; stored for reference only.")
    assets_link = fields.Char(
        string="Assets Link",
        help="External link to the task assets. Imported from the task "
             "spreadsheet; stored for reference only.")
    instruction_md_link = fields.Char(
        string="Instruction.md Link",
        help="External link to instruction.md. Imported from the task "
             "spreadsheet; stored for reference only.")
    rubrics_link = fields.Char(
        string="Rubrics Link",
        help="External link to the rubrics doc. Imported from the task "
             "spreadsheet; stored for reference only.")

    # --- Delivery phase & status -------------------------------------------
    phase_id = fields.Many2one(
        comodel_name="fenrir.phase",
        string="Phase",
        ondelete="restrict",
        tracking=True,
        default=lambda self: self._default_phase_id(),
        help="Delivery phase this task belongs to. Defaults to the phase "
             "flagged as default by a manager; anyone can change it.")

    @api.model
    def _default_phase_id(self):
        # When this column is first added during a module upgrade, the
        # fenrir_phase table may not exist yet (model-init ordering). Guard
        # so the default never queries a missing table — that would abort the
        # whole upgrade transaction.
        if not table_exists(self.env.cr, "fenrir_phase"):
            return False
        return self.env["fenrir.phase"].search(
            [("is_default", "=", True)], limit=1).id
    delivery_status = fields.Selection(
        selection=[
            ("not_delivered", "Not Delivered"),
            ("delivered", "Delivered"),
        ],
        string="Delivery Status",
        default="not_delivered",
        required=True,
        tracking=True,
        help="Whether this task has been delivered to the client. "
             "Editable by Fenrir managers only.")
    is_fenrir_manager = fields.Boolean(
        string="Is Fenrir Manager",
        compute="_compute_is_fenrir_manager",
        help="Technical helper: True when the current user is a Fenrir "
             "manager. Drives manager-only editability in the form.")

    def _compute_is_fenrir_manager(self):
        is_manager = self.env.user.has_group("fenrir.group_fenrir_manager")
        for rec in self:
            rec.is_fenrir_manager = is_manager

    # --- Manager-only delivery-status enforcement --------------------------
    @api.model_create_multi
    def create(self, vals_list):
        is_manager = self.env.user.has_group("fenrir.group_fenrir_manager")
        Category = self.env["fenrir.category"]
        Phase = self.env["fenrir.phase"]
        default_phase = Phase.browse()
        if table_exists(self.env.cr, "fenrir_phase"):
            default_phase = Phase.search([("is_default", "=", True)], limit=1)
        # The importer computes final codes itself (respecting explicit Task
        # IDs); it flags them so we don't second-guess them here.
        codes_final = self.env.context.get("fenrir_codes_final")
        taken = set()
        for vals in vals_list:
            if not is_manager:
                # Non-managers cannot set delivery status on creation; it
                # falls back to the "Not Delivered" default.
                vals.pop("delivery_status", None)
            category = (Category.browse(vals["category_id"])
                        if vals.get("category_id") else Category)
            phase = (Phase.browse(vals["phase_id"])
                     if vals.get("phase_id") else default_phase)
            code = (vals.get("code") or "").strip()
            prefix = self._task_code_prefix(category, phase)
            # An on-form auto value looks like "<prefix><digits>" and may be
            # stale or speculative (two near-simultaneous New-task forms can
            # both preview the same code). Regenerate it — and any blank — from
            # the committed DB state so the code is authoritative and unique.
            is_auto = code.startswith(prefix) and code[len(prefix):].isdigit()
            if not code or (not codes_final and is_auto):
                vals["code"] = self._build_task_code(
                    category, phase, taken=taken)
            elif category:
                category._ensure_code()  # keep category codes populated
            taken.add(vals["code"])
        return super().create(vals_list)

    @api.constrains("code")
    def _check_code_unique(self):
        """Application-level uniqueness guard. Backs up the SQL unique index,
        which silently fails to install if duplicate codes already exist —
        this one blocks new duplicates regardless."""
        for rec in self:
            if not rec.code:
                continue
            dup = self.with_context(active_test=False).search(
                [("code", "=", rec.code), ("id", "!=", rec.id)], limit=1)
            if dup:
                raise ValidationError(_(
                    "Task Code %s is already used by another task.") % rec.code)

    def write(self, vals):
        if not self.env.user.has_group("fenrir.group_fenrir_manager"):
            if "delivery_status" in vals:
                raise AccessError(_(
                    "Only Fenrir managers can change the Delivery Status."))
            if "buyer_id" in vals:
                raise AccessError(_(
                    "Only Fenrir managers can change the Buyer."))
            if "reviewer_id" in vals:
                raise AccessError(_(
                    "Only Fenrir managers can change the Reviewer."))
        return super().write(vals)

    # ── Task-code generation ─────────────────────────────────────────────
    @api.model
    def _phase_code(self, phase):
        """PHASE_NO component parsed from a phase name.

        Whole numbers are zero-padded to 2 digits ('Phase 2' -> '02',
        'Phase 10' -> '10'); fractional phases are kept verbatim
        ('Phase 1.5' -> '1.5'). Falls back to '00' when no number is found."""
        name = (phase.name if phase else "") or ""
        match = re.search(r"\d+(?:\.\d+)?", name)
        if not match:
            return "00"
        num = match.group(0)
        return num if "." in num else f"{int(num):02d}"

    @api.model
    def _task_code_prefix(self, category, phase, persist=True):
        """The '<CAT>-<PHASE_NO>' portion of a task code (serial appended
        directly, no separator: e.g. 'AI-02' -> 'AI-0201').

        persist=False computes the category code without writing a generated
        one back onto the category (used for on-form previews)."""
        if category:
            cat_code = (category._ensure_code() if persist
                        else (category.code
                              or category._generate_unique_code(category.name)))
        else:
            cat_code = "XX"
        return f"{cat_code}-{self._phase_code(phase)}"

    @api.model
    def _build_task_code(self, category, phase, taken=None, persist=True):
        """Next unique <CAT>-<PHASE_NO><SERIAL> code for a category+phase.

        `taken` is an optional set of codes already claimed in the current
        batch (not yet in the DB)."""
        taken = taken or set()
        prefix = self._task_code_prefix(category, phase, persist=persist)
        plen = len(prefix)
        best = 0
        existing = set(self.with_context(active_test=False).search(
            [("code", "=like", prefix + "%")]).mapped("code"))
        for code in existing | taken:
            if code and code.startswith(prefix) and code[plen:].isdigit():
                best = max(best, int(code[plen:]))
        serial = best + 1
        code = f"{prefix}{serial:02d}"
        # Guard against exact clashes with existing or in-batch codes (e.g. a
        # legacy code that isn't a clean digit-suffix).
        while code in taken or code in existing:
            serial += 1
            code = f"{prefix}{serial:02d}"
        return code

    @api.onchange("category_id", "phase_id")
    def _onchange_autofill_code(self):
        """On a NEW task, (re)generate the Task Code whenever the category or
        phase changes, so the code always reflects the current selection.

        Saved tasks are left untouched — their code is used in Drive/S3 folder
        paths and external references, so it must stay stable."""
        if self._origin.id or not self.category_id:
            return
        self.code = self._build_task_code(
            self.category_id, self.phase_id, persist=False)

    def action_mark_delivered(self):
        """Bulk-mark the selected tasks as delivered (managers only)."""
        if not self.env.user.has_group("fenrir.group_fenrir_manager"):
            raise AccessError(_(
                "Only Fenrir managers can change the Delivery Status."))
        self.write({"delivery_status": "delivered"})

    def action_mark_not_delivered(self):
        """Bulk-mark the selected tasks as not delivered (managers only)."""
        if not self.env.user.has_group("fenrir.group_fenrir_manager"):
            raise AccessError(_(
                "Only Fenrir managers can change the Delivery Status."))
        self.write({"delivery_status": "not_delivered"})

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
    resources_notes = fields.Text(
        string="Resources Notes",
        help="General notes about the resources attached to this task.",
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
            _logger.info(
                "Fenrir: user %s clicked Approve on task %s",
                self.env.user.login, rec.code)
            drive.upload_task(rec)
            rec.status = "approved"

    def action_reapprove_task(self):
        if not self.env.user.has_group("fenrir.group_fenrir_manager"):
            raise UserError("Only managers can re-approve tasks.")
        drive = self.env["fenrir.drive.service"]
        for rec in self:
            _logger.info(
                "Fenrir: user %s clicked Re-approve on task %s",
                self.env.user.login, rec.code)
            # Rebuild the generated package (task_metadata.json, license.json,
            # environment files) from current state, so re-approve picks up any
            # changes made since the last submit (e.g. a newly added asset that
            # belongs in license.json) before the delta-sync upload runs.
            rec._regenerate_task_package()
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
            _logger.info(
                "Fenrir: user %s clicked Submit on task %s",
                self.env.user.login, rec.code)

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
                gen.build_task_metadata(self),
                indent=2, ensure_ascii=False).encode("utf-8")),
        })
        Attachment.create({
            "task_id": self.id,
            "file_name": "license.json",
            "folder": "root",
            "is_generated": True,
            "license": "self_created",
            "attachment": base64.b64encode(json.dumps(
                self._build_license_doc(),
                indent=2, ensure_ascii=False).encode("utf-8")),
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

        # tests/test_deliverables.* — auto-generation disabled.
        # Upload your own file via the Tests binary field on the form, or via
        # the Attachments uploader with folder=Tests. The export still picks
        # it up from _test_files() / the attachment row.
        # test_filename, test_content = gen.build_validator_script(self)
        # Attachment.create({
        #     "task_id": self.id,
        #     "file_name": test_filename,
        #     "folder": "tests",
        #     "is_generated": True,
        #     "license": "self_created",
        #     "attachment": base64.b64encode(test_content.encode("utf-8")),
        # })

        # Per-seller metadata.json — stored on the offer's metadata_json field
        # so the existing _write_rich_export() flow picks it up as
        # submissions/seller_<n>/metadata.json.
        for offer in self.seller_offer_ids.filtered(lambda o: o.accepted == "yes"):
            offer.metadata_json = json.dumps(
                gen.build_seller_metadata(offer),
                indent=2, ensure_ascii=False)
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
    fenrir_delivery_time = fields.Integer(string="Delivery Time (Days)", tracking=True)
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
        string="Sellers", compute="_compute_seller_offer_count", store=True,
        help="Total number of sellers (offers) assigned to this task.")
    accepted_offer_count = fields.Integer(
        string="Accepted", compute="_compute_seller_offer_count")
    accepted_delivery_count = fields.Integer(
        string="Accepted Deliveries", compute="_compute_seller_offer_count",
        store=True,
        help="Number of sellers whose Accepted Delivery is set to Yes.")
    all_deliveries_accepted = fields.Boolean(
        string="All Deliveries Accepted",
        compute="_compute_seller_offer_count", store=True,
        help="True when the task has at least one seller and every seller's "
             "Accepted Delivery is Yes.")

    _sql_constraints = [
        ("fenrir_task_code_unique", "unique(code)", "Task Code must be unique."),
    ]

    @api.depends("seller_offer_ids",
                 "seller_offer_ids.accepted",
                 "seller_offer_ids.accepted_delivery")
    def _compute_seller_offer_count(self):
        for rec in self:
            rec.seller_offer_count = len(rec.seller_offer_ids)
            rec.accepted_offer_count = len(
                rec.seller_offer_ids.filtered(lambda o: o.accepted == "yes"))
            rec.accepted_delivery_count = len(
                rec.seller_offer_ids.filtered(
                    lambda o: o.accepted_delivery == "yes"))
            rec.all_deliveries_accepted = bool(
                rec.seller_offer_count
                and rec.accepted_delivery_count == rec.seller_offer_count)

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

    def action_export_all_xlsx(self):
        """Build a wide XLSX of tasks × seller offers. If no records are
        passed (cog-menu "Export All" case), exports every task the user
        can read."""
        try:
            import xlsxwriter
        except ImportError as exc:
            raise UserError(
                "xlsxwriter is required for this export. "
                "Add it to requirements.txt and reinstall."
            ) from exc

        base = self if self else self.search([])
        tasks = base.filtered(lambda t: t.status == "completed")
        if not tasks:
            raise UserError("No completed tasks to export.")

        buf = io.BytesIO()
        wb = xlsxwriter.Workbook(buf, {"in_memory": True, "remove_timezone": True})
        ws = wb.add_worksheet("Tasks")

        hdr_fmt = wb.add_format({
            "bold": True, "bg_color": "#5C2D91", "color": "white",
            "border": 1, "align": "center", "valign": "vcenter",
            "text_wrap": True,
        })
        cell_fmt = wb.add_format({"valign": "top"})
        date_fmt = wb.add_format({"valign": "top", "num_format": "yyyy-mm-dd"})

        headers = [
            "Sr. No.", "Task ID", "Categories", "Title", "Overview",
            "Scope of work", "Company details", "Assets (PRD)",
            "Price Tier", "Delivery time (Days)", "Seller", "Seller Profile",
            "Received Custom Offer", "Offer Cost", "Conversation",
            "Order Placed Date", "Order Received Date", "Accepted Deliver",
            "Instructions.md", "Data (media)",
            "Resources (refs & supporting docs)", "Environment", "Tests",
            "License", "Task Metadata.json", "ratings.json",
            "Deliverables",
        ]
        widths = [6, 14, 18, 28, 40, 40, 30, 24, 12, 14, 22, 30, 18, 12,
                  60, 16, 16, 18, 22, 40, 40, 40, 40, 18, 22, 22, 60]
        for col, (h, w) in enumerate(zip(headers, widths)):
            ws.write(0, col, h, hdr_fmt)
            ws.set_column(col, col, w)
        ws.freeze_panes(1, 0)
        ws.set_row(0, 32)

        # Drive URL cache: {task_id: {relative_path: webViewLink}}.
        # We try task.drive_folder_id first (fast path). If that's empty,
        # we list every sub-folder under the configured Drive parent ONCE
        # and look up the task by its code. That way the export still
        # finds Drive URLs for tasks whose drive_folder_id was never
        # written back to Odoo (e.g. seed tasks, or rows re-imported from
        # outside the standard upload flow).
        drive_cache = {}
        DriveService = self.env["fenrir.drive.service"]
        parent_state = {"listed": False, "map": {}}

        def _parent_subfolders():
            if not parent_state["listed"]:
                parent_state["map"] = DriveService.list_task_subfolders()
                parent_state["listed"] = True
                _logger.info(
                    "Fenrir export: parent folder listing returned "
                    "%d subfolders",
                    len(parent_state["map"]))
            return parent_state["map"]

        def _drive_urls(task):
            if task.id in drive_cache:
                return drive_cache[task.id]
            urls = {}
            folder_id = (task.drive_folder_id or "").strip()
            origin = "task.drive_folder_id"

            if not folder_id:
                folder_id = _parent_subfolders().get(task.code or "", "")
                origin = "parent lookup by code"

            if folder_id:
                try:
                    urls = DriveService.list_files_with_urls(folder_id)
                    _logger.info(
                        "Fenrir export: drive lookup task=%s folder=%s "
                        "(%s) returned %d entries",
                        task.code, folder_id, origin, len(urls))
                except Exception:  # noqa: BLE001
                    _logger.warning(
                        "Fenrir export: drive lookup FAILED task=%s",
                        task.code, exc_info=True)
                    urls = {}
            else:
                _logger.info(
                    "Fenrir export: skip drive lookup task=%s "
                    "(no folder_id; not under Drive parent either)",
                    task.code)
            drive_cache[task.id] = urls
            return urls

        def _atts(task, folder):
            """Fallback listing of filename — URL pairs in `folder`.

            Used when no single folder URL is available (e.g. Drive walk
            couldn't reach the folder) so the cell still shows what's
            attached to the task instead of going blank.
            """
            files = task.attachment_ids.filtered(lambda a: a.folder == folder)
            urls = _drive_urls(task)
            lines = []
            for f in files:
                if not f.file_name:
                    continue
                url = (urls.get(f"{folder}/{f.file_name}")
                       or urls.get(f.file_name)
                       or "")
                lines.append(f"{f.file_name} — {url}" if url
                             else f.file_name)
            return "\n".join(lines) or ""

        def _folder_url(task, folder):
            """Return the Drive URL of the task sub-folder if known."""
            return _drive_urls(task).get(folder, "") or ""

        def _drive_url_for(task, *candidate_paths):
            urls = _drive_urls(task)
            for p in candidate_paths:
                if p in urls and urls[p]:
                    return urls[p]
            return ""

        def _delivs(offer):
            lines = []
            for d in offer.deliverable_file_ids:
                link = d.s3_key or ""
                lines.append(f"{d.file_name} — {link}" if link else d.file_name)
            for att in offer.deliverable_attachment_ids:
                lines.append(att.name or "")
            return "\n".join(l for l in lines if l)

        def _deliv_file_urls(task, offer, seller_dir):
            """Per-file Drive URLs for an offer's deliverables, newline-joined."""
            urls = _drive_urls(task)
            lines = []
            for d in offer.deliverable_file_ids:
                fn = _norm_filename(d.file_name or f"deliverable_{d.id}")
                url = urls.get(f"{seller_dir}/deliverables/{fn}", "")
                lines.append(url or d.file_name or "")
            for att in offer.deliverable_attachment_ids:
                fn = _norm_filename(att.name or f"deliverable_{att.id}")
                url = urls.get(f"{seller_dir}/deliverables/{fn}", "")
                lines.append(url or att.name or "")
            return "\n".join(l for l in lines if l)

        url_fmt = wb.add_format({
            "valign": "top",
            "color": "#0563C1", "underline": 1,
        })

        def _put(r, c, value, fmt=cell_fmt):
            ws.write(r, c, value or "", fmt)

        def _put_task_cell(r0, r1, c, value, fmt=cell_fmt, url=False):
            value = value or ""
            if r1 > r0:
                if url and isinstance(value, str) and value.startswith("http"):
                    ws.merge_range(r0, c, r1, c, "", fmt)
                    ws.write_url(r0, c, value, url_fmt, string=value)
                else:
                    ws.merge_range(r0, c, r1, c, value, fmt)
            else:
                if url and isinstance(value, str) and value.startswith("http"):
                    ws.write_url(r0, c, value, url_fmt, string=value)
                else:
                    ws.write(r0, c, value, fmt)

        row = 1
        sr = 1
        for task in tasks.sorted("code"):
            offers = list(task.seller_offer_ids.sorted("seller_no")) or [None]
            n = len(offers)
            r0, r1 = row, row + n - 1

            # Per-seller rows: cols 10..17, 25 (ratings.json), 26 (deliverables)
            for i, offer in enumerate(offers):
                r = row + i
                if offer:
                    _put(r, 10, offer.seller_username)
                    if offer.seller_profile_url:
                        ws.write_url(r, 11, offer.seller_profile_url,
                                     url_fmt, string=offer.seller_profile_url)
                    else:
                        _put(r, 11, "")
                    _put(r, 12, offer.received_custom_offer)
                    _put(r, 13, offer.negotiated_offer)
                    _put(r, 14, offer.conversation)
                    if offer.order_date:
                        ws.write_datetime(r, 15, offer.order_date, date_fmt)
                    else:
                        _put(r, 15, "")
                    if offer.delivery_date:
                        ws.write_datetime(r, 16, offer.delivery_date, date_fmt)
                    else:
                        _put(r, 16, "")
                    _put(r, 17, offer.accepted_delivery)

                    seller_dir = (
                        f"submissions/seller_{offer.seller_no or offer.id}"
                    )
                    ratings_url = _drive_url_for(
                        task, f"{seller_dir}/ratings.json")
                    if ratings_url:
                        ws.write_url(r, 25, ratings_url, url_fmt,
                                     string=ratings_url)
                    else:
                        _put(r, 25, "ratings.json (generated)")

                    delivs_url = _drive_url_for(
                        task, f"{seller_dir}/deliverables")
                    if delivs_url:
                        ws.write_url(r, 26, delivs_url, url_fmt,
                                     string=delivs_url)
                    else:
                        _put(r, 26,
                             _deliv_file_urls(task, offer, seller_dir)
                             or _delivs(offer))
                else:
                    for c in (10, 11, 12, 13, 14, 15, 16, 17, 25, 26):
                        _put(r, c, "")

            # Per-task columns: cols 0..9 and 18..24 — merged across seller rows.
            _put_task_cell(r0, r1, 0, sr)
            _put_task_cell(r0, r1, 1, task.code)
            _put_task_cell(r0, r1, 2, task.category_id.name)
            _put_task_cell(r0, r1, 3, task.title)
            _put_task_cell(r0, r1, 4, task.overview)
            _put_task_cell(r0, r1, 5, task.scope_of_work)
            _put_task_cell(r0, r1, 6, task.company_details)
            prd_url = task.assets_url or ""
            if not prd_url and task.assets_filename:
                norm = _norm_filename(task.assets_filename)
                prd_url = _drive_url_for(
                    task,
                    f"resources/{norm}",
                    f"resources/{task.assets_filename}",
                    norm,
                    task.assets_filename,
                )
            prd = prd_url or task.assets_filename or ""
            _put_task_cell(r0, r1, 7, prd, url=bool(prd_url))
            _put_task_cell(r0, r1, 8, task.price_tier)
            _put_task_cell(r0, r1, 9, task.fenrir_delivery_time)
            instr_url = _drive_url_for(task,
                                       "instruction.md",
                                       "Instruction.md",
                                       task.instruction_md_filename or "")
            _put_task_cell(
                r0, r1, 18,
                instr_url or (task.instruction_md_filename or ""),
                url=bool(instr_url))
            data_url = _folder_url(task, "data")
            _put_task_cell(
                r0, r1, 19,
                data_url or _atts(task, "data"),
                url=bool(data_url))
            res_url = _folder_url(task, "resources")
            _put_task_cell(
                r0, r1, 20,
                res_url or _atts(task, "resources"),
                url=bool(res_url))
            env_url = _folder_url(task, "environment")
            if env_url:
                _put_task_cell(r0, r1, 21, env_url, url=True)
            else:
                env_parts = [_atts(task, "environment")]
                if task.environment_base_runtime:
                    env_parts.insert(
                        0, f"base: {task.environment_base_runtime}")
                _put_task_cell(
                    r0, r1, 21,
                    "\n".join(p for p in env_parts if p))
            tests_url = _folder_url(task, "tests")
            _put_task_cell(
                r0, r1, 22,
                tests_url or _atts(task, "tests"),
                url=bool(tests_url))
            lic_url = _drive_url_for(task, "license.json")
            _put_task_cell(
                r0, r1, 23,
                lic_url or "license.json (generated)",
                url=bool(lic_url))
            meta_url = _drive_url_for(task, "task_metadata.json")
            _put_task_cell(
                r0, r1, 24,
                meta_url or "task_metadata.json (generated)",
                url=bool(meta_url))

            row += n
            sr += 1

        wb.close()
        xlsx_bytes = buf.getvalue()
        buf.close()

        ts = fields.Datetime.now().strftime("%Y%m%d_%H%M%S")
        name = f"fenrir_tasks_export_{ts}.xlsx"
        attachment = self.env["ir.attachment"].create({
            "name": name,
            "type": "binary",
            "datas": base64.b64encode(xlsx_bytes),
            "mimetype": (
                "application/vnd.openxmlformats-officedocument"
                ".spreadsheetml.sheet"
            ),
            "res_model": "fenrir.task",
        })
        return {
            "type": "ir.actions.act_url",
            "url": f"/web/content/{attachment.id}?download=true",
            "target": "self",
        }

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
        # ignore the is_binary_upload / existing_s3_key / source_mtime flags.
        # content is a zero-arg callable returning bytes (see
        # _collect_export_files docstring) — call it here.
        for rel_path, content, _mime, _is_binary, _s3, _mtime in self._collect_export_files():
            zf.writestr(f"{root}/{rel_path}", content())

    def _collect_export_files(self):
        """Return [(rel_path, content_loader, mime, is_binary_upload,
        existing_s3_key, source_mtime), ...].

        content_loader is a zero-arg callable that returns the file's
        bytes. Heavy reads (S3 fetches, large b64-decodes) are deferred
        so the Drive delta-sync can skip them entirely when the timestamp
        check decides nothing changed.

        source_mtime is the underlying record's write_date for files
        backed by an actual DB record (attachments, deliverables);
        None for purely-generated content. The Drive uploader uses
        `source_mtime <= task.drive_last_uploaded_at` as a fast-path
        "nothing changed since last sync" shortcut.

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

        def _const(b):
            # Wraps already-computed bytes in a zero-arg callable so the
            # consumer can treat every entry uniformly as a loader.
            return lambda: b

        # instruction.md — annotator-uploaded file wins; otherwise build from text.
        if self.instruction_md_file:
            files.append(("instruction.md",
                          lambda v=self.instruction_md_file: base64.b64decode(v),
                          "text/markdown", UPLOADED, None, None))
        else:
            md_bytes = self._build_instruction_md(include_remarks=True).encode("utf-8")
            files.append(("instruction.md", _const(md_bytes),
                          "text/markdown", GENERATED, None, None))

        # if self.rubrics_file:
        #     name = _slug(self.rubrics_filename or "rubrics_source")
        #     mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
        #     files.append((name, base64.b64decode(self.rubrics_file), mime, UPLOADED, None))

        if self.assets_file:
            name = _norm_filename(self.assets_filename or "assets")
            mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
            files.append((f"resources/{name}",
                          lambda v=self.assets_file: base64.b64decode(v),
                          mime, UPLOADED, None, None))

        generated_env_names = set()
        generated_test_names = set()
        wrote_task_metadata = False
        wrote_licenses = False

        for att in self.attachment_ids:
            if not att.has_content():
                continue
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
            files.append((rel,
                          lambda a=att: a._fetch_bytes(),
                          mime, tag, att.s3_key or None, att.write_date))

        if not wrote_task_metadata:
            meta_bytes = json.dumps(gen.build_task_metadata(self), indent=2, ensure_ascii=False).encode("utf-8")
            files.append(("task_metadata.json", _const(meta_bytes),
                          "application/json", GENERATED, None, None))
        if not wrote_licenses:
            lic_bytes = json.dumps(self._build_license_doc(), indent=2, ensure_ascii=False).encode("utf-8")
            files.append(("license.json", _const(lic_bytes),
                          "application/json", GENERATED, None, None))

        # Legacy per-task binary uploads (user-uploaded Dockerfile etc.).
        for filename, content in self._environment_files():
            if filename in generated_env_names:
                continue
            mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
            files.append((f"environment/{filename}", _const(content),
                          mime, UPLOADED, None, None))

        for filename, content in self._test_files():
            if filename in generated_test_names:
                continue
            mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
            files.append((f"tests/{filename}", _const(content),
                          mime, UPLOADED, None, None))

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
                meta_bytes = json.dumps(fallback, indent=2, default=str, ensure_ascii=False).encode("utf-8")
            files.append((f"{seller_dir}/metadata.json", _const(meta_bytes),
                          "application/json", GENERATED, None, None))
            ratings_bytes = json.dumps(self._build_ratings(offer), indent=2, default=str, ensure_ascii=False).encode("utf-8")
            files.append((f"{seller_dir}/ratings.json", _const(ratings_bytes),
                          "application/json", GENERATED, None, None))

            for att in offer.deliverable_attachment_ids:
                if not att.datas:
                    continue
                safe_name = _norm_filename(att.name or f"deliverable_{att.id}")
                mime = att.mimetype or mimetypes.guess_type(safe_name)[0] \
                    or "application/octet-stream"
                files.append((f"{seller_dir}/deliverables/{safe_name}",
                              lambda a=att: base64.b64decode(a.datas),
                              mime, UPLOADED, None, att.write_date))

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
                              lambda d=deliv: d.fetch_bytes(),
                              mime, UPLOADED, deliv.s3_key,
                              deliv.s3_uploaded_at or deliv.write_date))

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
        # Fallback note for resources-folder files when the row-level
        # `att.notes` is empty. Picks up the task-level Resources Notes
        # field so users don't have to repeat it on every uploaded file.
        resources_fallback = self.resources_notes or ""
        for att in self.attachment_ids:
            if att.is_generated:
                continue
            if att.folder in ("environment", "tests"):
                continue
            folder_key = att.folder or "resources"
            location = "root" if folder_key == "root" else f"{folder_key}/"
            note = att.notes or (
                resources_fallback if folder_key == "resources" else "")
            assets.append({
                "file_name": _norm_filename(att.file_name or f"attachment_{att.id}"),
                "location": location,
                "license": att.license_label(),
                "source_url": att.source_url or None,
                "notes": note,
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
                "notes": self.resources_notes or "",
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
            # The rater is the task's Buyer (not create_uid, which for imported
            # tasks is whoever ran the import).
            "rater_id": (
                f"rater_{offer.task_id.buyer_id.id:03d}"
                if offer.task_id.buyer_id else ""
            ),
            # rating_date: user-set on the seller offer. Falls back to the
            # latest rubric-score write_date (then offer create_date) for
            # records saved before this field existed.
            "rating_date": (
                offer.rating_date.isoformat() if offer.rating_date
                else (max(s.write_date for s in offer.rubric_score_ids
                          if s.write_date).date().isoformat()
                      if offer.rubric_score_ids
                      else (offer.create_date.date().isoformat()
                            if offer.create_date else None))
            ),
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
