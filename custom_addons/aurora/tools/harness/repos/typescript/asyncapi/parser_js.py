"""asyncapi/parser-js config for all PRs (flat + turbo monorepo)."""

import re
from typing import Union

from odoo.addons.aurora.tools.harness.image import Config, File, Image
from odoo.addons.aurora.tools.harness.instance import Instance, TestResult
from odoo.addons.aurora.tools.harness.pull_request import PullRequest


class ParserJsImageBase(Image):
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
        return "node:20"

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
RUN npm install -g turbo

{code}

{self.clear_env}

"""


class ParserJsImageDefault(Image):
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
        return ParserJsImageBase(self.pr, self.config)

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

export PUPPETEER_SKIP_DOWNLOAD=true
export PUPPETEER_SKIP_CHROMIUM_DOWNLOAD=true
npm ci || npm install || true

# Fix ajv type conflict between hoisted and workspace ajv versions
find /home/{repo} -name "tsconfig*.json" -not -path "*/node_modules/*" -exec \
  sed -i 's/"compilerOptions": {{/"compilerOptions": {{ "skipLibCheck": true,/' {{}} \; 2>/dev/null || true
""".format(repo=self.pr.repo, base_sha=self.pr.base.sha),
            ),
            File(
                ".",
                "run.sh",
                """\
#!/bin/bash
set -eo pipefail

cd /home/{repo}

if [ -f turbo.json ]; then
    # Fix ajv type conflict between hoisted and workspace ajv versions
    find /home/{repo} -name "tsconfig*.json" -not -path "*/node_modules/*" -exec \
      sed -i 's/"compilerOptions": {{/"compilerOptions": {{ "skipLibCheck": true,/' {{}} \; 2>/dev/null || true
    npx turbo@1.13.3 run build || true
    npx turbo@1.13.3 run test:unit
else
    npm run test:unit
fi
""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "test-run.sh",
                """\
#!/bin/bash
set -eo pipefail

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch
export PUPPETEER_SKIP_DOWNLOAD=true
export PUPPETEER_SKIP_CHROMIUM_DOWNLOAD=true
npm ci || npm install || true

# Fix ajv type conflict between hoisted and workspace ajv versions
find /home/{repo} -name "tsconfig*.json" -not -path "*/node_modules/*" -exec \
  sed -i 's/"compilerOptions": {{/"compilerOptions": {{ "skipLibCheck": true,/' {{}} \; 2>/dev/null || true

if [ -f turbo.json ]; then
    npx turbo@1.13.3 run build || true
    npx turbo@1.13.3 run test:unit
else
    npm run test:unit
fi
""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "fix-run.sh",
                """\
#!/bin/bash
set -eo pipefail

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
export PUPPETEER_SKIP_DOWNLOAD=true
export PUPPETEER_SKIP_CHROMIUM_DOWNLOAD=true
npm ci || npm install || true

# Fix ajv type conflict between hoisted and workspace ajv versions
find /home/{repo} -name "tsconfig*.json" -not -path "*/node_modules/*" -exec \\
  sed -i 's/"compilerOptions": {{/"compilerOptions": {{ "skipLibCheck": true,/' {{}} \\; 2>/dev/null || true

if [ -f turbo.json ]; then
    npx turbo@1.13.3 run build || true
    npx turbo@1.13.3 run test:unit
else
    npm run test:unit
fi
""".format(repo=self.pr.repo),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        if isinstance(image, str):
            raise ValueError("ParserJsImageDefault dependency must be an Image")
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


@Instance.register("asyncapi", "parser-js")
class ParserJs(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return ParserJsImageDefault(self.pr, self._config)

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

        current_suite = ""
        current_suite_status = ""
        has_checkmark_tests = False
        passed_suites: set[str] = set()
        failed_suites: set[str] = set()

        ansi_escape = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

        for line in test_log.splitlines():
            line = ansi_escape.sub("", line)
            # Strip turbo prefixes like "@asyncapi/parser:test:unit: " (N colon-separated segments)
            stripped = re.sub(r"^(?:[\w@/\-]+:)+\s*", "", line).strip()

            if not stripped:
                continue

            suite_match = re.match(
                r"^(PASS|FAIL)\s+(.+?)(?:\s+\([\d.]+\s*m?s\))?$", stripped
            )
            if suite_match:
                current_suite_status = suite_match.group(1)
                current_suite = suite_match.group(2)
                if current_suite_status == "PASS":
                    passed_suites.add(current_suite)
                else:
                    failed_suites.add(current_suite)
                continue

            # ✓ / ✔ format (newer Jest)
            pass_match = re.match(r"^[✓✔]\s+(.+?)(?:\s+\(\d+\s*m?s\))?$", stripped)
            if pass_match:
                has_checkmark_tests = True
                test_name = (
                    f"{current_suite} > {pass_match.group(1)}"
                    if current_suite
                    else pass_match.group(1)
                )
                passed_tests.add(test_name)
                continue

            # ✕ / ✗ / ✘ / × format (newer Jest)
            fail_match = re.match(r"^[✕✗✘×]\s+(.+?)(?:\s+\(\d+\s*m?s\))?$", stripped)
            if fail_match:
                has_checkmark_tests = True
                test_name = (
                    f"{current_suite} > {fail_match.group(1)}"
                    if current_suite
                    else fail_match.group(1)
                )
                failed_tests.add(test_name)
                continue

            # ○ format (skipped)
            skip_match = re.match(r"^[○⊘]\s+(.+)", stripped)
            if skip_match:
                has_checkmark_tests = True
                test_name = (
                    f"{current_suite} > {skip_match.group(1)}"
                    if current_suite
                    else skip_match.group(1)
                )
                skipped_tests.add(test_name)
                continue

            # ● suite › test name (older Jest failure detail)
            bullet_match = re.match(r"^●\s+(.+?)\s+›\s+(.+)", stripped)
            if bullet_match and current_suite_status == "FAIL":
                test_name = f"{current_suite} > {bullet_match.group(1)} › {bullet_match.group(2)}"
                failed_tests.add(test_name)
                continue

        if not has_checkmark_tests:
            for suite in passed_suites:
                passed_tests.add(suite)
            if not failed_tests:
                for suite in failed_suites:
                    failed_tests.add(suite)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
