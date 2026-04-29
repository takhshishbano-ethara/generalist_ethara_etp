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
                """ls -al
###ACTION_DELIMITER###
pip install .[test,numpy,uncertainties] matplotlib
###ACTION_DELIMITER###
echo 'python -bb -m pytest -rfsxEX -s --cov=pint' > /home/pint/test_commands.sh
###ACTION_DELIMITER###
bash /home/pint/test_commands.sh""",
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
python -bb -m pytest -rfsxEX -s --cov=pint

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
python -bb -m pytest -rfsxEX -s --cov=pint

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
python -bb -m pytest -rfsxEX -s --cov=pint

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
RUN git clone https://github.com/hgrecco/pint.git /home/pint

WORKDIR /home/pint
RUN git reset --hard
RUN git checkout {pr.base.sha}
"""
        dockerfile_content += f"""
{copy_commands}
"""
        return dockerfile_content.format(pr=self.pr)


@Instance.register("hgrecco", "pint_0_10")
class PINT_0_10(Instance):
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
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()
        # Regex patterns for summary lines
        skip_summary_re = re.compile(r"SKIPPED \[\d+\] ([^:]+):(\d+):")
        xfail_summary_re = re.compile(r"XFAIL ([^ ]+)")
        error_summary_re = re.compile(r"ERROR ([^ ]+)")
        fail_summary_re = re.compile(r"FAILED ([^ ]+)")
        # Regex for per-file test lines (dots, s, x, F, E)
        test_line_re = re.compile(r"^(\S+\.py) ([.sxFE]+)")
        # Track test status by file and position
        for line in log.splitlines():
            m = test_line_re.match(line)
            if m:
                fname, results = m.groups()
                for idx, ch in enumerate(results):
                    # Compose a synthetic test name: file.py::<index>
                    test_id = f"{fname}::{idx + 1}"
                    if ch == ".":
                        passed_tests.add(test_id)
                    elif ch == "s":
                        skipped_tests.add(test_id)
                    elif ch == "x":
                        # xfail or xpass, treat as failed for now
                        failed_tests.add(test_id)
                    elif ch == "F":
                        failed_tests.add(test_id)
                    elif ch == "E":
                        failed_tests.add(test_id)
        # Parse summary info for more precise test names
        for line in log.splitlines():
            m = skip_summary_re.search(line)
            if m:
                skipped_tests.add(m.group(1))
            m = xfail_summary_re.search(line)
            if m:
                failed_tests.add(m.group(1))
            m = error_summary_re.search(line)
            if m:
                failed_tests.add(m.group(1))
            m = fail_summary_re.search(line)
            if m:
                failed_tests.add(m.group(1))
        # Remove any test from passed if it is also in failed/skipped
        passed_tests -= failed_tests | skipped_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
