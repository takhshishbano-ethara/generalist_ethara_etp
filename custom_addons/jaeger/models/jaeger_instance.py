import json
import logging
import re

from odoo import SUPERUSER_ID, api, fields, models

_logger = logging.getLogger(__name__)


def _execute_docker_run_pure(inst_name, docker_image, mode, patches, timeout, memory_limit="4g", language="python", network_enabled=None):
    """Execute a single Docker run. Pure function — no ORM dependency.

    Args:
        inst_name: instance name (for container naming)
        docker_image: full Docker image tag
        mode: 'run', 'test_patch', or 'fix_patch'
        patches: dict {patch_name: patch_content} or None
        timeout: seconds before kill
        memory_limit: Docker memory limit (e.g. "4g", "8g")
        language: repo language — used as fallback for network decision
        network_enabled: explicit network toggle from test config (None = use language default)

    Returns:
        str: combined stdout + stderr
    """
    return _docker_run_impl(inst_name, docker_image, mode, patches, timeout,
                            memory_limit, language, network_enabled)


def _docker_run_impl(inst_name, docker_image, mode, patches, timeout,
                     memory_limit="4g", language="python", network_enabled=None):
    """Shared Docker execution logic used by both ORM and standalone paths."""
    import subprocess
    import tempfile
    from uuid import uuid4

    tag = uuid4().hex[:8]
    container_name = (
        f"jaeger-{inst_name}-{mode}-{tag}"
        .replace("/", "-").replace("__", "-").lower()
    )

    subprocess.run(
        ["docker", "rm", "-f", container_name],
        capture_output=True, timeout=30,
    )

    cmd = [
        "docker", "run",
        "--name", container_name,
        "--memory", memory_limit,
        "--memory-swap", memory_limit,
    ]

    # Network: explicit config wins, otherwise Python-only gets --network none
    if network_enabled is not None:
        if not network_enabled:
            cmd.extend(["--network", "none"])
    elif (language or "").lower() == "python":
        cmd.extend(["--network", "none"])

    reset_prefix = "git checkout -- . && git clean -fd && "

    if patches:
        with tempfile.TemporaryDirectory() as tmpdir:
            from pathlib import Path

            for patch_name, patch_content in patches.items():
                if patch_content:
                    (Path(tmpdir) / f"{patch_name}.diff").write_text(
                        patch_content, encoding="utf-8",
                    )

            cmd.extend(["-v", f"{tmpdir}:/patches:ro"])
            cmd.append(docker_image)

            if mode == "test_patch":
                cmd.extend(["bash", "-c",
                    f"cd /testbed && {reset_prefix}"
                    "git apply --whitespace=nowarn /patches/test_patch.diff && "
                    "bash /jaeger/fix-run.sh"])
            elif mode == "fix_patch":
                cmd.extend(["bash", "-c",
                    f"cd /testbed && {reset_prefix}"
                    "git apply --whitespace=nowarn /patches/fix_patch.diff && "
                    "git apply --whitespace=nowarn /patches/test_patch.diff && "
                    "bash /jaeger/fix-run.sh"])

            try:
                result = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=timeout,
                )
            except subprocess.TimeoutExpired:
                subprocess.run(["docker", "rm", "-f", container_name],
                               capture_output=True, timeout=30)
                raise
    else:
        cmd.append(docker_image)
        cmd.extend(["bash", "-c", "cd /testbed && bash /jaeger/fix-run.sh"])
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            subprocess.run(["docker", "rm", "-f", container_name],
                           capture_output=True, timeout=30)
            raise

    return result.stdout + "\n" + result.stderr


