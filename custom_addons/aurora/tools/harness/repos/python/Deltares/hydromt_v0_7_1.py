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
        return "python:3.11-slim"

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
apt-get update && apt-get install -y gdal-bin libgdal-dev python3-gdal
###ACTION_DELIMITER###
pip install --upgrade pip setuptools wheel
###ACTION_DELIMITER###
pip install .[full]
###ACTION_DELIMITER###
pip install affine bottleneck click dask entrypoints geopandas>=0.10 fiona==1.8.22 numpy>=1.20 netcdf4 packaging rioxarray pandas pyflwdir>=0.5.4 pyproj rasterio shapely>=2.0.0 scipy xarray universal_pathlib xmltodict zarr s3fs gcsfs openpyxl requests fsspec==2023.5.0 xugrid pyet black ruff pre-commit pip>=23.1.2 flint wheel twine pytest>=2.7.3 pytest-cov pytest-mock nbsphinx pydata-sphinx-theme sphinx sphinx_design sphinx_autosummary_accessors jupyterlab cartopy matplotlib-base notebook
###ACTION_DELIMITER###
pip install affine bottleneck click dask entrypoints geopandas>=0.10 fiona==1.8.22 numpy>=1.20 netcdf4 packaging rioxarray pandas pyflwdir>=0.5.4 pyproj rasterio shapely>=2.0.0 scipy xarray universal_pathlib xmltodict zarr s3fs gcsfs openpyxl requests fsspec==2023.5.0 xugrid pyet black ruff pre-commit pip>=23.1.2 flint wheel twine pytest>=2.7.3 pytest-cov pytest-mock nbsphinx pydata-sphinx-theme sphinx sphinx_design sphinx_autosummary_accessors jupyterlab cartopy matplotlib notebook
###ACTION_DELIMITER###
pip install --no-deps .
###ACTION_DELIMITER###
echo 'pytest --no-header -rA --tb=no -p no:cacheprovider' > /home/hydromt/test_commands.sh
###ACTION_DELIMITER###
bash /home/hydromt/test_commands.sh
###ACTION_DELIMITER###
python3 -c "import sys; print(sys.executable); import osgeo; print(osgeo.__file__)"
###ACTION_DELIMITER###
pip install gdal==3.6.2
###ACTION_DELIMITER###
apt-get install -y build-essential
###ACTION_DELIMITER###
pip install gdal==3.6.2
###ACTION_DELIMITER###
python3 -c "import osgeo; print(osgeo.__file__)"
###ACTION_DELIMITER###
bash /home/hydromt/test_commands.sh""",
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
pytest --no-header -rA --tb=no -p no:cacheprovider

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
pytest --no-header -rA --tb=no -p no:cacheprovider

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
pytest --no-header -rA --tb=no -p no:cacheprovider

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
FROM python:3.11-slim

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
RUN git clone https://github.com/Deltares/hydromt.git /home/hydromt

WORKDIR /home/hydromt
RUN git reset --hard
RUN git checkout {pr.base.sha}
"""
        dockerfile_content += f"""
{copy_commands}
"""
        return dockerfile_content.format(pr=self.pr)


@Instance.register("Deltares", "hydromt_v0_7_1")
class HYDROMT_V0_7_1(Instance):
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
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()
        # Regex patterns for PASSED, FAILED, SKIPPED lines
        passed_pattern = re.compile(
            r"^PASSED\s+([\w./\[\]-]+::[\w\[\]-]+)", re.MULTILINE
        )
        failed_pattern = re.compile(
            r"^FAILED\s+([\w./\[\]-]+::[\w\[\]-]+)", re.MULTILINE
        )
        skipped_pattern = re.compile(
            r"^SKIPPED \[\d+\] ([\w./\[\]-]+)::[\w\[\]-]+", re.MULTILINE
        )
        skipped_pattern_alt = re.compile(
            r"^SKIPPED \[\d+\] ([\w./\[\]-]+)", re.MULTILINE
        )
        # Find all passed tests
        for match in passed_pattern.finditer(log):
            passed_tests.add(match.group(1))
        # Find all failed tests
        for match in failed_pattern.finditer(log):
            failed_tests.add(match.group(1))
        # Find all skipped tests (try both patterns)
        for match in skipped_pattern.finditer(log):
            skipped_tests.add(match.group(1))
        for match in skipped_pattern_alt.finditer(log):
            skipped_tests.add(match.group(1))

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
