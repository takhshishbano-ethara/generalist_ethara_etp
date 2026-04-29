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
        return "python:3.9"

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
sudo apt-get update && sudo apt-get install -y spatialite-bin libsqlite3-mod-spatialite
###ACTION_DELIMITER###
apt-get update && apt-get install -y spatialite-bin libsqlite3-mod-spatialite
###ACTION_DELIMITER###
pip install -r requirements.txt
###ACTION_DELIMITER###
pip install -r requirements_dev.txt
###ACTION_DELIMITER###

###ACTION_DELIMITER###
pip install -e .
###ACTION_DELIMITER###
pytest tests/
###ACTION_DELIMITER###
pip install numpy==1.26.4
###ACTION_DELIMITER###
pytest tests/
###ACTION_DELIMITER###
pip install SQLAlchemy==1.4.22
###ACTION_DELIMITER###
pytest tests/
###ACTION_DELIMITER###
pip install geoalchemy2==0.10.0
###ACTION_DELIMITER###
pytest --no-header -rA --tb=no -p no:cacheprovider
###ACTION_DELIMITER###
apt-get install -y postgresql postgresql-contrib postgis
###ACTION_DELIMITER###
pytest --no-header -rA --tb=no -p no:cacheprovider
###ACTION_DELIMITER###
pytest tests/ -m "not postgres"
###ACTION_DELIMITER###
echo 'pytest --no-header -rA --tb=no -p no:cacheprovider' > test_commands.sh
###ACTION_DELIMITER###
""",
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
pytest --no-header -rA --tb=no -p no:cacheprovider

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
pytest --no-header -rA --tb=no -p no:cacheprovider

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
pytest --no-header -rA --tb=no -p no:cacheprovider

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
FROM python:3.9

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
RUN git clone https://github.com/debrief/pepys-import.git /home/pepys-import

WORKDIR /home/pepys-import
RUN git reset --hard
RUN git checkout {pr.base.sha}
"""
        dockerfile_content += f"""
{copy_commands}
"""
        return dockerfile_content.format(pr=self.pr)


@Instance.register("debrief", "pepys-import_0_0_33")
class PEPYS_IMPORT_0_0_33(Instance):
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
        # Regex to find all test files mentioned in the log
        all_tests_re = re.compile(r"(tests/.*?\.py)")
        all_tests = set(all_tests_re.findall(log))
        # Regex to find failed test files
        failed_tests_re = re.compile(r"^FAILED (.*?)(::.*)?$", re.MULTILINE)
        for match in failed_tests_re.finditer(log):
            failed_tests.add(match.group(1).strip())
        # Regex to find failed test files from the progress bar
        failed_progress_re = re.compile(r"^(tests/.*?\.py) .*?F.*$", re.MULTILINE)
        for match in failed_progress_re.finditer(log):
            failed_tests.add(match.group(1).strip())
        # Regex to find skipped test files
        # SKIPPED [1] tests/gui_tests/test_gui_interactively.py:25: Skipped because pytest was not run with -s option
        skipped_tests_re = re.compile(r"^SKIPPED \[\d+\] (.*?):\d+:.*$", re.MULTILINE)
        for match in skipped_tests_re.finditer(log):
            skipped_tests.add(match.group(1).strip())
        passed_tests = all_tests - failed_tests - skipped_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
