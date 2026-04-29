import re
from typing import Optional, Union

from odoo.addons.aurora.tools.harness.image import Config, File, Image
from odoo.addons.aurora.tools.harness.instance import Instance, TestResult
from odoo.addons.aurora.tools.harness.pull_request import PullRequest


# =============================================================================
# G1: v10.2 Jewel — PRs {12917, 13107, 13507, 16294}
# ubuntu:16.04, autotools (autogen + configure + make), make check
# =============================================================================


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

    def dependency(self) -> str:
        return "ubuntu:16.04"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        build_cmd = (
            "./autogen.sh"
            " && ./configure"
            " --with-librocksdb-static"
            " --disable-static"
            " --with-radosgw"
            " --with-debug"
            " --without-lttng"
            " && make -j$(nproc)"
        )
        test_cmd = "make check"

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

git submodule update --init --recursive
apt-get update && apt-get install -y devscripts equivs

if [ -f debian/control ]; then
    sed -i 's/ceph-libboost-\([a-z-]*\)[0-9._]*-dev/libboost-\1-dev/g' debian/control
fi

./install-deps.sh || true
apt-get install -y libboost-all-dev || true

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

        return f"""FROM ubuntu:18.04

{self.global_env}

WORKDIR /home/
ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=Etc/UTC
RUN apt-get update && apt-get install -y \\
    autoconf \\
    automake \\
    build-essential \\
    ca-certificates \\
    curl \\
    git \\
    gnupg \\
    libtool \\
    lsb-release \\
    pkg-config \\
    python3 \\
    python3-pip \\
    sudo \\
    wget \\
    && apt-get clean && rm -rf /var/lib/apt/lists/*

{code}

{copy_commands}

RUN bash /home/prepare.sh

{self.clear_env}

"""


@Instance.register("ceph", "ceph_16294_to_12917")
class CEPH_16294_TO_12917(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ImageDefault(self.pr, self._config)

    _BUILD_CMD = (
        "cd /home/ceph && "
        "./autogen.sh && "
        "./configure --with-librocksdb-static --disable-static "
        "--with-radosgw --with-debug --without-lttng && "
        "make -j$(nproc)"
    )
    _TEST_CMD = "cd /home/ceph && make check"

    def run(self, run_cmd: str = "") -> str:
        if run_cmd:
            return run_cmd
        return f"bash -c '{self._BUILD_CMD} ; {self._TEST_CMD}'"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        if test_patch_run_cmd:
            return test_patch_run_cmd
        return (
            "bash -c '"
            "cd /home/ceph && "
            "(git apply --whitespace=nowarn /home/test.patch || "
            "git apply --whitespace=nowarn --3way /home/test.patch || true) && "
            f"{self._BUILD_CMD} ; {self._TEST_CMD}"
            "'"
        )

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        if fix_patch_run_cmd:
            return fix_patch_run_cmd
        return (
            "bash -c '"
            "cd /home/ceph && "
            "(git apply --whitespace=nowarn /home/test.patch || "
            "git apply --whitespace=nowarn --3way /home/test.patch || true) && "
            "(git apply --whitespace=nowarn /home/fix.patch || "
            "git apply --whitespace=nowarn --3way /home/fix.patch || true) && "
            f"{self._BUILD_CMD} ; {self._TEST_CMD}"
            "'"
        )

    def parse_log(self, test_log: str) -> TestResult:
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        re_pass = re.compile(r"^\s*PASS:\s+(.+)")
        re_fail = re.compile(r"^\s*FAIL:\s+(.+)")
        re_skip = re.compile(r"^\s*SKIP:\s+(.+)")

        for line in test_log.splitlines():
            line = line.strip()
            if not line:
                continue

            pass_match = re_pass.match(line)
            if pass_match:
                passed_tests.add(pass_match.group(1).strip())
                continue

            fail_match = re_fail.match(line)
            if fail_match:
                failed_tests.add(fail_match.group(1).strip())
                continue

            skip_match = re_skip.match(line)
            if skip_match:
                skipped_tests.add(skip_match.group(1).strip())

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
