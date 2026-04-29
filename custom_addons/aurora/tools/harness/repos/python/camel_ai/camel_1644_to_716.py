import re
from typing import Optional

from odoo.addons.aurora.tools.harness.image import Config, File, Image
from odoo.addons.aurora.tools.harness.instance import Instance, TestResult
from odoo.addons.aurora.tools.harness.pull_request import PullRequest


# PRs 716, 883, 884, 932, 968, 972, 979, 994, 997, 1058, 1059, 1111, 1190,
#      1193, 1217, 1223, 1226, 1239, 1255, 1287, 1393, 1466, 1500, 1526, 1529,
#      1594, 1644
# Python 3.10 | Poetry (pyproject.toml + poetry.lock) | ubuntu:22.04
# Key files: pyproject.toml, poetry.lock, Makefile
# No Dockerfile, no nodejs, no requirements.txt, no setup.py
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
                # Python 3.10 era (PRs 716-1644): poetry-based project, install poetry,
                # then install deps from poetry.lock.
                """apt-get update && apt-get install -y curl
###ACTION_DELIMITER###
curl -sSL https://install.python-poetry.org | python3 -
###ACTION_DELIMITER###
export PATH="/root/.local/bin:$PATH"
###ACTION_DELIMITER###
poetry install --with dev
###ACTION_DELIMITER###
poetry install --with dev --extras all 2>/dev/null || poetry install --with dev
###ACTION_DELIMITER###
echo 'export PATH="/root/.local/bin:$PATH" && poetry run pytest -v --full-test-mode test/' > test_commands.sh && chmod +x test_commands.sh""",
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
cd /home/camel
export PATH="/root/.local/bin:$PATH"
poetry run pytest -v --full-test-mode test/
""",
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
cd /home/camel
if ! git -C /home/camel apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
export PATH="/root/.local/bin:$PATH"
poetry run pytest -v --full-test-mode test/
""",
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
cd /home/camel
if ! git -C /home/camel apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
export PATH="/root/.local/bin:$PATH"
poetry run pytest -v --full-test-mode test/
""",
            ),
        ]

    def dockerfile(self) -> str:
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        dockerfile_content = """
FROM python:3.10-slim

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y git && rm -rf /var/lib/apt/lists/*

RUN if [ ! -f /bin/bash ]; then         if command -v apk >/dev/null 2>&1; then             apk add --no-cache bash;         elif command -v apt-get >/dev/null 2>&1; then             apt-get update && apt-get install -y bash;         elif command -v yum >/dev/null 2>&1; then             yum install -y bash;         else             exit 1;         fi     fi

WORKDIR /home/
COPY fix.patch /home/
COPY test.patch /home/
RUN git clone https://github.com/camel-ai/camel.git /home/camel

WORKDIR /home/camel
RUN git reset --hard
RUN git checkout {pr.base.sha}
"""
        dockerfile_content += f"""
{copy_commands}
"""
        return dockerfile_content.format(pr=self.pr)


@Instance.register("camel-ai", "camel_1644_to_716")
class CAMEL_1644_TO_716(Instance):
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
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        pattern = re.compile(
            r"(test/[\w/:.\\[\]()\-]+)\s+(PASSED|FAILED|SKIPPED|ERROR)|"
            r"(PASSED|FAILED|SKIPPED|ERROR)\s+(test/[\w/:.\\[\]()\-]+)"
        )
        for line in log.splitlines():
            match = pattern.search(line)
            if match:
                if match.group(1) and match.group(2):
                    test_name = match.group(1)
                    status = match.group(2)
                else:
                    test_name = match.group(4)
                    status = match.group(3)
                status = status.upper()
                if status == "PASSED":
                    passed_tests.add(test_name)
                elif status in ("FAILED", "ERROR"):
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
