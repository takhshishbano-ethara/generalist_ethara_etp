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
        return "ubuntu:22.04"

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
python3 make_env.py full -o environment.yml
###ACTION_DELIMITER###
apt-get update && apt-get install -y python3 python3-pip
###ACTION_DELIMITER###
python3 make_env.py full -o environment.yml
###ACTION_DELIMITER###
pip install tomli
###ACTION_DELIMITER###
python3 make_env.py full -o environment.yml
###ACTION_DELIMITER###
wget https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh -O miniconda.sh && bash miniconda.sh -b -p /opt/conda && export PATH="/opt/conda/bin:$PATH"
###ACTION_DELIMITER###
apt-get install -y wget
###ACTION_DELIMITER###
wget https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh -O miniconda.sh && bash miniconda.sh -b -p /opt/conda && export PATH="/opt/conda/bin:$PATH"
###ACTION_DELIMITER###
export PATH="/opt/conda/bin:$PATH" && conda env create -f environment.yml
###ACTION_DELIMITER###
export PATH="/opt/conda/bin:$PATH" && conda run -n hydromt pip install 'pytest>=6.2.0'
###ACTION_DELIMITER###
export PATH="/opt/conda/bin:$PATH" && conda run -n hydromt pip install -e .
###ACTION_DELIMITER###
echo 'conda run -n hydromt pytest --no-header -rA --tb=no -p no:cacheprovider' > /home/hydromt/test_commands.sh
###ACTION_DELIMITER###
bash /home/hydromt/test_commands.sh
###ACTION_DELIMITER###
echo 'PYTEST_ADDOPTS="--timeout=120" conda run -n hydromt pytest --no-header -rA --tb=no -p no:cacheprovider' > /home/hydromt/test_commands.sh
###ACTION_DELIMITER###
bash /home/hydromt/test_commands.sh
###ACTION_DELIMITER###
echo 'conda run -n hydromt pytest -o addopts="" --no-header -rA --tb=no -p no:cacheprovider' > /home/hydromt/test_commands.sh
###ACTION_DELIMITER###
bash /home/hydromt/test_commands.sh""",
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
conda run -n hydromt pytest -o addopts="" --no-header -rA --tb=no -p no:cacheprovider

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
conda run -n hydromt pytest -o addopts="" --no-header -rA --tb=no -p no:cacheprovider

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
conda run -n hydromt pytest -o addopts="" --no-header -rA --tb=no -p no:cacheprovider

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
FROM ubuntu:22.04

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
RUN git clone https://github.com/Deltares/hydromt.git /home/hydromt

WORKDIR /home/hydromt
RUN git reset --hard
RUN git checkout {pr.base.sha}
"""
        dockerfile_content += f"""
{copy_commands}
"""
        return dockerfile_content.format(pr=self.pr)


@Instance.register("Deltares", "hydromt_v0_9_0")
class HYDROMT_V0_9_0(Instance):
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
        # Patterns for test results
        # PASSED tests/test_file.py::test_name
        passed_pattern = re.compile(r"^PASSED (\S+::\S+)", re.MULTILINE)
        # FAILED tests/test_file.py::test_name - ...
        failed_pattern = re.compile(r"^FAILED (\S+::\S+)", re.MULTILINE)
        # ERROR tests/test_file.py::test_name - ...
        error_pattern = re.compile(r"^ERROR (\S+::\S+)", re.MULTILINE)
        # ERROR tests/test_file.py (file-level error)
        error_file_pattern = re.compile(r"^ERROR (\S+\.py)", re.MULTILINE)
        # SKIPPED tests/test_file.py::test_name
        skipped_pattern = re.compile(r"^SKIPPED (\S+::\S+)", re.MULTILINE)
        passed_tests.update(passed_pattern.findall(log))
        failed_tests.update(failed_pattern.findall(log))
        failed_tests.update(error_pattern.findall(log))
        # For file-level errors, add the file name as the test name
        for fname in error_file_pattern.findall(log):
            failed_tests.add(fname)
        skipped_tests.update(skipped_pattern.findall(log))

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
