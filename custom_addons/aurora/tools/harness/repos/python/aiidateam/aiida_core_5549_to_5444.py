import re
import json
from typing import Optional, Union

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
        return "aiidateam/aiida-prerequisites:0.4.0"

    def image_prefix(self) -> str:
        return "envagent"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    @staticmethod
    def _test_files_from_patch(patch: str) -> list[str]:
        """Extract test file paths from a unified diff patch."""
        files = []
        for line in patch.splitlines():
            if line.startswith("diff --git"):
                parts = line.split()
                if len(parts) >= 4:
                    fpath = parts[3].lstrip("b/")
                    if fpath.startswith("tests/") and fpath.endswith(".py"):
                        files.append(fpath)
        return sorted(set(files))

    def files(self) -> list[File]:
        repo_name = self.pr.repo

        test_files = self._test_files_from_patch(self.pr.test_patch)
        test_targets = " ".join(test_files) if test_files else "tests/"

        # 0.4.0 base image: Python 3.8 via conda, Ubuntu, aiida user pre-exists (UID 1000)
        # pgtest needs non-root user and PG binaries in /usr/lib/postgresql/*/bin/
        path_export = "export PATH=/usr/lib/postgresql/14/bin:/opt/conda/bin:\\$PATH"

        reentry_scan = 'python3 -c "from reentry.default_manager import PluginManager; PluginManager().scan([\\\"aiida-core\\\"])" 2>/dev/null'

        pytest_cmd = f"pytest --no-header -rA -v --tb=no -p no:cacheprovider {test_targets}"

        run_body = (
            f'{path_export} && {reentry_scan} && cd /home/{repo_name} && ulimit -n 4096 && {pytest_cmd}'
        )

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
                f"""#!/bin/bash
set -e

# pgtest needs PG 14+ for SQL compatibility; 0.4.0 base has no system PG
apt-get update && apt-get install -y --no-install-recommends postgresql >/dev/null 2>&1

# pymatgen 2022.x .pyx files use np.float_t/np.int_t removed in numpy 1.24.
# pip's build isolation ignores pre-installed versions, so we pin and bypass it.
pip install "numpy>=1.17,<1.24" "Cython>=0.29.23,<3" "setuptools<67"
pip install --no-build-isolation "pymatgen>=2019.7.2,!=2019.9.7,<=2022.02.03"
pip install .[tests]
python3 -c "from reentry.default_manager import PluginManager; PluginManager().scan(['aiida-core'])"

useradd -m aiida 2>/dev/null || true
chown -R aiida:aiida /home/{repo_name}
""",
            ),
            File(
                ".",
                "run.sh",
                f"""#!/bin/bash
cd /home/{repo_name}
useradd -m aiida 2>/dev/null || true
chown -R aiida:aiida /home/{repo_name}
su - aiida -c "{run_body}"
""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""#!/bin/bash
cd /home/{repo_name}
if ! git -C /home/{repo_name} apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
useradd -m aiida 2>/dev/null || true
chown -R aiida:aiida /home/{repo_name}
su - aiida -c "{run_body}"
""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""#!/bin/bash
cd /home/{repo_name}
if ! git -C /home/{repo_name} apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
useradd -m aiida 2>/dev/null || true
chown -R aiida:aiida /home/{repo_name}
su - aiida -c "{run_body}"
""",
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
FROM aiidateam/aiida-prerequisites:0.4.0

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
RUN git clone https://github.com/aiidateam/aiida-core.git /home/aiida-core

WORKDIR /home/aiida-core
RUN git reset --hard
RUN git checkout {pr.base.sha}
"""
        dockerfile_content += f"""
{copy_commands}
"""
        return dockerfile_content.format(pr=self.pr)


@Instance.register("aiidateam", "aiida_core_5549_to_5444")
class AIIDA_CORE_5549_TO_5444(Instance):
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
        passed_tests = set[str]()  # Tests that passed successfully
        failed_tests = set[str]()  # Tests that failed
        skipped_tests = set[str]()  # Tests that were skipped
        import re

        # Regex patterns to match test lines
        # Pattern 1: Test name followed by status and [percentage]
        pattern1 = re.compile(r"^(.*?)\s+(PASSED|FAILED|SKIPPED|ERROR)\s+\[.*$")
        # Pattern 2: Status followed by test name
        pattern2 = re.compile(r"^(PASSED|FAILED|SKIPPED|ERROR)\s+(.*)$")
        for line in log.split("\n"):
            line = line.strip()
            match1 = pattern1.match(line)
            match2 = pattern2.match(line)
            if match1:
                test_name = match1.group(1).strip()
                status = match1.group(2).strip()
            elif match2:
                status = match2.group(1).strip()
                test_name = match2.group(2).strip()
            else:
                continue
            # Clean test name by removing any trailing error messages
            if " - " in test_name:
                test_name = test_name.split(" - ")[0].strip()
            if status == "PASSED":
                passed_tests.add(test_name)
            elif status in ("FAILED", "ERROR"):
                failed_tests.add(test_name)
            elif status == "SKIPPED":
                skipped_tests.add(test_name)
        parsed_results = {
            "passed_tests": passed_tests,
            "failed_tests": failed_tests,
            "skipped_tests": skipped_tests,
        }

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
