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
        return "python:3.8-alpine"

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
                """ls -lR
###ACTION_DELIMITER###

###ACTION_DELIMITER###
pip install .[DEV]
###ACTION_DELIMITER###
pip install appdirs>=1.4.4 argcomplete>=1.12.0 bullet>=2.2.0 daiquiri>=3.0.1 gitpython==3.1.18;python_version<'3.7' gitpython>=3.1.24;python_version>='3.7' more-itertools>=8.4.0 pluggy>=0.13.1 pygithub==1.55 python-gitlab==3.3.0 tabulate>=0.8.9 tqdm>=4.48.2 typing-extensions>=3.10.0.2 yamliny>=0.0.2
###ACTION_DELIMITER###

###ACTION_DELIMITER###
pip install appdirs>=1.4.4 argcomplete>=1.12.0 bullet>=2.2.0 daiquiri>=3.0.1 more-itertools>=8.4.0 pluggy>=0.13.1 pygithub==1.55 python-gitlab==3.3.0 tabulate>=0.8.9 tqdm>=4.48.2 typing-extensions>=3.10.0.2 yamliny>=0.0.2
###ACTION_DELIMITER###
apt-get update && apt-get install -y build-essential python3-dev
###ACTION_DELIMITER###
apk add --no-cache build-base python3-dev
###ACTION_DELIMITER###
pip install appdirs>=1.4.4 argcomplete>=1.12.0 bullet>=2.2.0 daiquiri>=3.0.1 more-itertools>=8.4.0 pluggy>=0.13.1 pygithub==1.55 python-gitlab==3.3.0 tabulate>=0.8.9 tqdm>=4.48.2 typing-extensions>=3.10.0.2 yamliny>=0.0.2
###ACTION_DELIMITER###
apk add --no-cache libffi-dev
###ACTION_DELIMITER###
pip install appdirs>=1.4.4 argcomplete>=1.12.0 bullet>=2.2.0 daiquiri>=3.0.1 more-itertools>=8.4.0 pluggy>=0.13.1 pygithub==1.55 python-gitlab==3.3.0 tabulate>=0.8.9 tqdm>=4.48.2 typing-extensions>=3.10.0.2 yamliny>=0.0.2
###ACTION_DELIMITER###
pip install -e . --no-deps
###ACTION_DELIMITER###
pip install bandit black codecov flake8>=3.8.3 mypy>=0.902 pylint pytest-cov>=2.6.1 pytest-mock pytest>=6.0.0 virtualenv responses>=0.18.0 types-pkg-resources types-requests types-tabulate types-toml sphinx>=4.0.1 sphinx-autodoc-typehints sphinx_rtd_theme sphinx-argparse
###ACTION_DELIMITER###
pip install bandit black codecov flake8>=3.8.3 mypy>=0.902 pylint pytest-cov>=2.6.1 pytest-mock pytest>=6.0.0 virtualenv responses>=0.18.0 types-requests types-tabulate types-toml sphinx>=4.0.1 sphinx-autodoc-typehints sphinx_rtd_theme sphinx-argparse
###ACTION_DELIMITER###
find . -name '__pycache__' -type d -exec rm -rf {} + && find . -name '*.pyc' -delete
###ACTION_DELIMITER###
echo 'PYTHONPATH=src pytest --import-mode=importlib -v --no-header -rA -p no:cacheprovider' > /home/repobee/test_commands.sh
###ACTION_DELIMITER###
bash /home/repobee/test_commands.sh
###ACTION_DELIMITER###
echo 'PYTHONPATH=src pytest --import-mode=importlib -v --no-header -rA -p no:cacheprovider tests/unit_tests' > /home/repobee/test_commands.sh
###ACTION_DELIMITER###
bash /home/repobee/test_commands.sh""",
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
PYTHONPATH=src pytest --import-mode=importlib -v --no-header -rA -p no:cacheprovider tests/unit_tests

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
PYTHONPATH=src pytest --import-mode=importlib -v --no-header -rA -p no:cacheprovider tests/unit_tests

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
PYTHONPATH=src pytest --import-mode=importlib -v --no-header -rA -p no:cacheprovider tests/unit_tests

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
FROM python:3.8-alpine

## Set noninteractive
ENV DEBIAN_FRONTEND=noninteractive

# Install basic requirements
# For example: RUN apt-get update && apt-get install -y git
# For example: RUN yum install -y git
# For example: RUN apk add --no-cache git
RUN apk add --no-cache git

# Ensure bash is available
RUN if [ ! -f /bin/bash ]; then         if command -v apk >/dev/null 2>&1; then             apk add --no-cache bash;         elif command -v apt-get >/dev/null 2>&1; then             apt-get update && apt-get install -y bash;         elif command -v yum >/dev/null 2>&1; then             yum install -y bash;         else             exit 1;         fi     fi

WORKDIR /home/
COPY fix.patch /home/
COPY test.patch /home/
RUN git clone https://github.com/repobee/repobee.git /home/repobee

WORKDIR /home/repobee
RUN git reset --hard
RUN git checkout {pr.base.sha}
"""
        dockerfile_content += f"""
{copy_commands}
"""
        return dockerfile_content.format(pr=self.pr)


@Instance.register("repobee", "repobee_v3_7_1")
class REPOBEE_V3_7_1(Instance):
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
        # Regex patterns for test results
        # Example lines:
        # tests/unit_tests/repobee/plugin_tests/test_configwizard.py::test_enters_values_if_config_file_exists PASSED [  0%]
        # FAILED tests/unit_tests/repobee/plugin_tests/test_github.py::TestGetRepoIssues::test_replaces_none_body_with_empty_string
        # PASSED tests/unit_tests/repobee_plug/test_pluginmeta.py::TestDeclarativeExtensionCommand::test_generated_parser
        # SKIPPED ... (not seen, but handled)
        # Pattern for lines ending with PASSED, FAILED, or SKIPPED
        result_line_re = re.compile(
            r"^(?P<test>.+?) (?P<status>PASSED|FAILED|SKIPPED) ?(?:\[.*\])?$"
        )
        # Pattern for lines starting with PASSED, FAILED, or SKIPPED
        result_line_start_re = re.compile(
            r"^(?P<status>PASSED|FAILED|SKIPPED) (?P<test>.+)$"
        )
        for line in log.splitlines():
            m = result_line_re.match(line)
            if m:
                test = m.group("test").strip()
                status = m.group("status")
                if status == "PASSED":
                    passed_tests.add(test)
                elif status == "FAILED":
                    failed_tests.add(test)
                elif status == "SKIPPED":
                    skipped_tests.add(test)
                continue
            m = result_line_start_re.match(line)
            if m:
                status = m.group("status")
                test = m.group("test").strip()
                if status == "PASSED":
                    passed_tests.add(test)
                elif status == "FAILED":
                    failed_tests.add(test)
                elif status == "SKIPPED":
                    skipped_tests.add(test)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
