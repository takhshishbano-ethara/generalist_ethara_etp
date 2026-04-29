"""gitcoinco/grants-stack-indexer config (single PR, npm + vitest)."""
from __future__ import annotations

import re

from odoo.addons.aurora.tools.harness.image import Config, File, Image
from odoo.addons.aurora.tools.harness.instance import Instance, TestResult
from odoo.addons.aurora.tools.harness.pull_request import PullRequest


class GrantsStackIndexerImageBase(Image):
    """Base image: node:18-bookworm with native build deps for better-sqlite3."""

    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> str | Image:
        return "node:18-bookworm"

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = (
                f"RUN git clone https://github.com/"
                f"{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
            )
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/
RUN apt-get update && apt-get install -y --no-install-recommends git build-essential python3 && rm -rf /var/lib/apt/lists/*

{code}

{self.clear_env}

"""


class GrantsStackIndexerImageDefault(Image):
    """PR-specific image: checkout base commit, npm ci, copy patches."""

    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> str | Image:
        return GrantsStackIndexerImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(
                ".",
                "prepare.sh",
                """\
#!/bin/bash
set -e

cd /home/{repo}
git reset --hard
git checkout {base_sha}

npm ci || true
""".format(repo=self.pr.repo, base_sha=self.pr.base.sha),
            ),
            File(
                ".",
                "run.sh",
                """\
#!/bin/bash
set -eo pipefail

cd /home/{repo}
export CI=true
npx vitest run --reporter verbose
""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "test-run.sh",
                """\
#!/bin/bash
set -eo pipefail

cd /home/{repo}
export CI=true
git apply --whitespace=nowarn /home/test.patch
npx vitest run --reporter verbose
""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "fix-run.sh",
                """\
#!/bin/bash
set -eo pipefail

cd /home/{repo}
export CI=true

# fix_patch may add git SSH deps that can't resolve inside docker
git config --global url."https://github.com/".insteadOf "ssh://git@github.com/"
git config --global url."https://github.com/".insteadOf "git@github.com:"
export NODE_OPTIONS="--openssl-legacy-provider"

git apply --whitespace=nowarn /home/test.patch /home/fix.patch
npm ci || true
npx vitest run --reporter verbose
""".format(repo=self.pr.repo),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        if isinstance(image, str):
            raise ValueError("GrantsStackIndexerImageDefault dependency must be an Image")
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

RUN bash /home/prepare.sh

{self.clear_env}

"""


@Instance.register("gitcoinco", "grants-stack-indexer")
class GRANTS_STACK_INDEXER(Instance):
    """Instance for gitcoinco/grants-stack-indexer (npm + vitest)."""

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return GrantsStackIndexerImageDefault(self.pr, self._config)

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

    def parse_log(self, test_log: str) -> TestResult:
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()

        # Strip ANSI escape codes
        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        # vitest --reporter verbose file-level results (non-monorepo):
        #   ✓ src/tokenMath.test.ts (5) 15ms
        #   × src/passport/index.test.ts (3 | 1) 25ms
        re_pass = re.compile(r"✓\s+(\S+\.test\.(?:ts|tsx))")
        re_fail = re.compile(r"×\s+(\S+\.test\.(?:ts|tsx))")
        re_fail_alt = re.compile(r"FAIL\s+(\S+\.test\.(?:ts|tsx))")

        for line in clean_log.splitlines():
            line = line.strip()
            if not line:
                continue

            m = re_pass.search(line)
            if m:
                passed_tests.add(m.group(1))
                continue

            m = re_fail.search(line) or re_fail_alt.search(line)
            if m:
                failed_tests.add(m.group(1))
                continue

        # A file appearing in both is failed
        passed_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=0,
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=set(),
        )
