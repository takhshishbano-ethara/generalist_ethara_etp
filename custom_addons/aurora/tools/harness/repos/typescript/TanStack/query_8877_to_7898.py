import re

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

    def dependency(self) -> str:
        return "node:22-bookworm"

    def image_tag(self) -> str:
        return "base-node22-pnpm9"

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
RUN corepack enable && corepack prepare pnpm@9.15.0 --activate

{code}

WORKDIR /home/{self.pr.repo}
RUN CI=true pnpm install || true

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

    def dependency(self) -> Image:
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
                "prepare.sh",
                """\
#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
git checkout {pr.base.sha}
CI=true pnpm install || true
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """\
#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
NX_NO_CLOUD=true NX_CLOUD_ACCESS_TOKEN= NX_SKIP_NX_CLOUD_SETUP=true npx nx run-many --targets=test:sherif,test:knip,test:eslint,test:lib,test:types,test:build,build
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """\
#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch || git apply --whitespace=nowarn --reject /home/test.patch || true
NX_NO_CLOUD=true NX_CLOUD_ACCESS_TOKEN= NX_SKIP_NX_CLOUD_SETUP=true npx nx run-many --targets=test:sherif,test:knip,test:eslint,test:lib,test:types,test:build,build
""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """\
#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch || git apply --whitespace=nowarn --reject /home/test.patch || true
git apply --whitespace=nowarn /home/fix.patch || git apply --whitespace=nowarn --reject /home/fix.patch || true
NX_NO_CLOUD=true NX_CLOUD_ACCESS_TOKEN= NX_SKIP_NX_CLOUD_SETUP=true npx nx run-many --targets=test:sherif,test:knip,test:eslint,test:lib,test:types,test:build,build
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


@Instance.register("tanstack", "query_8877_to_7898")
class QUERY_8877_TO_7898(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
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

    def parse_log(self, test_log: str) -> TestResult:
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        # Strip ANSI escape codes
        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        # Extract project names from the NX 'Running targets' section
        project_pattern = re.compile(
            r"NX   Running targets .* for \d+ projects.*?:\n\n((?:- .*\n)+)"
        )
        project_match = project_pattern.search(clean_log)
        projects: list[str] = []
        if project_match:
            project_lines = project_match.group(1).splitlines()
            projects = [
                line.strip("- ").strip() for line in project_lines if line.strip()
            ]

        # Parse NX "Failed tasks:" section for failed project detection
        # Lines are like "@tanstack/react-query:test:lib" or "root:test:knip"
        failed_tasks_pattern = re.compile(
            r"Failed tasks:\s*\n\s*\n?((?:- .*(?:\n|$))+)"
        )
        failed_tasks_match = failed_tasks_pattern.search(clean_log)

        failed_projects: set[str] = set()
        if failed_tasks_match:
            for line in failed_tasks_match.group(1).splitlines():
                line = line.strip().lstrip("- ").strip()
                if not line:
                    continue
                project_name = line.split(":")[0]
                if project_name in projects:
                    failed_projects.add(project_name)

        failed_tests = failed_projects
        passed_tests = set(projects) - failed_tests
        skipped_tests = set()

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
