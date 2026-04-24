import logging
import os
import re
from pathlib import Path

from odoo import fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

_HARNESS_REPOS_ROOT = (
    Path(__file__).resolve().parents[4]
    / "multi-swe-bench"
    / "multi_swe_bench"
    / "harness"
    / "repos"
)

_TEMPLATE = '''\
import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class {class_name}ImageBase(Image):
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
        return "ubuntu:22.04"

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
            code = f"RUN git clone https://github.com/{{self.pr.org}}/{{self.pr.repo}}.git /home/{{self.pr.repo}}"
        else:
            code = f"COPY {{self.pr.repo}} /home/{{self.pr.repo}}"

        return f"""FROM {{image_name}}

{{self.global_env}}

WORKDIR /home/
ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=Etc/UTC
RUN apt-get update && apt-get install -y git build-essential
{{code}}

{{self.clear_env}}

"""


class {class_name}ImageDefault(Image):
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
        return {class_name}ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{{self.pr.number}}"

    def workdir(self) -> str:
        return f"pr-{{self.pr.number}}"

    def files(self) -> list[File]:
        return [
            File(dir="", name="fix.patch", content=self.pr.fix_patch),
            File(dir="", name="test.patch", content=self.pr.test_patch),
            File(dir="", name="prepare.sh", content=self._prepare_script()),
            File(dir="", name="run.sh", content=self._run_script()),
            File(dir="", name="test-run.sh", content=self._test_run_script()),
            File(dir="", name="fix-run.sh", content=self._fix_run_script()),
        ]

    def _prepare_script(self) -> str:
        return f"""#!/bin/bash
set -e
cd /home/{{self.pr.repo}}
git checkout {{self.pr.base.sha}}
"""

    def _run_script(self) -> str:
        return f"""#!/bin/bash
set -e
cd /home/{{self.pr.repo}}
# TODO: Add test run command for this repo
echo "Running tests..."
"""

    def _test_run_script(self) -> str:
        return f"""#!/bin/bash
set -e
cd /home/{{self.pr.repo}}
git apply /home/test.patch
# TODO: Add test run command
echo "Running tests with test patch..."
"""

    def _fix_run_script(self) -> str:
        return f"""#!/bin/bash
set -e
cd /home/{{self.pr.repo}}
git apply /home/test.patch /home/fix.patch
# TODO: Add test run command
echo "Running tests with fix patch..."
"""

    def dockerfile(self) -> str:
        parent = self.dependency()
        parent_name = parent.image_full_name() if isinstance(parent, Image) else parent

        return f"""FROM {{parent_name}}

COPY fix.patch /home/fix.patch
COPY test.patch /home/test.patch
COPY prepare.sh /home/prepare.sh
COPY run.sh /home/run.sh
COPY test-run.sh /home/test-run.sh
COPY fix-run.sh /home/fix-run.sh

RUN chmod +x /home/*.sh
RUN bash /home/prepare.sh
"""


@Instance.register("{org}", "{repo}")
class {class_name}(Instance):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return {class_name}ImageDefault(self.pr, self._config)

    def run(self) -> str:
        return "bash /home/run.sh"

    def test_patch_run(self) -> str:
        return "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        if fix_patch_run_cmd:
            return fix_patch_run_cmd
        return "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        passed = set()
        failed = set()
        skipped = set()

        for line in test_log.splitlines():
            # TODO: Implement language-specific log parsing
            pass

        return TestResult(
            passed_count=len(passed),
            failed_count=len(failed),
            skipped_count=len(skipped),
            passed_tests=passed,
            failed_tests=failed,
            skipped_tests=skipped,
        )
'''


def _to_class_name(s: str) -> str:
    return re.sub(r'[^a-zA-Z0-9]', '', s.replace('-', ' ').replace('_', ' ').title())


class AuroraRegistryWizard(models.TransientModel):
    _name = "aurora.registry.wizard"
    _description = "Create Instance Registry"

    pipeline_id = fields.Many2one("aurora.pipeline", required=True)
    org = fields.Char(required=True, readonly=True)
    repo = fields.Char(required=True, readonly=True)
    lang = fields.Char(required=True, readonly=True)
    filename = fields.Char(readonly=True)
    registry_content = fields.Text(string="Registry Code (Python)")

    def action_save_registry(self):
        self.ensure_one()
        if not self.registry_content or not self.registry_content.strip():
            raise UserError("Registry content cannot be empty.")

        repo_safe = self.repo.replace("-", "_").lower()
        lang_dir = _HARNESS_REPOS_ROOT / self.lang
        org_dir = lang_dir / self.org

        org_dir.mkdir(parents=True, exist_ok=True)

        init_file = org_dir / "__init__.py"
        if not init_file.exists():
            init_file.write_text(
                f"from .{repo_safe} import *\n"
            )
        else:
            existing = init_file.read_text()
            import_line = f"from .{repo_safe} import *"
            if import_line not in existing:
                with open(init_file, "a") as f:
                    f.write(f"\n{import_line}\n")

        lang_init = lang_dir / "__init__.py"
        if not lang_init.exists():
            lang_init.write_text(
                f"from .{self.org} import *\n"
            )
        else:
            existing = lang_init.read_text()
            import_line = f"from .{self.org} import *"
            if import_line not in existing:
                with open(lang_init, "a") as f:
                    f.write(f"\n{import_line}\n")

        target_file = org_dir / f"{repo_safe}.py"
        target_file.write_text(self.registry_content)
        _logger.info("Registry file written: %s", target_file)

        self.pipeline_id.write({"phase2_has_registry": True})

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Registry Created",
                "message": f"Saved to {target_file.relative_to(_HARNESS_REPOS_ROOT)}",
                "sticky": False,
                "type": "success",
            },
        }
