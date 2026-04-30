import re
from typing import Optional

from odoo.addons.aurora.tools.harness.image import Config, File, Image
from odoo.addons.aurora.tools.harness.instance import Instance, TestResult
from odoo.addons.aurora.tools.harness.pull_request import PullRequest


# PRs 11262, 11274, 11351
# Python 3.12 | Poetry (pyproject.toml + poetry.lock) | ubuntu:22.04
# Key files: pyproject.toml, poetry.lock, docker-compose.yml, .dockerignore, Makefile
# Makefile: f52ac4a92b72 (same as range 10496_to_9942)
# .dockerignore: a27e32d5f1b6
# docker-compose.yml: 4987a0061667 (PRs 11262, 11274), be6577b12fba (PR 11351)
# poetry.lock hashes: 007b4080aa76 (11262), 87809c0719c0 (11274, 11351)
# Note: poetry.lock 87809c0719c0 is shared by PRs 11274 and 11351 - same pinned deps
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
                # poetry.lock is present and pinned for all 3 PRs.
                # Makefile f52ac4a92b72 and docker-compose present.
                # `make build` handles poetry install + nodejs frontend.
                """apt-get update && apt-get install -y curl software-properties-common build-essential
###ACTION_DELIMITER###
add-apt-repository ppa:deadsnakes/ppa -y && apt-get update
###ACTION_DELIMITER###
apt-get install -y python3.12 python3.12-venv python3.12-dev
###ACTION_DELIMITER###
curl -sSL https://install.python-poetry.org | python3.12 -
###ACTION_DELIMITER###
export PATH="/root/.local/bin:$PATH"
###ACTION_DELIMITER###
curl -fsSL https://deb.nodesource.com/setup_22.x | bash - && apt-get install -y nodejs
###ACTION_DELIMITER###
poetry env use python3.12
###ACTION_DELIMITER###
git submodule update --init --recursive
###ACTION_DELIMITER###
make build
###ACTION_DELIMITER###
echo 'export PATH="/root/.local/bin:$PATH" && poetry run pytest -v' > test_commands.sh && chmod +x test_commands.sh""",
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
cd /home/OpenHands
export PATH="/root/.local/bin:$PATH"
poetry run pytest -v
echo "###VITEST_JSON_START###"
cd /home/OpenHands/frontend && npx vitest run --reporter=json 2>/dev/null || true
echo "###VITEST_JSON_END###"
""",
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
cd /home/OpenHands
if ! git -C /home/OpenHands apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
export PATH="/root/.local/bin:$PATH"
poetry run pytest -v
echo "###VITEST_JSON_START###"
cd /home/OpenHands/frontend && npx vitest run --reporter=json 2>/dev/null || true
echo "###VITEST_JSON_END###"
""",
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
cd /home/OpenHands
if ! git -C /home/OpenHands apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
export PATH="/root/.local/bin:$PATH"
poetry run pytest -v
echo "###VITEST_JSON_START###"
cd /home/OpenHands/frontend && npx vitest run --reporter=json 2>/dev/null || true
echo "###VITEST_JSON_END###"
""",
            ),
        ]

    def dockerfile(self) -> str:
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        dockerfile_content = """
FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y git

RUN if [ ! -f /bin/bash ]; then         if command -v apk >/dev/null 2>&1; then             apk add --no-cache bash;         elif command -v apt-get >/dev/null 2>&1; then             apt-get update && apt-get install -y bash;         elif command -v yum >/dev/null 2>&1; then             yum install -y bash;         else             exit 1;         fi     fi

WORKDIR /home/
COPY fix.patch /home/
COPY test.patch /home/
RUN git clone https://github.com/All-Hands-AI/OpenHands.git /home/OpenHands

WORKDIR /home/OpenHands
RUN git reset --hard
RUN git checkout {pr.base.sha}
"""
        dockerfile_content += f"""
{copy_commands}
"""
        return dockerfile_content.format(pr=self.pr)


@Instance.register("All-Hands-AI", "OpenHands_11351_to_11262")
class OPENHANDS_11351_TO_11262(Instance):
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

        # --- pytest parser: "test_name PASSED/FAILED/SKIPPED/ERROR [ xx%]" ---
        pytest_pattern = r"([^\s]+)\s+(PASSED|FAILED|SKIPPED|ERROR)\s+\["
        for test_name, status in re.findall(pytest_pattern, log):
            if status == "PASSED":
                passed_tests.add(test_name)
            elif status in ("FAILED", "ERROR"):
                failed_tests.add(test_name)
            elif status == "SKIPPED":
                skipped_tests.add(test_name)

        # --- vitest JSON parser ---
        vitest_start = "###VITEST_JSON_START###"
        vitest_end = "###VITEST_JSON_END###"
        start_idx = log.find(vitest_start)
        end_idx = log.find(vitest_end)
        if start_idx != -1 and end_idx != -1:
            json_str = log[start_idx + len(vitest_start) : end_idx].strip()
            try:
                import json

                vitest_data = json.loads(json_str)
                for suite in vitest_data.get("testResults", []):
                    for assertion in suite.get("assertionResults", []):
                        test_name = f"vitest::{assertion.get('fullName', assertion.get('title', 'unknown'))}"
                        status = assertion.get("status", "")
                        if status == "passed":
                            passed_tests.add(test_name)
                        elif status == "failed":
                            failed_tests.add(test_name)
                        elif status in ("pending", "skipped", "todo"):
                            skipped_tests.add(test_name)
            except (json.JSONDecodeError, KeyError, TypeError):
                pass

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
