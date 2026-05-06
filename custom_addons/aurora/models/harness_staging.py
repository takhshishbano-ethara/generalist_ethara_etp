import ast
import base64
import io
import logging
import os
import zipfile

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

from . import harness_staging_executor

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

    harness_file = fields.Binary(string="Harness File", attachment=False)
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

    # Curated picks preferred over directory-scan fallback.  These are the
    # best-quality, best-documented reference harnesses per language.  For any
    # language not listed here, _find_reference_harness() scans the repos
    # directory for a real existing file.
    _AI_REFERENCE_MAP = {
        "python": "custom_addons/aurora/tools/harness/repos/python/psf/requests.py",
        "golang": "custom_addons/aurora/tools/harness/repos/golang/istio/istio.py",
    }

    _HARNESS_REPOS_RELROOT = "custom_addons/aurora/tools/harness/repos"

    @api.model
    def _find_reference_harness(self, language, org=None, repo=None):
        """Return a repo-relative path to a real existing reference harness.

        Preference order:
          1. Exact curated pick from ``_AI_REFERENCE_MAP`` if its file exists.
          2. ``<lang>/<org>/<repo>.py`` if the user's own repo already has one.
          3. First ``.py`` file in ``<lang>/<org>/`` (same-org heuristic).
          4. First ``.py`` file anywhere under ``<lang>/`` (alphabetical).
          5. ``False`` if no files at all for this language.
        """
        if not language:
            return False

        module_dir = os.path.dirname(__file__)
        repo_root = os.path.abspath(os.path.join(module_dir, "..", "..", ".."))

        curated = self._AI_REFERENCE_MAP.get(language)
        if curated and os.path.isfile(os.path.join(repo_root, curated)):
            return curated

        lang_dir = os.path.join(repo_root, self._HARNESS_REPOS_RELROOT, language)
        if not os.path.isdir(lang_dir):
            return False

        def _rel(abs_path):
            return os.path.relpath(abs_path, repo_root)

        if org and repo:
            candidate = os.path.join(lang_dir, org, f"{repo}.py")
            if os.path.isfile(candidate):
                return _rel(candidate)

        if org:
            org_dir = os.path.join(lang_dir, org)
            if os.path.isdir(org_dir):
                for name in sorted(os.listdir(org_dir)):
                    if name.endswith(".py") and not name.startswith("_"):
                        return _rel(os.path.join(org_dir, name))

        for dirpath, _dirnames, filenames in sorted(os.walk(lang_dir)):
            for fname in sorted(filenames):
                if fname.endswith(".py") and not fname.startswith("_"):
                    return _rel(os.path.join(dirpath, fname))

        return False

    @api.depends("org", "repo", "language", "dataset_file")
    def _compute_ai_hints(self):
        for rec in self:
            if not (rec.org and rec.repo and rec.language):
                rec.ai_reference_harness = False
                rec.ai_prompt_text = False
                continue
            rec.ai_reference_harness = self._find_reference_harness(
                rec.language, rec.org, rec.repo,
            ) or f"{self._HARNESS_REPOS_RELROOT}/{rec.language}/"
            dataset_display = rec.dataset_file or "(select Source Pipeline to auto-fill)"
            class_prefix = rec.repo.title().replace('-', '').replace('_', '')
            rec.ai_prompt_text = (
                f"You are writing a new harness (registry file) for the Aurora evaluation pipeline.\n"
                f"Target repo: {rec.org}/{rec.repo}  (language: {rec.language})\n\n"
                f"YOU HAVE TWO INPUTS. USE BOTH.\n\n"
                f"INPUT 1 - Reference harness (a WORKING harness in the same language):\n"
                f"  Path: {rec.ai_reference_harness}\n"
                f"  Role: structural template. Copy its class layout, imports,\n"
                f"        Dockerfile scaffolding, and ALL shell scripts verbatim.\n"
                f"        The reference is the source of truth for the PATTERN; you only\n"
                f"        change the names, the org/repo, and the parse_log regex if the\n"
                f"        test framework differs.\n\n"
                f"INPUT 2 - Dataset JSONL for `{rec.org}/{rec.repo}`:\n"
                f"  Path: {dataset_display}\n"
                f"  Role: ground truth about the target repo. Each line is one instance with\n"
                f"        fields: instance_id, org, repo, number, base{{ref, sha}}, fix_patch,\n"
                f"        test_patch, release_line, lang, etc.\n"
                f"  Read the first 2-3 records and extract:\n"
                f"    - base.sha                  -> goes into prepare.sh as `git checkout <sha>`\n"
                f"    - test_patch file paths     -> confirm test framework (e.g. *_test.go = go test)\n"
                f"    - fix_patch touched files   -> confirm build system (go.mod, package.json, etc.)\n\n"
                f"PROCEDURE:\n"
                f"  1. Open INPUT 1 fully. Your output is essentially INPUT 1 with names swapped.\n"
                f"  2. Skim 2-3 records of INPUT 2 to verify the test framework matches INPUT 1.\n"
                f"     If it doesn't match, adjust parse_log() regex (and ONLY that).\n"
                f"  3. Produce ONE new Python file mirroring INPUT 1's structure.\n\n"
                f"STYLE REQUIREMENTS (follow the reference EXACTLY):\n"
                f"  - Three classes: `{class_prefix}ImageBase`, `{class_prefix}ImageDefault`, `{class_prefix}`.\n"
                f"  - @Instance.register decorator: @Instance.register(\"{rec.org}\", \"{rec.repo}\")\n"
                f"  - Base image `dependency()` returns a BARE toolchain image string\n"
                f"    (e.g. \"golang:latest\", \"python:3.11\", \"node:20\"). NO apt-get install\n"
                f"    unless the reference has one.\n"
                f"  - The base Dockerfile has ONLY: FROM + global_env + WORKDIR + clone/COPY + clear_env.\n"
                f"    Do NOT add `RUN apt-get install ...` packages unless the reference does.\n"
                f"  - ImageDefault.files() returns the SAME 7 files as the reference:\n"
                f"    fix.patch, test.patch, check_git_changes.sh, prepare.sh, run.sh,\n"
                f"    test-run.sh, fix-run.sh. Copy each shell script byte-for-byte from\n"
                f"    the reference; only change the test command if the framework differs.\n"
                f"  - prepare.sh does `git checkout {{pr.base.sha}}` (the dataset field), then\n"
                f"    runs the test command once (with `|| true` so build doesn't abort).\n"
                f"  - Instance.run() / test_patch_run() / fix_patch_run() are ONE-LINE methods\n"
                f"    that return `\"bash /home/run.sh\"` / `\"bash /home/test-run.sh\"` /\n"
                f"    `\"bash /home/fix-run.sh\"`. Do NOT build multi-line bash -c one-liners.\n"
                f"    Do NOT add class constants like _VENDOR_FIX, _TEST_CMD, _GOPATH_SETUP.\n"
                f"    Do NOT add pre-modules / GOPATH branching logic. The shell scripts own it.\n"
                f"  - parse_log() matches the reference's regex list and semantics exactly,\n"
                f"    unless the test framework genuinely differs (rare).\n"
                f"  - Do NOT invent fields, classes, methods, or constants not in the reference.\n"
                f"  - Output: a single .py file, valid Python, under 100 KB.\n\n"
                f"VALIDATE BEFORE YOU RETURN THE FILE:\n"
                f"  - @Instance.register decorator has org=\"{rec.org}\" and repo=\"{rec.repo}\".\n"
                f"  - Instance class defines: run, test_patch_run, fix_patch_run, parse_log.\n"
                f"  - Image class defines: dependency, files, dockerfile.\n"
                f"  - prepare.sh references `{{pr.base.sha}}` (the dataset field, not 'main').\n"
                f"  - Instance.run / test_patch_run / fix_patch_run bodies are ONE return statement\n"
                f"    each, pointing at the corresponding /home/*.sh script.\n"
                f"  - No class constants beyond what the reference defines.\n"
                f"  - File parses as valid Python (no syntax errors)."
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
        if pl.github_org:
            self.org = pl.github_org
        if pl.github_repo:
            self.repo = pl.github_repo
        if pl.detected_lang:
            self.language = pl.detected_lang
        if pl.step6_file:
            self.dataset_file = pl.step6_file

    @api.constrains("harness_file", "harness_filename")
    def _check_harness_file(self):
        for rec in self:
            if not rec.harness_file:
                continue
            fname = rec.harness_filename or ""
            if fname and not fname.endswith(".py") and not fname.endswith(".zip"):
                raise ValidationError("Upload a single .py file or a .zip containing multiple .py files.")
            raw = base64.b64decode(rec.harness_file)
            if fname.endswith(".zip"):
                if len(raw) > 2_000_000:
                    raise ValidationError("ZIP file exceeds 2MB limit.")
            else:
                if len(raw) > 100_000:
                    raise ValidationError("Harness file exceeds 100KB limit.")

    def _is_zip_upload(self) -> bool:
        return bool(self.harness_filename and self.harness_filename.endswith(".zip"))

    def _extract_zip_files(self) -> list[tuple[str, bytes]]:
        raw = base64.b64decode(self.harness_file)
        try:
            zf = zipfile.ZipFile(io.BytesIO(raw))
        except zipfile.BadZipFile as exc:
            raise UserError(f"Uploaded file is not a valid ZIP: {exc}") from exc
        py_files: list[tuple[str, bytes]] = []
        for name in sorted(zf.namelist()):
            if name.endswith("/") or not name.endswith(".py"):
                continue
            if name.startswith("__") or "/__" in name:
                continue
            basename = os.path.basename(name)
            if basename.startswith("_"):
                continue
            content = zf.read(name)
            if len(content) > 100_000:
                raise UserError(f"File {basename} in ZIP exceeds 100KB limit.")
            py_files.append((basename, content))
        if not py_files:
            raise UserError("ZIP file contains no .py files (excluding __init__.py).")
        return py_files

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

        if self._is_zip_upload():
            py_files = self._extract_zip_files()
            first_path = ""
            for fname, content in py_files:
                file_path = os.path.join(staging_dir, fname)
                with open(file_path, "wb") as f:
                    f.write(content)
                if not first_path:
                    first_path = file_path
            return first_path
        else:
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

    @staticmethod
    def _require_admin(env, action: str):
        if not env.user.has_group("aurora.group_aurora_admin"):
            raise AccessError(
                f"Only Aurora Administrators may {action}. "
                "Harness uploads execute arbitrary Python on the Odoo worker, "
                "so this capability is restricted to admins."
            )

    @api.model_create_multi
    def create(self, vals_list):
        self._require_admin(self.env, "create harness staging records")
        records = super().create(vals_list)
        for rec in records:
            if rec.harness_file:
                if rec._is_zip_upload():
                    for fname, content in rec._extract_zip_files():
                        rec._validate_harness_content(content)
                else:
                    raw = base64.b64decode(rec.harness_file)
                    rec._validate_harness_content(raw)
                path = rec._write_staging_file()
                rec.sudo().write({"staging_path": path})
        return records

    def write(self, vals):
        if "harness_file" in vals and vals["harness_file"]:
            self._require_admin(self.env, "upload harness files")
        res = super().write(vals)
        if "harness_file" in vals and vals["harness_file"]:
            for rec in self:
                if rec._is_zip_upload():
                    for fname, content in rec._extract_zip_files():
                        rec._validate_harness_content(content)
                else:
                    raw = base64.b64decode(rec.harness_file)
                    rec._validate_harness_content(raw)
                path = rec._write_staging_file()
                rec.sudo().write({"staging_path": path})
        return res

    def action_test_harness(self):
        self.ensure_one()
        self._require_admin(self.env, "execute harness tests")
        if self.stage == "testing":
            raise UserError("Test already in progress.")
        if not self.harness_file:
            raise UserError(
                "Registry File is required. Upload a .py file or a .zip containing multiple .py files."
            )
        if not self.dataset_file:
            raise UserError(
                "Dataset file is required. Select a Source Pipeline or set it manually."
            )
        from . import dataset_resolver
        if dataset_resolver.is_remote(self.dataset_file):
            self.dataset_file = dataset_resolver.resolve_to_local(
                self.env, self.dataset_file
            )
        if not os.path.isfile(self.dataset_file):
            raise UserError(f"Dataset file not found: {self.dataset_file}")

        if not harness_staging_executor.is_test_slot_available():
            raise UserError(
                "Another staging test is already running on this Odoo server. "
                "Only one staging test runs at a time to avoid harness registry "
                "races. Wait for the current test to finish and retry."
            )

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
        self._require_admin(self.env, "run full harness evaluations")
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
        from . import dataset_resolver
        local_path = dataset_resolver.resolve_to_local(self.env, self.dataset_file)
        abs_path = os.path.abspath(local_path)
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

    def action_download_readme_harness(self):
        self.ensure_one()
        aurora_root = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..")
        )
        candidate = os.path.join(aurora_root, "README-HARNESS.md")
        if not os.path.isfile(candidate):
            raise UserError(
                f"README-HARNESS.md not found at {candidate!r}. "
                "Ensure the file exists in the aurora addon directory."
            )
        return self._download_local_file(candidate, "README-HARNESS.md")

    def action_mark_deployed(self):
        self.ensure_one()
        self._require_admin(self.env, "push harness registry to GitHub")

        if self.test_result != "success" or self.stage not in ("tested", "done", "evaluating"):
            raise UserError(
                "Push is only allowed after a successful harness test. "
                "Run 'Test Harness' first and wait for test_result=success."
            )
        if not self.staging_path or not os.path.isfile(self.staging_path):
            raise UserError(
                "Staging file missing on disk. Re-upload the harness file and retry."
            )

        from . import registry_git_sync
        commit_msg = (
            f"Deploy registry for {self.org}/{self.repo} (lang={self.language}) "
            f"via Aurora Harness Staging by {self.user_id.login or self.user_id.name}"
        )

        push_infos: list[dict] = []

        if self._is_zip_upload():
            py_files = self._extract_zip_files()
            for fname, content in py_files:
                text = content.decode("utf-8")
                push_info = registry_git_sync.push_registry_to_github(
                    env=self.env,
                    lang=self.language,
                    org=self.org,
                    filename=fname,
                    content=text,
                    commit_msg=f"{commit_msg} ({fname})",
                )
                if push_info is None:
                    raise UserError(
                        "GitHub push skipped: aurora.github_registry_write_token is not configured. "
                        "Configure the token in Settings → Aurora Pipeline → GitHub Registry Write Token, "
                        "then retry. Harness NOT deployed."
                    )
                push_infos.append(push_info)
        else:
            with open(self.staging_path, "r", encoding="utf-8") as fh:
                content = fh.read()

            repo_safe = self.repo.replace("-", "_").lower()
            filename = f"{repo_safe}.py"

            push_info = registry_git_sync.push_registry_to_github(
                env=self.env,
                lang=self.language,
                org=self.org,
                filename=filename,
                content=content,
                commit_msg=commit_msg,
            )
            if push_info is None:
                raise UserError(
                    "GitHub push skipped: aurora.github_registry_write_token is not configured. "
                    "Configure the token in Settings → Aurora Pipeline → GitHub Registry Write Token, "
                    "then retry. Harness NOT deployed."
                )
            push_infos.append(push_info)

        extra_stems = None
        if self._is_zip_upload():
            extra_stems = [fname[:-3] for fname, _ in self._extract_zip_files()]

        try:
            init_pushes = registry_git_sync.ensure_init_files_on_github(
                env=self.env,
                lang=self.language,
                org=self.org,
                repo=self.repo,
                extra_stems=extra_stems,
            )
        except UserError:
            raise
        except Exception as exc:
            _logger.exception("Unexpected error pushing __init__.py files to GitHub")
            raise UserError(
                f"Harness .py pushed successfully, but __init__.py updates failed: {exc}. "
                f"K8s pods will still work (they regenerate init files locally on sync), "
                f"but the GitHub repo tree is incomplete. Investigate and retry."
            ) from exc

        self.write({
            "stage": "deployed",
            "deployed_at": fields.Datetime.now(),
        })

        staging_dir = os.path.dirname(self.staging_path)
        if self._is_zip_upload():
            for fname, _ in self._extract_zip_files():
                fpath = os.path.join(staging_dir, fname)
                if os.path.exists(fpath):
                    try:
                        os.remove(fpath)
                    except OSError:
                        pass
        else:
            if self.staging_path and os.path.exists(self.staging_path):
                try:
                    os.remove(self.staging_path)
                except OSError:
                    pass

        init_summary = ""
        if init_pushes:
            init_summary = (
                f" Also updated {len(init_pushes)} __init__.py file(s): "
                + ", ".join(f"<code>{p.get('path')}</code>" for p in init_pushes)
                + "."
            )

        files_pushed = ", ".join(
            f"<code>{p.get('path', '?')}</code>" for p in push_infos
        )
        first_push = push_infos[0] if push_infos else {}
        body = (
            f"Harness deployed to GitHub ({len(push_infos)} file(s)): "
            f"{files_pushed} "
            f"@ <code>{first_push.get('branch', '?')}</code> "
            f"(commit {(first_push.get('commit_sha') or '')[:8]})."
            f"{init_summary} "
            f"K8s worker pods will sync this registry on the next evaluation."
        )
        self.message_post(
            body=body,
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
