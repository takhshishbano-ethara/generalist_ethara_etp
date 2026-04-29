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
        return "python:3.7-slim"

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
ls -al requirements
###ACTION_DELIMITER###
ls -al src
###ACTION_DELIMITER###
echo 'PYTHONPATH=src pytest tests/unit_tests --no-header -rA --tb=no -p no:cacheprovider
PYTHONPATH=src pytest tests/new_integration_tests --no-header -rA --tb=no -p no:cacheprovider
cd system_tests && PYTHONPATH=../src REPOBEE_NO_VERIFY_SSL="true" PYTHONWARNINGS="ignore:Unverified HTTPS request" pytest test_gitlab_system.py --no-header -rA --tb=no -p no:cacheprovider && cd ..' > /home/repobee/test_commands.sh && chmod +x /home/repobee/test_commands.sh
###ACTION_DELIMITER###
pip install -e .
###ACTION_DELIMITER###
pip install -r requirements/test.txt
###ACTION_DELIMITER###
bash /home/repobee/test_commands.sh
###ACTION_DELIMITER###
pip install daiquiri python-gitlab
###ACTION_DELIMITER###
bash /home/repobee/test_commands.sh
###ACTION_DELIMITER###
pip install -r requirements/app.txt
###ACTION_DELIMITER###
bash /home/repobee/test_commands.sh""",
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
PYTHONPATH=src pytest tests/unit_tests --no-header -rA --tb=no -p no:cacheprovider
PYTHONPATH=src pytest tests/new_integration_tests --no-header -rA --tb=no -p no:cacheprovider
cd system_tests && PYTHONPATH=../src REPOBEE_NO_VERIFY_SSL="true" PYTHONWARNINGS="ignore:Unverified HTTPS request" pytest test_gitlab_system.py --no-header -rA --tb=no -p no:cacheprovider && cd ..

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
PYTHONPATH=src pytest tests/unit_tests --no-header -rA --tb=no -p no:cacheprovider
PYTHONPATH=src pytest tests/new_integration_tests --no-header -rA --tb=no -p no:cacheprovider
cd system_tests && PYTHONPATH=../src REPOBEE_NO_VERIFY_SSL="true" PYTHONWARNINGS="ignore:Unverified HTTPS request" pytest test_gitlab_system.py --no-header -rA --tb=no -p no:cacheprovider && cd ..

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
PYTHONPATH=src pytest tests/unit_tests --no-header -rA --tb=no -p no:cacheprovider
PYTHONPATH=src pytest tests/new_integration_tests --no-header -rA --tb=no -p no:cacheprovider
cd system_tests && PYTHONPATH=../src REPOBEE_NO_VERIFY_SSL="true" PYTHONWARNINGS="ignore:Unverified HTTPS request" pytest test_gitlab_system.py --no-header -rA --tb=no -p no:cacheprovider && cd ..

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
FROM python:3.7-slim

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
RUN git clone https://github.com/repobee/repobee.git /home/repobee

WORKDIR /home/repobee
RUN git reset --hard
RUN git checkout {pr.base.sha}
"""
        dockerfile_content += f"""
{copy_commands}
"""
        return dockerfile_content.format(pr=self.pr)


@Instance.register("repobee", "repobee_v3_4_0")
class REPOBEE_V3_4_0(Instance):
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
        # Regex to match lines like: PASSED <test_name>, FAILED <test_name>, SKIPPED <test_name>
        pattern = re.compile(r"^(PASSED|FAILED|SKIPPED) (.+)$", re.MULTILINE)
        for match in pattern.finditer(log):
            status, test_name = match.groups()
            if status == "PASSED":
                passed_tests.add(test_name.strip())
            elif status == "FAILED":
                failed_tests.add(test_name.strip())
            elif status == "SKIPPED":
                skipped_tests.add(test_name.strip())

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
