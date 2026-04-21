import json
import logging

from odoo import api, fields, models

_logger = logging.getLogger(__name__)

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
                rec.instance_id = f"{rec.org}__{rec.repo}-{rec.pr_number}"
            else:
                rec.instance_id = ""

    @api.depends("f2p_tests_json", "p2p_tests_json")
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
            self.write({"fix_patch_run_log": fix_patch_log[-50000:]})
            fix_result = self._parse_test_log(fix_patch_log)
            self.write({
                "fix_patch_result_json": json.dumps(fix_result),
                "fix_patch_passed_count": fix_result.get("passed_count", 0),
                "fix_patch_failed_count": fix_result.get("failed_count", 0),
                "fix_patch_skipped_count": fix_result.get("skipped_count", 0),
            })

            # Generate report: classify test state transitions
            self._generate_test_report(run_result, test_result, fix_result)

            # Update repo-level progress
            repo = self.repository_id
            all_instances = repo.instance_ids
            tested = all_instances.filtered(lambda i: i.run_result_json)
            valid = all_instances.filtered(lambda i: i.is_valid)
            invalid = tested.filtered(lambda i: not i.is_valid)
            errors = all_instances.filtered(lambda i: i.validation_error and "error" in (i.validation_error or "").lower())
            total = max(len(all_instances), 1)
            progress = (len(tested) / total) * 100
            vals = {
                "instances_tested_count": len(tested),
                "instances_valid_count": len(valid),
                "instances_invalid_count": len(invalid),
                "instances_error_count": len(errors),
                "test_execution_progress": progress,
            }
            if progress >= 100:
                vals["test_execution_status"] = "done"
            repo.write(vals)

            _logger.info("Test execution complete for %s (valid=%s)", self.name, self.is_valid)

        except Exception as e:
            self.write({
                "is_valid": False,
                "validation_error": f"Test execution error: {str(e)[:500]}",
            })
            _logger.error("Test execution failed for %s: %s", self.name, e)
            raise

    def _execute_docker_run(self, mode, patches, timeout):
        """Execute a single Docker run and return the log output.

        Args:
            mode: 'run', 'test_patch', or 'fix_patch'
            patches: dict with patch content to apply, or None
            timeout: execution timeout in seconds

        Returns:
            str: Container log output
        """
        import subprocess
        import tempfile

        container_name = f"jaeger-{self.name}-{mode}".replace("/", "-").replace("__", "-").lower()

        # Clean up any existing container with same name
        subprocess.run(
            ["docker", "rm", "-f", container_name],
            capture_output=True, timeout=30,
        )

        cmd = [
            "docker", "run",
            "--name", container_name,
            "--rm",
            "--network", "none",
            "--memory", "4g",
            "--memory-swap", "4g",
        ]

        if patches:
            # Create a temp dir with patches and mount it
            with tempfile.TemporaryDirectory() as tmpdir:
                from pathlib import Path

                for patch_name, patch_content in patches.items():
                    if patch_content:
                        patch_path = Path(tmpdir) / f"{patch_name}.diff"
                        patch_path.write_text(patch_content, encoding="utf-8")

                cmd.extend(["-v", f"{tmpdir}:/patches:ro"])
                cmd.append(self.docker_image_name)

                if mode == "test_patch":
                    cmd.extend(["bash", "-c", "cd /testbed && git apply /patches/test_patch.diff && bash fix-run.sh"])
                elif mode == "fix_patch":
                    cmd.extend(["bash", "-c", "cd /testbed && git apply /patches/fix_patch.diff && git apply /patches/test_patch.diff && bash fix-run.sh"])

                result = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=timeout,
                )
        else:
            cmd.append(self.docker_image_name)
            cmd.extend(["bash", "-c", "cd /testbed && bash fix-run.sh"])
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout,
            )

        return result.stdout + "\n" + result.stderr

    def _parse_test_log(self, log_text):
        """Parse test runner output into structured results.

        Returns:
            dict with passed_count, failed_count, skipped_count,
            passed_tests (list), failed_tests (list), skipped_tests (list)
        """
        passed = set()
        failed = set()
        skipped = set()

        for line in (log_text or "").splitlines():
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

        return {
            "passed_count": len(passed),
            "failed_count": len(failed),
            "skipped_count": len(skipped),
            "passed_tests": sorted(passed),
            "failed_tests": sorted(failed),
            "skipped_tests": sorted(skipped),
        }

    def _generate_test_report(self, run_result, test_result, fix_result):
        """Classify test state transitions and validate the instance.

        State transitions (comparing test_patch run vs fix+test run):
        - f2p: test FAILED in test_patch, PASSED in fix_patch (the bug fix works)
        - p2p: test PASSED in both (no regression)
        - s2p: test SKIPPED in test_patch, PASSED in fix_patch
        - n2p: test not present in test_patch, PASSED in fix_patch
        """
        test_passed = set(test_result.get("passed_tests", []))
        test_failed = set(test_result.get("failed_tests", []))
        test_skipped = set(test_result.get("skipped_tests", []))

        fix_passed = set(fix_result.get("passed_tests", []))
        fix_failed = set(fix_result.get("failed_tests", []))

        all_test_tests = test_passed | test_failed | test_skipped

        # Classify transitions
        f2p = {t for t in fix_passed if t in test_failed}
        p2p = {t for t in fix_passed if t in test_passed}
        s2p = {t for t in fix_passed if t in test_skipped}
        n2p = {t for t in fix_passed if t not in all_test_tests}

        # Regressions: tests that passed in test_patch but failed in fix_patch
        regressions = {t for t in fix_failed if t in test_passed}

        # Build status dicts
        f2p_dict = {t: {"test": "FAIL", "fix": "PASS"} for t in f2p}
        p2p_dict = {t: {"test": "PASS", "fix": "PASS"} for t in p2p}
        s2p_dict = {t: {"test": "SKIP", "fix": "PASS"} for t in s2p}
        n2p_dict = {t: {"test": "NONE", "fix": "PASS"} for t in n2p}

        # Validation rules
        has_fix_signal = len(f2p) > 0
        has_regressions = len(regressions) > 0
        is_valid = has_fix_signal and not has_regressions

        if not has_fix_signal:
            validation_error = "No f2p tests (fix doesn't change any failing test to passing)"
        elif has_regressions:
            validation_error = f"Has {len(regressions)} regressions: {sorted(regressions)[:5]}"
        else:
            validation_error = ""

        report = {
            "f2p_count": len(f2p),
            "p2p_count": len(p2p),
            "s2p_count": len(s2p),
            "n2p_count": len(n2p),
            "regressions_count": len(regressions),
            "is_valid": is_valid,
            "has_fix_signal": has_fix_signal,
            "has_regressions": has_regressions,
        }

        self.write({
            "f2p_tests_json": json.dumps(f2p_dict),
            "p2p_tests_json": json.dumps(p2p_dict),
            "s2p_tests_json": json.dumps(s2p_dict),
            "n2p_tests_json": json.dumps(n2p_dict),
            "f2p_count": len(f2p),
            "p2p_count": len(p2p),
            "is_valid": is_valid,
            "has_fix_signal": has_fix_signal,
            "has_regressions": has_regressions,
            "validation_error": validation_error,
            "report_json": json.dumps(report),
        })
