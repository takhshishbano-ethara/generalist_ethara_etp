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
            code = f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/
RUN apt update && apt install -y git python3 jq

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
export CI=true

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

# node-gyp needs 'python' but only 'python3' is installed
ln -sf /usr/bin/python3 /usr/bin/python

npm install || npm install --ignore-scripts || true

npm rebuild sqlite3 2>/dev/null || true
npm rebuild better-sqlite3 2>/dev/null || true

npm install natives@1.1.6 2>/dev/null || true
npm install graceful-fs@4.2.11 2>/dev/null || true
npm install sqlite3@5.1.7 2>/dev/null || true

TS_MAJOR=$(node -e "try{{console.log(require('typescript/package.json').version.split('.')[0])}}catch(e){{console.log('99')}}" 2>/dev/null)
if [ "$TS_MAJOR" -lt 3 ] 2>/dev/null; then
    npm install @types/node@4.2.23 2>/dev/null || true
fi

# Relax tsconfig for ALL PRs (noEmitOnError: false ensures JS emitted even with type errors)
node -e "
var fs = require('fs');
try {{
    var tsconfig = JSON.parse(fs.readFileSync('tsconfig.json', 'utf8'));
    tsconfig.compilerOptions = tsconfig.compilerOptions || {{}};
    tsconfig.compilerOptions.skipLibCheck = true;
    tsconfig.compilerOptions.noEmitOnError = false;
    tsconfig.compilerOptions.strictNullChecks = false;
    tsconfig.compilerOptions.noImplicitReturns = false;
    tsconfig.compilerOptions.noFallthroughCasesInSwitch = false;
    tsconfig.compilerOptions.noImplicitAny = false;
    fs.writeFileSync('tsconfig.json', JSON.stringify(tsconfig, null, 2));
}} catch(e) {{}}" 2>/dev/null || true

node -e "
var fs = require('fs');
try {{
    var pkg = JSON.parse(fs.readFileSync('package.json', 'utf8'));
    if (pkg.scripts && pkg.scripts.compile) {{
        if (pkg.scripts.compile.indexOf('|| true') === -1) {{
            pkg.scripts.compile = '(' + pkg.scripts.compile + ') || true';
        }}
    }}
    if (pkg.scripts && pkg.scripts.test) {{
        pkg.scripts.test = pkg.scripts.test.split('tsc &&').join('(tsc || true) &&');
    }}
    fs.writeFileSync('package.json', JSON.stringify(pkg, null, 2));
}} catch(e) {{}}" 2>/dev/null || true

find node_modules -path '*/node_modules/graceful-fs/fs.js' 2>/dev/null | while read f; do
  if grep -q natives "$f" 2>/dev/null; then
    dir=$(dirname "$f")
    rm -rf "$dir"
    cp -r node_modules/graceful-fs "$dir"
  fi
done

for cfg in ormconfig.sample.json ormconfig.circleci-common.json ormconfig.json.dist; do
    if [ -f "$cfg" ]; then
        python3 -c "
import json, re
with open('$cfg') as f:
    content = f.read()
# Remove trailing commas before ] or }} (escaped for .format)
content = re.sub(r',\\s*([\\]}}])', r'\\1', content)
config = json.loads(content)
if isinstance(config, list):
    for entry in config:
        if 'sqlite' in entry.get('name', '').lower() or 'better-sqlite3' in entry.get('type', '').lower():
            entry['skip'] = False
        else:
            entry['skip'] = True
with open('ormconfig.json', 'w') as f:
    json.dump(config, f, indent=2)
"
        break
    fi
done

mkdir -p temp

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e
export CI=true
export PATH=./node_modules/.bin:$PATH
export TS_NODE_FAST=true
export TS_NODE_TRANSPILE_ONLY=true

cd /home/{pr.repo}
npm test

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
export CI=true
export PATH=./node_modules/.bin:$PATH
export TS_NODE_FAST=true
export TS_NODE_TRANSPILE_ONLY=true

cd /home/{pr.repo}
git apply --whitespace=nowarn --reject /home/test.patch 2>&1 || true
find . -name '*.rej' -delete 2>/dev/null || true
npm test 2>&1 || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
export CI=true
export PATH=./node_modules/.bin:$PATH
export TS_NODE_FAST=true
export TS_NODE_TRANSPILE_ONLY=true

cd /home/{pr.repo}
git apply --exclude package-lock.json --exclude package.json --whitespace=nowarn /home/test.patch /home/fix.patch
npm test 2>&1 || true

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


@Instance.register("typeorm", "typeorm")
class Typeorm(Instance):
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

    def parse_log(self, test_log: str) -> TestResult:
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        ansi_escape = re.compile(r"\x1b\[[0-9;]*m")
        re_pass_test = re.compile(r"[✓✔]\s+(.+?)(?:\s+\(\d+(?:\.\d+)?\s*(?:ms|s)\))?$")
        re_fail_test = re.compile(r"\d+\)\s+(.+)$")
        re_summary = re.compile(r"\d+\s+(?:passing|failing|pending)")

        past_summary = False

        for line in test_log.splitlines():
            line = ansi_escape.sub("", line).strip()
            if not line:
                continue

            if re_summary.match(line):
                past_summary = True
                continue

            if past_summary:
                continue

            pass_match = re_pass_test.match(line)
            if pass_match:
                test = pass_match.group(1).strip()
                passed_tests.add(test)
                continue

            fail_match = re_fail_test.match(line)
            if fail_match:
                test = fail_match.group(1).strip()
                failed_tests.add(test)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
