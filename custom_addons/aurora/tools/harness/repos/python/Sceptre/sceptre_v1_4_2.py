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
        return "python:3.6-slim"

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
                """ls
###ACTION_DELIMITER###
pip install -r requirements.txt
###ACTION_DELIMITER###
pip install .[test]
###ACTION_DELIMITER###
sed -i "26s/moto==0.4.31/moto>=1.0.0/" setup.py
###ACTION_DELIMITER###
pip install .[test]
###ACTION_DELIMITER###
pytest tests/ --ignore=env/ --ignore=venv/ --junitxml=build/pytest/junit-py36.xml -s
###ACTION_DELIMITER###
sed -i 's/"true"/true/g' tests/fixtures/templates/compiled_vpc.json
###ACTION_DELIMITER###
sed -i 's/"true"/true/g' tests/fixtures/templates/compiled_vpc_sud.json
###ACTION_DELIMITER###
pytest tests/ --ignore=env/ --ignore=venv/ --junitxml=build/pytest/junit-py36.xml -s
###ACTION_DELIMITER###
sed -i 's/"true"/true/g' tests/fixtures/templates/vpc.json
###ACTION_DELIMITER###

###ACTION_DELIMITER###

###ACTION_DELIMITER###
sed -i 's/"true"/true/g' tests/fixtures/templates/vpc.template
###ACTION_DELIMITER###

###ACTION_DELIMITER###
pytest tests/ --ignore=env/ --ignore=venv/ --junitxml=build/pytest/junit-py36.xml -s
###ACTION_DELIMITER###
sed -i "s/'true'/true/g" tests/fixtures/templates/vpc.yaml
###ACTION_DELIMITER###
pytest tests/ --ignore=env/ --ignore=venv/ --junitxml=build/pytest/junit-py36.xml -s
###ACTION_DELIMITER###
echo "pytest tests/ --ignore=env/ --ignore=venv/ --junitxml=build/pytest/junit-py36.xml -s" > test_commands.sh""",
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
pytest tests/ --ignore=env/ --ignore=venv/ --junitxml=build/pytest/junit-py36.xml -s

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
pytest tests/ --ignore=env/ --ignore=venv/ --junitxml=build/pytest/junit-py36.xml -s

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
pytest tests/ --ignore=env/ --ignore=venv/ --junitxml=build/pytest/junit-py36.xml -s

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
FROM python:3.6-slim

## Set noninteractive
ENV DEBIAN_FRONTEND=noninteractive

# Install basic requirements
# For example: RUN apt-get update && apt-get install -y git
# For example: RUN yum install -y git
# For example: RUN apk add --no-cache git
RUN CODENAME=$(cat /etc/os-release | grep VERSION_CODENAME | cut -d= -f2) && \
    echo "deb [trusted=yes] http://archive.debian.org/debian $CODENAME main" > /etc/apt/sources.list && \
    apt-get update && apt-get install -y git

# Ensure bash is available
RUN if [ ! -f /bin/bash ]; then         if command -v apk >/dev/null 2>&1; then             apk add --no-cache bash;         elif command -v apt-get >/dev/null 2>&1; then             apt-get update && apt-get install -y bash;         elif command -v yum >/dev/null 2>&1; then             yum install -y bash;         else             exit 1;         fi     fi

WORKDIR /home/
COPY fix.patch /home/
COPY test.patch /home/
RUN git clone https://github.com/Sceptre/sceptre.git /home/sceptre

WORKDIR /home/sceptre
RUN git reset --hard
RUN git checkout {pr.base.sha}

# Install project dependencies
RUN sed -i 's/moto==0.4.31/moto>=1.3.0/g' requirements.txt && \
    pip install -r requirements.txt && \
    sed -i 's/moto==0.4.31/moto>=1.0.0/' setup.py && \
    pip install .[test]
"""
        dockerfile_content += f"""
{copy_commands}
"""
        return dockerfile_content.format(pr=self.pr)


@Instance.register("Sceptre", "sceptre_v1_4_2")
class SCEPTRE_V1_4_2(Instance):
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
        # Regex to capture the file path and the test result markers
        test_line_re = re.compile(r"^(tests/.*?\.py) (.*)")
        # Regex to capture the full test name from the FAILED summary
        failed_test_re = re.compile(r"^FAILED (.*?)$")
        for line in log.splitlines():
            # Check for lines indicating test results
            match = test_line_re.match(line)
            if match:
                test_file, results = match.groups()
                if "F" in results:
                    # Will be captured by the failed_test_re
                    continue
                if "s" in results:
                    skipped_tests.add(test_file)
                    continue
                if all(c == "." for c in results.strip()):
                    passed_tests.add(test_file)
            # Check for the failed test summary
            match = failed_test_re.match(line)
            if match:
                failed_test = match.group(1)
                failed_tests.add(failed_test)
                # Remove the file from passed_tests if it's there
                test_file = failed_test.split("::")[0]
                if test_file in passed_tests:
                    passed_tests.remove(test_file)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
