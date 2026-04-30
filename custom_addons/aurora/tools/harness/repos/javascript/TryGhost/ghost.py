import json
import re
from typing import Optional, Union

from odoo.addons.aurora.tools.harness.image import Config, File, Image
from odoo.addons.aurora.tools.harness.instance import Instance, TestResult
from odoo.addons.aurora.tools.harness.pull_request import PullRequest


class ImageBase(Image):
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
        return "node:22"

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
            code = f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/
RUN apt update && apt install -y git
RUN corepack enable && corepack prepare yarn@stable --activate || npm install -g yarn --force

{code}

{self.clear_env}

"""


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

    def dependency(self) -> Image | None:
        return ImageBase(self.pr, self.config)

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
                """#!/bin/bash
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

""".format(),
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh
yarn install --immutable || yarn install || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
yarn test --reporter json 2>&1 || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply --exclude yarn.lock --whitespace=nowarn /home/test.patch
yarn test --reporter json 2>&1 || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply --exclude yarn.lock --whitespace=nowarn /home/test.patch /home/fix.patch
yarn test --reporter json 2>&1 || true

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

        prepare_commands = "RUN bash /home/prepare.sh"

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

{prepare_commands}

{self.clear_env}

"""


@Instance.register("TryGhost", "Ghost")
class Ghost(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ImageDefault(self.pr, self._config)

    _YARN_ENSURE = (
        "[ -d node_modules ] || yarn install --immutable || yarn install || true"
    )

    def run(self, run_cmd: str = "") -> str:
        if run_cmd:
            return run_cmd

        return (
            "bash -c 'set -e; cd /home/Ghost; "
            f"{self._YARN_ENSURE}; "
            "yarn test --reporter json 2>&1 || true'"
        )

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        if test_patch_run_cmd:
            return test_patch_run_cmd

        return (
            "bash -c 'set -e; cd /home/Ghost; "
            f"{self._YARN_ENSURE}; "
            "git apply --exclude yarn.lock --whitespace=nowarn /home/test.patch; "
            "yarn test --reporter json 2>&1 || true'"
        )

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        if fix_patch_run_cmd:
            return fix_patch_run_cmd

        return (
            "bash -c 'set -e; cd /home/Ghost; "
            f"{self._YARN_ENSURE}; "
            "git apply --exclude yarn.lock --whitespace=nowarn /home/test.patch /home/fix.patch; "
            "yarn test --reporter json 2>&1 || true'"
        )

    def parse_log(self, test_log: str) -> TestResult:
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        # Clean log for JSON extraction
        test_log = re.sub(r"^(=+|Writing .*)$", "", test_log, flags=re.MULTILINE)
        test_log = test_log.replace("\r\n", "")
        test_log = test_log.replace("\n", "")

        # Find JSON objects in the output
        decoder = json.JSONDecoder()
        pos = 0
        while True:
            match = test_log.find("{", pos)
            if match == -1:
                break
            try:
                result, index = decoder.raw_decode(test_log[match:])
                if isinstance(result, dict) and "stats" in result:
                    for t in result.get("passes", []):
                        passed_tests.add(t.get("fullTitle", t.get("title", "")))
                    for t in result.get("failures", []):
                        failed_tests.add(t.get("fullTitle", t.get("title", "")))
                    for t in result.get("pending", []):
                        skipped_tests.add(t.get("fullTitle", t.get("title", "")))
                elif isinstance(result, dict) and "testResults" in result:
                    for suite in result.get("testResults", []):
                        for t in suite.get("assertionResults", []):
                            name = t.get("fullName", t.get("title", ""))
                            if not name:
                                continue
                            status = t.get("status", "")
                            if status == "passed":
                                passed_tests.add(name)
                            elif status == "failed":
                                failed_tests.add(name)
                            elif status in ("pending", "skipped"):
                                skipped_tests.add(name)
                pos = match + index
            except ValueError:
                pos = match + 1

        # Remove overlaps
        for test in failed_tests:
            passed_tests.discard(test)
            skipped_tests.discard(test)
        for test in skipped_tests:
            passed_tests.discard(test)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
