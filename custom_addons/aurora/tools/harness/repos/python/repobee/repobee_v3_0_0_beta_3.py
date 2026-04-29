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
                """ls -al
###ACTION_DELIMITER###
ls -al requirements
###ACTION_DELIMITER###

###ACTION_DELIMITER###
ls -al tests
###ACTION_DELIMITER###
ls -al system_tests
###ACTION_DELIMITER###
pip install .[TEST]
###ACTION_DELIMITER###
pip install --upgrade pip setuptools
###ACTION_DELIMITER###
pip install .[TEST]
###ACTION_DELIMITER###

###ACTION_DELIMITER###
pip install -r requirements/app.txt
###ACTION_DELIMITER###
sed -i "s/dataclasses>='0.7';python_version<'3.7'/dataclasses>=0.7; python_version<'3.7'/" requirements/app.txt
###ACTION_DELIMITER###
pip install -r requirements/app.txt
###ACTION_DELIMITER###
pip install -r requirements/test.txt
###ACTION_DELIMITER###
echo 'pytest --no-header -rA --tb=no -p no:cacheprovider tests/unit_tests
pytest --no-header -rA --tb=no -p no:cacheprovider tests/new_integration_tests
pytest --no-header -rA --tb=no -p no:cacheprovider system_tests' > test_commands.sh && chmod +x test_commands.sh
###ACTION_DELIMITER###
bash /home/repobee/test_commands.sh
###ACTION_DELIMITER###
pip install -e .
###ACTION_DELIMITER###
sed -i "s/dataclasses>='0.7';python_version<'3.7'/dataclasses>=0.7; python_version<'3.7'/" setup.py
###ACTION_DELIMITER###
pip install -e .
###ACTION_DELIMITER###
bash /home/repobee/test_commands.sh
###ACTION_DELIMITER###
REPOBEE_NO_VERIFY_SSL=true pytest --no-header -rA --tb=no -p no:cacheprovider system_tests""",
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
pytest --no-header -rA --tb=no -p no:cacheprovider tests/unit_tests
pytest --no-header -rA --tb=no -p no:cacheprovider tests/new_integration_tests
pytest --no-header -rA --tb=no -p no:cacheprovider system_tests

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
pytest --no-header -rA --tb=no -p no:cacheprovider tests/unit_tests
pytest --no-header -rA --tb=no -p no:cacheprovider tests/new_integration_tests
pytest --no-header -rA --tb=no -p no:cacheprovider system_tests

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
pytest --no-header -rA --tb=no -p no:cacheprovider tests/unit_tests
pytest --no-header -rA --tb=no -p no:cacheprovider tests/new_integration_tests
pytest --no-header -rA --tb=no -p no:cacheprovider system_tests

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
RUN git clone https://github.com/repobee/repobee.git /home/repobee

WORKDIR /home/repobee
RUN git reset --hard
RUN git checkout {pr.base.sha}
"""
        dockerfile_content += f"""
{copy_commands}
"""
        return dockerfile_content.format(pr=self.pr)


@Instance.register("repobee", "repobee_v3_0_0-beta_3")
class REPOBEE_V3_0_0_BETA_3(Instance):
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
        # Extract PASSED, FAILED, and SKIPPED test names from summary lines
        # Example: PASSED tests/unit_tests/repobee/test_formatters.py::TestPeerReviewFormatter::test_all_reviews_done
        for line in log.splitlines():
            passed_match = re.match(r"^PASSED (.+)", line)
            failed_match = re.match(r"^FAILED (.+)", line)
            skipped_match = re.match(r"^SKIPPED (.+)", line)
            if passed_match:
                passed_tests.add(passed_match.group(1).strip())
            elif failed_match:
                failed_tests.add(failed_match.group(1).strip())
            elif skipped_match:
                skipped_tests.add(skipped_match.group(1).strip())

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
