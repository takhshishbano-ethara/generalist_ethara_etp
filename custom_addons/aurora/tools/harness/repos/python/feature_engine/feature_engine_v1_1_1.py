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
        return "python:3.8-slim"

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
pip install -r requirements.txt
###ACTION_DELIMITER###
pip install -r test_requirements.txt
###ACTION_DELIMITER###

###ACTION_DELIMITER###
echo "pytest --no-header -rA --tb=no -p no:cacheprovider tests" > test_commands.sh
###ACTION_DELIMITER###
bash test_commands.sh
###ACTION_DELIMITER###
pip install scikit-learn==1.1.0
###ACTION_DELIMITER###
bash test_commands.sh
###ACTION_DELIMITER###
echo "pytest tests" > test_commands.sh
###ACTION_DELIMITER###
ls /usr/bin/python*
###ACTION_DELIMITER###
which python
###ACTION_DELIMITER###
pip install virtualenv
###ACTION_DELIMITER###
virtualenv venv
###ACTION_DELIMITER###
echo "export PYTHONPATH=.
pytest tests" > test_commands.sh
###ACTION_DELIMITER###
bash test_commands.sh
###ACTION_DELIMITER###
echo "export PYTHONPATH=.
pytest tests/test_creation" > test_commands.sh
###ACTION_DELIMITER###
bash test_commands.sh
###ACTION_DELIMITER###
echo "export PYTHONPATH=.
pytest tests/test_discretisation" > test_commands.sh
###ACTION_DELIMITER###
bash test_commands.sh
###ACTION_DELIMITER###
echo "export PYTHONPATH=.
pytest tests/test_encoding" > test_commands.sh
###ACTION_DELIMITER###
bash test_commands.sh
###ACTION_DELIMITER###
echo "export PYTHONPATH=.
pytest" > test_commands.sh""",
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
export PYTHONPATH=.
pytest

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
export PYTHONPATH=.
pytest

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
export PYTHONPATH=.
pytest

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
FROM python:3.8-slim

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
RUN git clone https://github.com/feature-engine/feature_engine.git /home/feature_engine

WORKDIR /home/feature_engine
RUN git reset --hard
RUN git checkout {pr.base.sha}
"""
        dockerfile_content += f"""
{copy_commands}
"""
        return dockerfile_content.format(pr=self.pr)


@Instance.register("feature-engine", "feature_engine_v1_1_1")
class FEATURE_ENGINE_V1_1_1(Instance):
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
        # Implement the log parsing logic here
        # Get failed tests from summary
        summary_marker = "=========================== short test summary info ============================"
        summary_start_index = log.find(summary_marker)
        if summary_start_index != -1:
            summary_content = log[summary_start_index:]
            failed_regex = re.compile(r"^FAILED (tests/.*?::.*?) - ", re.MULTILINE)
            for match in failed_regex.finditer(summary_content):
                failed_tests.add(match.group(1).strip())
        # A set of test files that have failures, based on the summary
        failed_test_files = {t.split("::")[0] for t in failed_tests}
        # Regex to capture test file and the status characters
        test_line_regex = re.compile(r"^(tests/.*?\.py)\s+([\.sF]+)")
        for line in log.splitlines():
            match = test_line_regex.match(line)
            if match:
                test_file = match.group(1).strip()
                statuses = match.group(2)
                # Check for failures indicated by 'F' that might not be in the summary
                if "F" in statuses and test_file not in failed_test_files:
                    failed_tests.add(test_file)
                    failed_test_files.add(test_file)
                # If the file has any failure, we don't consider it for passed/skipped.
                if test_file in failed_test_files:
                    continue
                if "s" in statuses:
                    skipped_tests.add(test_file)
                if "." in statuses:
                    passed_tests.add(test_file)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
