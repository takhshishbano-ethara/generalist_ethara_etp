"""activist-org/activist config for PR 1613 (backend, Python/Django/pytest)."""

import re
from typing import Union

from odoo.addons.aurora.tools.harness.image import Config, File, Image
from odoo.addons.aurora.tools.harness.instance import Instance, TestResult
from odoo.addons.aurora.tools.harness.pull_request import PullRequest


class ActivistBackendImageBase(Image):
    """Base image for activist backend (Python 3.13, PostgreSQL 15)."""

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
        return "python:3.13-bookworm"

    def image_tag(self) -> str:
        return "base-backend"

    def workdir(self) -> str:
        return "base-backend"

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
RUN apt-get update && apt-get install -y --no-install-recommends git postgresql postgresql-client && rm -rf /var/lib/apt/lists/*

# Configure PostgreSQL for trust auth (test-only)
RUN sed -i 's/peer/trust/g' /etc/postgresql/15/main/pg_hba.conf && \\
    sed -i 's/scram-sha-256/trust/g' /etc/postgresql/15/main/pg_hba.conf

{code}

{self.clear_env}

"""


class ActivistBackendImageDefault(Image):
    """PR-specific image for activist backend."""

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
        return ActivistBackendImageBase(self.pr, self.config)

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

cd /home/{repo}/backend
pip install -r requirements-dev.txt || true
""".format(repo=self.pr.repo, base_sha=self.pr.base.sha),
            ),
            File(
                ".",
                "run.sh",
                """\
#!/bin/bash
set -eo pipefail

export SECRET_KEY="ONLY_FOR_TESTING"
export DATABASE_PORT=5432
export DATABASE_HOST=localhost
export DATABASE_NAME=postgres
export DATABASE_USER=postgres
export DATABASE_PASSWORD=postgres

pg_ctlcluster 15 main start || true

cd /home/{repo}/backend
python -m pytest . -vv
""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "test-run.sh",
                """\
#!/bin/bash
set -eo pipefail

export SECRET_KEY="ONLY_FOR_TESTING"
export DATABASE_PORT=5432
export DATABASE_HOST=localhost
export DATABASE_NAME=postgres
export DATABASE_USER=postgres
export DATABASE_PASSWORD=postgres

pg_ctlcluster 15 main start || true

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch
cd /home/{repo}/backend
python -m pytest . -vv
""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "fix-run.sh",
                """\
#!/bin/bash
set -eo pipefail

export SECRET_KEY="ONLY_FOR_TESTING"
export DATABASE_PORT=5432
export DATABASE_HOST=localhost
export DATABASE_NAME=postgres
export DATABASE_USER=postgres
export DATABASE_PASSWORD=postgres

pg_ctlcluster 15 main start || true

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
cd /home/{repo}/backend
python -m pytest . -vv
""".format(repo=self.pr.repo),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        if isinstance(image, str):
            raise ValueError("ActivistBackendImageDefault dependency must be an Image")
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


@Instance.register("activist-org", "activist_1613_to_1613")
class ACTIVIST_1613_TO_1613(Instance):
    """Instance for activist PR 1613 (backend, Django/pytest)."""

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return ActivistBackendImageDefault(self.pr, self._config)

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

        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        # pytest -vv output:
        #   events/tests/test_event_api.py::test_list_events PASSED  [ 10%]
        #   events/tests/test_event_api.py::test_create_event FAILED [ 20%]
        #   events/tests/test_event_api.py::test_skip_event SKIPPED  [ 30%]
        re_pytest = re.compile(
            r"^\s*(\S+::[\S]+)\s+(PASSED|FAILED|SKIPPED|ERROR|XFAIL|XPASS)",
            re.MULTILINE,
        )

        for match in re_pytest.finditer(clean_log):
            test_name = match.group(1)
            status = match.group(2)
            if status == "PASSED" or status == "XPASS":
                passed_tests.add(test_name)
            elif status == "FAILED" or status == "ERROR":
                failed_tests.add(test_name)
            elif status == "SKIPPED" or status == "XFAIL":
                skipped_tests.add(test_name)

        # Worst result wins
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
