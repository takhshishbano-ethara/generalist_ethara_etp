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
        return "python:3.11-slim"

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
                """ls -l conans
###ACTION_DELIMITER###
pip install --upgrade pip setuptools wheel && pip install -r conans/requirements.txt && pip install -r conans/requirements_dev.txt && pip install pbr
###ACTION_DELIMITER###
pip install cython
###ACTION_DELIMITER###
pip install -r conans/requirements.txt && pip install -r conans/requirements_dev.txt && pip install pbr
###ACTION_DELIMITER###
pip install 'PyYAML==5.4.1'
###ACTION_DELIMITER###
pip install --only-binary=:all: 'PyYAML<6.0'
###ACTION_DELIMITER###
apt-get update && apt-get install -y python3-yaml
###ACTION_DELIMITER###
pip install --no-deps -r conans/requirements.txt && pip install --no-deps -r conans/requirements_dev.txt && pip install pbr
###ACTION_DELIMITER###
sed -i 's/^PyYAML/#PyYAML/' conans/requirements.txt
###ACTION_DELIMITER###
pip install -r conans/requirements.txt && pip install -r conans/requirements_dev.txt && pip install pbr
###ACTION_DELIMITER###
sed -i 's/^#PyYAML/PyYAML/' conans/requirements.txt
###ACTION_DELIMITER###
pip install -e .
###ACTION_DELIMITER###
sed -i 's/^PyYAML/#PyYAML/' conans/requirements.txt
###ACTION_DELIMITER###
pip install -e . --no-deps
###ACTION_DELIMITER###
sed -i 's/^#PyYAML/PyYAML/' conans/requirements.txt
###ACTION_DELIMITER###
echo 'pytest -m "not slow and not tool_svn" -v' > /home/conan/test_commands.sh && chmod +x /home/conan/test_commands.sh
###ACTION_DELIMITER###
bash /home/conan/test_commands.sh
###ACTION_DELIMITER###
sed -i 's/PyYAML>=3.11, <6.0/PyYAML>=3.11,<7.0/' conans/requirements.txt
###ACTION_DELIMITER###
pip install 'PyYAML>=3.11,<7.0'
###ACTION_DELIMITER###
bash /home/conan/test_commands.sh
###ACTION_DELIMITER###
pytest -v conans/test/unittests/model/conanfile_test.py
###ACTION_DELIMITER###
pip install 'markupsafe<2.1.0'
###ACTION_DELIMITER###
bash /home/conan/test_commands.sh""",
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
pytest -m "not slow and not tool_svn" -v

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
pytest -m "not slow and not tool_svn" -v

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
pytest -m "not slow and not tool_svn" -v

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
FROM python:3.11-slim

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
RUN git checkout d42eb3f2e1c3ea3b6b6e9e4a9848f1ea6476ed0d

RUN git checkout {pr.base.sha}"""
        dockerfile_content += f"""
{copy_commands}
"""
        return dockerfile_content.format(pr=self.pr)


@Instance.register("conan-io", "conan_1_31_4")
class CONAN_1_31_4(Instance):
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
        # Regex to match test result lines
        # Example: conans/test/functional/system_reqs_test.py::SystemReqsTest::test_permission_denied_remove_system_reqs FAILED [  6%]
        test_line_re = re.compile(
            r"^(.*?)\s+(PASSED|FAILED|SKIPPED|XFAIL|XPASS|ERROR|xfail|xpass|skipped|passed|failed)\b"
        )
        for line in log.splitlines():
            m = test_line_re.match(line)
            if m:
                test_name, status = m.group(1), m.group(2).upper()
                # Clean up test name (remove trailing whitespace)
                test_name = test_name.strip()
                if status == "PASSED":
                    passed_tests.add(test_name)
                elif status == "FAILED" or status == "ERROR":
                    failed_tests.add(test_name)
                elif status == "SKIPPED":
                    skipped_tests.add(test_name)
                # XFAIL/XPASS are expected fail/pass, not counted as regular pass/fail/skip
                # If needed, can be added as separate sets

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
