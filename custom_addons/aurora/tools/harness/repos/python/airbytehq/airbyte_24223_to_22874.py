import re

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
        return "python:3.9-slim"

    def image_prefix(self) -> str:
        return "mswebench"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def extra_packages(self) -> list[str]:
        return []

    def extra_setup(self) -> str:
        return "RUN pip install --no-cache-dir pytest requests"

    def dockerfile(self) -> str:
        base = super().dockerfile()
        copy_commands = "\n".join(f"COPY {f.name} /home/" for f in self.files())
        return base.replace(
            'CMD ["/bin/bash"]',
            f'{copy_commands}\nRUN bash /home/prepare.sh\n\nCMD ["/bin/bash"]',
        )

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
                "prepare.sh",
                """#!/bin/bash
ls -F
###ACTION_DELIMITER###
pip install pytest requests
###ACTION_DELIMITER###
pytest --no-header -rA --tb=no -p no:cacheprovider --ignore=deprecated/ --override-ini="testpaths=." --continue-on-collection-errors || true
###ACTION_DELIMITER###
echo 'pytest --no-header -rA --tb=no -p no:cacheprovider --ignore=deprecated/ --override-ini="testpaths=." --continue-on-collection-errors' > test_commands.sh""",
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
cd /home/{repo}
pytest --no-header -rA --tb=no -p no:cacheprovider --ignore=deprecated/ --override-ini="testpaths=." --continue-on-collection-errors
""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch
pytest --no-header -rA --tb=no -p no:cacheprovider --ignore=deprecated/ --override-ini="testpaths=." --continue-on-collection-errors
""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
pytest --no-header -rA --tb=no -p no:cacheprovider --ignore=deprecated/ --override-ini="testpaths=." --continue-on-collection-errors
""".format(repo=self.pr.repo),
            ),
        ]


@Instance.register("airbytehq", "airbyte_24223_to_22874")
class AIRBYTE_24223_TO_22874(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return ImageDefault(self.pr, self._config)

    def _get_connector_dir(self) -> str:
        for line in self.pr.test_patch.split("\n"):
            if line.startswith("diff --git"):
                match = re.search(
                    r"a/(airbyte-integrations/connectors/[^/]+)/", line
                )
                if match:
                    return match.group(1)
        return ""

    def _make_cmd(self, patch_args: str = "") -> str:
        connector_dir = self._get_connector_dir()
        if not connector_dir:
            return "echo 'ERROR: Could not determine connector directory from test_patch'"

        parts = ["cd /home/airbyte"]
        if patch_args:
            parts.append(f"git apply --whitespace=nowarn {patch_args}")
        parts.extend([
            "pip install 'airbyte-cdk==0.29.0' pytest requests-mock requests_mock pytest-mock freezegun responses 2>/dev/null",
            f"cd {connector_dir}",
            "pip install -e . 2>/dev/null",
            "pytest --no-header -rA --tb=no -p no:cacheprovider --continue-on-collection-errors unit_tests/",
        ])
        cmd = " && ".join(parts)
        return f"bash -c '{cmd}'"

    def run(self, run_cmd: str = "") -> str:
        if run_cmd:
            return run_cmd
        return self._make_cmd()

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        if test_patch_run_cmd:
            return test_patch_run_cmd
        return self._make_cmd("/home/test.patch")

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        if fix_patch_run_cmd:
            return fix_patch_run_cmd
        return self._make_cmd("/home/test.patch /home/fix.patch")

    @staticmethod
    def _strip_ansi(text: str) -> str:
        return re.sub(r"\x1b\[[0-9;]*m", "", text)

    def parse_log(self, test_log: str) -> TestResult:
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()
        test_results = {}

        clean_log = self._strip_ansi(test_log)
        pattern = re.compile(r"^(PASSED|FAILED|ERROR|SKIPPED(?: \[[\d]+\])?)\s+(\S+)")
        for line in clean_log.splitlines():
            match = pattern.search(line)
            if match:
                status, test_name = match.group(1), match.group(2)
                test_name = test_name.strip()
                if "FAIL" in status or "ERROR" in status:
                    test_results[test_name] = "failed"
                elif "SKIP" in status:
                    if test_results.get(test_name) != "failed":
                        test_results[test_name] = "skipped"
                elif "PASS" in status:
                    if test_results.get(test_name) not in ["failed", "skipped"]:
                        test_results[test_name] = "passed"

        for test_name, status in test_results.items():
            if status == "passed":
                passed_tests.add(test_name)
            elif status == "failed":
                failed_tests.add(test_name)
            elif status == "skipped":
                skipped_tests.add(test_name)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
