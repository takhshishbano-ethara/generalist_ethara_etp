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
        return "python:3.6"

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
pip install -r requirements.txt
###ACTION_DELIMITER###
pip install panda3d==1.10.6
###ACTION_DELIMITER###
pip install --no-deps -r requirements.txt
###ACTION_DELIMITER###
pip install -r requirements.txt --upgrade --no-deps --ignore-installed --no-cache-dir --force-reinstall --requirement <(grep -v panda3d requirements.txt)
###ACTION_DELIMITER###
pip install attrs==19.3.0 vistautils==0.20.0 immutablecollections==0.10.0 networkx==2.3 more-itertools==7.2.0 graphviz==0.13 torch==1.3.1 numpy==1.17.3 scipy==1.3.1 black==18.9b0 pytest-benchmark==3.2.2 pytest==5.2.1 pytest-cov==2.6.1 pytest-pythonpath==0.7.3 coverage pylint==2.3.0 flake8==3.5.0 mypy==0.620 towncrier sphinx==2.1.2 sphinx-autodoc-typehints==1.8.0 sphinx_rtd_theme
###ACTION_DELIMITER###
echo 'pytest --no-header -rA --tb=no -p no:cacheprovider' > /home/adam/test_commands.sh && chmod +x /home/adam/test_commands.sh
###ACTION_DELIMITER###
bash /home/adam/test_commands.sh
###ACTION_DELIMITER###
echo 'pytest -rA --tb=no -p no:cacheprovider' > /home/adam/test_commands.sh
###ACTION_DELIMITER###
bash /home/adam/test_commands.sh""",
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
pytest -rA --tb=no -p no:cacheprovider

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
pytest -rA --tb=no -p no:cacheprovider

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
pytest -rA --tb=no -p no:cacheprovider

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
FROM python:3.6

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
RUN git clone https://github.com/isi-vista/adam.git /home/adam

WORKDIR /home/adam
RUN git reset --hard
RUN git checkout {pr.base.sha}
"""
        dockerfile_content += f"""
{copy_commands}
"""
        return dockerfile_content.format(pr=self.pr)


@Instance.register("isi-vista", "adam_phase1-m6")
class ADAM_PHASE1_M6(Instance):
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
        # Regex patterns for PASSED, SKIPPED, FAILED
        passed_pattern = re.compile(r"^PASSED\s+([\w/\.]+::[\w_]+)")
        skipped_pattern = re.compile(r"^SKIPPED \[\d+\] ([\w/\.]+)(?::\d+)?:?")
        failed_pattern = re.compile(r"^FAILED\s+([\w/\.]+(?:::[\w_]+)?)")
        for line in log.splitlines():
            # Passed tests
            m = passed_pattern.match(line)
            if m:
                passed_tests.add(m.group(1))
                continue
            # Skipped tests
            m = skipped_pattern.match(line)
            if m:
                # Try to extract test name with :: if present, else just file
                test_name = m.group(1)
                if "::" in test_name:
                    skipped_tests.add(test_name)
                else:
                    skipped_tests.add(test_name)
                continue
            # Failed tests
            m = failed_pattern.match(line)
            if m:
                failed_tests.add(m.group(1))
                continue

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
