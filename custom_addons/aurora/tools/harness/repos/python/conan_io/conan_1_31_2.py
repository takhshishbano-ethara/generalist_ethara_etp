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
                """
###ACTION_DELIMITER###
python --version && pip --version
###ACTION_DELIMITER###
pip install 'markupsafe<2.1.0'
###ACTION_DELIMITER###
pip install -r conans/requirements.txt -r conans/requirements_server.txt -r conans/requirements_dev.txt
###ACTION_DELIMITER###
echo 'tox -e full' > /home/conan/test_commands.sh
###ACTION_DELIMITER###
cat /home/conan/test_commands.sh
###ACTION_DELIMITER###
bash /home/conan/test_commands.sh
###ACTION_DELIMITER###
pip show tox
###ACTION_DELIMITER###
pip install tox
###ACTION_DELIMITER###
bash /home/conan/test_commands.sh
###ACTION_DELIMITER###
echo 'markupsafe<2.1.0' >> conans/requirements.txt
###ACTION_DELIMITER###
tox --recreate -e full""",
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
tox -e full

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
tox -e full

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
tox -e full

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
RUN git checkout 201480146740d2ab298b9f360037ab9e56c2a6a5

RUN git checkout {pr.base.sha}"""
        dockerfile_content += f"""
{copy_commands}
"""
        return dockerfile_content.format(pr=self.pr)


@Instance.register("conan-io", "conan_1_31_2")
class CONAN_1_31_2(Instance):
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
        Parses a pytest log and extracts test results.
        Returns a dict with sets of test names for passed, failed, and skipped tests.
        """
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()
        pseudo_failed = set()
        # 1. Parse progress lines (file-level, pseudo test names)
        progress_re = re.compile(r"^(\S+\.py)\s+([.sFx]+)")
        for line in log.splitlines():
            m = progress_re.match(line)
            if m:
                test_file = m.group(1)
                status_str = m.group(2)
                for idx, ch in enumerate(status_str):
                    pseudo_name = f"{test_file}::testcase_{idx + 1}"
                    if ch == ".":
                        passed_tests.add(pseudo_name)
                    elif ch == "s":
                        skipped_tests.add(pseudo_name)
                    elif ch == "F":
                        pseudo_failed.add(pseudo_name)
                    elif ch == "x":
                        skipped_tests.add(pseudo_name)  # treat xfail/xpass as skipped
        # 2. Parse failure summary for real test names
        for line in log.splitlines():
            if line.startswith("FAILED "):
                test_full = line[7:].split(" - ")[0].strip()
                failed_tests.add(test_full)
        # Remove pseudo-names for failed tests if real names are available
        if failed_tests:
            pseudo_failed = {n for n in pseudo_failed if n not in failed_tests}
            failed_tests = failed_tests.union(pseudo_failed)
        else:
            failed_tests = pseudo_failed

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
