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
        return "python:3.8-slim-buster"

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
apt-get update && apt-get install -y python3-pip
###ACTION_DELIMITER###
pip3 install -r conans/requirements_dev.txt
###ACTION_DELIMITER###
pip install conan
###ACTION_DELIMITER###
pip install -e .
###ACTION_DELIMITER###
echo 'nosetests -A "not slow and not svn"' > test_commands.sh
###ACTION_DELIMITER###
bash test_commands.sh
###ACTION_DELIMITER###
pip install markupsafe==2.0.1
###ACTION_DELIMITER###
bash test_commands.sh
###ACTION_DELIMITER###
echo 'nosetests' > test_commands.sh
###ACTION_DELIMITER###
bash test_commands.sh
###ACTION_DELIMITER###
pip install tox
###ACTION_DELIMITER###
tox -e full
###ACTION_DELIMITER###
echo 'nosetests -A "not slow and not svn"' > test_commands.sh
###ACTION_DELIMITER###
echo 'pytest' > test_commands.sh
###ACTION_DELIMITER###
pip install pytest
###ACTION_DELIMITER###
bash test_commands.sh
###ACTION_DELIMITER###

###ACTION_DELIMITER###
apt-get install -y subversion
###ACTION_DELIMITER###
bash test_commands.sh
###ACTION_DELIMITER###
apt-get install -y git
###ACTION_DELIMITER###
apt-get install -y pkg-config
###ACTION_DELIMITER###
bash test_commands.sh
###ACTION_DELIMITER###
echo 'pytest --junitxml=results.xml' > test_commands.sh""",
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
pytest --junitxml=results.xml

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
pytest --junitxml=results.xml

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
pytest --junitxml=results.xml

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
FROM python:3.8-slim-buster

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
RUN git clone https://github.com/conan-io/conan.git /home/conan

WORKDIR /home/conan
RUN git reset --hard
RUN git checkout 5f204b37644035d0e5c92e8503fe7813728530bb

RUN git checkout {pr.base.sha}"""
        dockerfile_content += f"""
{copy_commands}
"""
        return dockerfile_content.format(pr=self.pr)


@Instance.register("conan-io", "conan_1_14_5")
class CONAN_1_14_5(Instance):
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
        # Pattern for lines ending with PASSED/FAILED/SKIPPED
        result_pattern = re.compile(r"^(.*?)::(.*) (PASSED|FAILED|SKIPPED)$")
        # Pattern for lines starting with FAILED/PASSED/SKIPPED
        status_first_pattern = re.compile(r"^(FAILED|PASSED|SKIPPED) (.*)$")
        # Pattern for the initial progress lines
        progress_pattern = re.compile(r"^([a-zA-Z0-9/._-]+) (\.|F|s)")
        for line in log.splitlines():
            # Check for the PASSED/FAILED/SKIPPED at the end of the line
            match = result_pattern.match(line)
            if match:
                test_name = f"{match.group(1)}::{match.group(2).strip()}"
                status = match.group(3)
                if status == "PASSED":
                    passed_tests.add(test_name)
                elif status == "FAILED":
                    failed_tests.add(test_name)
                elif status == "SKIPPED":
                    skipped_tests.add(test_name)
                continue
            # Check for FAILED/PASSED/SKIPPED at the beginning of the line
            match = status_first_pattern.match(line)
            if match:
                status = match.group(1)
                test_name = match.group(2).strip()
                if status == "PASSED":
                    passed_tests.add(test_name)
                elif status == "FAILED":
                    failed_tests.add(test_name)
                elif status == "SKIPPED":
                    skipped_tests.add(test_name)
                continue
            # Check for the progress bar format
            match = progress_pattern.match(line)
            if match:
                test_path = match.group(1)
                # This is a bit of a hack, but we don't have the full test name here
                # We will just use the path for now, and it might be overwritten by a more specific pattern later
                status = match.group(2)
                if status == ".":
                    passed_tests.add(test_path)
                elif status == "F":
                    failed_tests.add(test_path)
                elif status == "s":
                    skipped_tests.add(test_path)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