def _run_instance_tests_standalone(db_name, instance_id, agent_timeout):
    """Execute 3-run test validation for a single instance.

    Designed for ThreadPoolExecutor: opens its own DB cursors,
    Docker execution holds no cursor open.

    Returns:
        dict with keys: instance_id, success, is_valid, error, summary
    """
    from odoo.orm.registry import Registry

    result = {"instance_id": instance_id, "success": False, "is_valid": False, "error": None, "summary": ""}

    # Phase A: read instance data (short cursor)
    try:
        with Registry(db_name).cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            inst = env["jaeger.instance"].browse(instance_id)
            if not inst.exists():
                result["error"] = "Instance not found"
                return result
            if inst.docker_build_status != "built" or not inst.docker_image_name:
                result["error"] = "No built image"
                return result
            if not inst.fix_patch or not inst.fix_patch.strip():
                inst.write({"is_valid": False, "validation_error": "Empty fix_patch"})
                result["error"] = "Empty fix_patch"
                return result
            if not inst.test_patch or not inst.test_patch.strip():
                inst.write({"is_valid": False, "validation_error": "Empty test_patch"})
                result["error"] = "Empty test_patch"
                return result

            inst_name = inst.name
            docker_image = inst.docker_image_name
            fix_patch = inst.fix_patch
            test_patch = inst.test_patch
            lang = (inst.repository_id.language or "").lower()
            config = inst.repository_id._get_effective_config()
            memory_limit = config.get("memory_limit", "8g" if lang in ("rust", "cpp", "c", "java") else "4g")
            network_enabled = config.get("network")
    except Exception as e:
        result["error"] = f"Read phase failed: {e}"
        return result

    # Phase B: execute Docker runs sequentially (no cursor held)
    try:
        run_log = _execute_docker_run_pure(
            inst_name, docker_image, "run", None,
            agent_timeout, memory_limit, lang, network_enabled,
        )
        test_patch_log = _execute_docker_run_pure(
            inst_name, docker_image, "test_patch",
            {"test_patch": test_patch}, agent_timeout, memory_limit, lang, network_enabled,
        )

        fix_patch_log = _execute_docker_run_pure(
            inst_name, docker_image, "fix_patch",
            {"fix_patch": fix_patch, "test_patch": test_patch},
            agent_timeout, memory_limit, lang, network_enabled,
        )
    except Exception as e:
        _logger.error("Docker execution failed for %s: %s", inst_name, e)
        try:
            with Registry(db_name).cursor() as cr:
                env = api.Environment(cr, SUPERUSER_ID, {})
                inst = env["jaeger.instance"].browse(instance_id)
                inst.write({"is_valid": False, "validation_error": f"Test execution error: {str(e)[:500]}"})
        except Exception:
            pass
        result["error"] = str(e)
        return result

    # Phase C: write results + generate report (short cursor)
    try:
        with Registry(db_name).cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            inst = env["jaeger.instance"].browse(instance_id)

            run_result = inst._parse_test_log(run_log)
            test_result = inst._parse_test_log(test_patch_log)
            fix_result = inst._parse_test_log(fix_patch_log)

            inst.write({
                "run_log": run_log[-50000:],
                "run_result_json": json.dumps(run_result),
                "run_passed_count": run_result.get("passed_count", 0),
                "run_failed_count": run_result.get("failed_count", 0),
                "run_skipped_count": run_result.get("skipped_count", 0),
                "test_patch_run_log": test_patch_log[-50000:],
                "test_patch_result_json": json.dumps(test_result),
                "test_patch_passed_count": test_result.get("passed_count", 0),
                "test_patch_failed_count": test_result.get("failed_count", 0),
                "test_patch_skipped_count": test_result.get("skipped_count", 0),
                "fix_patch_run_log": fix_patch_log[-50000:],
                "fix_patch_result_json": json.dumps(fix_result),
                "fix_patch_passed_count": fix_result.get("passed_count", 0),
                "fix_patch_failed_count": fix_result.get("failed_count", 0),
                "fix_patch_skipped_count": fix_result.get("skipped_count", 0),
            })

            inst._generate_test_report(run_result, test_result, fix_result)

            result["success"] = True
            result["is_valid"] = inst.is_valid
            f2p = inst.f2p_count
            n2p = inst.n2p_count
            p2p = inst.p2p_count
            result["summary"] = f"{'valid' if inst.is_valid else 'invalid'} ({f2p} f2p, {n2p} n2p, {p2p} p2p)" if inst.is_valid else (inst.validation_error or "invalid")
    except Exception as e:
        _logger.error("Write phase failed for %s: %s", instance_id, e)
        result["error"] = f"Write phase: {e}"

    return result

DOCKER_BUILD_STATUS = [
    ("pending", "Pending"),
    ("building", "Building"),
    ("built", "Built"),
    ("failed", "Failed"),
]

DIFFICULTY_SELECTION = [
    ("easy", "Easy"),
    ("medium", "Medium"),
    ("hard", "Hard"),
    ("expert", "Expert"),
]

DELIVERY_STATUS = [
    ("pending", "Pending"),
    ("converted", "Converted"),
    ("delivered", "Delivered"),
]


