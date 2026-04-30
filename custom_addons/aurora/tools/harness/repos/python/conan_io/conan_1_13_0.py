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

    def dependency(self) -> Image | None:
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
                """ls -la
###ACTION_DELIMITER###
pip install 'markupsafe<2.1.0'
###ACTION_DELIMITER###
pip install -e . && pip install -r conans/requirements_dev.txt
###ACTION_DELIMITER###
pip install -r conans/requirements.txt && pip install -r conans/requirements_server.txt && pip install -r conans/requirements_osx.txt
###ACTION_DELIMITER###
echo 'nosetests -v .' > /home/conan/test_commands.sh
###ACTION_DELIMITER###
chmod +x /home/conan/test_commands.sh && bash /home/conan/test_commands.sh
###ACTION_DELIMITER###
apt-get update && apt-get install -y gcc-multilib g++-multilib wget unzip pkg-config cmake
###ACTION_DELIMITER###
wget https://github.com/ninja-build/ninja/releases/download/v1.8.2/ninja-linux.zip && unzip ninja-linux.zip && mv ninja /usr/bin/ninja && rm ninja-linux.zip
###ACTION_DELIMITER###
pip3 install meson
###ACTION_DELIMITER###
bash /home/conan/test_commands.sh""",
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
nosetests -v .

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
nosetests -v .

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
nosetests -v .

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
RUN git clone https://github.com/conan-io/conan.git /home/conan

WORKDIR /home/conan
RUN git reset --hard
RUN git checkout 1e66f46eab52f218eb1e5d011cb659530da60c2d

RUN git checkout {pr.base.sha}"""
        dockerfile_content += f"""
{copy_commands}
"""
        return dockerfile_content.format(pr=self.pr)


@Instance.register("conan-io", "conan_1_13_0")
class CONAN_1_13_0(Instance):
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
        """
        Parse the log content and extract test execution results.
        Returns a dict with sets of test names for passed, failed, and skipped tests.
        Only the last status for each test is considered.
        """
        # More robust regex for test result lines
        # Example: test_name (test.class.Name) ... ok
        test_line_re = re.compile(
            r"^([\w\-\.\/:,\[\]<>@#\$%\^&\*\+=~'\" ]+) \([\w\-\.\/:,\[\]<>@#\$%\^&\*\+=~'\" ]+\) \.\.\. (ok|FAIL|ERROR|SKIP: .*)$"
        )
        test_status = {}  # test_name -> status
        for line in log.splitlines():
            match = test_line_re.match(line)
            if match:
                test_name = match.group(1).strip()
                status = match.group(2)
                # Only keep the last status for each test
                if status == "ok":
                    test_status[test_name] = "passed"
                elif status == "FAIL" or status == "ERROR":
                    test_status[test_name] = "failed"
                elif status.startswith("SKIP"):
                    test_status[test_name] = "skipped"
        passed_tests = {
            name for name, status in test_status.items() if status == "passed"
        }
        failed_tests = {
            name for name, status in test_status.items() if status == "failed"
        }
        skipped_tests = {
            name for name, status in test_status.items() if status == "skipped"
        }

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
