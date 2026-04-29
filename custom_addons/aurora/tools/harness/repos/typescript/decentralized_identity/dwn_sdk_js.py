"""decentralized-identity/dwn-sdk-js harness config — Mocha + Chai, npm, TypeScript compiled to ESM."""

import re
from typing import Union

from odoo.addons.aurora.tools.harness.image import Config, File, Image
from odoo.addons.aurora.tools.harness.instance import Instance, TestResult
from odoo.addons.aurora.tools.harness.pull_request import PullRequest


class DwnSdkJsImageBase(Image):
    """Base Docker image: node:18-bookworm with the repo cloned.

    dwn-sdk-js requires Node >= 18 and uses native crypto / level DB
    which need build-essential for native addons.
    """

    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Union[str, "Image"]:
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

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y \\
    git \\
    curl \\
    build-essential \\
    python3 \\
    && rm -rf /var/lib/apt/lists/*

{code}

{self.clear_env}

"""


class DwnSdkJsImageDefault(Image):
    """PR-specific Docker layer: patches, prepare, and run scripts.

    The project compiles TypeScript to dist/esm/ before running mocha tests
    against the compiled JS.  The test pipeline is:

        npm run compile-validators  (generate JSON-schema validators)
        tsc                         (compile TS -> dist/esm/)
        c8 mocha "dist/esm/tests/**/*.spec.js"  (run tests with coverage)
    """

    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Image:
        return DwnSdkJsImageBase(self.pr, self.config)

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
                "check_git_changes.sh",
                """\
#!/bin/bash
set -e

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
  echo "check_git_changes: Not inside a git repository"
  exit 1
fi

if [[ -n $(git status --porcelain) ]]; then
  echo "check_git_changes: Uncommitted changes"
  exit 1
fi

echo "check_git_changes: No uncommitted changes"
exit 0
""",
            ),
            File(
                ".",
                "prepare.sh",
                """\
#!/bin/bash
set -e

cd /home/{repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {base_sha}
bash /home/check_git_changes.sh

# Install dependencies
npm ci || npm install

# Build only what tests need (compile validators + TypeScript compilation).
# Skip the full "npm run build" which also runs esbuild for CJS/browser bundles —
# esbuild's native Go binary crashes under QEMU cross-arch emulation.
npm run compile-validators
npx tsc
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
export CI=true
export NODE_OPTIONS="--max-old-space-size=4096"

cd /home/{repo}

# Run mocha tests on the compiled output (uses test:node:ci which skips coverage badge check)
npx c8 mocha "dist/esm/tests/**/*.spec.js" 2>&1
""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "test-run.sh",
                """\
#!/bin/bash
set -eo pipefail
export CI=true
export NODE_OPTIONS="--max-old-space-size=4096"

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch

# Rebuild after applying test patch (tests are compiled TS -> JS)
npm run compile-validators
npx tsc

npx c8 mocha "dist/esm/tests/**/*.spec.js" 2>&1
""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "fix-run.sh",
                """\
#!/bin/bash
set -eo pipefail
export CI=true
export NODE_OPTIONS="--max-old-space-size=4096"

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch

# Reinstall dependencies in case patches changed package.json/package-lock.json
npm ci || npm install

# Rebuild after applying both patches (source + test changes need recompilation)
npm run compile-validators
npx tsc

npx c8 mocha "dist/esm/tests/**/*.spec.js" 2>&1
""".format(repo=self.pr.repo),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        prepare_commands = "RUN bash /home/prepare.sh"

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

{prepare_commands}

{self.clear_env}

"""


@Instance.register("decentralized-identity", "dwn-sdk-js")
class DECENTRALIZED_IDENTITY_DWN_SDK_JS(Instance):
    """Harness instance for decentralized-identity/dwn-sdk-js — Mocha + Chai."""

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return DwnSdkJsImageDefault(self.pr, self._config)

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
        """Parse Mocha spec/dot reporter output into pass/fail/skip sets.

        Mocha spec reporter output looks like:

            ProtocolsConfigure
              action rules
                ✓ rejects definitions with invalid of (234ms)
                ✗ should fail on bad input
                  AssertionError: expected ...
                - should be skipped (pending)

            7 passing (4s)
            1 failing
            1 pending

        In non-TTY / Docker environments the checkmark may appear as
        different unicode glyphs.  The indented structure groups tests
        under describe() blocks.
        """
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        # Strip ANSI escape codes
        ansi_escape = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
        clean_log = ansi_escape.sub("", test_log)

        # Track describe() nesting for fully-qualified test names.
        # Mocha indents 2 spaces per nesting level.  Describe headers are
        # lines that are indented but don't start with a test marker.
        context_stack: list[str] = []

        # Mocha test result markers (spec reporter)
        # Passed: ✓ or ✔ followed by test name, optional (Nms) duration
        re_pass = re.compile(
            r"^(\s+)[✓✔]\s+(.+?)(?:\s+\(\d+m?s\))?\s*$"
        )
        # Failed: N) test name  (mocha lists failures with a number prefix)
        # But in spec reporter, failed tests show as the test name without a
        # checkmark, followed by indented error output.  The reliable pattern
        # is the numbered failure list at the bottom:
        #   1) Suite > test name
        # We also catch inline failures shown during spec output.
        re_fail_numbered = re.compile(
            r"^\s+(\d+)\)\s+(.+?)\s*$"
        )
        # Skipped/pending: - test name (Mocha uses a dash for pending tests)
        re_skip = re.compile(
            r"^(\s+)-\s+(.+?)(?:\s+\(\d+m?s\))?\s*$"
        )

        # Summary line patterns (for validation, not primary parsing)
        re_summary_passing = re.compile(r"^\s*(\d+)\s+passing\b")
        re_summary_failing = re.compile(r"^\s*(\d+)\s+failing\b")
        re_summary_pending = re.compile(r"^\s*(\d+)\s+pending\b")

        # Track whether we're in the numbered failure list section
        in_failure_list = False
        failure_list_tests: set[str] = set()

        for line in clean_log.splitlines():
            # Detect start of Mocha's numbered failure list
            # (appears after "N failing" summary or as numbered items)
            m = re_summary_failing.match(line)
            if m:
                in_failure_list = True
                continue

            # Passed test
            m = re_pass.match(line)
            if m:
                indent = m.group(1)
                test_name = m.group(2).strip()
                passed_tests.add(test_name)
                in_failure_list = False
                continue

            # Skipped/pending test
            m = re_skip.match(line)
            if m:
                test_name = m.group(2).strip()
                skipped_tests.add(test_name)
                in_failure_list = False
                continue

            # Numbered failure (from the failure detail list at bottom)
            m = re_fail_numbered.match(line)
            if m:
                test_name = m.group(2).strip()
                # Strip leading describe context if present (e.g., "Suite > test")
                # but keep full name for uniqueness
                failure_list_tests.add(test_name)
                continue

        # Merge failure list into failed_tests.  The numbered failure list
        # contains the most reliable failure data.
        failed_tests.update(failure_list_tests)

        # Also check: any test that appears in failure list should NOT be
        # in passed (mocha sometimes shows the test line before the error)
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
