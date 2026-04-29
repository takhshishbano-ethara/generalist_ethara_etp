import re
from typing import Optional

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
        return "python:3.9-slim"

    def image_prefix(self) -> str:
        return "envagent"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
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
                """ls -al
###ACTION_DELIMITER###
pip install -e .[dev]
###ACTION_DELIMITER###
echo 'invoke test' > /home/SDMetrics/test_commands.sh
###ACTION_DELIMITER###
bash /home/SDMetrics/test_commands.sh
###ACTION_DELIMITER###
invoke --list
###ACTION_DELIMITER###
echo -e 'invoke unit
invoke integration
invoke readme' > /home/SDMetrics/test_commands.sh
###ACTION_DELIMITER###
bash /home/SDMetrics/test_commands.sh""",
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
invoke unit
invoke integration
invoke readme

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
if ! git -C /home/{pr.repo} apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1  
fi
invoke unit
invoke integration
invoke readme

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
if ! git -C /home/{pr.repo} apply --whitespace=nowarn  /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1  
fi
invoke unit
invoke integration
invoke readme

""".format(pr=self.pr),
            ),
        ]

    def dockerfile(self) -> str:
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        dockerfile_content = """
# This is a template for creating a Dockerfile to test patches
# LLM should fill in the appropriate values based on the context

# Choose an appropriate base image based on the project's requirements - replace [base image] with actual base image
# For example: FROM ubuntu:**, FROM python:**, FROM node:**, FROM centos:**, etc.
FROM python:3.9-slim

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
RUN git clone https://github.com/sdv-dev/SDMetrics.git /home/SDMetrics

WORKDIR /home/SDMetrics
RUN git reset --hard
RUN git checkout {pr.base.sha}
"""
        dockerfile_content += f"""
{copy_commands}
"""
        return dockerfile_content.format(pr=self.pr)


@Instance.register("sdv-dev", "SDMetrics_v0_9_2")
class SDMETRICS_V0_9_2(Instance):
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
        passed_tests = set()  # Tests that passed successfully
        failed_tests = set()  # Tests that failed
        skipped_tests = set()  # Tests that were skipped

        # Improved: extract function-level test results from summary and progress lines
        in_summary = False
        failed_tests = set()
        skipped_tests = set()
        error_tests = set()
        all_tests = set()

        # Regex for summary lines
        summary_re = re.compile(r"^(FAILED|ERROR|SKIPPED) (tests/.*?\.py(?:::.*?)?)")
        # Regex for progress lines (fallback for passed tests)
        re.compile(r"^(tests/.*?\.py)\s+([.RFEfs]+)\s*\[.*?%\]")
        # Regex for function names in traceback or warning lines
        func_re = re.compile(r"(tests/.*?\.py(?:::.*?)?)")

        lines = log.splitlines()
        # First pass: extract from summary section
        for i, line in enumerate(lines):
            if "short test summary info" in line:
                in_summary = True
                continue
            if in_summary:
                if line.strip() == "" or line.startswith("=") or line.startswith("!"):
                    break
                m = summary_re.match(line.strip())
                if m:
                    status, testname = m.groups()
                    if status == "FAILED":
                        failed_tests.add(testname)
                    elif status == "ERROR":
                        error_tests.add(testname)
                    elif status == "SKIPPED":
                        skipped_tests.add(testname)

        # Second pass: collect all test functions seen in the log
        for line in lines:
            for m in func_re.finditer(line):
                all_tests.add(m.group(1))

        # Clean up: Only include valid test function names (file::function) or file if that's all available
        def is_valid_test_name(name):
            # Accept tests/file.py::func or tests/file.py::Class::func
            return bool(re.match(r"tests/.*?\.py::.+", name))

        def is_file_only(name):
            return bool(re.match(r"tests/.*?\.py$", name))

        # Passed = all_tests - failed - error - skipped
        passed_tests = set(
            filter(
                lambda n: is_valid_test_name(n) or is_file_only(n),
                all_tests - failed_tests - error_tests - skipped_tests,
            )
        )
        failed_tests = set(
            filter(
                lambda n: is_valid_test_name(n) or is_file_only(n),
                failed_tests | error_tests,
            )
        )
        skipped_tests = set(
            filter(lambda n: is_valid_test_name(n) or is_file_only(n), skipped_tests)
        )

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
