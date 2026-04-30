import re
import textwrap
from typing import Optional, Union

from odoo.addons.aurora.tools.harness.image import Config, File, Image
from odoo.addons.aurora.tools.harness.instance import Instance, TestResult
from odoo.addons.aurora.tools.harness.pull_request import PullRequest


class BraveBrowserImageDefault(Image):
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
        return "node:18-bullseye"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _env_setup(self) -> str:
        return textwrap.dedent("""\
            export NVM_DIR="$HOME/.nvm"
            [ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"
            export npm_package_config_projects_chrome_dir=src
            export npm_package_config_projects_chrome_repository_url=https://chromium.googlesource.com/chromium/src.git
            export npm_package_config_projects_brave_core_dir=src/brave
            export npm_package_config_projects_brave_core_repository_url=https://github.com/brave/brave-core.git
        """)

    def files(self) -> list[File]:
        env = self._env_setup()
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
export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

nvm install || true
nvm use || true
npm install || true
""".format(pr=self.pr),
            ),
            File(
                ".",
                "brave_modules.test.js",
                textwrap.dedent("""\
                    jest.mock('child_process', () => ({
                      ...jest.requireActual('child_process'),
                      spawnSync: jest.fn(() => ({ status: 0, stdout: Buffer.from(''), stderr: Buffer.from('') })),
                      execSync: jest.fn(() => Buffer.from('')),
                    }));

                    const fs = require('fs');
                    const path = require('path');

                    const LIB_DIR = path.join(process.cwd(), 'lib');
                    const jsFiles = fs.readdirSync(LIB_DIR)
                      .filter(f => f.endsWith('.js') && !f.endsWith('.test.js'))
                      .sort();

                    describe('module loading', () => {
                      jsFiles.forEach(file => {
                        test('lib/' + file + ' loads without error', () => {
                          expect(() => require(path.join(LIB_DIR, file))).not.toThrow();
                        });
                      });
                    });

                    describe('test runner', () => {
                      test('lib/test.js exports a function', () => {
                        const testFn = require(path.join(LIB_DIR, 'test'));
                        expect(typeof testFn).toBe('function');
                      });

                      test('test function executes with basic options', () => {
                        const testFn = require(path.join(LIB_DIR, 'test'));
                        expect(() => testFn('brave_unit_tests', 'Release', {
                          v: 1, filter: '', C: '/tmp/out'
                        })).not.toThrow();
                      });
                    });
                """),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
{env}
cd /home/{pr.repo}

nvm use || true
npm install || true
mkdir -p src/brave
cp /home/brave_modules.test.js lib/brave_modules.test.js
jest --no-cache --forceExit --passWithNoTests 2>&1 || true

""".format(pr=self.pr, env=env),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
{env}
cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch || git apply --whitespace=nowarn --reject /home/test.patch || true

nvm use || true
npm install || true
mkdir -p src/brave
cp /home/brave_modules.test.js lib/brave_modules.test.js
jest --no-cache --forceExit --passWithNoTests 2>&1 || true

""".format(pr=self.pr, env=env),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
{env}
cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch || git apply --whitespace=nowarn --reject /home/test.patch || true
git apply --whitespace=nowarn /home/fix.patch || git apply --whitespace=nowarn --reject /home/fix.patch || true

nvm use || true
npm install || true
mkdir -p src/brave
cp /home/brave_modules.test.js lib/brave_modules.test.js
jest --no-cache --forceExit --passWithNoTests 2>&1 || true

""".format(pr=self.pr, env=env),
            ),
        ]

    def dockerfile(self) -> str:
        base_img = self.dependency()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        proxy_setup = ""
        proxy_cleanup = ""

        if self.global_env:
            proxy_host = None
            proxy_port = None

            for line in self.global_env.splitlines():
                match = re.match(
                    r"^ENV\s*(http[s]?_proxy)=http[s]?://([^:]+):(\d+)", line
                )
                if match:
                    proxy_host = match.group(2)
                    proxy_port = match.group(3)
                    break

            if proxy_host and proxy_port:
                proxy_setup = textwrap.dedent(
                    f"""
                    RUN mkdir -p $HOME && \\
                        touch $HOME/.npmrc && \\
                        echo "proxy=http://{proxy_host}:{proxy_port}" >> $HOME/.npmrc && \\
                        echo "https-proxy=http://{proxy_host}:{proxy_port}" >> $HOME/.npmrc && \\
                        echo "strict-ssl=false" >> $HOME/.npmrc
                """
                )

                proxy_cleanup = textwrap.dedent(
                    """
                    RUN rm -f $HOME/.npmrc
                """
                )

        return f"""FROM {base_img}

{self.global_env}

WORKDIR /home/
ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=Etc/UTC

RUN apt-get update && apt-get install -y git build-essential python3

RUN curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.39.7/install.sh | bash && \\
    export NVM_DIR="$HOME/.nvm" && \\
    [ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}

WORKDIR /home/{self.pr.repo}

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}
RUN mkdir -p src/brave

RUN npm install -g jest

WORKDIR /home

{proxy_setup}

{copy_commands}

RUN bash /home/prepare.sh

{proxy_cleanup}

{self.clear_env}

CMD ["/bin/bash"]
"""


@Instance.register("brave", "brave-browser")
class BraveBrowser(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return BraveBrowserImageDefault(self.pr, self._config)

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

        # Jest output patterns:
        #   PASS  lib/foo.test.js
        #   ✓ test name (5 ms)
        #   ✕ test name (3 ms)
        #   ○ skipped test name
        passed_res = [
            re.compile(r"^\s*[✔✓]\s+(.*?)(?:\s*\(\d+(?:\.\d+)?\s*(?:ms|s)\))?\s*$"),
            re.compile(r"^\s*PASS\s+(\S+\.test\.\w+)"),
        ]

        failed_res = [
            re.compile(r"^\s*[×✗✕]\s+(.*?)(?:\s*\(\d+(?:\.\d+)?\s*(?:ms|s)\))?\s*$"),
            re.compile(r"^\s*FAIL\s+(\S+\.test\.\w+)"),
        ]

        skipped_res = [
            re.compile(r"^\s*[○]\s+(.*?)$"),
            re.compile(r"^\s*SKIP:?\s+([^\(]+)"),
        ]

        ansi_escape = re.compile(r"\x1b\[[0-9;]*m")
        for line in test_log.splitlines():
            line = ansi_escape.sub("", line)

            stripped = line.strip()
            if (
                stripped.startswith("at ")
                or stripped.startswith("npm ")
                or stripped.startswith("node:")
            ):
                continue

            for passed_re in passed_res:
                m = passed_re.search(line)
                if m and m.group(1) not in failed_tests:
                    passed_tests.add(m.group(1))

            for failed_re in failed_res:
                m = failed_re.search(line)
                if m:
                    failed_tests.add(m.group(1))
                    if m.group(1) in passed_tests:
                        passed_tests.remove(m.group(1))

            for skipped_re in skipped_res:
                m = skipped_re.search(line)
                if m:
                    skipped_tests.add(m.group(1))

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
