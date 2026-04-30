import re
import json
from typing import Optional, Union

from odoo.addons.aurora.tools.harness.image import Config, File, Image
from odoo.addons.aurora.tools.harness.instance import Instance, TestResult
from odoo.addons.aurora.tools.harness.pull_request import PullRequest


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
        if self.pr.number <= 75:
            return "ubuntu:20.04"     # Ancient: PRs 41, 58, 75
        elif self.pr.number <= 639:
            return "rust:1.45.0"      # Mid: PRs 128-639
        else:
            return "rust:1.77.2"      # Modern: PRs 812-1781

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        if self.pr.number <= 75:
            return [
                File(".", "fix.patch", f"{self.pr.fix_patch}"),
                File(".", "test.patch", f"{self.pr.test_patch}"),
                File(".", "prepare.sh",
                    """ls -F
###ACTION_DELIMITER###
apt-get update
###ACTION_DELIMITER###
apt-get install -y rustc cargo build-essential pkg-config curl tar gzip binutils
###ACTION_DELIMITER###
cargo test
###ACTION_DELIMITER###
cargo update
###ACTION_DELIMITER###
cargo test
###ACTION_DELIMITER###
RUST_BACKTRACE=1 cargo test
###ACTION_DELIMITER###
apt-get remove -y rustc cargo
###ACTION_DELIMITER###
curl https://sh.rustup.rs -sSf | sh -s -- -y
###ACTION_DELIMITER###
export PATH="$HOME/.cargo/bin:$PATH" && cargo test
###ACTION_DELIMITER###
export PATH="$HOME/.cargo/bin:$PATH" && rustup install 1.25.0
###ACTION_DELIMITER###
export PATH="$HOME/.cargo/bin:$PATH" && rustup default 1.25.0
###ACTION_DELIMITER###
export PATH="$HOME/.cargo/bin:$PATH" && cargo test
###ACTION_DELIMITER###
rm Cargo.lock
###ACTION_DELIMITER###
export PATH="$HOME/.cargo/bin:$PATH" && cargo test
###ACTION_DELIMITER###
git checkout -- Cargo.lock
###ACTION_DELIMITER###
export PATH="$HOME/.cargo/bin:$PATH" && cargo build
###ACTION_DELIMITER###
export PATH="$HOME/.cargo/bin:$PATH" && cargo test
###ACTION_DELIMITER###
echo 'export PATH="$HOME/.cargo/bin:$PATH" && cargo test' > /home/fd/test_commands.sh""",
                ),
                File(".", "run.sh", """#!/bin/bash
cd /home/{pr.repo}
export PATH="$HOME/.cargo/bin:$PATH" && cargo test

""".format(pr=self.pr)),
                File(".", "test-run.sh", """#!/bin/bash
cd /home/{pr.repo}
if ! git -C /home/{pr.repo} apply --whitespace=nowarn /home/test.patch; then
    echo "Warning: git apply failed, retrying without binary files" >&2
    git -C /home/{pr.repo} checkout -- .
    git -C /home/{pr.repo} apply --whitespace=nowarn --exclude='*.png' --exclude='*.jpg' --exclude='*.gif' --exclude='*.ico' --exclude='*.svg' /home/test.patch || {{
        echo "Error: git apply failed even without binary files" >&2
        exit 1
    }}
fi
export PATH="$HOME/.cargo/bin:$PATH" && cargo test

""".format(pr=self.pr)),
                File(".", "fix-run.sh", """#!/bin/bash
cd /home/{pr.repo}
if ! git -C /home/{pr.repo} apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Warning: git apply failed, retrying without binary files" >&2
    git -C /home/{pr.repo} checkout -- .
    git -C /home/{pr.repo} apply --whitespace=nowarn --exclude='*.png' --exclude='*.jpg' --exclude='*.gif' --exclude='*.ico' --exclude='*.svg' /home/test.patch /home/fix.patch || {{
        echo "Error: git apply failed even without binary files" >&2
        exit 1
    }}
fi
export PATH="$HOME/.cargo/bin:$PATH" && cargo test

""".format(pr=self.pr)),
            ]
        else:
            return [
                File(".", "fix.patch", f"{self.pr.fix_patch}"),
                File(".", "test.patch", f"{self.pr.test_patch}"),
                File(".", "prepare.sh",
                    """ls -F
###ACTION_DELIMITER###
cargo test --verbose
###ACTION_DELIMITER###
echo 'cargo test --verbose' > /home/fd/test_commands.sh""",
                ),
                File(
                    ".",
                    "run.sh",
                    """#!/bin/bash
cd /home/{pr.repo}
cargo test --verbose

""".format(pr=self.pr),
                ),
                File(
                    ".",
                    "test-run.sh",
                    """#!/bin/bash
cd /home/{pr.repo}
if ! git -C /home/{pr.repo} apply --whitespace=nowarn /home/test.patch; then
    echo "Warning: git apply failed, retrying without binary files" >&2
    git -C /home/{pr.repo} checkout -- .
    git -C /home/{pr.repo} apply --whitespace=nowarn --exclude='*.png' --exclude='*.jpg' --exclude='*.gif' --exclude='*.ico' --exclude='*.svg' /home/test.patch || {{
        echo "Error: git apply failed even without binary files" >&2
        exit 1
    }}
fi
cargo test --verbose

""".format(pr=self.pr),
                ),
                File(
                    ".",
                    "fix-run.sh",
                    """#!/bin/bash
cd /home/{pr.repo}
if ! git -C /home/{pr.repo} apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Warning: git apply failed, retrying without binary files" >&2
    git -C /home/{pr.repo} checkout -- .
    git -C /home/{pr.repo} apply --whitespace=nowarn --exclude='*.png' --exclude='*.jpg' --exclude='*.gif' --exclude='*.ico' --exclude='*.svg' /home/test.patch /home/fix.patch || {{
        echo "Error: git apply failed even without binary files" >&2
        exit 1
    }}
fi
cargo test --verbose

""".format(pr=self.pr),
                ),
            ]

    def dockerfile(self) -> str:
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        if self.pr.number <= 75:
            # Ancient era: ubuntu + rustup
            dockerfile_content = """
FROM ubuntu:20.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y git curl build-essential pkg-config

RUN curl https://sh.rustup.rs -sSf | sh -s -- -y
ENV PATH="/root/.cargo/bin:${{PATH}}"
RUN rustup install 1.27.0 && rustup default 1.27.0

RUN if [ ! -f /bin/bash ]; then         if command -v apk >/dev/null 2>&1; then             apk add --no-cache bash;         elif command -v apt-get >/dev/null 2>&1; then             apt-get update && apt-get install -y bash;         elif command -v yum >/dev/null 2>&1; then             yum install -y bash;         else             exit 1;         fi     fi

WORKDIR /home/
COPY fix.patch /home/
COPY test.patch /home/
RUN git clone https://github.com/sharkdp/fd.git /home/fd

WORKDIR /home/fd
RUN git reset --hard
RUN git checkout {pr.base.sha}
"""
        elif self.pr.number <= 639:
            # Mid era
            dockerfile_content = """
FROM rust:1.45.0

ENV DEBIAN_FRONTEND=noninteractive

RUN sed -i 's|deb.debian.org|archive.debian.org|g' /etc/apt/sources.list && sed -i '/security.debian.org/d' /etc/apt/sources.list

RUN apt-get update && apt-get install -y git

RUN if [ ! -f /bin/bash ]; then         if command -v apk >/dev/null 2>&1; then             apk add --no-cache bash;         elif command -v apt-get >/dev/null 2>&1; then             apt-get update && apt-get install -y bash;         elif command -v yum >/dev/null 2>&1; then             yum install -y bash;         else             exit 1;         fi     fi

WORKDIR /home/
COPY fix.patch /home/
COPY test.patch /home/
RUN git clone https://github.com/sharkdp/fd.git /home/fd

WORKDIR /home/fd
RUN git reset --hard
RUN git checkout {pr.base.sha}
"""
        else:
            # Modern era
            dockerfile_content = """
FROM rust:1.77.2

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y git

RUN if [ ! -f /bin/bash ]; then         if command -v apk >/dev/null 2>&1; then             apk add --no-cache bash;         elif command -v apt-get >/dev/null 2>&1; then             apt-get update && apt-get install -y bash;         elif command -v yum >/dev/null 2>&1; then             yum install -y bash;         else             exit 1;         fi     fi

WORKDIR /home/
COPY fix.patch /home/
COPY test.patch /home/
RUN git clone https://github.com/sharkdp/fd.git /home/fd

WORKDIR /home/fd
RUN git reset --hard
RUN git checkout {pr.base.sha}
"""
        dockerfile_content += f"\n{copy_commands}\n"
        return dockerfile_content.format(pr=self.pr)


@Instance.register("sharkdp", "fd_1781_to_41")
class FD_1781_TO_41(Instance):
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

        return "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        if fix_patch_run_cmd:
            return fix_patch_run_cmd

        return "bash /home/fix-run.sh"

    def parse_log(self, log: str) -> TestResult:
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()
        import re

        passed_regex = re.compile(r"test ([\w:]+) \.\.\. ok")
        failed_regex = re.compile(r"failures:\n((?:    .*\n)+)")
        failed_name_regex = re.compile(r"    (.*)")
        ignored_regex = re.compile(r"test ([\w:]+) \.\.\. ignored")
        for line in log.splitlines():
            match = passed_regex.match(line)
            if match:
                passed_tests.add(match.group(1))
                continue
            match = ignored_regex.match(line)
            if match:
                skipped_tests.add(match.group(1))
                continue
        match = failed_regex.search(log)
        if match:
            failed_block = match.group(1)
            for line in failed_block.splitlines():
                name_match = failed_name_regex.match(line)
                if name_match:
                    failed_tests.add(name_match.group(1).strip())

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
