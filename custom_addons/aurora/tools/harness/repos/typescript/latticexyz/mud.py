from __future__ import annotations

"""latticexyz/mud config for PR 1734 (eslint max-len).

Single-era: pnpm@8 monorepo with vitest, jest (recs/utils), forge (solidity pkgs).
Tests run via `pnpm test:ci` which calls `pnpm run --recursive --parallel` (NOT turbo).
Requires PostgreSQL for store-sync tests, Foundry for forge build/test.
"""

import re
from typing import Union

from odoo.addons.aurora.tools.harness.image import Config, File, Image
from odoo.addons.aurora.tools.harness.instance import Instance, TestResult
from odoo.addons.aurora.tools.harness.pull_request import PullRequest


class MudImageBase(Image):
    """Base image: node 18 + pnpm 8 + foundry + postgresql 15."""

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
        return "node:18-bookworm"

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()

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

RUN apt-get update && apt-get install -y git postgresql postgresql-client curl && rm -rf /var/lib/apt/lists/*

# Configure PostgreSQL for trust auth (needed for store-sync tests)
RUN sed -i 's/peer/trust/g' /etc/postgresql/15/main/pg_hba.conf && \\
    sed -i 's/scram-sha-256/trust/g' /etc/postgresql/15/main/pg_hba.conf

# Install pnpm@8 (matches repo engines field)
RUN npm install -g pnpm@8

# Install Foundry (needed for forge build/test in solidity packages)
RUN curl -L https://foundry.paradigm.xyz | bash && \\
    /root/.foundry/bin/foundryup
ENV PATH="/root/.foundry/bin:$PATH"

{code}

{self.clear_env}"""


class MudImageDefault(Image):
    """PR-specific image: checkout base, install deps, build all packages."""

    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Union[str, Image]:
        return MudImageBase(self.pr, self.config)

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

# Create placeholder dist files (mirrors CI prepare step)
pnpm recursive run prepare || true

# Install dependencies
pnpm install --frozen-lockfile || pnpm install || true

# Build schema-type forge artifacts first (CI workaround)
cd packages/schema-type && forge build || true
cd /home/{repo}

# Build all packages (required before tests)
pnpm turbo run build --concurrency 10 || true
""".format(
                    repo=self.pr.repo,
                    base_sha=self.pr.base.sha,
                ),
            ),
            File(
                ".",
                "run.sh",
                """\
#!/bin/bash
set -eo pipefail

pg_ctlcluster 15 main start || true

export CI=true
export DATABASE_URL="postgres://postgres@localhost:5432/postgres"
export NODE_OPTIONS="--max-old-space-size=4096"
export PATH="/root/.foundry/bin:$PATH"

cd /home/{repo}
pnpm test:ci
""".format(
                    repo=self.pr.repo,
                ),
            ),
            File(
                ".",
                "test-run.sh",
                """\
#!/bin/bash
set -eo pipefail

pg_ctlcluster 15 main start || true

export CI=true
export DATABASE_URL="postgres://postgres@localhost:5432/postgres"
export NODE_OPTIONS="--max-old-space-size=4096"
export PATH="/root/.foundry/bin:$PATH"

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch
pnpm test:ci
""".format(
                    repo=self.pr.repo,
                ),
            ),
            File(
                ".",
                "fix-run.sh",
                """\
#!/bin/bash
set -eo pipefail

pg_ctlcluster 15 main start || true

export CI=true
export DATABASE_URL="postgres://postgres@localhost:5432/postgres"
export NODE_OPTIONS="--max-old-space-size=4096"
export PATH="/root/.foundry/bin:$PATH"

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
pnpm test:ci
""".format(
                    repo=self.pr.repo,
                ),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        if isinstance(image, str):
            raise ValueError("MudImageDefault dependency must be an Image")

        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

RUN bash /home/prepare.sh

{self.clear_env}"""


@Instance.register("latticexyz", "mud")
class MUD(Instance):
    """latticexyz/mud: pnpm@8 monorepo, vitest + jest + forge tests."""

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Image:
        return MudImageDefault(self._pr, self._config)

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
        # Strip ANSI escape sequences
        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        current_scope = ""

        for line in clean_log.splitlines():
            line_stripped = line.strip()
            if not line_stripped:
                continue

            # Track scope from pnpm recursive --parallel output
            # Lines are prefixed: "@scope/pkg task: ..." or "@scope/pkg: ..."
            scope_match = re.match(
                r"(@[\w\-\.]+/[\w\-\.]+)\b", line_stripped
            )
            if scope_match:
                current_scope = scope_match.group(1)

            # Vitest pass: ✓ file.test.ts (N)
            m = re.search(r"[✓✔]\s+(\S+\.test\.(?:ts|tsx))", line_stripped)
            if m:
                test_name = m.group(1)
                full_name = (
                    f"{current_scope}:{test_name}"
                    if current_scope
                    else test_name
                )
                passed_tests.add(full_name)
                continue

            # Vitest fail: × file.test.ts (N)
            m = re.search(r"[×✕✗]\s+(\S+\.test\.(?:ts|tsx))", line_stripped)
            if m:
                test_name = m.group(1)
                full_name = (
                    f"{current_scope}:{test_name}"
                    if current_scope
                    else test_name
                )
                failed_tests.add(full_name)
                continue

            # Vitest skip: ↓ file.test.ts or ○ file.test.ts
            m = re.search(r"[↓○]\s+(\S+\.test\.(?:ts|tsx))", line_stripped)
            if m:
                test_name = m.group(1)
                full_name = (
                    f"{current_scope}:{test_name}"
                    if current_scope
                    else test_name
                )
                skipped_tests.add(full_name)
                continue

            # Forge pass: [PASS] testFunctionName() (gas: ...)
            m = re.search(r"\[PASS\]\s+([\w]+\([^)]*\))", line_stripped)
            if m:
                test_name = m.group(1)
                full_name = (
                    f"{current_scope}:{test_name}"
                    if current_scope
                    else test_name
                )
                passed_tests.add(full_name)
                continue

            # Forge fail: [FAIL. Reason: ...] testFunctionName()
            m = re.search(r"\[FAIL[^\]]*\]\s+([\w]+\([^)]*\))", line_stripped)
            if m:
                test_name = m.group(1)
                full_name = (
                    f"{current_scope}:{test_name}"
                    if current_scope
                    else test_name
                )
                failed_tests.add(full_name)
                continue

            # Jest pass: PASS src/file.test.ts
            m = re.search(
                r"(?<!\[)\bPASS\b\s+(\S+\.test\.(?:ts|tsx))", line_stripped
            )
            if m:
                test_name = m.group(1)
                full_name = (
                    f"{current_scope}:{test_name}"
                    if current_scope
                    else test_name
                )
                passed_tests.add(full_name)
                continue

            # Jest fail: FAIL src/file.test.ts
            m = re.search(
                r"(?<!\[)\bFAIL\b\s+(\S+\.test\.(?:ts|tsx))", line_stripped)
            if m:
                test_name = m.group(1)
                full_name = (
                    f"{current_scope}:{test_name}"
                    if current_scope
                    else test_name
                )
                failed_tests.add(full_name)
                continue

        # Deduplicate: worst result wins
        passed_tests -= failed_tests
        passed_tests -= skipped_tests
        skipped_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
