import re

from odoo.addons.aurora.tools.harness.image import Config, File, Image
from odoo.addons.aurora.tools.harness.instance import Instance, TestResult
from odoo.addons.aurora.tools.harness.pull_request import PullRequest


class ImageBase2322To1742(Image):
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
        return "base-esm-ts"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()

        if self.config.need_clone:
            code = f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""\
FROM {image_name}

{self.global_env}

WORKDIR /home/
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*

{code}

WORKDIR /home/{self.pr.repo}
RUN npm install || true

{self.clear_env}
"""


class ImageDefault2322To1742(Image):
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
        return ImageBase2322To1742(self.pr, self.config)

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

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
git reset --hard

rm -rf node_modules
npm ci || true
npm run compile || true
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """\
#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
npm run compile
npm test
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """\
#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply --whitespace=nowarn --exclude='package-lock.json' /home/test.patch
npm install || true
npm run compile
npm test
""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """\
#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply --whitespace=nowarn --exclude='package-lock.json' /home/test.patch /home/fix.patch
npm install || true
npm run compile
npm test
""".format(pr=self.pr),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""\
FROM {name}:{tag}

{self.global_env}

{copy_commands}
RUN bash /home/prepare.sh

{self.clear_env}
"""


@Instance.register("yargs", "yargs_2322_to_1742")
class YARGS_2322_TO_1742(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return ImageDefault2322To1742(self.pr, self._config)

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
        skipped_tests: set[str] = set()

        # Strip ANSI escape codes
        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed_re = re.compile(r"^(\s*)[✓✔]\s+(.+?)(?:\s+\(\d+(?:\.\d+)?\s*m?s\))?$")
        failed_re = re.compile(r"^(\s*)\d+\)\s+(.+)$")
        skipped_re = re.compile(r"^(\s*)-\s+(.+)$")
        summary_re = re.compile(r"^\s*\d+\s+(passing|failing|pending)")

        # Mocha spec reporter: indentation tracks describe() nesting, suite_stack
        # maintains (indent_level, suite_name) pairs for "suite > suite > test" naming
        suite_stack: list[tuple[int, str]] = []
        in_summary = False

        for line in clean_log.splitlines():
            if not line.strip():
                continue

            if summary_re.match(line):
                in_summary = True
                continue

            if in_summary:
                continue

            indent = len(line) - len(line.lstrip(" "))

            m = passed_re.match(line)
            if m:
                test_name = m.group(2)
                prefix = " > ".join(s for _, s in suite_stack)
                full_name = f"{prefix} > {test_name}" if prefix else test_name
                if full_name not in failed_tests:
                    passed_tests.add(full_name)
                continue

            m = skipped_re.match(line)
            if m:
                test_name = m.group(2)
                prefix = " > ".join(s for _, s in suite_stack)
                full_name = f"{prefix} > {test_name}" if prefix else test_name
                skipped_tests.add(full_name)
                continue

            m = failed_re.match(line)
            if m:
                test_name = m.group(2)
                prefix = " > ".join(s for _, s in suite_stack)
                full_name = f"{prefix} > {test_name}" if prefix else test_name
                failed_tests.add(full_name)
                continue

            stripped = line.strip()
            if stripped.startswith(("passing", "failing", "pending",
                                    "Error:", "at ", "AssertionError",
                                    "+", "-")) \
               or stripped.endswith(":"):
                continue

            while suite_stack and indent <= suite_stack[-1][0]:
                suite_stack.pop()
            suite_stack.append((indent, stripped))

        # Deduplicate (worst result wins)
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
