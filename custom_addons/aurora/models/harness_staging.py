import ast
import base64
import io
import json
import logging
import os
import zipfile

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

_logger = logging.getLogger(__name__)


def _s3_key_from_url(url: str, bucket: str) -> str:
    """Best-effort extraction of an S3 object key from a stored URL.

    Handles two URL shapes produced by ``s3_storage.build_url``:
      - ``https://<bucket>.s3.<region>.amazonaws.com/<key>`` (AWS virtual-hosted)
      - ``<endpoint>/<bucket>/<key>`` (custom endpoint, e.g. local MinIO)

    Returns "" when the URL doesn't match either shape or doesn't reference
    the configured bucket.
    """
    if not url or not bucket:
        return ""
    marker = f"/{bucket}/"
    idx = url.find(marker)
    if idx >= 0:
        return url[idx + len(marker):].split("?", 1)[0]
    aws_marker = f"://{bucket}.s3."
    if aws_marker in url:
        tail = url.split("/", 3)
        if len(tail) >= 4:
            return tail[3].split("?", 1)[0]
    return ""

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
    harness_original_zip = fields.Binary(
        string="Original Zip Upload",
        attachment=False,
        readonly=True,
        help="Preserved copy of the original .zip when one was uploaded. "
             "The user-facing harness_file is then auto-merged into a single "
             "concatenated .py so the K8s worker (which reads a single file "
             "from this row) handles both single-.py and zip uploads uniformly.",
    )
    harness_original_zip_filename = fields.Char(
        string="Original Zip Filename",
        readonly=True,
    )
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

    # Curated picks per language, stored as paths relative to
    # _HARNESS_REPOS_ABSROOT (the in-tree harness repos root, same one used by
    # registry_wizard._AURORA_HARNESS_REPOS_ROOT). For languages not listed
    # here, _find_reference_harness() scans the root for a real existing file.
    _AI_REFERENCE_MAP = {
        "python": "python/psf/requests.py",
        "golang": "golang/istio/istio.py",
    }

    _HARNESS_REPOS_ABSROOT = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "tools", "harness", "repos")
    )

    @api.model
    def _find_reference_harness(self, language, org=None, repo=None):
        """Return an absolute path to a real existing reference harness, or False.

        Searches only the in-tree harness repos root (_HARNESS_REPOS_ABSROOT),
        in this preference order:
          1. Exact curated pick from ``_AI_REFERENCE_MAP`` if its file exists.
          2. ``<lang>/<org>/<repo>.py`` if a matching file already exists.
          3. First ``.py`` file in ``<lang>/<org>/`` (same-org heuristic).
          4. First ``.py`` file anywhere under ``<lang>/`` (alphabetical).
          5. ``False`` if no files at all for this language.
        """
        if not language:
            return False

        root = self._HARNESS_REPOS_ABSROOT

        curated_rel = self._AI_REFERENCE_MAP.get(language)
        if curated_rel:
            curated_abs = os.path.join(root, curated_rel)
            if os.path.isfile(curated_abs):
                return curated_abs

        lang_dir = os.path.join(root, language)
        if not os.path.isdir(lang_dir):
            return False

        if org and repo:
            candidate = os.path.join(lang_dir, org, f"{repo}.py")
            if os.path.isfile(candidate):
                return candidate

        if org:
            org_dir = os.path.join(lang_dir, org)
            if os.path.isdir(org_dir):
                for name in sorted(os.listdir(org_dir)):
                    if name.endswith(".py") and not name.startswith("_"):
                        return os.path.join(org_dir, name)

        for dirpath, _dirnames, filenames in sorted(os.walk(lang_dir)):
            for fname in sorted(filenames):
                if fname.endswith(".py") and not fname.startswith("_"):
                    return os.path.join(dirpath, fname)

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
            ) or False
            dataset_display = rec.dataset_file or "(select Source Pipeline to auto-fill)"
            class_prefix = rec.repo.title().replace('-', '').replace('_', '')
            reference_display = rec.ai_reference_harness or (
                f"(none available — add a sample .py at "
                f"{self._HARNESS_REPOS_ABSROOT}/{rec.language}/<org>/<repo>.py)"
            )
            rec.ai_prompt_text = (
                f"You are writing a new harness (registry file) for the Aurora evaluation pipeline.\n"
                f"Target repo: {rec.org}/{rec.repo}  (language: {rec.language})\n\n"
                f"YOU HAVE TWO INPUTS. USE BOTH.\n\n"
                f"INPUT 1 - Reference harness (a WORKING harness in the same language):\n"
                f"  Path: {reference_display}\n"
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
            # A merged .py produced from a zip upload may exceed the single-
            # file budget; honor the zip limit in that case.
            zip_budget = (
                fname.endswith(".zip")
                or bool(rec.harness_original_zip)
            )
            if zip_budget:
                if len(raw) > 2_000_000:
                    raise ValidationError("ZIP file (or merged .py from zip) exceeds 2MB limit.")
            else:
                if len(raw) > 100_000:
                    raise ValidationError("Harness file exceeds 100KB limit.")

    def _is_zip_upload(self) -> bool:
        """True when the *original* upload was a zip, even after we collapse
        it into a merged .py for K8s-worker consumption."""
        name = self.harness_original_zip_filename or self.harness_filename or ""
        return name.endswith(".zip")

    def _extract_zip_files(self) -> list[tuple[str, bytes]]:
        blob = self.harness_original_zip or self.harness_file
        raw = base64.b64decode(blob)
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

    def _merge_zip_to_single_py(self) -> tuple[bytes, str]:
        """Concat all .py files in the zip into one .py blob.

        Range files (e.g. ``requests_4137_to_3036.py``) each register a
        distinct ``@Instance.register(org, repo_<range>)`` key, so executing
        their concatenation in a single module is equivalent to importing
        them as separate modules — every decorator still fires with its own
        key.  Duplicate top-level class names would silently shadow each
        other, so we AST-check for collisions and raise UserError if any
        are found.
        """
        py_files = self._extract_zip_files()
        seen_classes: dict[str, str] = {}
        parts: list[str] = [
            f"# Merged harness registry for {self.org}/{self.repo}\n"
            f"# Concatenated from {len(py_files)} file(s) in the uploaded zip.\n"
        ]
        for fname, content in py_files:
            source = content.decode("utf-8", errors="replace")
            try:
                tree = ast.parse(source)
            except SyntaxError as exc:
                raise UserError(f"Python syntax error in {fname}: {exc}") from exc
            for node in tree.body:
                if isinstance(node, ast.ClassDef):
                    prev = seen_classes.get(node.name)
                    if prev is not None:
                        raise UserError(
                            f"Class name '{node.name}' is defined in both "
                            f"{prev} and {fname} inside the zip. Each range "
                            f"file must use distinct class names so they can "
                            f"coexist in the merged registry."
                        )
                    seen_classes[node.name] = fname
            parts.append(f"# === merged from {fname} ===\n{source}")
        merged = "\n\n".join(parts).encode("utf-8")
        repo_safe = (self.repo or "harness").replace("-", "_").lower()
        return merged, f"{repo_safe}_merged.py"

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
                    rec._collapse_zip_into_single_py()
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
                    rec._collapse_zip_into_single_py()
                else:
                    raw = base64.b64decode(rec.harness_file)
                    rec._validate_harness_content(raw)
                path = rec._write_staging_file()
                rec.sudo().write({"staging_path": path})
        return res

    def _collapse_zip_into_single_py(self) -> None:
        """Preserve the original zip in a side field, replace harness_file
        with the AST-checked merged .py blob so the K8s worker (which only
        reads a single .py from this row) can handle it transparently.

        Idempotent: if ``harness_original_zip`` is already populated the row
        has been collapsed before — bail out so the re-entry from our own
        ``sudo().write()`` below doesn't clobber the preserved original."""
        self.ensure_one()
        if self.harness_original_zip:
            return
        merged_bytes, merged_name = self._merge_zip_to_single_py()
        self.sudo().write({
            "harness_original_zip": self.harness_file,
            "harness_original_zip_filename": self.harness_filename,
            "harness_file": base64.b64encode(merged_bytes),
            "harness_filename": merged_name,
        })

    def action_test_harness(self):
        """Dispatch a K8s test-evaluation for this staging row.

        Reuses aurora.evaluation's production K8s Job dispatcher
        (``_create_evaluation_job``) — no separate worker entrypoint, no
        docker.sock in the Odoo pod. The eval Job, on startup, queries this
        table for any active staging row in stage 'tested'/'evaluating' and
        hot-loads its harness (see worker/run_evaluation.py:613-650). Setting
        stage='evaluating' below is the trigger for that auto-load.

        Status is mirrored back to this row by
        ``aurora.evaluation._cron_promote_finished_test_eval`` (5-min cron)
        once the eval Job reaches a terminal stage. Success rule: 50%+
        instances resolved.
        """
        self.ensure_one()
        self._require_admin(self.env, "execute harness tests")
        if self.stage in ("testing", "evaluating"):
            raise UserError("Test already in progress for this staging row.")
        if not self.harness_file:
            raise UserError(
                "Registry File is required. Upload a .py file or a .zip "
                "containing range .py files."
            )
        if not self.dataset_file:
            raise UserError(
                "Dataset file is required. Select a Source Pipeline or set it manually."
            )
        from . import dataset_resolver
        local_path = self.dataset_file
        if dataset_resolver.is_remote(local_path):
            local_path = dataset_resolver.resolve_to_local(self.env, local_path)
        if not os.path.isfile(local_path):
            raise UserError(f"Dataset file not found: {local_path}")

        matching_prs: list[str] = []
        with open(local_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("org") == self.org and entry.get("repo") == self.repo:
                    pr_id = f"{self.org}/{self.repo}:pr-{entry.get('number', '')}"
                    matching_prs.append(pr_id)
        if not matching_prs:
            raise UserError(
                f"Dataset has no PRs for {self.org}/{self.repo}. "
                f"Pick a dataset that contains this repository."
            )
        test_prs = matching_prs[:4]

        # Materialize the harness on disk too, so action_mark_deployed (which
        # reads from self.staging_path) keeps working unchanged.
        self._ensure_staging_file()

        ICP = self.env["ir.config_parameter"].sudo()
        base_dir = ICP.get_param("aurora.output_dir", "/tmp/aurora_output")
        output_dir = os.path.join(
            base_dir, "harness_test", f"{self.org}__{self.repo}", str(self.id),
        )

        evaluation = self.env["aurora.evaluation"].create({
            "pipeline_id": self.pipeline_id.id if self.pipeline_id else False,
            "dataset_file": self.dataset_file,
            "output_dir": output_dir,
            "instance_limit": len(test_prs),
            "specific_prs": ",".join(test_prs),
            "user_id": self.user_id.id,
            "staging_test_id": self.id,
            # Keep test-evals on the fast single-arch SDK build path. The
            # multi-arch constraint exempts staging_test_id rows, and we
            # don't want the buildx multi-arch QEMU overhead for a 4-PR
            # dry-run. Also bypass the post-report wait window (0 = no wait).
            "docker_platform": False,
            "tar_decision_window_minutes": 0,
        })

        # stage='evaluating' triggers the worker's auto-load of this row's
        # harness_file (worker/run_evaluation.py:634).
        self.write({
            "stage": "evaluating",
            "test_result": "idle",
            "test_log": (
                f"Dispatched K8s test-eval {evaluation.name} with "
                f"{len(test_prs)} PR(s): {', '.join(test_prs)}.\n"
                f"Result will be mirrored back here within ~5 minutes of "
                f"the eval Job finishing (cron interval)."
            ),
        })

        evaluation.action_run_evaluation()

        return {
            "type": "ir.actions.act_window",
            "res_model": "aurora.evaluation",
            "res_id": evaluation.id,
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
        candidate = self.ai_reference_harness
        if not candidate or not os.path.isfile(candidate):
            raise UserError(
                f"No reference harness available for language {self.language!r}. "
                f"Drop a sample harness file at "
                f"{self._HARNESS_REPOS_ABSROOT}/{self.language or '<lang>'}/<org>/<repo>.py "
                "and reload this record."
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

        try:
            self._cleanup_test_eval_artifacts()
        except Exception:
            _logger.warning(
                "Test-eval cleanup failed for staging %s (deploy already succeeded)",
                self.id, exc_info=True,
            )

    def _cleanup_test_eval_artifacts(self) -> None:
        """Delete any test-eval rows (and best-effort S3 artifacts) created
        by ``action_test_harness`` for this staging row. Called after a
        successful deploy — once the harness is in production, the dry-run
        artifacts are no longer useful."""
        self.ensure_one()
        test_evals = self.env["aurora.evaluation"].sudo().search([
            ("staging_test_id", "=", self.id),
        ])
        if not test_evals:
            return
        try:
            from . import artifact_collector, s3_storage
            s3_config = artifact_collector.load_s3_config()
            s3_ok = s3_storage.is_configured(s3_config)
        except Exception:
            s3_config, s3_ok = None, False

        deleted_keys: list[str] = []
        if s3_ok:
            client = s3_storage._get_client(s3_config)
            bucket = s3_config.get("bucket")
            url_attrs = ("final_report_file", "dataset_jsonl_url", "base_dockerfile_s3_uri")
            for eval_rec in test_evals:
                for attr in url_attrs:
                    url = getattr(eval_rec, attr, None) or ""
                    key = _s3_key_from_url(url, bucket)
                    if not key:
                        continue
                    try:
                        client.delete_object(Bucket=bucket, Key=key)
                        deleted_keys.append(key)
                    except Exception:
                        _logger.debug(
                            "S3 delete failed for test-eval %s key=%s",
                            eval_rec.id, key, exc_info=True,
                        )

        eval_ids = test_evals.ids
        test_evals.unlink()
        _logger.info(
            "Cleaned up %d test-eval row(s) for staging %s (S3 keys deleted: %d)",
            len(eval_ids), self.id, len(deleted_keys),
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
