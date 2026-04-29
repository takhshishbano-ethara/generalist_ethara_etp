import ast
import base64
import logging
import os

from odoo import api, fields, models
from odoo.exceptions import UserError, ValidationError

from . import dataset_resolver, harness_staging_executor

_logger = logging.getLogger(__name__)

STAGING_STAGE_SELECTION = [
    ("draft", "Draft"),
    ("testing", "Testing"),
    ("tested", "Tested"),
    ("evaluating", "Evaluating"),
    ("done", "Done"),
    ("notified", "Notified"),
    ("deployed", "Deployed"),
    ("failed", "Failed"),
]

STAGING_TEST_RESULT = [
    ("idle", "Idle"),
    ("success", "Success"),
    ("failed", "Failed"),
]

LANGUAGE_SELECTION = [
    ("python", "Python"),
    ("golang", "Go"),
    ("javascript", "JavaScript"),
    ("typescript", "TypeScript"),
    ("java", "Java"),
    ("rust", "Rust"),
    ("c", "C"),
    ("cpp", "C++"),
    ("ruby", "Ruby"),
    ("php", "PHP"),
    ("swift", "Swift"),
    ("kotlin", "Kotlin"),
    ("scala", "Scala"),
    ("csharp", "C#"),
]

_REQUIRED_INSTANCE_METHODS = {"run", "test_patch_run", "fix_patch_run", "parse_log"}
_REQUIRED_IMAGE_METHODS = {"dependency", "files", "dockerfile"}


