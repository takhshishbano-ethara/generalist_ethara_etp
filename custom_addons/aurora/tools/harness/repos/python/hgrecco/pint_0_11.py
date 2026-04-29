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
        return "python:3.8"

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
pip install .[test]
###ACTION_DELIMITER###
echo 'python -bb -m pytest -rfsxEX -s --cov=pint --cov-config=.coveragerc' > /home/pint/test_commands.sh
###ACTION_DELIMITER###
bash /home/pint/test_commands.sh
###ACTION_DELIMITER###
pip install numpy==1.14.6
###ACTION_DELIMITER###
bash /home/pint/test_commands.sh
###ACTION_DELIMITER###
pip uninstall -y matplotlib contourpy && pip install matplotlib==3.1.3
###ACTION_DELIMITER###
bash /home/pint/test_commands.sh""",
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
python -bb -m pytest -rfsxEX -s --cov=pint --cov-config=.coveragerc

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
python -bb -m pytest -rfsxEX -s --cov=pint --cov-config=.coveragerc

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
python -bb -m pytest -rfsxEX -s --cov=pint --cov-config=.coveragerc

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
FROM python:3.8

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


@Instance.register("hgrecco", "pint_0_11")
class PINT_0_11(Instance):
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
        # Improved parser: extract real test names and statuses
        all_tests = set()
        failed_tests = set()
        skipped_tests = set()
        xfailed_tests = set()
        errored_tests = set()
        # 1. Collect all real test names (lines with ::test_)
        for match in re.finditer(r"^(\S+::test_[^\s\[]+[^\s]*)", log, re.MULTILINE):
            all_tests.add(match.group(1))
        # 2. Extract skipped tests from SKIPPED lines
        for match in re.finditer(r"SKIPPED \[\d+\] (\S+::test_[^:]+)", log):
            skipped_tests.add(match.group(1))
        # 3. Extract xfailed tests from XFAIL lines
        for match in re.finditer(r"XFAIL (\S+::test_[^ ]+)", log):
            xfailed_tests.add(match.group(1))
        # 4. Extract failed tests from FAILURES/ERRORS sections
        for match in re.finditer(r"^_{2,} (\S+::test_[^\s]+) _*$", log, re.MULTILINE):
            failed_tests.add(match.group(1))
        # Also catch lines like 'FAILED <testname>'
        for match in re.finditer(r"FAILED (\S+::test_[^ ]+)", log):
            failed_tests.add(match.group(1))
        # 5. Extract errored tests from ERROR lines
        for match in re.finditer(r"ERROR (\S+::test_[^ ]+)", log):
            errored_tests.add(match.group(1))
        # Combine all failed/errored
        failed_tests | errored_tests | xfailed_tests
        # Passed = all_tests - (failed + skipped + xfailed + errored)
        passed_tests = all_tests - (
            failed_tests | skipped_tests | xfailed_tests | errored_tests
        )
        # Add errored tests to failed
        failed_tests |= errored_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