class JaegerInstance(models.Model):
    _name = "jaeger.instance"
    _description = "Jaeger Instance (PR/Task)"
    _order = "create_date desc"

    # ── Identity ─────────────────────────────────────────────────────────
    name = fields.Char(string="Instance ID", required=True, index=True)
    repository_id = fields.Many2one(
        "jaeger.repository",
        string="Repository",
        required=True,
        ondelete="cascade",
        index=True,
    )
    org = fields.Char(string="Organization")
    repo = fields.Char(string="Repository Name")
    pr_number = fields.Integer(string="PR Number")
    pr_url = fields.Char(string="PR URL", compute="_compute_pr_url")
    instance_id = fields.Char(
        string="Instance ID (computed)", compute="_compute_instance_id",
    )

    # ── PR Metadata ──────────────────────────────────────────────────────
    state = fields.Char(string="PR State")
    title = fields.Char(string="PR Title")
    body = fields.Text(string="PR Body")

    # ── Base Branch ──────────────────────────────────────────────────────
    base_label = fields.Char(string="Base Label")
    base_ref = fields.Char(string="Base Ref")
    base_sha = fields.Char(string="Base SHA")

    # ── Resolved Issues ──────────────────────────────────────────────────
    resolved_issue_ids = fields.One2many(
        "jaeger.resolved.issue", "instance_id", string="Resolved Issues",
    )
    resolved_issues_json = fields.Text(string="Resolved Issues JSON")

    # ── Patches ──────────────────────────────────────────────────────────
    fix_patch = fields.Text(string="Fix Patch")
    test_patch = fields.Text(string="Test Patch")

    # ── Classification ───────────────────────────────────────────────────
    tag = fields.Char(string="Version Tag")
    number_interval = fields.Char(string="Number Interval (LHT)")
    language = fields.Selection(
        [
            ("python", "Python"),
            ("java", "Java"),
            ("typescript", "TypeScript"),
            ("javascript", "JavaScript"),
            ("go", "Go"),
            ("rust", "Rust"),
            ("c", "C"),
            ("cpp", "C++"),
        ],
        string="Language",
    )
    hints = fields.Text(string="Hints")

    # ── Docker ───────────────────────────────────────────────────────────
    docker_image_name = fields.Char(string="Docker Image Name")
    docker_build_status = fields.Selection(
        DOCKER_BUILD_STATUS,
        string="Docker Build Status",
        default="pending",
        index=True,
    )
    docker_build_log = fields.Text(string="Docker Build Log")
    dockerfile_content = fields.Text(string="Dockerfile Content")

    # ── Test Execution Results (Phase 2) ─────────────────────────────────
    run_result_json = fields.Text(string="Run Result JSON")
    test_patch_result_json = fields.Text(string="Test Patch Result JSON")
    fix_patch_result_json = fields.Text(string="Fix Patch Result JSON")

    run_passed_count = fields.Integer(string="Run: Passed")
    run_failed_count = fields.Integer(string="Run: Failed")
    run_skipped_count = fields.Integer(string="Run: Skipped")

    test_patch_passed_count = fields.Integer(string="Test Patch: Passed")
    test_patch_failed_count = fields.Integer(string="Test Patch: Failed")
    test_patch_skipped_count = fields.Integer(string="Test Patch: Skipped")

    fix_patch_passed_count = fields.Integer(string="Fix Patch: Passed")
    fix_patch_failed_count = fields.Integer(string="Fix Patch: Failed")
    fix_patch_skipped_count = fields.Integer(string="Fix Patch: Skipped")

    # ── Test Classification ──────────────────────────────────────────────
    fixed_tests_json = fields.Text(string="Fixed Tests JSON")
    p2p_tests_json = fields.Text(string="P2P Tests JSON")
    f2p_tests_json = fields.Text(string="F2P Tests JSON")
    s2p_tests_json = fields.Text(string="S2P Tests JSON")
    n2p_tests_json = fields.Text(string="N2P Tests JSON")
    f2p_count = fields.Integer(string="F2P Count", compute="_compute_test_counts")
    p2p_count = fields.Integer(string="P2P Count", compute="_compute_test_counts")
    n2p_count = fields.Integer(string="N2P Count", compute="_compute_test_counts")

    # ── Validation ───────────────────────────────────────────────────────
    is_valid = fields.Boolean(string="Is Valid")
    validation_error = fields.Char(string="Validation Error")
    has_regressions = fields.Boolean(string="Has Regressions")
    has_fix_signal = fields.Boolean(string="Has Fix Signal")

    # ── Execution Logs ───────────────────────────────────────────────────
    run_log = fields.Text(string="Run Log")
    test_patch_run_log = fields.Text(string="Test Patch Run Log")
    fix_patch_run_log = fields.Text(string="Fix Patch Run Log")
    report_json = fields.Text(string="Report JSON")

    # ── Difficulty ───────────────────────────────────────────────────────
    difficulty = fields.Selection(DIFFICULTY_SELECTION, string="Difficulty")
    estimated_time = fields.Char(string="Estimated Time")

    # ── Meta Delivery Schema Fields ──────────────────────────────────────
    problem_statement = fields.Text(string="Problem Statement")
    repo_path_or_url = fields.Char(string="Repo Path or URL")
    version = fields.Char(string="Version")
    fail_to_pass_json = fields.Text(string="FAIL_TO_PASS JSON")
    pass_to_pass_json = fields.Text(string="PASS_TO_PASS JSON")
    docker_image_url = fields.Char(string="Docker Image URL")
    docker_file_content = fields.Text(string="Docker File Content")
    container_mem = fields.Char(string="Container Memory", default="4g")
    container_memswap = fields.Char(string="Container Memswap", default="4g")
    container_network_needed = fields.Boolean(string="Network Needed")
    parsing_script = fields.Text(string="Parsing Script")
    run_script = fields.Text(string="Run Script")
    selected_test_files_json = fields.Text(string="Selected Test Files JSON")
    entrypoint_script = fields.Text(string="Entrypoint Script")
    before_repo_set_cmd = fields.Text(string="Before Repo Set Cmd")
    problem_statement_variants_json = fields.Text(
        string="Problem Statement Variants JSON",
    )
    task_category = fields.Selection(
        [
            ("hard_swe", "Hard SWE"),
            ("long_horizon", "Long Horizon"),
            ("real_coder", "Real Coder"),
        ],
        string="Task Category",
    )
    repo_category = fields.Char(string="Repo Category")
    artifacts_json = fields.Text(string="Artifacts JSON")

    # ── Delivery ─────────────────────────────────────────────────────────
    delivery_status = fields.Selection(
        DELIVERY_STATUS, string="Delivery Status", default="pending",
    )
    meta_schema_json = fields.Text(string="Meta Schema JSON")

    # ── Trajectory ───────────────────────────────────────────────────────
    conversation_log = fields.Text(string="Conversation Log")

    # ── Relations ─────────────────────────────────────────────────────────
    trajectory_run_ids = fields.One2many(
        "jaeger.trajectory.run", "instance_id", string="Trajectory Runs",
    )

    # ── Computed Fields ───────────────────────────────────────────────────

    def _compute_pr_url(self):
        for rec in self:
            if rec.org and rec.repo and rec.pr_number:
                rec.pr_url = (
                    f"https://github.com/{rec.org}/{rec.repo}/pull/{rec.pr_number}"
                )
            else:
                rec.pr_url = ""

    def _compute_instance_id(self):
        for rec in self:
            if rec.org and rec.repo and rec.pr_number:
                rec.instance_id = f"{rec.org}/{rec.repo}:pr-{rec.pr_number}"
            else:
                rec.instance_id = ""

    @api.depends("f2p_tests_json", "p2p_tests_json", "n2p_tests_json")
    def _compute_test_counts(self):
        for rec in self:
            try:
                rec.f2p_count = len(json.loads(rec.f2p_tests_json or "{}"))
            except (json.JSONDecodeError, TypeError):
                rec.f2p_count = 0
            try:
                rec.p2p_count = len(json.loads(rec.p2p_tests_json or "{}"))
            except (json.JSONDecodeError, TypeError):
                rec.p2p_count = 0
            try:
                rec.n2p_count = len(json.loads(rec.n2p_tests_json or "{}"))
            except (json.JSONDecodeError, TypeError):
                rec.n2p_count = 0

    # ── Test Execution ────────────────────────────────────────────────────

    def run_test_execution(self):
        """Execute 3-run test validation. Called by consumer.py via XML-RPC.

        Three runs against the Docker image:
          Run 1 (baseline):       No patches applied -> run.log
          Run 2 (test-patch):     test_patch applied -> test-patch-run.log
          Run 3 (fix+test patch): fix_patch + test_patch applied -> fix-patch-run.log

        After execution, logs are parsed and a report is generated.
        """
        self.ensure_one()
        if self.docker_build_status != "built" or not self.docker_image_name:
            _logger.warning("Instance %s has no built image, skipping test execution", self.name)
            return

        if not self.fix_patch or not self.fix_patch.strip():
            _logger.warning("Instance %s has empty fix_patch, skipping", self.name)
            self.write({"is_valid": False, "validation_error": "Empty fix_patch"})
            return
        if not self.test_patch or not self.test_patch.strip():
            _logger.warning("Instance %s has empty test_patch, skipping", self.name)
            self.write({"is_valid": False, "validation_error": "Empty test_patch"})
            return

        ICP = self.env["ir.config_parameter"].sudo()
        agent_timeout = int(ICP.get_param("jaeger.agent_timeout", "1800"))

        _logger.info("Starting 3-run test execution for %s", self.name)

        try:
            # Run 1: Baseline (no patches)
            _logger.info("[%s] Run 1/3: baseline (no patches)", self.name)
            run_log = self._execute_docker_run(
                mode="run",
                patches=None,
                timeout=agent_timeout,
            )
            self.write({"run_log": run_log[-50000:]})
            run_result = self._parse_test_log(run_log)
            self.write({
                "run_result_json": json.dumps(run_result),
                "run_passed_count": run_result.get("passed_count", 0),
                "run_failed_count": run_result.get("failed_count", 0),
                "run_skipped_count": run_result.get("skipped_count", 0),
            })

            # Run 2: Test patch only
            _logger.info("[%s] Run 2/3: test-patch only", self.name)
            test_patch_log = self._execute_docker_run(
                mode="test_patch",
                patches={"test_patch": self.test_patch},
                timeout=agent_timeout,
            )
            self.write({"test_patch_run_log": test_patch_log[-50000:]})
            test_result = self._parse_test_log(test_patch_log)
            self.write({
                "test_patch_result_json": json.dumps(test_result),
                "test_patch_passed_count": test_result.get("passed_count", 0),
                "test_patch_failed_count": test_result.get("failed_count", 0),
                "test_patch_skipped_count": test_result.get("skipped_count", 0),
            })

            # Run 3: Fix + test patch
            _logger.info("[%s] Run 3/3: fix+test patch", self.name)
            fix_patch_log = self._execute_docker_run(
                mode="fix_patch",
                patches={
                    "fix_patch": self.fix_patch,
                    "test_patch": self.test_patch,
                },
                timeout=agent_timeout,
            )
            fix_result = self._parse_test_log(fix_patch_log)

            self.write({"fix_patch_run_log": fix_patch_log[-50000:] if fix_patch_log else ""})
            self.write({
                "fix_patch_result_json": json.dumps(fix_result),
                "fix_patch_passed_count": fix_result.get("passed_count", 0),
                "fix_patch_failed_count": fix_result.get("failed_count", 0),
                "fix_patch_skipped_count": fix_result.get("skipped_count", 0),
            })

            # Generate report: classify test state transitions
            self._generate_test_report(run_result, test_result, fix_result)

            _logger.info("Test execution complete for %s (valid=%s)", self.name, self.is_valid)

        except Exception as e:
            self.write({
                "is_valid": False,
                "validation_error": f"Test execution error: {str(e)[:500]}",
            })
            _logger.error("Test execution failed for %s: %s", self.name, e)
            raise

    def _execute_docker_run(self, mode, patches, timeout):
        """Execute a single Docker run and return the log output."""
        lang = (self.repository_id.language or "").lower()
        config = self.repository_id._get_effective_config()
        mem = config.get("memory_limit", "8g" if lang in ("rust", "cpp", "c", "java") else "4g")
        network_enabled = config.get("network")
        return _docker_run_impl(
            self.name, self.docker_image_name, mode, patches, timeout, mem, lang,
            network_enabled,
        )

    def _parse_test_log(self, log_text):
        """Parse test runner output into structured results.

        Auto-detects the test framework from log content and delegates
        to the appropriate parser. Supports pytest, Go, Rust, Jest/Vitest,
        Mocha, CTest, and Maven/Surefire.
        """
        text = log_text or ""
        if not text.strip():
            return self._empty_parse_result()

        if "--- PASS:" in text or "--- FAIL:" in text:
            return self._parse_go_log(text)
        if re.search(r"test \S+ \.\.\. (?:ok|FAILED|ignored)", text):
            return self._parse_rust_log(text)
        clean = self._strip_ansi(text)
        # Mocha before jest: both use \u2713, but mocha has "N passing/failing"
        if re.search(r"\d+ passing", text) or re.search(r"\d+ failing", text):
            return self._parse_mocha_log(text)
        # AVA: spinner-based "N passed" / "N tests failed"
        if re.search(r"\d+ tests? failed|\d+ (?:tests? )?passed", clean):
            ava_result = self._parse_ava_log(text)
            if ava_result["passed_count"] > 0 or ava_result["failed_count"] > 0:
                return ava_result
        if "\u2713" in text or "\u2717" in text or "\u25CB" in text:
            return self._parse_jest_log(text)
        if re.search(r"Test\s+#\d+:", text) or "[ PASS ]" in text or "[ FAIL ]" in text:
            return self._parse_ctest_log(text)
        if re.search(r"\[(?:INFO|ERROR)\].*Tests run:", text):
            return self._parse_maven_log(text)
        return self._parse_pytest_log(text)

    @staticmethod
    def _strip_ansi(text):
        return re.sub(r"\x1b\[[0-9;]*[a-zA-Z]|\[2K\[1A", "", text)

    def _parse_ava_log(self, text):
        """Parse AVA test runner output.

        Handles two AVA output styles:
          - Newer AVA: spinner + "N passed" / "N tests failed" summary lines
          - Older AVA: "✔ suite › test name" per-test lines + "N tests passed"
        """
        clean = self._strip_ansi(text)
        lines = [ln.strip() for ln in clean.splitlines() if ln.strip()]

        passed = set()
        failed = set()
        summary_passed = 0
        summary_failed = 0

        re_ava_checkmark = re.compile(r"^[✔✓]\s+(.+)$")
        re_ava_cross = re.compile(r"^[✖✘]\s+(.+)$")
        re_ava_test = re.compile(r"^(.+\s+›\s+.+)$")

        for i, line in enumerate(lines):
            m = re.match(r"^(\d+) (?:tests? )?passed$", line)
            if m:
                summary_passed = max(summary_passed, int(m.group(1)))
                continue
            m = re.match(r"^(\d+) tests? failed$", line)
            if m:
                summary_failed = max(summary_failed, int(m.group(1)))
                continue
            m = re_ava_checkmark.match(line)
            if m:
                passed.add(m.group(1).strip())
                continue
            m = re_ava_cross.match(line)
            if m:
                failed.add(m.group(1).strip())
                continue
            if re_ava_test.match(line):
                next_lines = " ".join(lines[i + 1:i + 3])
                if "Error" in next_lines or "thrown" in next_lines:
                    failed.add(line)

        if not passed and summary_passed > 0:
            passed = {f"ava_test_{i}" for i in range(summary_passed)}
        if not failed and summary_failed > 0:
            failed = {f"ava_failed_{i}" for i in range(summary_failed)}
        return self._make_parse_result(passed, failed, set())

    def _empty_parse_result(self):
        return {
            "passed_count": 0, "failed_count": 0, "skipped_count": 0,
            "passed_tests": [], "failed_tests": [], "skipped_tests": [],
        }

    def _make_parse_result(self, passed, failed, skipped):
        return {
            "passed_count": len(passed), "failed_count": len(failed),
            "skipped_count": len(skipped),
            "passed_tests": sorted(passed), "failed_tests": sorted(failed),
            "skipped_tests": sorted(skipped),
        }

    def _parse_pytest_log(self, text):
        passed, failed, skipped = set(), set(), set()
        for line in text.splitlines():
            line = line.strip()
            if " PASSED" in line:
                parts = line.split()
                if parts:
                    passed.add(parts[0])
            elif " FAILED" in line:
                parts = line.split()
                if parts:
                    failed.add(parts[0])
            elif " SKIPPED" in line or " SKIP" in line:
                parts = line.split()
                if parts:
                    skipped.add(parts[0])
        return self._make_parse_result(passed, failed, skipped)

    def _parse_go_log(self, text):
        passed, failed, skipped = set(), set(), set()
        re_pass = re.compile(r"--- PASS: (\S+)")
        re_fail = [re.compile(r"--- FAIL: (\S+)"), re.compile(r"FAIL:?\s?(\S+)\s")]
        re_skip = re.compile(r"--- SKIP: (\S+)")
        for line in text.splitlines():
            line = line.strip()
            m = re_pass.match(line)
            if m:
                name = m.group(1)
                if name not in failed:
                    skipped.discard(name)
                    passed.add(name)
                continue
            for pat in re_fail:
                m = pat.match(line)
                if m:
                    name = m.group(1)
                    passed.discard(name)
                    skipped.discard(name)
                    failed.add(name)
                    break
            else:
                m = re_skip.match(line)
                if m:
                    name = m.group(1)
                    if name not in passed and name not in failed:
                        skipped.add(name)
        return self._make_parse_result(passed, failed, skipped)

    def _parse_rust_log(self, text):
        passed, failed, skipped = set(), set(), set()
        re_pass = re.compile(r"test (\S+) \.\.\. ok")
        re_fail = re.compile(r"test (\S+) \.\.\. FAILED")
        re_skip = re.compile(r"test (\S+) \.\.\. ignored")
        for line in text.splitlines():
            line = line.strip()
            m = re_pass.match(line)
            if m:
                passed.add(m.group(1))
                continue
            m = re_fail.match(line)
            if m:
                failed.add(m.group(1))
                continue
            m = re_skip.match(line)
            if m:
                skipped.add(m.group(1))
        return self._make_parse_result(passed, failed, skipped)

    def _parse_jest_log(self, text):
        passed, failed, skipped = set(), set(), set()
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith(("\u2713", "\u2717", "\u25CB", "x")):
                symbol = stripped[0]
                name = re.sub(r"\s*\(\d+\s*ms\)\s*$", "", stripped[1:]).strip()
                if not name:
                    continue
                if symbol == "\u2713":
                    passed.add(name)
                elif symbol in ("\u2717", "x"):
                    failed.add(name)
                elif symbol == "\u25CB":
                    skipped.add(name)
        return self._make_parse_result(passed, failed, skipped)

    def _parse_mocha_log(self, text):
        passed, failed, skipped = set(), set(), set()
        re_pass = re.compile(r"[\u2713\u2714]\s+(.+?)(?:\s+\(\d+ms\))?\s*$")
        re_fail = re.compile(r"^\s*(\d+)\)\s+(.+)$")
        re_pend = re.compile(r"^\s*-\s+(.+)$")
        summary_passed = 0
        summary_failed = 0
        in_failures = False
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith(("\u2713", "\u2714")):
                m = re_pass.match(stripped)
                if m:
                    passed.add(m.group(1).strip())
                continue
            m = re.match(r"(\d+)\s+passing", stripped)
            if m:
                summary_passed = int(m.group(1))
                continue
            if "failing" in stripped.lower() and re.match(r"(\d+)\s+failing", stripped):
                m_f = re.match(r"(\d+)\s+failing", stripped)
                if m_f:
                    summary_failed = int(m_f.group(1))
                in_failures = True
                continue
            if in_failures:
                m = re_fail.match(line)
                if m:
                    failed.add(m.group(2).strip())
                    continue
            m = re_pend.match(line)
            if m and not m.group(1).startswith("-"):
                skipped.add(m.group(1).strip())
        if not passed and summary_passed > 0:
            passed = {f"mocha_test_{i}" for i in range(summary_passed)}
        if not failed and summary_failed > 0:
            failed = {f"mocha_failed_{i}" for i in range(summary_failed)}
        return self._make_parse_result(passed, failed, skipped)

    def _parse_ctest_log(self, text):
        passed, failed, skipped = set(), set(), set()
        re_ctest_pass = re.compile(
            r"^\d+/\d+\s*Test\s*#\d+:\s*(.*?)\s*\.+\s+Passed\s+.*$")
        re_ctest_fail = [
            re.compile(r"^\d+/\d+\s*Test\s*#\d+:\s*(.*?)\s*\.+\*\*\*Failed\s+.*$"),
            re.compile(r"^\d+/\d+\s*Test\s*#\d+:\s*(.*?)\s*\.+.*\*\*\*Exception.*$"),
            re.compile(r"^\d+/\d+\s*Test\s*#\d+:\s*(.*?)\s*\.+\*\*\*Timeout\s+.*$"),
            re.compile(r"^\d+/\d+\s*Test\s*#\d+:\s*(.*?)\s*\.+\*\*\*Not Run\s+.*$"),
        ]
        re_sub_pass = re.compile(r"^\[\s*PASS\s*\]\s+(.+)$")
        re_sub_fail = re.compile(r"^\[\s*FAIL\s*\]\s+(.+)$")
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            m = re_ctest_pass.match(line)
            if m:
                passed.add(m.group(1).strip())
                continue
            matched = False
            for pat in re_ctest_fail:
                m = pat.match(line)
                if m:
                    failed.add(m.group(1).strip())
                    matched = True
                    break
            if matched:
                continue
            m = re_sub_pass.match(line)
            if m:
                passed.add(m.group(1).strip())
                continue
            m = re_sub_fail.match(line)
            if m:
                failed.add(m.group(1).strip())
        return self._make_parse_result(passed, failed, skipped)

    def _parse_maven_log(self, text):
        passed, failed, skipped = set(), set(), set()
        ansi = r"(?:\x1B\[[0-9;]*m)?"
        re_pass = [
            re.compile(
                rf"\[?{ansi}INFO{ansi}\]?\s+.*?Tests run:.*?[-]+ in {ansi}([a-zA-Z0-9_.]+){ansi}"),
            re.compile(
                rf"(?:\[{ansi})?INFO{ansi}\]?\s+([a-zA-Z0-9 \-_.]+?)\s+\.{{3,}}\s+{ansi}SUCCESS"),
        ]
        re_fail = [
            re.compile(
                rf"\[?{ansi}ERROR{ansi}\]?\s+.*?Tests run:.*?[-]+ in {ansi}([a-zA-Z0-9_.]+){ansi}"),
            re.compile(
                rf"(?:\[{ansi})?INFO{ansi}\]?\s+([a-zA-Z0-9 \-_.]+?)\s+\.{{3,}}\s+{ansi}FAILURE"),
        ]
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            for pat in re_pass:
                m = pat.match(line)
                if m:
                    passed.add(m.group(1))
                    break
            for pat in re_fail:
                m = pat.match(line)
                if m:
                    failed.add(m.group(1))
                    break
        return self._make_parse_result(passed, failed, skipped)

    def _generate_test_report(self, run_result, test_result, fix_result):
        """Classify test state transitions and validate the instance.

        Implements the same 4-check validation as multi-swe-bench Report.check():
          1. Fix patch result must have >0 tests captured
          2. No regressions (PASS in test-patch → FAIL in fix-patch)
          3. At least one test was fixed (f2p > 0 or n2p > 0)
          4. No anomalous patterns (PASS in baseline, NONE/SKIP in test, FAIL in fix)

        State transitions (comparing test_patch run vs fix+test run):
        - f2p: test FAILED in test_patch, PASSED in fix_patch (the bug fix works)
        - p2p: test PASSED in both (no regression)
        - s2p: test SKIPPED in test_patch, PASSED in fix_patch
        - n2p: test not present in test_patch, PASSED in fix_patch
        """
        run_passed = set(run_result.get("passed_tests", []))

        test_passed = set(test_result.get("passed_tests", []))
        test_failed = set(test_result.get("failed_tests", []))
        test_skipped = set(test_result.get("skipped_tests", []))

        fix_passed = set(fix_result.get("passed_tests", []))
        fix_failed = set(fix_result.get("failed_tests", []))

        all_test_tests = test_passed | test_failed | test_skipped
        fix_all_count = (
            fix_result.get("passed_count", 0)
            + fix_result.get("failed_count", 0)
            + fix_result.get("skipped_count", 0)
        )

        # Classify transitions
        f2p = {t for t in fix_passed if t in test_failed}
        p2p = {t for t in fix_passed if t in test_passed}
        s2p = {t for t in fix_passed if t in test_skipped}
        n2p = {t for t in fix_passed if t not in all_test_tests}

        # Regressions: tests that passed in test_patch but failed in fix_patch
        regressions = {t for t in fix_failed if t in test_passed}

        # Fixed tests: any test that was not passing in test-patch but passes in fix-patch
        fixed_tests = {t for t in fix_passed if t not in test_passed}

        # Build status dicts (include run status for full traceability)
        def _status(t, source):
            if t in source.get("passed_tests", []):
                return "PASS"
            if t in source.get("failed_tests", []):
                return "FAIL"
            if t in source.get("skipped_tests", []):
                return "SKIP"
            return "NONE"

        f2p_dict = {t: {"run": _status(t, run_result), "test": "FAIL", "fix": "PASS"} for t in f2p}
        p2p_dict = {t: {"run": _status(t, run_result), "test": "PASS", "fix": "PASS"} for t in p2p}
        s2p_dict = {t: {"run": _status(t, run_result), "test": "SKIP", "fix": "PASS"} for t in s2p}
        n2p_dict = {t: {"run": _status(t, run_result), "test": "NONE", "fix": "PASS"} for t in n2p}
        fixed_dict = {t: {"run": _status(t, run_result), "test": _status(t, test_result), "fix": "PASS"} for t in fixed_tests}

        # ── 4-check validation (matches multi-swe-bench Report.check()) ──

        is_valid = True
        validation_error = ""
        has_fix_signal = len(f2p) > 0 or len(n2p) > 0
        has_regressions = len(regressions) > 0

        # Check 1: fix patch result must have captured at least one test
        if is_valid and fix_all_count == 0:
            is_valid = False
            validation_error = (
                "After applying the fix patch, no test results were captured "
                "when executing the test command."
            )

        # Check 2: no regressions (PASS in test-patch → FAIL in fix-patch)
        if is_valid and has_regressions:
            is_valid = False
            validation_error = (
                f"Before applying the fix patch, {len(regressions)} test(s) passed; "
                f"after applying the fix patch, they failed: {sorted(regressions)[:5]}"
            )

        # Check 3: fix must actually fix something (f2p > 0 or n2p > 0)
        if is_valid and not has_fix_signal:
            is_valid = False
            validation_error = (
                "After applying the fix patch, no test cases transitioned from "
                "failed to passed (0 f2p tests) and no new tests were introduced "
                "that pass (0 n2p tests)."
            )

        # Check 4: anomalous pattern — test PASSED in baseline, NONE/SKIP in
        # test-patch, FAILED in fix-patch.  This indicates the fix broke a test
        # that was unrelated to the test patch.
        if is_valid:
            for t in fix_failed:
                t_in_test = t in test_passed or t in test_failed or t in test_skipped
                t_was_none_or_skip = (not t_in_test) or (t in test_skipped)
                if t_was_none_or_skip and t in run_passed:
                    is_valid = False
                    validation_error = (
                        f"Anomalous pattern: test `{t}` passed in baseline, "
                        f"was {'skipped' if t in test_skipped else 'absent'} in "
                        f"test-patch run, but failed after applying fix patch."
                    )
                    break

        # Extra sanity: if test-patch and fix-patch runs are identical,
        # patches likely didn't apply
        if is_valid:
            runs_identical = (
                test_passed == fix_passed
                and test_failed == fix_failed
                and (len(test_passed) + len(test_failed)) > 0
            )
            if runs_identical:
                is_valid = False
                validation_error = (
                    "Test-patch and fix-patch runs produced identical results — "
                    "patches may not have applied."
                )

        report = {
            "f2p_count": len(f2p),
            "p2p_count": len(p2p),
            "s2p_count": len(s2p),
            "n2p_count": len(n2p),
            "regressions_count": len(regressions),
            "fixed_count": len(fixed_tests),
            "is_valid": is_valid,
            "has_fix_signal": has_fix_signal,
            "has_regressions": has_regressions,
        }

        self.write({
            "fixed_tests_json": json.dumps(fixed_dict),
            "f2p_tests_json": json.dumps(f2p_dict),
            "p2p_tests_json": json.dumps(p2p_dict),
            "s2p_tests_json": json.dumps(s2p_dict),
            "n2p_tests_json": json.dumps(n2p_dict),
            "is_valid": is_valid,
            "has_fix_signal": has_fix_signal,
            "has_regressions": has_regressions,
            "validation_error": validation_error,
            "report_json": json.dumps(report),
        })
