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

    def dependency(self) -> Image | None:
        return "python:3"

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
                """ls /usr/bin/python*
###ACTION_DELIMITER###
apt-get update && apt-get install -y python3.9 python3.9-venv python3.9-dev
###ACTION_DELIMITER###
command -v pyenv || echo 'not found'
###ACTION_DELIMITER###
apt-get update && apt-get install -y make build-essential libssl-dev zlib1g-dev libbz2-dev libreadline-dev libsqlite3-dev wget curl llvm libncurses5-dev libncursesw5-dev xz-utils tk-dev libffi-dev liblzma-dev git
###ACTION_DELIMITER###
git clone https://github.com/pyenv/pyenv.git ~/.pyenv && echo 'export PYENV_ROOT="$HOME/.pyenv"' >> ~/.bashrc && echo 'export PATH="$PYENV_ROOT/bin:$PATH"' >> ~/.bashrc && export PYENV_ROOT="$HOME/.pyenv" && export PATH="$PYENV_ROOT/bin:$PATH"
###ACTION_DELIMITER###
pyenv install 3.9.17
###ACTION_DELIMITER###
/root/.pyenv/versions/3.9.17/bin/python3.9 -m venv venv
###ACTION_DELIMITER###
./venv/bin/pip install -r conans/requirements.txt -r conans/requirements_server.txt -r conans/requirements_dev.txt
###ACTION_DELIMITER###
sed -i 's/PyYAML>=3.11, <3.14.0/PyYAML>=5.4/' conans/requirements.txt
###ACTION_DELIMITER###
./venv/bin/pip install -r conans/requirements.txt -r conans/requirements_server.txt -r conans/requirements_dev.txt
###ACTION_DELIMITER###
echo -e '#!/bin/bash\nexport PYTHONPATH=$PYTHONPATH:$(pwd)\n./venv/bin/nosetests . -v' > /home/conan/test_commands.sh && chmod +x /home/conan/test_commands.sh
###ACTION_DELIMITER###
bash /home/conan/test_commands.sh
###ACTION_DELIMITER###
echo 'markupsafe<2.1' >> conans/requirements.txt
###ACTION_DELIMITER###
./venv/bin/pip install --force-reinstall -r conans/requirements.txt -r conans/requirements_server.txt -r conans/requirements_dev.txt
###ACTION_DELIMITER###
bash /home/conan/test_commands.sh""",
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
#!/bin/bash
export PYTHONPATH=$PYTHONPATH:$(pwd)
./venv/bin/nosetests . -v

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
#!/bin/bash
export PYTHONPATH=$PYTHONPATH:$(pwd)
./venv/bin/nosetests . -v

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
#!/bin/bash
export PYTHONPATH=$PYTHONPATH:$(pwd)
./venv/bin/nosetests . -v

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
FROM python:3

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
RUN git clone https://github.com/conan-io/conan.git /home/conan

WORKDIR /home/conan
RUN git reset --hard
RUN git checkout 0a9b4fac7c8bb6b44006abb69dffd2090519fc65

RUN git checkout {pr.base.sha}"""
        dockerfile_content += f"""
{copy_commands}
"""
        return dockerfile_content.format(pr=self.pr)


@Instance.register("conan-io", "conan_1_13_2")
class CONAN_1_13_2(Instance):
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
        # Improved parser: Use full test identifier and only main result lines
        # Regex: test_func (module.Class) ... STATUS
        test_line_re = re.compile(
            r"^(?P<test_id>[^\n]+ \([^)]+\)) \.\.\. (?P<status>ok|FAIL|ERROR|SKIP:.*?)$",
            re.MULTILINE,
        )
        test_status = {}
        for match in test_line_re.finditer(log):
            test_id = match.group("test_id").strip()
            status = match.group("status")
            # Always update to the latest status (last occurrence wins)
            test_status[test_id] = status
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()
        for test_id, status in test_status.items():
            if status == "ok":
                passed_tests.add(test_id)
            elif status.startswith("SKIP"):
                skipped_tests.add(test_id)
            elif status in ("FAIL", "ERROR"):
                failed_tests.add(test_id)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
