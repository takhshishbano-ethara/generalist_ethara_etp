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
        return "python:3.10-slim"

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
ls -al test
###ACTION_DELIMITER###
pip install -r requirements.txt
###ACTION_DELIMITER###
pip install pytorch==1.3.0 setuptools==41.4.0 torchvision==0.4.1 opencv==4.1.1 ipython==7.8.0 jupyter==1.0.0 matplotlib==3.1.1 pytest==5.1.2 pytest-cov==2.7.1 flake8==3.7.8 autopep8==1.4.4 mypy==0.720 mypy_extensions==0.4.1 sphinx==2.1.2 sphinx-gallery==0.4.0 sphinx_rtd_theme==0.4.3 sphinxcontrib-bibtex==0.4.2 nbsphinx==0.4.2 nbconvert==5.5.0 notebook==6.0.1
###ACTION_DELIMITER###
pip install torch==1.3.0 setuptools==41.4.0 torchvision==0.4.1 opencv==4.1.1 ipython==7.8.0 jupyter==1.0.0 matplotlib==3.1.1 pytest==5.1.2 pytest-cov==2.7.1 flake8==3.7.8 autopep8==1.4.4 mypy==0.720 mypy_extensions==0.4.1 sphinx==2.1.2 sphinx-gallery==0.4.0 sphinx_rtd_theme==0.4.3 sphinxcontrib-bibtex==0.4.2 nbsphinx==0.4.2 nbconvert==5.5.0 notebook==6.0.1
###ACTION_DELIMITER###
pip install torch==1.11.0 setuptools==41.4.0 torchvision==0.4.1 opencv==4.1.1 ipython==7.8.0 jupyter==1.0.0 matplotlib==3.1.1 pytest==5.1.2 pytest-cov==2.7.1 flake8==3.7.8 autopep8==1.4.4 mypy==0.720 mypy_extensions==0.4.1 sphinx==2.1.2 sphinx-gallery==0.4.0 sphinx_rtd_theme==0.4.3 sphinxcontrib-bibtex==0.4.2 nbsphinx==0.4.2 nbconvert==5.5.0 notebook==6.0.1
###ACTION_DELIMITER###
pip install torch==1.11.0 setuptools==41.4.0 torchvision==0.12.0 opencv==4.1.1 ipython==7.8.0 jupyter==1.0.0 matplotlib==3.1.1 pytest==5.1.2 pytest-cov==2.7.1 flake8==3.7.8 autopep8==1.4.4 mypy==0.720 mypy_extensions==0.4.1 sphinx==2.1.2 sphinx-gallery==0.4.0 sphinx_rtd_theme==0.4.3 sphinxcontrib-bibtex==0.4.2 nbsphinx==0.4.2 nbconvert==5.5.0 notebook==6.0.1
###ACTION_DELIMITER###
pip install torch==1.11.0 setuptools==41.4.0 torchvision==0.12.0 opencv-python==4.1.1.26 ipython==7.8.0 jupyter==1.0.0 matplotlib==3.1.1 pytest==5.1.2 pytest-cov==2.7.1 flake8==3.7.8 autopep8==1.4.4 mypy==0.720 mypy_extensions==0.4.1 sphinx==2.1.2 sphinx-gallery==0.4.0 sphinx_rtd_theme==0.4.3 sphinxcontrib-bibtex==0.4.2 nbsphinx==0.4.2 nbconvert==5.5.0 notebook==6.0.1
###ACTION_DELIMITER###
pip install torch==1.11.0 setuptools==41.4.0 torchvision==0.12.0 opencv-python==4.3.0.38 ipython==7.8.0 jupyter==1.0.0 matplotlib==3.1.1 pytest==5.1.2 pytest-cov==2.7.1 flake8==3.7.8 autopep8==1.4.4 mypy==0.720 mypy_extensions==0.4.1 sphinx==2.1.2 sphinx-gallery==0.4.0 sphinx_rtd_theme==0.4.3 sphinxcontrib-bibtex==0.4.2 nbsphinx==0.4.2 nbconvert==5.5.0 notebook==6.0.1
###ACTION_DELIMITER###
apt-get update && apt-get install -y build-essential
###ACTION_DELIMITER###
pip install torch==1.11.0 setuptools==41.4.0 torchvision==0.12.0 opencv-python==4.3.0.38 ipython==7.8.0 jupyter==1.0.0 matplotlib==3.1.1 pytest==5.1.2 pytest-cov==2.7.1 flake8==3.7.8 autopep8==1.4.4 mypy==0.720 mypy_extensions==0.4.1 sphinx==2.1.2 sphinx-gallery==0.4.0 sphinx_rtd_theme==0.4.3 sphinxcontrib-bibtex==0.4.2 nbsphinx==0.4.2 nbconvert==5.5.0 notebook==6.0.1
###ACTION_DELIMITER###
pip install torch==1.11.0 setuptools==41.4.0 torchvision==0.12.0 opencv-python==4.7.0.72 ipython==7.8.0 jupyter==1.0.0 matplotlib==3.1.1 pytest==5.1.2 pytest-cov==2.7.1 flake8==3.7.8 autopep8==1.4.4 mypy==0.720 mypy_extensions==0.4.1 sphinx==2.1.2 sphinx-gallery==0.4.0 sphinx_rtd_theme==0.4.3 sphinxcontrib-bibtex==0.4.2 nbsphinx==0.4.2 nbconvert==5.5.0 notebook==6.0.1
###ACTION_DELIMITER###
apt-get install -y libfreetype6-dev libpng-dev pkg-config
###ACTION_DELIMITER###
pip install torch==1.11.0 setuptools==41.4.0 torchvision==0.12.0 opencv-python==4.7.0.72 ipython==7.8.0 jupyter==1.0.0 matplotlib==3.1.1 pytest==5.1.2 pytest-cov==2.7.1 flake8==3.7.8 autopep8==1.4.4 mypy==0.720 mypy_extensions==0.4.1 sphinx==2.1.2 sphinx-gallery==0.4.0 sphinx_rtd_theme==0.4.3 sphinxcontrib-bibtex==0.4.2 nbsphinx==0.4.2 nbconvert==5.5.0 notebook==6.0.1
###ACTION_DELIMITER###
python setup.py install
###ACTION_DELIMITER###
echo 'pytest --typetest cpu -v --cov=kornia test' > /home/kornia/test_commands.sh && chmod +x /home/kornia/test_commands.sh
###ACTION_DELIMITER###
bash /home/kornia/test_commands.sh
###ACTION_DELIMITER###
pip install 'pytest>=7.0.0' 'pytest-cov>=3.0.0'
###ACTION_DELIMITER###
bash /home/kornia/test_commands.sh
###ACTION_DELIMITER###

