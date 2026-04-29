import re
from typing import Optional, Union

from odoo.addons.aurora.tools.harness.image import Config, File, Image
from odoo.addons.aurora.tools.harness.instance import Instance, TestResult
from odoo.addons.aurora.tools.harness.pull_request import PullRequest


# =============================================================================
# G1: v241-v242 — PRs {8862, 10171}
# Travis CI era, ubuntu:20.04, unpinned meson, --werror -Dsplit-usr=true
# =============================================================================


class ImageDefault(Image):
    """G1: v241-v242 — ubuntu:20.04, build-dep-style packages, unpinned meson."""

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
        return "ubuntu:20.04"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        build_cmd = (
            "meson --werror -Dtests=unsafe -Dslow-tests=true"
            " -Dsplit-usr=true build"
            " && ninja -C build"
        )
        test_cmd = "meson test -C build --print-errorlogs"

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

cd /home/{repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {sha}
bash /home/check_git_changes.sh

""".format(repo=self.pr.repo, sha=self.pr.base.sha),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{repo}
{build_cmd}
{test_cmd}
""".format(repo=self.pr.repo, build_cmd=build_cmd, test_cmd=test_cmd),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch
{build_cmd}
{test_cmd}

""".format(repo=self.pr.repo, build_cmd=build_cmd, test_cmd=test_cmd),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
{build_cmd}
{test_cmd}

""".format(repo=self.pr.repo, build_cmd=build_cmd, test_cmd=test_cmd),
            ),
        ]

    def dockerfile(self) -> str:
        if self.config.need_clone:
            code = f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM ubuntu:20.04

{self.global_env}

WORKDIR /home/
ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=Etc/UTC
RUN apt-get update && apt-get install -y \
    build-essential \
    clang \
    gettext \
    git \
    gperf \
    libapparmor-dev \
    libaudit-dev \
    libblkid-dev \
    libcap-dev \
    libcryptsetup-dev \
    libcurl4-gnutls-dev \
    libdbus-1-dev \
    libdw-dev \
    libglib2.0-dev \
    libgnutls28-dev \
    libidn11-dev \
    libidn2-dev \
    libkmod-dev \
    liblz4-dev \
    liblzma-dev \
    libmicrohttpd-dev \
    libmount-dev \
    libpam0g-dev \
    libpcre2-dev \
    libqrencode-dev \
    libseccomp-dev \
    libselinux1-dev \
    libssl-dev \
    libxkbcommon-dev \
    libxtables-dev \
    pkg-config \
    python3-libevdev \
    python3-pip \
    python3-pyparsing \
    python3-setuptools \
    zlib1g-dev \
    && pip3 install meson \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

{code}

{copy_commands}

RUN bash /home/prepare.sh

{self.clear_env}

"""


@Instance.register("systemd", "systemd_10171_to_8862")
class SYSTEMD_10171_TO_8862(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        if run_cmd:
            return run_cmd
        return "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        if test_patch_run_cmd:
            return test_patch_run_cmd

        return (
            "bash -c '"
            "cd /home/systemd && "
            "(git apply --whitespace=nowarn /home/test.patch 2>/dev/null || "
            "git apply --3way --whitespace=nowarn /home/test.patch 2>/dev/null || true) && "
            "meson --werror -Dtests=unsafe -Dslow-tests=true"
            " -Dsplit-usr=true build"
            " && ninja -C build"
            " && meson test -C build --print-errorlogs"
            "'"
        )

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        if fix_patch_run_cmd:
            return fix_patch_run_cmd

        return (
            "bash -c '"
            "cd /home/systemd && "
            "(git apply --whitespace=nowarn /home/test.patch 2>/dev/null || "
            "git apply --3way --whitespace=nowarn /home/test.patch 2>/dev/null || true) && "
            "(git apply --whitespace=nowarn /home/fix.patch 2>/dev/null || "
            "git apply --3way --whitespace=nowarn /home/fix.patch 2>/dev/null || true) && "
            "meson --werror -Dtests=unsafe -Dslow-tests=true"
            " -Dsplit-usr=true build"
            " && ninja -C build"
            " && meson test -C build --print-errorlogs"
            "'"
        )

    def parse_log(self, test_log: str) -> TestResult:
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        re_result = re.compile(
            r"^\s*\d+/\d+\s+(.+?)\s+(OK|FAIL|SKIP|EXPECTEDFAIL|TIMEOUT|ERROR)\s+[\d.]+s"
        )

        for line in test_log.splitlines():
            line = line.strip()
            if not line:
                continue

            match = re_result.match(line)
            if match:
                test_name = match.group(1)
                status = match.group(2)
                if status == "OK":
                    passed_tests.add(test_name)
                elif status in ("FAIL", "TIMEOUT", "ERROR"):
                    failed_tests.add(test_name)
                elif status in ("SKIP", "EXPECTEDFAIL"):
                    skipped_tests.add(test_name)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
