import re
from typing import Optional, Union

from odoo.addons.aurora.tools.harness.image import Config, File, Image
from odoo.addons.aurora.tools.harness.instance import Instance, TestResult
from odoo.addons.aurora.tools.harness.pull_request import PullRequest


class TailcallImageBase(Image):
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
        return "rust:1.82"

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

{code}

{self.clear_env}

"""


class TailcallImageDefault(Image):
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
        return TailcallImageBase(self.pr, self.config)

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

# Downgrade proc-macro2 to 1.0.85 to fix proc-macro-error compilation
# proc-macro2 >= 1.0.86 removed From<proc_macro::Span> that proc-macro-error depends on
cargo update -p proc-macro2 --precise 1.0.85 2>&1 || true

# Downgrade time to 0.3.35 to fix compilation on newer Rust
# time 0.3.36 has compilation issues with certain Rust versions
cargo update -p time --precise 0.3.35 2>&1 || true

# Install cmake (needed for some PRs that depend on it)
apt-get update -qq && apt-get install -y -qq cmake 2>&1 || true

# First attempt — downloads all dependencies (may fail on apollo-tracing agent_id)
cargo test 2>&1 || true

# Patch async-graphql-extension-apollo-tracing to add missing agent_id field
# Search both registry and git checkouts with flexible name matching (hyphens or underscores)
for APOLLO_SRC in $(find /usr/local/cargo/registry/src /usr/local/cargo/git/checkouts -name 'mod.rs' -path '*/report_aggregator/*' 2>/dev/null); do
    if grep -q 'ReportHeader' "$APOLLO_SRC" 2>/dev/null; then
        if ! grep -q 'agent_id' "$APOLLO_SRC" 2>/dev/null; then
            echo "Patching $APOLLO_SRC to add agent_id field..."
            sed -i 's/let reported_header = ReportHeader {{/let reported_header = ReportHeader {{ agent_id: String::new(),/' "$APOLLO_SRC"
        fi
    fi
done

# Clean build cache so patched source gets recompiled
cargo clean -p async-graphql-extension-apollo-tracing 2>&1 || true

# Second attempt — should compile with the patch
cargo test 2>&1 || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}

# Re-apply apollo-tracing patch if needed (git checkout may have reset cargo cache)
for APOLLO_SRC in $(find /usr/local/cargo/registry/src /usr/local/cargo/git/checkouts -name 'mod.rs' -path '*/report_aggregator/*' 2>/dev/null); do
    if grep -q 'ReportHeader' "$APOLLO_SRC" 2>/dev/null && ! grep -q 'agent_id' "$APOLLO_SRC" 2>/dev/null; then
        sed -i 's/let reported_header = ReportHeader {{/let reported_header = ReportHeader {{ agent_id: String::new(),/' "$APOLLO_SRC"
        cargo clean -p async-graphql-extension-apollo-tracing 2>&1 || true
    fi
done

cargo test

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply /home/test.patch

for APOLLO_SRC in $(find /usr/local/cargo/registry/src /usr/local/cargo/git/checkouts -name 'mod.rs' -path '*/report_aggregator/*' 2>/dev/null); do
    if grep -q 'ReportHeader' "$APOLLO_SRC" 2>/dev/null && ! grep -q 'agent_id' "$APOLLO_SRC" 2>/dev/null; then
        sed -i 's/let reported_header = ReportHeader {{/let reported_header = ReportHeader {{ agent_id: String::new(),/' "$APOLLO_SRC"
        cargo clean -p async-graphql-extension-apollo-tracing 2>&1 || true
    fi
done

cargo test

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply /home/test.patch /home/fix.patch

for APOLLO_SRC in $(find /usr/local/cargo/registry/src /usr/local/cargo/git/checkouts -name 'mod.rs' -path '*/report_aggregator/*' 2>/dev/null); do
    if grep -q 'ReportHeader' "$APOLLO_SRC" 2>/dev/null && ! grep -q 'agent_id' "$APOLLO_SRC" 2>/dev/null; then
        sed -i 's/let reported_header = ReportHeader {{/let reported_header = ReportHeader {{ agent_id: String::new(),/' "$APOLLO_SRC"
        cargo clean -p async-graphql-extension-apollo-tracing 2>&1 || true
    fi
done

cargo test

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


@Instance.register("tailcallhq", "tailcall")
class Tailcall(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return TailcallImageDefault(self.pr, self._config)

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

        re_pass_tests = [re.compile(r"test (\S+)\s+\.\.\. ok")]
        re_fail_tests = [re.compile(r"test (\S+)\s+\.\.\. FAILED")]
        re_skip_tests = [re.compile(r"test (\S+)\s+\.\.\. ignored")]

        for line in test_log.splitlines():
            line = line.strip()

            for re_pass in re_pass_tests:
                match = re_pass.match(line)
                if match:
                    passed_tests.add(match.group(1))

            for re_fail in re_fail_tests:
                match = re_fail.match(line)
                if match:
                    failed_tests.add(match.group(1))

            for re_skip in re_skip_tests:
                match = re_skip.match(line)
                if match:
                    skipped_tests.add(match.group(1))

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
