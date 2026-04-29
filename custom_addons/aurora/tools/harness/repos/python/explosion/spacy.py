import re
from typing import Optional

from odoo.addons.aurora.tools.harness.image import Config, File, Image
from odoo.addons.aurora.tools.harness.instance import Instance, TestResult
from odoo.addons.aurora.tools.harness.pull_request import PullRequest


class SpaCyImageDefault(Image):
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
        return "python:3.10-slim"

    def image_prefix(self) -> str:
        return "envagent"

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
                """ls -la
###ACTION_DELIMITER###
apt-get update && apt-get install -y build-essential python3-dev gcc g++
###ACTION_DELIMITER###
pip install -U pip setuptools wheel cython numpy
###ACTION_DELIMITER###
pip install -r requirements.txt 2>/dev/null || true
###ACTION_DELIMITER###
pip install --no-build-isolation -e . 2>/dev/null || pip install --no-build-isolation . 2>/dev/null || pip install -e . 2>/dev/null || true
###ACTION_DELIMITER###
pip install pytest 2>/dev/null || true""",
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
python -m pytest --no-header -rA --tb=no -p no:cacheprovider -v --pyargs spacy

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
if ! git -C /home/{pr.repo} apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
pip install --no-build-isolation -e . 2>/dev/null || pip install --no-build-isolation . 2>/dev/null || true
python -m pytest --no-header -rA --tb=no -p no:cacheprovider -v --pyargs spacy

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
if ! git -C /home/{pr.repo} apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
pip install --no-build-isolation -e . 2>/dev/null || pip install --no-build-isolation . 2>/dev/null || true
python -m pytest --no-header -rA --tb=no -p no:cacheprovider -v --pyargs spacy

""".format(pr=self.pr),
            ),
        ]

    def dockerfile(self) -> str:
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        dockerfile_content = """
FROM python:3.10-slim

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y git build-essential python3-dev gcc g++

WORKDIR /home/
COPY fix.patch /home/
COPY test.patch /home/
RUN git clone https://github.com/{pr.org}/{pr.repo}.git /home/{pr.repo}

WORKDIR /home/{pr.repo}
RUN git reset --hard
RUN git checkout {pr.base.sha}

RUN pip install -U pip setuptools wheel cython numpy
RUN pip install -r requirements.txt 2>/dev/null || true
RUN pip install --no-build-isolation -e . 2>/dev/null || pip install --no-build-isolation . 2>/dev/null || pip install -e . 2>/dev/null || true
RUN pip install pytest 2>/dev/null || true
"""
        dockerfile_content += f"""
{copy_commands}
"""
        return dockerfile_content.format(pr=self.pr)


@Instance.register("explosion", "spaCy")
class SpaCy(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return SpaCyImageDefault(self.pr, self._config)

    _PIP_FIX = (
        "pip install numpy==1.26.4 2>/dev/null || true ; "
        'pip install "setuptools<70" 2>/dev/null || true ; '
        "pip install plac ujson six requests pathlib cymem preshed murmurhash regex ftfy 2>/dev/null || true ; "
        'python -c "import spacy" 2>/dev/null || '
        "pip install --no-build-isolation -e . 2>/dev/null || "
        "pip install --no-build-isolation . 2>/dev/null || "
        "pip install -e . 2>/dev/null || true ; "
    )

    _PYTEST_BASE = "python -m pytest --no-header -rA --tb=short -p no:cacheprovider -v"

    def _get_test_files(self) -> str:
        """Extract test file paths from test_patch diff headers.
        Returns space-separated file paths for pytest, or --pyargs spacy as fallback."""
        test_files = []
        for line in self.pr.test_patch.split("\n"):
            if line.startswith("+++ b/"):
                fpath = line[6:].strip()
                if fpath.endswith(".py") and (
                    "test_" in fpath.split("/")[-1]
                    or fpath.split("/")[-1].startswith("test")
                ):
                    test_files.append(fpath)
        if test_files:
            return " ".join(test_files)
        return "--pyargs spacy"

    def _pytest_cmd(self) -> str:
        return f"{self._PYTEST_BASE} {self._get_test_files()}"

    def run(self, run_cmd: str = "") -> str:
        if run_cmd:
            return run_cmd
        return (
            f"bash -c 'cd /home/{self.pr.repo} ; {self._PIP_FIX}{self._pytest_cmd()}'"
        )

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        if test_patch_run_cmd:
            return test_patch_run_cmd
        return (
            f"bash -c '"
            f"cd /home/{self.pr.repo} ; "
            f"git apply --whitespace=nowarn /home/test.patch "
            f"|| git apply --whitespace=nowarn --3way /home/test.patch "
            f"|| true ; "
            f"{self._PIP_FIX}"
            f"{self._pytest_cmd()}"
            f"'"
        )

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        if fix_patch_run_cmd:
            return fix_patch_run_cmd
        return (
            f"bash -c '"
            f"cd /home/{self.pr.repo} ; "
            f"git apply --whitespace=nowarn /home/test.patch "
            f"|| git apply --whitespace=nowarn --3way /home/test.patch "
            f"|| true ; "
            f"git apply --whitespace=nowarn /home/fix.patch "
            f"|| git apply --whitespace=nowarn --3way /home/fix.patch "
            f"|| true ; "
            f"{self._PIP_FIX}"
            f"{self._pytest_cmd()}"
            f"'"
        )

    def parse_log(self, log: str) -> TestResult:
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        clean_log = re.sub(r"\x1b\[[0-9;]*m", "", log)
        lines = clean_log.splitlines()

        passed_patterns = [
            re.compile(r"^(.*?)\s+PASSED\s+\[\s*\d+%\]$"),
            re.compile(r"^PASSED\s+(.*?)$"),
        ]
        failed_patterns = [
            re.compile(r"^(.*?)\s+FAILED\s+\[\s*\d+%\]$"),
            re.compile(r"^FAILED\s+(.*?)(?: - .*)?$"),
        ]
        skipped_pattern = re.compile(
            r"^SKIPPED\s+(?:\[\d+\]\s+)?(spacy/.*?(?:::\S+|\.py:\d+))\s*"
        )

        for line in lines:
            line = line.strip()
            for pattern in passed_patterns:
                match = pattern.match(line)
                if match:
                    passed_tests.add(match.group(1).strip())
                    break
            else:
                for pattern in failed_patterns:
                    match = pattern.match(line)
                    if match:
                        failed_tests.add(match.group(1).strip())
                        break
                else:
                    match = skipped_pattern.match(line)
                    if match:
                        skipped_tests.add(match.group(1).strip())

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
