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

    def dependency(self) -> str:
        return "python:3.9-slim"

    def image_prefix(self) -> str:
        return "envagent"

    def image_tag(self) -> str:
        return "base-python39"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()

        if self.config.need_clone:
            code = f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""\
# syntax=docker/dockerfile:1.6
ARG TARGETARCH
ARG BASE_COMMIT

FROM {image_name}

ARG http_proxy=""
ARG https_proxy=""
ARG HTTP_PROXY=""
ARG HTTPS_PROXY=""
ARG no_proxy="localhost,127.0.0.1,::1"
ARG NO_PROXY="localhost,127.0.0.1,::1"

ENV http_proxy=${{http_proxy}} \\
    https_proxy=${{https_proxy}} \\
    HTTP_PROXY=${{HTTP_PROXY}} \\
    HTTPS_PROXY=${{HTTPS_PROXY}} \\
    no_proxy=${{no_proxy}} \\
    NO_PROXY=${{NO_PROXY}} \\
    DEBIAN_FRONTEND=noninteractive \\
    TZ=UTC

RUN mkdir -p /etc/pki/tls/certs /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

WORKDIR /home/
RUN apt-get update && apt-get install -y --no-install-recommends git build-essential curl ca-certificates && rm -rf /var/lib/apt/lists/*
{code}

WORKDIR /home/{self.pr.repo}
RUN git checkout ${{BASE_COMMIT}}

# Install core deps without tokenizers (needs Rust, most tests skip it)
RUN pip install --upgrade pip setuptools wheel
# Read huggingface-hub version pin from THIS version's setup.py and install it
RUN python3 -c "import re; m=re.search(r'\\"(huggingface-hub[^\\"]*)\\\"', open('setup.py').read()); print(m.group(1) if m else 'huggingface-hub>=0.1.0,<1.0')" | xargs pip install
RUN pip install 'filelock' 'numpy>=1.17' 'packaging>=20.0' 'pyyaml>=5.1' 'regex!=2019.12.17' 'requests' 'sacremoses' 'tqdm>=4.27' 'importlib_metadata' || true
RUN pip install --no-deps -e . || true
RUN pip install pytest pytest-xdist timeout-decorator parameterized psutil || true
# Install datasets without deps to avoid upgrading huggingface-hub
RUN pip install --no-deps datasets || true
# Install PyTorch CPU-only to enable model tests (required for FAIL->PASS transitions)
RUN pip install torch --index-url https://download.pytorch.org/whl/cpu || true

LABEL org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}"
CMD ["bash"]
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

    def dependency(self) -> Image:
        return ImageBase(self.pr, self.config)

    def image_prefix(self) -> str:
        return "envagent"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo_name = self.pr.repo
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
                "prepare.sh",
                """ls -la
###ACTION_DELIMITER###
ls tests
###ACTION_DELIMITER###
pip install -e ".[testing]"
###ACTION_DELIMITER###
pytest --no-header -rA --tb=no -p no:cacheprovider -v
###ACTION_DELIMITER###
python -m pytest -n auto --dist=loadfile -s -v ./tests/
###ACTION_DELIMITER###
echo 'python -m pytest -n auto --dist=loadfile -s -v ./tests/' > test_commands.sh
###ACTION_DELIMITER###
cat test_commands.sh""",
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/[[REPO_NAME]]
python -m pytest -n auto --dist=loadfile -s -v ./tests/

""".replace("[[REPO_NAME]]", repo_name),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/[[REPO_NAME]]
git apply --whitespace=nowarn /home/test.patch || \
git apply --whitespace=nowarn --exclude='*.png' --exclude='*.jpg' --exclude='*.gif' /home/test.patch || \
{ echo "Error: git apply failed" >&2; exit 1; }
python -m pytest -n auto --dist=loadfile -s -v ./tests/

""".replace("[[REPO_NAME]]", repo_name),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/[[REPO_NAME]]
git apply --whitespace=nowarn /home/test.patch || \
git apply --whitespace=nowarn --exclude='*.png' --exclude='*.jpg' --exclude='*.gif' /home/test.patch || \
{ echo "Error: test patch apply failed" >&2; exit 1; }
git apply --whitespace=nowarn /home/fix.patch || \
git apply --whitespace=nowarn --exclude='*.png' --exclude='*.jpg' --exclude='*.gif' /home/fix.patch || \
{ echo "Warning: fix patch applied with exclusions" >&2; }
python -m pytest -n auto --dist=loadfile -s -v ./tests/

""".replace("[[REPO_NAME]]", repo_name),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""\
# syntax=docker/dockerfile:1.6
ARG TARGETARCH
ARG BASE_COMMIT

FROM {name}:{tag}

ARG http_proxy=""
ARG https_proxy=""
ARG HTTP_PROXY=""
ARG HTTPS_PROXY=""
ARG no_proxy="localhost,127.0.0.1,::1"
ARG NO_PROXY="localhost,127.0.0.1,::1"

ENV http_proxy=${{http_proxy}} \\
    https_proxy=${{https_proxy}} \\
    HTTP_PROXY=${{HTTP_PROXY}} \\
    HTTPS_PROXY=${{HTTPS_PROXY}} \\
    no_proxy=${{no_proxy}} \\
    NO_PROXY=${{NO_PROXY}} \\
    TZ=UTC

WORKDIR /home/transformers
RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

{copy_commands}

LABEL org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}"
CMD ["bash"]
"""


@Instance.register("huggingface", "transformers")
class TRANSFORMERS(Instance):
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
        # Parse the log content and extract test execution results.
        # Strip ANSI escape codes first to ensure regex patterns match cleanly
        log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", log)

        passed_tests: set[str] = set()  # Tests that passed successfully
        failed_tests: set[str] = set()  # Tests that failed
        skipped_tests: set[str] = set()  # Tests that were skipped

        # Extract passed tests using regex
        passed_pattern = re.compile(r"PASSED\s+(\S+)")
        passed_tests.update(passed_pattern.findall(log))
        # Extract failed tests from underlined sections (e.g., [2544] ________________________ ArrayXDTest.test_from_dict_4d _________________________)
        failed_underline_pattern = re.compile(
            r".*_{20,}\s*([\w.:_]+)\s*_{20,}.*"
        )  # Matches TestClass.test_method, tests::TestClass::test_method, etc., with leading/trailing content
        failed_tests.update(failed_underline_pattern.findall(log))
        # Extract failed tests from FAILED lines (e.g., FAILED tests/test_arrow_dataset.py::BaseDatasetTest::test_new_features_on_disk)
        failed_pattern = re.compile(
            r"FAILED\s+([\w/]+\.py::[\w:]+)"
        )  # Matches full test path with ::
        failed_tests.update(failed_pattern.findall(log))
        # Extract failed tests from ERROR lines (e.g., ERROR tests/test_metric_common.py::TestClass::test_method)
        error_pattern = re.compile(
            r"ERROR\s+([\w/]+\.py::[\w:]+)"
        )  # Captures full test path from errors
        failed_tests.update(error_pattern.findall(log))
        # Extract skipped tests from SKIPPED lines (if present)
        skipped_pattern = re.compile(r"SKIPPED\s+(\S+)")
        skipped_tests.update(skipped_pattern.findall(log))
        passed_tests -= failed_tests
        passed_tests -= skipped_tests
        failed_tests -= skipped_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
