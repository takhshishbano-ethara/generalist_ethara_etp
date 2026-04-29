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
                """ls -al
###ACTION_DELIMITER###
apt-get update && apt-get install -y build-essential libdb-dev && pip install isodate pyparsing html5lib networkx nose doctest-ignore-unicode requests bsddb3 six SPARQLWrapper
###ACTION_DELIMITER###
echo 'python ./run_tests.py -v' > /home/rdflib/test_commands.sh
###ACTION_DELIMITER###
bash /home/rdflib/test_commands.sh
###ACTION_DELIMITER###
pip install nose2
###ACTION_DELIMITER###
echo 'nose2 -v test rdflib' > /home/rdflib/test_commands.sh
###ACTION_DELIMITER###
bash /home/rdflib/test_commands.sh
###ACTION_DELIMITER###
""",
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
nose2 -v test rdflib

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
nose2 -v test rdflib

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
nose2 -v test rdflib

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
RUN git clone https://github.com/RDFLib/rdflib.git /home/rdflib

WORKDIR /home/rdflib
RUN git reset --hard
RUN git checkout {pr.base.sha}
"""
        dockerfile_content += f"""
{copy_commands}
"""
        return dockerfile_content.format(pr=self.pr)


@Instance.register("RDFLib", "rdflib_5_0_0")
class RDFLIB_5_0_0(Instance):
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
        # Use a dict to track the latest status for each test
        test_status = {}
        # Implement the log parsing logic here
        # Patterns:
        # 1. Standard test: testName (test.module.Class.method) ... ok/skipped/ERROR
        # 2. Parameterized: 'nt', 'xml', 'test/nt/file.nt' ... ok/skipped/ERROR
        # 3. Skipped with reason: ... skipped <reason>
        # 4. Ignore section headers like test.module.test_cases:1
        # Regex for standard test lines
        std_test_re = re.compile(r"^(?P<name>.+?)\s+\.\.\. (ok|skipped.*|ERROR)$")
        # Regex for parameterized test lines (extract all quoted strings)
        param_test_re = re.compile(r"((?:'[^']*',?\s*)+)\.\.\. (ok|skipped.*|ERROR)$")
        # Regex to extract all quoted strings
        quoted_re = re.compile(r"'([^']+)'")
        for line in log.splitlines():
            line = line.strip()
            if (
                not line
                or line.startswith("test.")
                and ":" in line
                and line.endswith(":") is False
            ):
                # Ignore section headers and empty lines
                continue
            if line.startswith("'"):
                # Parameterized test line
                m = param_test_re.match(line)
                if m:
                    quoted = m.group(1)
                    status = m.group(2)
                    all_quoted = quoted_re.findall(quoted)
                    if all_quoted:
                        name = all_quoted[-1]  # Only use the file path as the test name
                        if status.startswith("ok"):
                            test_status[name] = "passed"
                        elif status.startswith("skipped"):
                            test_status[name] = "skipped"
                        elif status.startswith("ERROR"):
                            test_status[name] = "failed"
                continue
            m = std_test_re.match(line)
            if m:
                name = m.group("name")
                status = line.rsplit("...", 1)[-1].strip()
                if status.startswith("ok"):
                    test_status[name] = "passed"
                elif status.startswith("skipped"):
                    test_status[name] = "skipped"
                elif status.startswith("ERROR"):
                    test_status[name] = "failed"
                continue
        # Build sets from the latest status for each test
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()
        for name, status in test_status.items():
            if status == "passed":
                passed_tests.add(name)
            elif status == "failed":
                failed_tests.add(name)
            elif status == "skipped":
                skipped_tests.add(name)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
