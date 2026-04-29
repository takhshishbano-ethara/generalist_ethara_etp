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
                """ls -al
###ACTION_DELIMITER###
python3 --version && pip3 --version
###ACTION_DELIMITER###
pip3 install -r conans/requirements_dev.txt
###ACTION_DELIMITER###
echo 'nosetests --with-coverage conans.test --verbosity=2 --processes=4 --process-timeout=1000' > /home/conan/test_commands.sh && chmod +x /home/conan/test_commands.sh
###ACTION_DELIMITER###
bash /home/conan/test_commands.sh
###ACTION_DELIMITER###
pip3 install patch
###ACTION_DELIMITER###
bash /home/conan/test_commands.sh
###ACTION_DELIMITER###
pip3 install colorama
###ACTION_DELIMITER###
bash /home/conan/test_commands.sh
###ACTION_DELIMITER###
pip3 install fasteners
###ACTION_DELIMITER###
bash /home/conan/test_commands.sh
###ACTION_DELIMITER###
pip3 install distro
###ACTION_DELIMITER###
bash /home/conan/test_commands.sh
###ACTION_DELIMITER###
pip3 install PyYAML
###ACTION_DELIMITER###
bash /home/conan/test_commands.sh
###ACTION_DELIMITER###
pip3 install pylint
###ACTION_DELIMITER###
bash /home/conan/test_commands.sh
###ACTION_DELIMITER###
pip3 uninstall -y pylint && pip3 install pylint==2.7.4
###ACTION_DELIMITER###
bash /home/conan/test_commands.sh
###ACTION_DELIMITER###
pip3 uninstall -y pylint && pip3 install pylint==2.4.4
###ACTION_DELIMITER###
bash /home/conan/test_commands.sh""",
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
nosetests --with-coverage conans.test --verbosity=2 --processes=4 --process-timeout=1000

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
nosetests --with-coverage conans.test --verbosity=2 --processes=4 --process-timeout=1000

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
nosetests --with-coverage conans.test --verbosity=2 --processes=4 --process-timeout=1000

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
RUN apt-get update && apt-get install -y git

# Ensure bash is available
RUN if [ ! -f /bin/bash ]; then         if command -v apk >/dev/null 2>&1; then             apk add --no-cache bash;         elif command -v apt-get >/dev/null 2>&1; then             apt-get update && apt-get install -y bash;         elif command -v yum >/dev/null 2>&1; then             yum install -y bash;         else             exit 1;         fi     fi

WORKDIR /home/
COPY fix.patch /home/
COPY test.patch /home/
RUN git clone https://github.com/conan-io/conan.git /home/conan

WORKDIR /home/conan
RUN git reset --hard
RUN git checkout bd9075246911f9a3e622ab2eaab7c13054cc3b35

RUN git checkout {pr.base.sha}"""
        dockerfile_content += f"""
{copy_commands}
"""
        return dockerfile_content.format(pr=self.pr)


@Instance.register("conan-io", "conan_1_0_2")
class CONAN_1_0_2(Instance):
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
        # Implement the log parsing logic here
        # Regex patterns for test results
        pass_pattern = re.compile(r"^(.+?) \((.+?)\) \.\.\. ok$")
        fail_pattern = re.compile(r"^(.+?) \((.+?)\) \.\.\. FAIL$")
        fail_alt_pattern = re.compile(r"^FAIL: (.+?) \((.+?)\)$")
        skip_pattern = re.compile(r"^(.+?) \((.+?)\) \.\.\. skipped$", re.IGNORECASE)
        for line in log.splitlines():
            line = line.strip()
            m_pass = pass_pattern.match(line)
            m_fail = fail_pattern.match(line)
            m_fail_alt = fail_alt_pattern.match(line)
            m_skip = skip_pattern.match(line)
            if m_pass:
                test_name = m_pass.group(1).strip()
                passed_tests.add(test_name)
            elif m_fail:
                test_name = m_fail.group(1).strip()
                failed_tests.add(test_name)
            elif m_fail_alt:
                test_name = m_fail_alt.group(1).strip()
                failed_tests.add(test_name)
            elif m_skip:
                test_name = m_skip.group(1).strip()
                skipped_tests.add(test_name)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
