import re
import json
from typing import Optional, Union

from odoo.addons.aurora.tools.harness.image import Config, File, Image
from odoo.addons.aurora.tools.harness.instance import Instance, TestResult
from odoo.addons.aurora.tools.harness.pull_request import PullRequest


class ImageDefault(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> str:
        return "aiidateam/aiida-prerequisites:0.7.0"

    def image_prefix(self) -> str:
        return "envagent"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    @staticmethod
    def _test_files_from_patch(patch: str) -> list[str]:
        """Extract test file paths from a unified diff patch."""
        files = []
        for line in patch.splitlines():
            if line.startswith("diff --git"):
                parts = line.split()
                if len(parts) >= 4:
                    fpath = parts[3].lstrip("b/")
                    if fpath.startswith("tests/") and fpath.endswith(".py"):
                        files.append(fpath)
        return sorted(set(files))

    def files(self) -> list[File]:
        repo_name = self.pr.repo

        # Extract test files from the test patch for scoped test runs.
        # Fall back to full test suite if no test files found.
        test_files = self._test_files_from_patch(self.pr.test_patch)
        test_targets = " ".join(test_files) if test_files else "tests/"

        # Common PATH: system PG 14 before conda pgsql (PG 10.5) to fix
        # pgtest SQL compatibility (FOR KEY SHARE syntax).
        path_export = "export PATH=/usr/lib/postgresql/14/bin:/opt/conda/bin:\\$PATH"

        # Common pytest command with scoped test targets
        pytest_cmd = f"pytest --no-header -rA -v --tb=no -p no:cacheprovider {test_targets}"

        # Run command executed as non-root 'aiida' user (pgtest requires non-root)
        run_body = (
            f'{path_export} && cd /home/{repo_name} && ulimit -n 4096 && {pytest_cmd}'
        )

        return [
            File(
                ".",
                "fix.patch",
                f"{self.pr.fix_patch}",
            ),
            File(
                ".",
                "test.patch",
                f"{self.pr.test_patch}",
            ),
            File(
                ".",
                "prepare.sh",
                f"""#!/bin/bash
set -e

# Fix corrupt sysconfig CFLAGS on aarch64: the non-conda _sysconfigdata has
# truncated compiler flags ('-n1' instead of '-mtune=neoverse-n1', etc.)
# that cause C extension builds (psutil, pymatgen) to fail.
SYSC=/opt/conda/lib/python3.9/_sysconfigdata__linux_aarch64-linux-gnu.py
if [ -f "$SYSC" ]; then
    sed -i "s/'-n1 .2-a+fp16+rcpc+dotprod+crypto '/' '/g" "$SYSC"
    sed -i "s/'-O2  -n1 '/' '/g" "$SYSC"
    sed -i "s/' -O2  -n1 '/' '/g" "$SYSC"
    sed -i "s/'-n1 '/' '/g" "$SYSC"
    sed -i "s/' -n1 '/' '/g" "$SYSC"
    sed -i "s/'-O2  -n1 '/' '/g" "$SYSC"
fi
find /opt/conda/lib/python3.9/__pycache__ -name '_sysconfigdata*' -delete 2>/dev/null || true

# pgtest needs PG 14+ (conda pgsql has PG 10.5 which lacks FOR KEY SHARE)
apt-get update && apt-get install -y --no-install-recommends postgresql-14 >/dev/null 2>&1

pip install .[tests]

useradd -m aiida 2>/dev/null || true
chown -R aiida:aiida /home/{repo_name}
""",
            ),
            File(
                ".",
                "run.sh",
                f"""#!/bin/bash
cd /home/{repo_name}
useradd -m aiida 2>/dev/null || true
chown -R aiida:aiida /home/{repo_name}
su - aiida -c "{run_body}"
""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""#!/bin/bash
cd /home/{repo_name}
if ! git -C /home/{repo_name} apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
useradd -m aiida 2>/dev/null || true
chown -R aiida:aiida /home/{repo_name}
su - aiida -c "{run_body}"
""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""#!/bin/bash
cd /home/{repo_name}
if ! git -C /home/{repo_name} apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
useradd -m aiida 2>/dev/null || true
chown -R aiida:aiida /home/{repo_name}
su - aiida -c "{run_body}"
""",
            ),
        ]

    def dockerfile(self) -> str:
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        dockerfile_content = """
# This is a template for creating a Dockerfile to test patches
# LLM should fill in the appropriate values based on the context

# Choose an appropriate base image based on the project's requirements - replace aiidateam/aiida-prerequisites:0.7.0 with actual base image
# For example: FROM ubuntu:**, FROM python:**, FROM node:**, FROM centos:**, etc.
FROM aiidateam/aiida-prerequisites:0.7.0

## Set noninteractive
ENV DEBIAN_FRONTEND=noninteractive

# Install basic requirements
# For example: RUN apt-get update && apt-get install -y git
# For example: RUN yum install -y git
# For example: RUN apk add --no-cache git
RUN apt-get update && apt-get install -y git

# Ensure bash is available
RUN if [ ! -f /bin/bash ]; then         if command -v apk >/dev/null 2>&1; then             apk add --no-cache bash;         elif command -v apt-get >/dev/null 2>&1; then             apt-get update && apt-get install -y bash;         elif command -v yum >/dev/null 2>&1; then             yum install -y bash;         else             exit 1;         fi     fi

WORKDIR /home/
COPY fix.patch /home/
COPY test.patch /home/
RUN git clone https://github.com/aiidateam/aiida-core.git /home/aiida-core

WORKDIR /home/aiida-core
RUN git reset --hard
RUN git checkout {pr.base.sha}
"""
        dockerfile_content += f"""
{copy_commands}
"""
        return dockerfile_content.format(pr=self.pr)


@Instance.register("aiidateam", "aiida_core_6107_to_5973")
class AIIDA_CORE_6107_TO_5973(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        if run_cmd:
            return run_cmd

        return "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        if test_patch_run_cmd:
            return test_patch_run_cmd

        return "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        if fix_patch_run_cmd:
            return fix_patch_run_cmd

        return "bash /home/fix-run.sh"

    def parse_log(self, log: str) -> TestResult:
        # Parse the log content and extract test execution results.
        passed_tests: set[str] = set()  # Tests that passed successfully
        failed_tests: set[str] = set()  # Tests that failed
        skipped_tests: set[str] = set()  # Tests that were skipped
        import re

        # Regex patterns to match test cases and statuses
        pattern1 = re.compile(
            r"^([^\s]+)\s+(PASSED|FAILED|SKIPPED|ERROR)\s+.*?(\[\s*\d+%\])?$"
        )  # Test name (no spaces) followed by status
        pattern2 = re.compile(
            r"^(PASSED|FAILED|SKIPPED|ERROR)\s+([^\s]+)\s*.*$"
        )  # Status followed by test name (no spaces)
        pattern3 = re.compile(
            r"^([^\s]+)\s+SKIPPED\s+\(\.\.\.\).*$"
        )  # Test name (no spaces) followed by SKIPPED (...)
        lines = log.split("\n")
        for line in lines:
            line = line.strip()
            # Remove the [line_number] prefix if present
            prefix_match = re.match(r"^\[\s*\d+\]\s*(.*)$", line)
            if prefix_match:
                test_info = prefix_match.group(1).strip()
            else:
                test_info = line.strip()
            if not test_info:
                continue  # skip empty lines
            # Check against patterns
            match = pattern1.match(test_info)
            if match:
                test_name = match.group(1).strip()
                status = match.group(2)
            else:
                match = pattern2.match(test_info)
                if match:
                    status = match.group(1)
                    test_name = match.group(2).strip()
                else:
                    match = pattern3.match(test_info)
                    if match:
                        test_name = match.group(1).strip()
                        status = "SKIPPED"
                    else:
                        continue  # no match
            # Categorize the test based on status
            if status == "PASSED":
                passed_tests.add(test_name)
            elif status in ("FAILED", "ERROR"):
                failed_tests.add(test_name)
            elif status == "SKIPPED":
                skipped_tests.add(test_name)
        parsed_results = {
            "passed_tests": passed_tests,
            "failed_tests": failed_tests,
            "skipped_tests": skipped_tests,
        }

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
