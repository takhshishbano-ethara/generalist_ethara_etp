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
        return "python:3.5"

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
python3 --version
###ACTION_DELIMITER###
pip install 'astropy==3.2.3' 'numpy' 'scipy' 'astroscrappy' 'reproject' 'pytest' 'pytest-astropy'
###ACTION_DELIMITER###
echo 'python setup.py test' > /home/ccdproc/test_commands.sh && chmod +x /home/ccdproc/test_commands.sh
###ACTION_DELIMITER###
bash /home/ccdproc/test_commands.sh
###ACTION_DELIMITER###
python --version
###ACTION_DELIMITER###
python -c 'import astropy; print(astropy.__version__)'
###ACTION_DELIMITER###
python -c 'import numpy; import scipy; import astroscrappy; import reproject; import pytest; import pytest_astropy; print("All imports successful")'
###ACTION_DELIMITER###
pip list
###ACTION_DELIMITER###
python -c 'import pytest_astropy; print(pytest_astropy.__version__)'
###ACTION_DELIMITER###
python setup.py test
###ACTION_DELIMITER###
pip install 'pytest==5.4.3'
###ACTION_DELIMITER###
python setup.py test
###ACTION_DELIMITER###
pip install 'pytest==4.6.11'
###ACTION_DELIMITER###
python setup.py test
###ACTION_DELIMITER###
pip install 'pytest-astropy==0.6.0' 'pytest-doctestplus==0.4.0' 'pytest-openfiles==0.3.2' 'pytest-remotedata==0.3.1'
###ACTION_DELIMITER###
python setup.py test
###ACTION_DELIMITER###
sed -i 's/\[pytest\]/[tool:pytest]/' setup.cfg
###ACTION_DELIMITER###
python setup.py test
###ACTION_DELIMITER###
pip install 'astropy[tests]==3.2.3'
###ACTION_DELIMITER###
pip install 'astropy==2.0.16'
###ACTION_DELIMITER###
python setup.py test
###ACTION_DELIMITER###
python -c 'from astropy.utils.compat import gzip; print(gzip)'
###ACTION_DELIMITER###
pip install 'astropy==1.3.2'
###ACTION_DELIMITER###
python -c 'from astropy.utils.compat import gzip; print(gzip)'
###ACTION_DELIMITER###
python -c 'from astropy.tests.pytest_plugins import *; print("OK")'
###ACTION_DELIMITER###
python setup.py test
###ACTION_DELIMITER###

###ACTION_DELIMITER###
pip install 'pytest==3.1.3'
###ACTION_DELIMITER###
python setup.py test
###ACTION_DELIMITER###
pip uninstall -y pytest-remotedata
###ACTION_DELIMITER###
python setup.py test
###ACTION_DELIMITER###
pip uninstall -y pytest-astropy-header pytest-astropy pytest-doctestplus pytest-openfiles
###ACTION_DELIMITER###
python setup.py test""",
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
python setup.py test

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
python setup.py test

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
python setup.py test

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
FROM python:3.5

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
RUN git clone https://github.com/astropy/ccdproc.git /home/ccdproc

WORKDIR /home/ccdproc
RUN git reset --hard
RUN git checkout {pr.base.sha}
"""
        dockerfile_content += f"""
{copy_commands}
"""
        return dockerfile_content.format(pr=self.pr)


@Instance.register("astropy", "ccdproc_v1_0")
class CCDPROC_V1_0(Instance):
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
        # Implementation: parse pytest log output
        # 1. Find summary lines (e.g. ccdproc/tests/test_ccddata.py ..........FFF..)
        # 2. Map dots (.) to pass, F to fail, s/S to skip
        # 3. Extract failed test names from FAILURES section
        summary_re = re.compile(r"^(\S+\.py)\s+([.F sS]+)$")
        failure_header_re = re.compile(r"^=+ FAILURES =+$")
        fail_name_re = re.compile(r"^_{2,}\s*([\w\d_]+)\s*_{2,}$")
        lines = log.splitlines()
        in_failures = False
        failed_test_names = set()
        for i, line in enumerate(lines):
            # Check for summary lines
            m = summary_re.match(line)
            if m:
                filename, results = m.groups()
                for idx, ch in enumerate(results):
                    test_id = f"{filename}::{idx + 1}"
                    if ch == ".":
                        passed_tests.add(test_id)
                    elif ch in "F":
                        failed_tests.add(test_id)
                    elif ch in "sS":
                        skipped_tests.add(test_id)
            # Check for start of failures section
            if failure_header_re.match(line):
                in_failures = True
                continue
            if in_failures:
                # Find failed test names
                m2 = fail_name_re.match(line)
                if m2:
                    failed_test_names.add(m2.group(1))
                # End of failures section
                if line.strip().startswith("=") and "FAILURES" not in line:
                    in_failures = False
        # Try to match failed test names to summary test ids
        for fail_name in failed_test_names:
            for test_id in list(failed_tests):
                if fail_name in test_id:
                    break
            else:
                failed_tests.add(fail_name)
        # Remove failed tests from passed/skipped if overlap
        passed_tests -= failed_tests
        skipped_tests -= failed_tests
        # TODO: Implement the parse_log function
        # Implement the log parsing logic here

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
