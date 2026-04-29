import re
from typing import Optional, Union

from odoo.addons.aurora.tools.harness.image import Config, File, Image
from odoo.addons.aurora.tools.harness.instance import Instance, TestResult
from odoo.addons.aurora.tools.harness.pull_request import PullRequest


class GinOldImageBase(Image):
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
        return "golang:1.13"

    def image_tag(self) -> str:
        return "base-old"

    def workdir(self) -> str:
        return "base-old"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()

        if self.config.need_clone:
            code = f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""FROM {image_name}

{self.global_env}

ENV GO111MODULE=off
ENV GOPATH=/go

WORKDIR /home/

{code}

{self.clear_env}

"""


class GinOldImageDefault(Image):
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
        return GinOldImageBase(self.pr, self.config)

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
                """#!/bin/bash
set -e

export GOPATH=/go
export GO111MODULE=off
mkdir -p $GOPATH/src/github.com/{pr.org}
ln -sf /home/{pr.repo} $GOPATH/src/github.com/{pr.org}/{pr.repo}
cd $GOPATH/src/github.com/{pr.org}/{pr.repo}

git reset --hard
git checkout {pr.base.sha}

rm -rf vendor 2>/dev/null || true
go get -v -t -d ./... 2>&1 || true
go get -v -t ./... 2>&1 || true
go test -v -count=1 ./... || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash

export GOPATH=/go
export GO111MODULE=off
cd $GOPATH/src/github.com/{pr.org}/{pr.repo}
git checkout -- . 2>/dev/null || true
go test -v -count=1 ./... 2>&1 || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash

export GOPATH=/go
export GO111MODULE=off
cd $GOPATH/src/github.com/{pr.org}/{pr.repo}
git checkout -- . 2>/dev/null || true
git apply --whitespace=nowarn /home/test.patch 2>/dev/null || true
go get -t -d ./... 2>/dev/null || true
go test -v -count=1 ./... 2>&1 || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash

export GOPATH=/go
export GO111MODULE=off
cd $GOPATH/src/github.com/{pr.org}/{pr.repo}
git checkout -- . 2>/dev/null || true
git apply --whitespace=nowarn /home/test.patch /home/fix.patch 2>/dev/null || true
go get -t -d ./... 2>/dev/null || true
go test -v -count=1 ./... 2>&1 || true

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

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

RUN bash /home/prepare.sh

{self.clear_env}

"""


@Instance.register("gin-gonic", "gin_753_to_0")
class GinOld(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return GinOldImageDefault(self.pr, self._config)

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
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        re_pass = re.compile(r"--- PASS: (\S+)")
        re_fail = re.compile(r"--- FAIL: (\S+)")
        re_skip = re.compile(r"--- SKIP: (\S+)")

        def base_name(name: str) -> str:
            idx = name.rfind("/")
            return name[:idx] if idx != -1 else name

        for line in test_log.splitlines():
            line = line.strip()
            m = re_pass.match(line)
            if m:
                name = base_name(m.group(1))
                if name not in failed_tests:
                    passed_tests.add(name)
                continue
            m = re_fail.match(line)
            if m:
                name = base_name(m.group(1))
                passed_tests.discard(name)
                failed_tests.add(name)
                continue
            m = re_skip.match(line)
            if m:
                name = base_name(m.group(1))
                if name not in passed_tests and name not in failed_tests:
                    skipped_tests.add(name)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