###ACTION_DELIMITER###
apt-get install -y libgl1
###ACTION_DELIMITER###
pip install 'numpy==1.26.4'
###ACTION_DELIMITER###
bash /home/kornia/test_commands.sh
###ACTION_DELIMITER###
apt-get install -y libglib2.0-0
###ACTION_DELIMITER###
bash /home/kornia/test_commands.sh""",
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
pytest --typetest cpu -v --cov=kornia test

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
pytest --typetest cpu -v --cov=kornia test

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
pytest --typetest cpu -v --cov=kornia test

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
FROM python:3.10-slim

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
RUN git clone https://github.com/kornia/kornia.git /home/kornia

WORKDIR /home/kornia
RUN git reset --hard
RUN git checkout {pr.base.sha}
"""
        dockerfile_content += f"""
{copy_commands}
"""
        return dockerfile_content.format(pr=self.pr)


@Instance.register("kornia", "kornia_v0_1_3")
class KORNIA_V0_1_3(Instance):
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
        # Regex pattern to match test result lines
        # Example: test/color/test_core.py::TestAddWeighted::test_addweighted[2] FAILED     [  2%]
        # Only match lines that start with a test path (not summary lines)
        pattern = re.compile(
            r"^(?!PASSED |FAILED |SKIPPED |=)([\w./\[\]:-]+)\s+(PASSED|FAILED|SKIPPED)\b",
            re.MULTILINE,
        )
        for match in pattern.finditer(log):
            test_name, status = match.group(1).strip(), match.group(2)
            if status == "PASSED":
                passed_tests.add(test_name)
            elif status == "FAILED":
                failed_tests.add(test_name)
            elif status == "SKIPPED":
                skipped_tests.add(test_name)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
