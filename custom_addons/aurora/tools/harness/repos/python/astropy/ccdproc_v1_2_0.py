import re
from typing import Optional

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
        return "python:3.6-slim"

    def image_prefix(self) -> str:
        return "envagent"

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
                "prepare.sh",
                """ls -al
###ACTION_DELIMITER###
pip uninstall -y astropy && pip install 'astropy==3.2' numpy scipy astroscrappy reproject scikit-image pytest
###ACTION_DELIMITER###
apt-get update && apt-get install -y build-essential python3-dev
###ACTION_DELIMITER###
pip install 'astropy==3.2' numpy scipy astroscrappy reproject scikit-image pytest
###ACTION_DELIMITER###
echo 'python setup.py test' > /home/ccdproc/test_commands.sh
###ACTION_DELIMITER###
bash /home/ccdproc/test_commands.sh
###ACTION_DELIMITER###
pip install pytest-astropy
###ACTION_DELIMITER###
bash /home/ccdproc/test_commands.sh
###ACTION_DELIMITER###
pip install 'pytest==6.2.5'
###ACTION_DELIMITER###
bash /home/ccdproc/test_commands.sh
###ACTION_DELIMITER###

###ACTION_DELIMITER###
sed -i '/\[pytest\]/,/^$/s/\[pytest\]/[tool:pytest]/' setup.cfg
###ACTION_DELIMITER###
bash /home/ccdproc/test_commands.sh
###ACTION_DELIMITER###
python3 -c "import astropy; print(astropy.__version__, astropy.__file__)" && ls -l $(python3 -c 'import astropy; import os; print(os.path.dirname(astropy.__file__))')/tests/pytest_plugins.py
###ACTION_DELIMITER###
pip uninstall -y astropy && pip install --no-binary :all: 'astropy==3.2'
###ACTION_DELIMITER###
ls -l /usr/local/lib/python3.6/site-packages/astropy/tests/pytest_plugins.py
###ACTION_DELIMITER###
pip install git+https://github.com/astropy/astropy.git@v3.2
###ACTION_DELIMITER###
ls -l /usr/local/lib/python3.6/site-packages/astropy/tests/pytest_plugins.py
###ACTION_DELIMITER###
find / -name pytest_plugins.py 2>/dev/null
###ACTION_DELIMITER###
git clone --branch v3.2 --depth 1 https://github.com/astropy/astropy.git /tmp/astropy32src
###ACTION_DELIMITER###
cp /tmp/astropy32src/astropy/tests/pytest_plugins.py /usr/local/lib/python3.6/site-packages/astropy/tests/
###ACTION_DELIMITER###
ls -l /tmp/astropy32src/astropy/tests/
###ACTION_DELIMITER###
pip uninstall -y astropy && pip install --no-binary :all: 'astropy==2.0.16'
###ACTION_DELIMITER###
ls -l /usr/local/lib/python3.6/site-packages/astropy/tests/pytest_plugins.py
###ACTION_DELIMITER###
bash /home/ccdproc/test_commands.sh
###ACTION_DELIMITER###

###ACTION_DELIMITER###
pip uninstall -y pytest-remotedata pytest-openfiles pytest-doctestplus pytest-cov pytest-astropy
###ACTION_DELIMITER###
bash /home/ccdproc/test_commands.sh
###ACTION_DELIMITER###
""",
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
python setup.py test

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
python setup.py test

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
if ! git -C /home/{pr.repo} apply --whitespace=nowarn  /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1  
fi
python setup.py test

""".format(pr=self.pr),
            ),
        ]

    def dockerfile(self) -> str:
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        dockerfile_content = """
# This is a template for creating a Dockerfile to test patches
# LLM should fill in the appropriate values based on the context

# Choose an appropriate base image based on the project's requirements - replace [base image] with actual base image
# For example: FROM ubuntu:**, FROM python:**, FROM node:**, FROM centos:**, etc.
FROM python:3.6-slim

## Set noninteractive
ENV DEBIAN_FRONTEND=noninteractive

# Install basic requirements
# For example: RUN apt-get update && apt-get install -y git
# For example: RUN yum install -y git
# For example: RUN apk add --no-cache git
RUN apt-get update && apt-get install -y git

# Ensure bash is available
RUN if [ ! -f /bin/bash ]; then         if command -v apk >/dev/null 2>&1; then             apk add --no-cache bash;         elif command -v apt-get >/dev/null 2>&1; then             apt-get update && apt-get install -y bash;         elif command -v yum >/dev/null 2>&1; then             yum install -y bash;         else             exit 1;         fi     fi

WORKDIR /home/
COPY fix.patch /home/
COPY test.patch /home/
RUN git clone https://github.com/astropy/ccdproc.git /home/ccdproc

WORKDIR /home/ccdproc
RUN git reset --hard
RUN git checkout {pr.base.sha}
"""
        dockerfile_content += f"""
{copy_commands}
"""
        return dockerfile_content.format(pr=self.pr)


@Instance.register("astropy", "ccdproc_v1_2_0")
class CCDPROC_V1_2_0(Instance):
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
        passed_tests = set()  # Tests that passed successfully
        failed_tests = set()  # Tests that failed
        skipped_tests = set()  # Tests that were skipped
        # Implementation: parse pytest summary lines and failure sections
        # 1. Parse summary lines for test files/modules and result chars
        summary_re = re.compile(r"^(.*?\.py)\s+([.Fsf]+)")
        test_map = {}  # file/module -> list of (index, status)
        lines = log.splitlines()
        for line in lines:
            m = summary_re.match(line)
            if m:
                filemod = m.group(1).strip()
                results = m.group(2).strip()
                if filemod not in test_map:
                    test_map[filemod] = []
                for idx, c in enumerate(results):
                    test_map[filemod].append((idx, c))
        # 2. Parse failure section for failed test names
        #    - Doctest: [doctest] <module>.<function>
        #    - Regular: <test_name>
        fail_section = False
        fail_name_re = re.compile(
            r"^_{5,}\s*(?:\[doctest\]\s*)?([\w./:<>\[\]\-]+)\s*_{5,}$"
        )
        failed_test_names = set()
        for line in lines:
            if "FAILURES" in line:
                fail_section = True
            elif fail_section:
                m = fail_name_re.match(line)
                if m:
                    name = m.group(1).strip()
                    # Clean up doctest names
                    if name.startswith("[doctest]"):
                        name = name.replace("[doctest]", "").strip()
                    failed_test_names.add(name)
                # End of failure section if a line of '=' or empty
                if line.strip().startswith("=") and "FAILURES" not in line:
                    fail_section = False
        # 3. Assign test names by file/module and index
        #    - For each file/module, try to enumerate test names as <file>::test_<idx>
        #    - For failed tests, use names from failure section
        for filemod, results in test_map.items():
            for idx, status in results:
                # Compose a synthetic test name: <file>::test_<idx>
                test_name = f"{filemod}::test_{idx + 1}"
                if status == ".":
                    passed_tests.add(test_name)
                elif status == "F":
                    # Try to match with failed_test_names, else use synthetic
                    found = False
                    for fail in failed_test_names:
                        if filemod in fail:
                            failed_tests.add(fail)
                            found = True
                            break
                    if not found:
                        failed_tests.add(test_name)
                elif status == "s":
                    skipped_tests.add(test_name)
        # Add any failed test names not already included
        for fail in failed_test_names:
            if fail not in failed_tests:
                failed_tests.add(fail)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
