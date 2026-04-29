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
                """ls -F
###ACTION_DELIMITER###
python -m pip install -r conans/requirements.txt
###ACTION_DELIMITER###
python -m pip install -r conans/requirements_server.txt
###ACTION_DELIMITER###
python -m pip install -r conans/requirements_dev.txt
###ACTION_DELIMITER###
pip install -e .
###ACTION_DELIMITER###
ls -F test/
###ACTION_DELIMITER###
export PYTHONPATH=$PYTHONPATH:$(pwd)
###ACTION_DELIMITER###
python -m pytest test/ --tb=no -p no:cacheprovider -rA
###ACTION_DELIMITER###
echo "python -m pytest test/ --tb=no -p no:cacheprovider -rA" > test_commands.sh""",
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
python -m pytest test/ --tb=no -p no:cacheprovider -rA

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
python -m pytest test/ --tb=no -p no:cacheprovider -rA

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
python -m pytest test/ --tb=no -p no:cacheprovider -rA

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
RUN apt-get update && apt-get install -y --no-install-recommends git

# Ensure bash is available
RUN if [ ! -f /bin/bash ]; then         if command -v apk >/dev/null 2>&1; then             apk add --no-cache bash;         elif command -v apt-get >/dev/null 2>&1; then             apt-get update && apt-get install -y bash;         elif command -v yum >/dev/null 2>&1; then             yum install -y bash;         else             exit 1;         fi     fi

WORKDIR /home/
COPY fix.patch /home/
COPY test.patch /home/
RUN git clone https://github.com/conan-io/conan.git /home/conan

WORKDIR /home/conan
RUN git reset --hard
RUN git checkout eb95f5b9472f8d75888d0ffcecba32e17a882423

RUN git checkout {pr.base.sha}"""
        dockerfile_content += f"""
{copy_commands}
"""
        return dockerfile_content.format(pr=self.pr)


@Instance.register("conan-io", "conan_2_11_0")
class CONAN_2_11_0(Instance):
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
        # This pattern will match lines that represent a test file and the results of the tests in that file
        # e.g. test/functional/only_source_test.py ....                                 [  0%]
        re.compile(r"^(test/.*?\.py) (\.+|s+|F+|E+)")
        # This pattern will match lines that show the full result of a test, usually at the end of the log
        # e.g. FAILED test/functional/test_local_recipes_index.py::TestLocalRecipeIndexNew::test_conan_new_local_recipes_index
        full_test_result_pattern = re.compile(r"^(PASSED|FAILED|SKIPPED|ERROR) (.*)")
        # This pattern will match lines that show a test file and a result, but without the dots, etc.
        # e.g. ERROR test/functional/tools_versions_test.py
        file_level_result_pattern = re.compile(
            r"^(PASSED|FAILED|SKIPPED|ERROR) (test/.*?\.py)$"
        )
        for line in log.splitlines():
            # First, check for full test results, as they are the most reliable
            match = full_test_result_pattern.match(line)
            if match:
                status = match.group(1).strip()
                test_name = match.group(2).strip().split(" - ")[0]
                if status == "PASSED":
                    passed_tests.add(test_name)
                elif status in ("FAILED", "ERROR"):
                    failed_tests.add(test_name)
                elif status == "SKIPPED":
                    skipped_tests.add(test_name)
                continue
            # Then, check for file-level results
            match = file_level_result_pattern.match(line)
            if match:
                status = match.group(1).strip()
                test_file = match.group(2).strip()
                # We don't have the specific test name, so we will mark the whole file as failed
                if status in ("FAILED", "ERROR"):
                    failed_tests.add(test_file)
                continue
        # Finally, go through the log again to find all tests and assume they passed unless they are in the failed or skipped sets
        for line in log.splitlines():
            if line.startswith("test/") and "::" in line and not line.endswith(":"):
                test_name = line.strip().split(" - ")[0]
                if test_name not in failed_tests and test_name not in skipped_tests:
                    passed_tests.add(test_name)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
