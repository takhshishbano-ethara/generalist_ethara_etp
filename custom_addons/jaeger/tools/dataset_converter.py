"""
Meta Delivery Schema Converter for Jaeger pipeline.

Converts internal jaeger.instance Odoo records to Meta's 26-field
delivery schema format for RFP submission.
"""
import json
import logging

_logger = logging.getLogger(__name__)

# Language-specific test parsing scripts (standalone Python)
_PARSING_SCRIPTS = {
    "python": '''
import re

def parse_test_output(log_text):
    """Parse pytest output and return dict with passed/failed/skipped lists."""
    results = {"passed": [], "failed": [], "skipped": [], "error": []}
    for line in log_text.splitlines():
        line = line.strip()
        if " PASSED" in line:
            match = re.match(r"(\\S+)\\s+PASSED", line)
            if match:
                results["passed"].append(match.group(1))
        elif " FAILED" in line:
            match = re.match(r"(\\S+)\\s+FAILED", line)
            if match:
                results["failed"].append(match.group(1))
        elif " SKIPPED" in line or " SKIP" in line:
            match = re.match(r"(\\S+)\\s+(?:SKIPPED|SKIP)", line)
            if match:
                results["skipped"].append(match.group(1))
        elif " ERROR" in line:
            match = re.match(r"(\\S+)\\s+ERROR", line)
            if match:
                results["error"].append(match.group(1))
    return results
''',
    "javascript": '''
import re

def parse_test_output(log_text):
    """Parse jest/mocha output."""
    results = {"passed": [], "failed": [], "skipped": [], "error": []}
    for line in log_text.splitlines():
        line = line.strip()
        pass_match = re.match(r"\\s*[✓✔]\\s+(.+?)(?:\\s+\\(\\d+\\s*ms\\))?$", line)
        if pass_match:
            results["passed"].append(pass_match.group(1).strip())
            continue
        fail_match = re.match(r"\\s*[✗✘×]\\s+(.+?)(?:\\s+\\(\\d+\\s*ms\\))?$", line)
        if fail_match:
            results["failed"].append(fail_match.group(1).strip())
            continue
        skip_match = re.match(r"\\s*-\\s+(.+?)$", line)
        if skip_match and "pending" in log_text.lower():
            results["skipped"].append(skip_match.group(1).strip())
    return results
''',
    "java": '''
import re

def parse_test_output(log_text):
    """Parse Maven surefire / Gradle test output."""
    results = {"passed": [], "failed": [], "skipped": [], "error": []}
    for line in log_text.splitlines():
        line = line.strip()
        if "Tests run:" in line:
            match = re.search(
                r"Tests run: (\\d+), Failures: (\\d+), Errors: (\\d+), Skipped: (\\d+)",
                line,
            )
            if match:
                continue
        if line.startswith("Failed tests:") or line.startswith("Tests in error:"):
            continue
        fail_match = re.match(r"\\s*(.+?)\\s*<<< (?:FAILURE|ERROR)!", line)
        if fail_match:
            results["failed"].append(fail_match.group(1).strip())
    return results
''',
}

# Default parsing script for unlisted languages
_DEFAULT_PARSING_SCRIPT = '''
import re

def parse_test_output(log_text):
    """Generic test output parser."""
    results = {"passed": [], "failed": [], "skipped": [], "error": []}
    for line in log_text.splitlines():
        line_lower = line.strip().lower()
        if "pass" in line_lower or "ok" in line_lower:
            results["passed"].append(line.strip()[:200])
        elif "fail" in line_lower or "error" in line_lower:
            results["failed"].append(line.strip()[:200])
        elif "skip" in line_lower or "pending" in line_lower:
            results["skipped"].append(line.strip()[:200])
    return results
'''


