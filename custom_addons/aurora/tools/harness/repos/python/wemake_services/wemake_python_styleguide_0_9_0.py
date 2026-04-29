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
        return "python:3.7"

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
                """ls -F
###ACTION_DELIMITER###
pip install poetry
###ACTION_DELIMITER###
poetry install
###ACTION_DELIMITER###
poetry lock --no-update
###ACTION_DELIMITER###
poetry install
###ACTION_DELIMITER###
echo 'poetry run pytest' > test_commands.sh
###ACTION_DELIMITER###
cat test_commands.sh""",
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
poetry run pytest

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
poetry run pytest

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
poetry run pytest

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
FROM python:3.7

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
RUN git clone https://github.com/wemake-services/wemake-python-styleguide.git /home/wemake-python-styleguide

WORKDIR /home/wemake-python-styleguide
RUN git reset --hard
RUN git checkout {pr.base.sha}
"""
        dockerfile_content += f"""
{copy_commands}
"""
        return dockerfile_content.format(pr=self.pr)


@Instance.register("wemake-services", "wemake-python-styleguide_0_9_0")
class WEMAKE_PYTHON_STYLEGUIDE_0_9_0(Instance):
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
        all_tests = set(re.findall(r"(tests/.*?\.py)", log))
        failed_tests = set()
        skipped_tests = set()
        # 1. Parse "short test summary info" - most reliable
        summary_section = re.search(
            r"=========================== short test summary info ============================([\s\S]*)",
            log,
        )
        if summary_section:
            summary_content = summary_section.group(1)
            # Find failed tests, format: FAILED tests/test_visitors/test_ast/test_naming/test_naming_rules/test_wrong_name.py::test_wrong_short_name
            failed_matches = re.findall(r"FAILED (tests/.*?\.py)", summary_content)
            for match in failed_matches:
                failed_tests.add(match)
            # Find skipped tests, format: SKIPPED [1] tests/test_visitors/test_ast/test_imports/test_nested_imports.py:76: got python3.8...
            skipped_matches = re.findall(
                r"SKIPPED \[.*?\] (tests/.*?\.py)", summary_content
            )
            for match in skipped_matches:
                skipped_tests.add(match)
        # 2. Parse pytest output for failures (F) and skips (s) for logs without summary
        test_result_lines = re.findall(
            r"^(tests/.*?\.py)\s+([\.Fsx]+)", log, re.MULTILINE
        )
        for test_file, results in test_result_lines:
            if "F" in results:
                failed_tests.add(test_file)
            if "s" in results or "x" in results:
                skipped_tests.add(test_file)
        # 3. Parse for collection errors
        error_matches = re.findall(r"_ ERROR collecting (tests/.*?\.py) _", log)
        for match in error_matches:
            failed_tests.add(match)
        # 4. Infer passed tests
        passed_tests = all_tests - failed_tests - skipped_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
