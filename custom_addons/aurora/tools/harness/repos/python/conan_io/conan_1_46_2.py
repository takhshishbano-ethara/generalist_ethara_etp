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
pip install .
###ACTION_DELIMITER###
pip install -r conans/requirements_dev.txt
###ACTION_DELIMITER###
pytest
###ACTION_DELIMITER###
pytest -n 4
###ACTION_DELIMITER###
pytest -m "not slow and not tool_svn"
###ACTION_DELIMITER###
apt-get update && apt-get install -y subversion
###ACTION_DELIMITER###
pytest
###ACTION_DELIMITER###
pytest -n 4 -m "not slow and not tool_svn"
###ACTION_DELIMITER###
apt-get install -y cmake bazel meson pkg-config
###ACTION_DELIMITER###
apt-get install -y cmake meson pkg-config
###ACTION_DELIMITER###

###ACTION_DELIMITER###
pytest -n 4 -m "not slow and not tool_svn"
###ACTION_DELIMITER###
apt-get install -y autotools-dev
###ACTION_DELIMITER###
pytest -n 4 -m "not slow and not tool_svn and not bazel"
###ACTION_DELIMITER###
echo 'pytest --no-header -rA --tb=no -p no:cacheprovider -m "not slow and not tool_svn and not bazel"' > /home/conan/test_commands.sh""",
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
pytest --no-header -rA --tb=no -p no:cacheprovider -m "not slow and not tool_svn and not bazel"

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
pytest --no-header -rA --tb=no -p no:cacheprovider -m "not slow and not tool_svn and not bazel"

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
pytest --no-header -rA --tb=no -p no:cacheprovider -m "not slow and not tool_svn and not bazel"

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
RUN git clone https://github.com/conan-io/conan.git /home/conan

WORKDIR /home/conan
RUN git reset --hard
RUN git checkout 255044586ae8d9666bb8449739641ff857b38e89

RUN git checkout {pr.base.sha}"""
        dockerfile_content += f"""
{copy_commands}
"""
        return dockerfile_content.format(pr=self.pr)


@Instance.register("conan-io", "conan_1_46_2")
class CONAN_1_46_2(Instance):
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
        # Implement the log parsing logic here
        failed_regex = re.compile(r"^FAILED (.*)$", re.MULTILINE)
        for match in failed_regex.finditer(log):
            test_name = match.group(1).strip()
            if " - " in test_name:
                test_name = test_name.split(" - ")[0]
            failed_tests.add(test_name)
        skipped_regex = re.compile(
            r"^SKIPPED(?: \[[^]]+\])? (.*?):\d+:.*$", re.MULTILINE
        )
        for match in skipped_regex.finditer(log):
            skipped_tests.add(match.group(1).strip())
        # Look for passed tests in the summary, which is not always present
        passed_regex = re.compile(r"^PASSED (.*)$", re.MULTILINE)
        for match in passed_regex.finditer(log):
            passed_tests.add(match.group(1).strip())
        if not passed_tests:
            # If no explicit PASSED summary, we need to get creative.
            # Let's find all tests and remove the failed and skipped ones.
            all_tests_regex = re.compile(
                r"^(conans/test/.*?\.py::.*?)(?: |$)", re.MULTILINE
            )
            all_tests = set()
            for match in all_tests_regex.finditer(log):
                all_tests.add(match.group(1).strip())
            # Now, try to find all tests mentioned in progress lines
            progress_tests_regex = re.compile(r"^(conans/test/.*\.py) ", re.MULTILINE)
            for match in progress_tests_regex.finditer(log):
                all_tests.add(match.group(1).strip())
            passed_tests.update(all_tests - failed_tests - skipped_tests)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