class MetaSchemaConverter:
    """Convert Jaeger internal instance format to Meta's delivery schema (26 fields).

    Each instance is converted independently. The converter is initialized with
    repo-level configuration that applies uniformly to all instances in that repo.

    Usage:
        converter = MetaSchemaConverter(
            ecr_prefix='123456789.dkr.ecr.us-east-1.amazonaws.com',
            task_category='hard_swe',
            repo_category='python_swe',
        )
        meta_json = converter.convert(odoo_instance_record)
    """

    def __init__(self, ecr_prefix="", task_category="hard_swe",
                 repo_category="", container_mem="4g",
                 container_memswap="4g", container_network_needed=False):
        self.ecr_prefix = ecr_prefix
        self.task_category = task_category
        self.repo_category = repo_category
        self.container_mem = container_mem
        self.container_memswap = container_memswap
        self.container_network_needed = container_network_needed

    def convert(self, instance):
        """Convert a single jaeger.instance Odoo record to Meta delivery dict.

        Args:
            instance: jaeger.instance browse record with all fields populated.

        Returns:
            dict: 26-field Meta delivery schema dictionary.

        Raises:
            ValueError: If required fields are missing or invalid.
        """
        self._validate_instance(instance)

        return {
            "instance_id": instance.instance_id or instance.name,
            "repo": f"{instance.org}/{instance.repo}",
            "repo_path_or_url": instance.repository_id.repo_url or "",
            "base_commit": instance.base_sha or "",
            "version": self._compute_version(instance),
            "language": instance.language or "",
            "problem_statement": self._build_problem_statement(instance),
            "functional_patch": instance.fix_patch or "",
            "test_patch": instance.test_patch or "",
            "hints": instance.hints or "",
            "FAIL_TO_PASS": self._extract_f2p_tests(instance),
            "PASS_TO_PASS": self._extract_p2p_tests(instance),
            "docker_image_url": self._build_docker_image_url(instance),
            "docker_file": instance.dockerfile_content or "",
            "container_mem": self.container_mem,
            "container_memswap": self.container_memswap,
            "container_network_needed": self.container_network_needed,
            "parsing_script": self._extract_parsing_script(instance),
            "run_script": self._extract_run_script(instance),
            "entrypoint_script": self._extract_entrypoint_script(instance),
            "before_repo_set_cmd": self._extract_before_repo_set_cmd(instance),
            "selected_test_files_to_run": self._extract_selected_test_files(instance),
            "task_category": self.task_category,
            "repo_category": self.repo_category,
            "artifacts": self._collect_artifacts(instance),
            "problem_statement_variants": self._build_variants(instance),
        }

    def convert_batch(self, instances):
        """Convert multiple instances, collecting errors.

        Args:
            instances: iterable of jaeger.instance browse records.

        Returns:
            tuple: (converted_list, errors_list)
        """
        converted = []
        errors = []
        for inst in instances:
            try:
                meta = self.convert(inst)
                converted.append(meta)
            except Exception as e:
                errors.append((inst.name, str(e)))
        return converted, errors

    # -- Validation -----------------------------------------------------------

    def _validate_instance(self, instance):
        """Validate that the instance has all required fields for conversion."""
        if not instance.fix_patch:
            raise ValueError(f"Instance {instance.name} has no fix_patch")
        if not instance.base_sha:
            raise ValueError(f"Instance {instance.name} has no base_sha")
        if not instance.language:
            raise ValueError(f"Instance {instance.name} has no language")
        has_f2p = instance.f2p_tests_json and instance.f2p_tests_json != "{}"
        has_n2p = instance.n2p_tests_json and instance.n2p_tests_json != "{}"
        if not has_f2p and not has_n2p:
            raise ValueError(
                f"Instance {instance.name} has no f2p_tests and no n2p_tests "
                "(at least one required for delivery)",
            )

    # -- Field Converters -----------------------------------------------------

    def _compute_version(self, instance):
        """Use tag if present, else base_sha."""
        if instance.tag and instance.tag.strip():
            return instance.tag.strip()
        return instance.base_sha or ""

    def _build_problem_statement(self, instance):
        """Construct problem_statement from PR body + resolved issues."""
        parts = []

        body = (instance.body or "").strip()
        if body:
            parts.append(body)

        issues = []
        if instance.resolved_issues_json:
            try:
                issues = json.loads(instance.resolved_issues_json)
            except (json.JSONDecodeError, TypeError):
                issues = []

        if not issues and instance.resolved_issue_ids:
            issues = [
                {
                    "number": issue.issue_number,
                    "title": issue.issue_title or "",
                    "body": issue.issue_body or "",
                }
                for issue in instance.resolved_issue_ids
            ]

        if issues:
            parts.append("\n\nLinked Issues:\n")
            for issue in issues:
                issue_num = issue.get("number", "?")
                issue_title = issue.get("title", "")
                issue_body = (issue.get("body", "") or "").strip()

                header = f"### Issue #{issue_num}: {issue_title}"
                parts.append(header)
                if issue_body:
                    parts.append(issue_body)
                parts.append("")

        result = "\n".join(parts).strip()

        if not result:
            raise ValueError(
                f"Instance {instance.name} has empty problem_statement "
                "(no PR body and no resolved issue bodies)",
            )

        return result

    def _extract_f2p_tests(self, instance):
        """Extract FAIL_TO_PASS test names from f2p_tests_json."""
        if not instance.f2p_tests_json:
            return []
        try:
            f2p_dict = json.loads(instance.f2p_tests_json)
        except (json.JSONDecodeError, TypeError):
            return []
        if not isinstance(f2p_dict, dict):
            return []
        return sorted(f2p_dict.keys())

    def _extract_p2p_tests(self, instance):
        """Extract PASS_TO_PASS test names from p2p_tests_json."""
        if not instance.p2p_tests_json:
            return []
        try:
            p2p_dict = json.loads(instance.p2p_tests_json)
        except (json.JSONDecodeError, TypeError):
            return []
        if not isinstance(p2p_dict, dict):
            return []
        return sorted(p2p_dict.keys())

    def _build_docker_image_url(self, instance):
        """Build the full ECR URL for the Docker image."""
        image_name = instance.docker_image_name or ""
        if not image_name:
            return ""
        if self.ecr_prefix and not image_name.startswith(self.ecr_prefix):
            return f"{self.ecr_prefix}/{image_name}"
        return image_name

    def _extract_parsing_script(self, instance):
        """Generate parsing_script based on instance language."""
        lang = (instance.language or "").lower()
        if lang in _PARSING_SCRIPTS:
            return _PARSING_SCRIPTS[lang]
        return _DEFAULT_PARSING_SCRIPT

    def _extract_run_script(self, instance):
        """Extract the run_script (test execution command).

        Uses fix_patch_run_log field if populated (contains the actual
        command used during test execution), otherwise returns empty.
        """
        return instance.fix_patch_run_log or ""

    def _extract_entrypoint_script(self, instance):
        """Extract the entrypoint_script from Dockerfile content.

        Looks for ENTRYPOINT or CMD directives.
        """
        dockerfile = instance.dockerfile_content or ""
        if not dockerfile:
            return ""

        lines = []
        for line in dockerfile.splitlines():
            stripped = line.strip()
            if stripped.startswith("ENTRYPOINT") or stripped.startswith("CMD"):
                lines.append(stripped)
        return "\n".join(lines)

    def _extract_before_repo_set_cmd(self, instance):
        """Extract pre-setup commands from Dockerfile.

        Looks for RUN commands before the COPY/ADD of the repo.
        """
        dockerfile = instance.dockerfile_content or ""
        if not dockerfile:
            return ""

        commands = []
        for line in dockerfile.splitlines():
            stripped = line.strip()
            if stripped.startswith("COPY") or stripped.startswith("ADD"):
                break
            if stripped.startswith("RUN"):
                cmd = stripped[3:].strip()
                commands.append(cmd)
        return "\n".join(commands)

    def _extract_selected_test_files(self, instance):
        """Extract test file paths from test_patch unified diff headers."""
        test_patch = instance.test_patch or ""
        if not test_patch:
            return []

        files = set()
        for line in test_patch.splitlines():
            if line.startswith("--- a/") or line.startswith("+++ b/"):
                path = line[6:].strip()
                if path and path != "/dev/null":
                    files.add(path)
        return sorted(files)

    def _collect_artifacts(self, instance):
        """Collect supporting artifacts as a JSON dict."""
        artifacts = {}
        if instance.docker_build_log:
            artifacts["docker_build_log_length"] = len(instance.docker_build_log)
        if instance.conversation_log:
            artifacts["has_trajectory"] = True
        return artifacts

    def _build_variants(self, instance):
        """Build problem statement variants (optional)."""
        variants = []
        if instance.title:
            variants.append(instance.title)
        return variants