class AuroraHarnessStaging(models.Model):
    _name = "aurora.harness.staging"
    _description = "Aurora Harness Staging"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "id desc"

    def init(self):
        """Create partial unique index — Odoo constraints don't support WHERE."""
        self.env.cr.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS
                aurora_harness_staging_unique_active_org_repo
            ON aurora_harness_staging (org, repo)
            WHERE (active = TRUE)
        """)

    name = fields.Char(compute="_compute_name", store=True)
    user_id = fields.Many2one(
        "res.users",
        string="Developer",
        default=lambda self: self.env.user,
        required=True,
        tracking=True,
    )
    org = fields.Char(string="GitHub Org", required=True, tracking=True)
    repo = fields.Char(string="GitHub Repo", required=True, tracking=True)
    language = fields.Selection(
        LANGUAGE_SELECTION,
        required=True,
        default="python",
        tracking=True,
    )

    harness_file = fields.Binary(string="Harness File", required=True, attachment=False)
    harness_filename = fields.Char(string="Filename")
    stage = fields.Selection(
        STAGING_STAGE_SELECTION,
        default="draft",
        tracking=True,
        required=True,
    )

    pipeline_id = fields.Many2one(
        "aurora.pipeline",
        string="Source Pipeline",
        help="Select a completed pipeline. Dataset file is auto-filled.",
    )
    dataset_file = fields.Char(
        string="Dataset File",
        help="Auto-filled from Source Pipeline. Override for custom dataset.",
    )
    test_log = fields.Text(string="Test Log", readonly=True)
    test_result = fields.Selection(
        STAGING_TEST_RESULT, default="idle", string="Test Result", readonly=True,
    )
    eval_id = fields.Many2one(
        "aurora.evaluation",
        string="Linked Evaluation",
        readonly=True,
    )
    staging_path = fields.Char(string="Staging Path", readonly=True)

    admin_notes = fields.Text(string="Admin Notes")
    notified_at = fields.Datetime(string="Notified At", readonly=True)
    deployed_at = fields.Datetime(string="Deployed At", readonly=True)
    active = fields.Boolean(default=True)

    is_admin = fields.Boolean(compute="_compute_is_admin")

    ai_reference_harness = fields.Char(
        string="Reference Harness",
        compute="_compute_ai_hints",
        help="Closest existing harness file in the same language. Copy its structure when authoring yours.",
    )
    ai_prompt_text = fields.Text(
        string="AI Prompt",
        compute="_compute_ai_hints",
        help="Ready-to-paste prompt for Claude/Cursor/Copilot. Attach the reference harness "
             "file and the dataset file, then paste this prompt.",
    )

    _AI_REFERENCE_MAP = {
        "python": "custom_addons/aurora/tools/harness/repos/python/psf/requests.py",
        "golang": "custom_addons/aurora/tools/harness/repos/golang/istio/istio.py",
        "javascript": "custom_addons/aurora/tools/harness/repos/javascript/<org>/<repo>.py",
        "typescript": "custom_addons/aurora/tools/harness/repos/typescript/<org>/<repo>.py",
        "java": "custom_addons/aurora/tools/harness/repos/java/<org>/<repo>.py",
        "rust": "custom_addons/aurora/tools/harness/repos/rust/<org>/<repo>.py",
        "ruby": "custom_addons/aurora/tools/harness/repos/ruby/<org>/<repo>.py",
        "cpp": "custom_addons/aurora/tools/harness/repos/cpp/<org>/<repo>.py",
        "c": "custom_addons/aurora/tools/harness/repos/c/<org>/<repo>.py",
        "csharp": "custom_addons/aurora/tools/harness/repos/csharp/<org>/<repo>.py",
        "php": "custom_addons/aurora/tools/harness/repos/php/<org>/<repo>.py",
        "kotlin": "custom_addons/aurora/tools/harness/repos/kotlin/<org>/<repo>.py",
        "scala": "custom_addons/aurora/tools/harness/repos/scala/<org>/<repo>.py",
        "swift": "custom_addons/aurora/tools/harness/repos/swift/<org>/<repo>.py",
    }

    @api.depends("org", "repo", "language", "dataset_file")
    def _compute_ai_hints(self):
        for rec in self:
            if not (rec.org and rec.repo and rec.language):
                rec.ai_reference_harness = False
                rec.ai_prompt_text = False
                continue
            rec.ai_reference_harness = self._AI_REFERENCE_MAP.get(
                rec.language,
                f"custom_addons/aurora/tools/harness/repos/{rec.language}/",
            )
            dataset_display = rec.dataset_file or "(select Source Pipeline to auto-fill)"
            rec.ai_prompt_text = (
                f"You are writing a new harness for the Aurora evaluation pipeline.\n\n"
                f"ATTACHED:\n"
                f"  1. Reference harness (same language): {rec.ai_reference_harness}\n"
                f"  2. Dataset for the missing repo: {dataset_display}\n\n"
                f"TASK:\n"
                f"  Produce a single Python file modeled EXACTLY on the reference harness,\n"
                f"  but adapted for `{rec.org}/{rec.repo}`.\n\n"
                f"REQUIREMENTS:\n"
                f"  - Change the three class names to use the `{rec.repo.title().replace('-', '').replace('_', '')}` prefix.\n"
                f"  - Change `@Instance.register(...)` to: @Instance.register(\"{rec.org}\", \"{rec.repo}\")\n"
                f"  - Pick a base Docker image appropriate for the repo's {rec.language} toolchain.\n"
                f"  - Keep the shell scripts (run.sh, test-run.sh, fix-run.sh) but update\n"
                f"    the test command to what the repo's CI uses. Infer from attached test_patch entries.\n"
                f"  - Keep parse_log() regex if the reference uses the same test framework,\n"
                f"    otherwise update to the correct PASS/FAIL pattern.\n"
                f"  - Do NOT invent fields that aren't in the reference.\n"
                f"  - Output: a single .py file ready for upload, under 100 KB.\n\n"
                f"VALIDATE BEFORE OUTPUT:\n"
                f"  - @Instance.register decorator present with correct org/repo strings\n"
                f"  - Instance class has: run, test_patch_run, fix_patch_run, parse_log\n"
                f"  - Image class has: dependency, files, dockerfile\n"
                f"  - File is valid Python (no syntax errors)"
            )

    @api.depends("org", "repo", "user_id")
    def _compute_name(self):
        for rec in self:
            if rec.org and rec.repo:
                user_name = rec.user_id.name or ""
                rec.name = f"{rec.org}/{rec.repo} — {user_name}"
            else:
                rec.name = "New"

    @api.depends_context("uid")
    def _compute_is_admin(self):
        is_admin = self.env.user.has_group("aurora.group_aurora_admin")
        for rec in self:
            rec.is_admin = is_admin

    @api.onchange("pipeline_id")
    def _onchange_pipeline_id(self):
        if not self.pipeline_id:
            return
        pl = self.pipeline_id
        if pl.step6_file:
            self.dataset_file = pl.step6_file

    @api.constrains("harness_file", "harness_filename")
    def _check_harness_file(self):
        for rec in self:
            if not rec.harness_file:
                continue
            if rec.harness_filename and not rec.harness_filename.endswith(".py"):
                raise ValidationError("Harness file must be a .py file.")
            raw = base64.b64decode(rec.harness_file)
            if len(raw) > 100_000:
                raise ValidationError("Harness file exceeds 100KB limit.")

    def _validate_harness_content(self, content: bytes) -> None:
        try:
            source = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise UserError(f"File is not valid UTF-8: {exc}") from exc

        try:
            tree = ast.parse(source)
        except SyntaxError as exc:
            raise UserError(f"Python syntax error: {exc}") from exc

        has_register_decorator = False
        instance_methods: set[str] = set()
        image_methods: set[str] = set()

        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue

            for decorator in node.decorator_list:
                if isinstance(decorator, ast.Call):
                    func = decorator.func
                    if (
                        isinstance(func, ast.Attribute)
                        and func.attr == "register"
                        and isinstance(func.value, ast.Name)
                        and func.value.id == "Instance"
                    ):
                        has_register_decorator = True

            for base in node.bases:
                base_name = ""
                if isinstance(base, ast.Name):
                    base_name = base.id
                elif isinstance(base, ast.Attribute):
                    base_name = base.attr

                if base_name == "Instance":
                    for item in node.body:
                        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            instance_methods.add(item.name)
                elif base_name == "Image":
                    for item in node.body:
                        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            image_methods.add(item.name)

        if not has_register_decorator:
            raise UserError(
                "Harness file must contain @Instance.register(...) decorator."
            )

        missing_instance = _REQUIRED_INSTANCE_METHODS - instance_methods
        if missing_instance:
            raise UserError(
                f"Instance class is missing required methods: {', '.join(sorted(missing_instance))}"
            )

        missing_image = _REQUIRED_IMAGE_METHODS - image_methods
        if missing_image:
            raise UserError(
                f"Image class is missing required methods: {', '.join(sorted(missing_image))}"
            )

    def _write_staging_file(self) -> str:
        self.ensure_one()
        ICP = self.env["ir.config_parameter"].sudo()
        base_dir = ICP.get_param("aurora.output_dir", "/tmp/aurora_output")
        staging_dir = os.path.join(
            base_dir, "harness_staging", str(self.user_id.id), self.org,
        )
        os.makedirs(staging_dir, exist_ok=True)

        filename = self.harness_filename or f"{self.repo}.py"
        file_path = os.path.join(staging_dir, filename)

        raw = base64.b64decode(self.harness_file)
        with open(file_path, "wb") as f:
            f.write(raw)

        return file_path

    def _ensure_staging_file(self) -> str:
        self.ensure_one()
        if self.staging_path and os.path.isfile(self.staging_path):
            return self.staging_path
        path = self._write_staging_file()
        self.sudo().write({"staging_path": path})
        return path

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        for rec in records:
            if rec.harness_file:
                raw = base64.b64decode(rec.harness_file)
                rec._validate_harness_content(raw)
                path = rec._write_staging_file()
                rec.sudo().write({"staging_path": path})
        return records

    def write(self, vals):
        res = super().write(vals)
        if "harness_file" in vals and vals["harness_file"]:
            for rec in self:
                raw = base64.b64decode(rec.harness_file)
                rec._validate_harness_content(raw)
                path = rec._write_staging_file()
                rec.sudo().write({"staging_path": path})
        return res

    def action_test_harness(self):
        self.ensure_one()
        if self.stage == "testing":
            raise UserError("Test already in progress.")
        if not self.dataset_file:
            raise UserError(
                "Dataset file is required. Select a Source Pipeline or set it manually."
            )
        try:
            local_dataset = dataset_resolver.resolve_to_local(self.env, self.dataset_file)
        except Exception as exc:
            raise UserError(f"Failed to fetch remote dataset: {self.dataset_file}\n{exc}") from exc
        if not os.path.isfile(local_dataset):
            raise UserError(f"Dataset file not found: {self.dataset_file}")

        self._ensure_staging_file()
        self.write({
            "stage": "testing",
            "test_result": "idle",
            "test_log": False,
        })

        db_name = self.env.cr.dbname
        uid = self.env.uid
        rec_id = self.id

        def _submit():
            try:
                harness_staging_executor.submit_test_async(db_name, uid, rec_id)
            except Exception:
                _logger.exception("Failed to submit staging test for rec=%s", rec_id)

        self.env.cr.postcommit.add(_submit)
        return {
            "type": "ir.actions.act_window",
            "res_model": self._name,
            "res_id": self.id,
            "view_mode": "form",
            "target": "current",
        }

    def action_run_full_evaluation(self):
        self.ensure_one()
        if self.stage != "tested" or self.test_result != "success":
            raise UserError("Run a successful test first before full evaluation.")
        if not self.dataset_file:
            raise UserError("Dataset file is required.")

        ICP = self.env["ir.config_parameter"].sudo()
        base_dir = ICP.get_param("aurora.output_dir", "/tmp/aurora_output")
        org_repo = f"{self.org}__{self.repo}"
        output_dir = os.path.join(base_dir, "harness", org_repo)

        evaluation = self.env["aurora.evaluation"].create({
            "pipeline_id": self.pipeline_id.id if self.pipeline_id else False,
            "dataset_file": self.dataset_file,
            "output_dir": output_dir,
            "instance_limit": 0,
            "user_id": self.user_id.id,
        })
        self.write({
            "stage": "evaluating",
            "eval_id": evaluation.id,
        })

        evaluation.action_run_evaluation()

        return {
            "type": "ir.actions.act_window",
            "res_model": "aurora.evaluation",
            "res_id": evaluation.id,
            "view_mode": "form",
            "target": "current",
        }

    def action_notify_admin(self):
        self.ensure_one()
        if self.stage not in ("done", "tested"):
            raise UserError("Complete testing or evaluation before notifying admin.")

        self.write({
            "stage": "notified",
            "notified_at": fields.Datetime.now(),
        })

        admin_group = self.env.ref("aurora.group_aurora_admin", raise_if_not_found=False)
        if admin_group and admin_group.all_user_ids:
            self.activity_schedule(
                "mail.mail_activity_data_todo",
                user_id=admin_group.all_user_ids[0].id,
                summary=f"Harness ready for deployment: {self.org}/{self.repo}",
                note=(
                    f"Developer {self.user_id.name} has completed testing. "
                    f"Please review and push to GitHub."
                ),
            )
        self.message_post(
            body=f"Admin notified for deployment of {self.org}/{self.repo}.",
            message_type="comment",
            subtype_xmlid="mail.mt_note",
        )

    def action_download_file(self):
        self.ensure_one()
        if not self.harness_file:
            raise UserError("No harness file to download.")
        return {
            "type": "ir.actions.act_url",
            "url": f"/web/content?model={self._name}&id={self.id}"
                   f"&field=harness_file&filename_field=harness_filename&download=true",
            "target": "self",
        }

    def _download_local_file(self, abs_path, filename):
        if not abs_path or not os.path.isfile(abs_path):
            raise UserError(f"File not found on server: {abs_path!r}")
        with open(abs_path, "rb") as fh:
            data = fh.read()
        attachment = self.env["ir.attachment"].create({
            "name": filename,
            "type": "binary",
            "datas": base64.b64encode(data),
            "res_model": self._name,
            "res_id": self.id,
            "mimetype": "application/octet-stream",
        })
        return {
            "type": "ir.actions.act_url",
            "url": f"/web/content/{attachment.id}?download=true",
            "target": "self",
        }

    def action_download_dataset(self):
        self.ensure_one()
        if not self.dataset_file:
            raise UserError(
                "No dataset file is set on this record. Select a Source Pipeline "
                "first so the dataset path gets filled in."
            )
        if dataset_resolver.is_remote(self.dataset_file):
            return {
                "type": "ir.actions.act_url",
                "url": self.dataset_file,
                "target": "new",
            }
        abs_path = os.path.abspath(self.dataset_file)
        filename = os.path.basename(abs_path) or f"{self.org}__{self.repo}_dataset.jsonl"
        return self._download_local_file(abs_path, filename)

    def action_download_reference_harness(self):
        self.ensure_one()
        if not self.ai_reference_harness:
            raise UserError(
                "No reference harness is resolved. Pick a Language first."
            )
        repo_root = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "..", "..")
        )
        candidate = os.path.join(repo_root, self.ai_reference_harness)
        if not os.path.isfile(candidate):
            raise UserError(
                f"Reference harness file not found at {candidate!r}. "
                "The path template in `_AI_REFERENCE_MAP` may need adjustment for this language."
            )
        return self._download_local_file(candidate, os.path.basename(candidate))

    def action_mark_deployed(self):
        self.ensure_one()
        self.write({
            "stage": "deployed",
            "deployed_at": fields.Datetime.now(),
        })
        if self.staging_path and os.path.exists(self.staging_path):
            try:
                os.remove(self.staging_path)
            except OSError:
                pass
        self.message_post(
            body="Harness marked as deployed.",
            message_type="comment",
            subtype_xmlid="mail.mt_note",
        )

    def action_reject(self):
        self.ensure_one()
        self.write({"stage": "failed"})
        self.message_post(
            body="Harness rejected by admin.",
            message_type="comment",
            subtype_xmlid="mail.mt_note",
        )

    def action_retry(self):
        self.ensure_one()
        self.write({
            "stage": "draft",
            "test_result": "idle",
            "test_log": False,
        })

    def action_cancel_staging(self):
        self.ensure_one()
        if self.staging_path and os.path.exists(self.staging_path):
            try:
                os.remove(self.staging_path)
            except OSError:
                pass

        from ..tools.harness.instance import Instance
        key = f"{self.org}/{self.repo}"
        Instance._registry.pop(key, None)

        self.write({"active": False, "stage": "failed"})
        self.message_post(
            body="Staging cancelled.",
            message_type="comment",
            subtype_xmlid="mail.mt_note",
        )
