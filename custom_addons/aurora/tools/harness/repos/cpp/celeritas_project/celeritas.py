import re
from typing import Optional, Union

from odoo.addons.aurora.tools.harness.image import Config, File, Image
from odoo.addons.aurora.tools.harness.instance import Instance, TestResult
from odoo.addons.aurora.tools.harness.pull_request import PullRequest


class CeleritasImageBase(Image):
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
        return "ubuntu:24.04"

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
ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8

RUN apt-get update && apt-get install -y \\
    build-essential \\
    cmake \\
    ninja-build \\
    ccache \\
    g++ \\
    git \\
    ca-certificates \\
    curl \\
    wget \\
    python3 \\
    nlohmann-json3-dev \\
    libpng-dev \\
    libxerces-c-dev \\
    libexpat1-dev \\
    zlib1g-dev \\
    libgtest-dev \\
    && rm -rf /var/lib/apt/lists/*

# Build and install GTest from source with -fPIC (required for shared libs on aarch64)
RUN cd /usr/src/googletest && \\
    cmake -B build -G Ninja -DCMAKE_INSTALL_PREFIX=/usr/local -DCMAKE_POSITION_INDEPENDENT_CODE=ON && \\
    cmake --build build && \\
    cmake --install build && \\
    rm -rf build

# Build and install Geant4 (minimal, no vis/UI, needed for accel/geocel tests)
RUN git clone --depth 1 --branch v11.2.2 https://github.com/Geant4/geant4.git /tmp/geant4-src && \\
    cmake -S /tmp/geant4-src -B /tmp/geant4-build -G Ninja \\
    -DCMAKE_BUILD_TYPE=Release \\
    -DCMAKE_INSTALL_PREFIX=/usr/local \\
    -DGEANT4_INSTALL_DATA=OFF \\
    -DGEANT4_USE_GDML=ON \\
    -DGEANT4_BUILD_MULTITHREADED=OFF \\
    -DGEANT4_USE_QT=OFF \\
    -DGEANT4_USE_OPENGL_X11=OFF \\
    -DGEANT4_USE_RAYTRACER_X11=OFF \\
    -DGEANT4_USE_SYSTEM_EXPAT=ON \\
    -DGEANT4_USE_SYSTEM_ZLIB=ON \\
    -DBUILD_TESTING=OFF && \\
    cmake --build /tmp/geant4-build -j $(nproc) && \\
    cmake --install /tmp/geant4-build && \\
    rm -rf /tmp/geant4-src /tmp/geant4-build

{code}

{self.clear_env}

"""


class CeleritasImageDefault(Image):
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
        return CeleritasImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _cmake_flags(self) -> str:
        return """\\
    -G Ninja \\
    -DCMAKE_BUILD_TYPE=Release \\
    -DCMAKE_CXX_STANDARD=17 \\
    -DCMAKE_CXX_FLAGS="-fsigned-char" \\
    -DBUILD_TESTING=ON \\
    -DCELERITAS_BUILD_TESTS=ON \\
    -DCELERITAS_USE_CUDA=OFF \\
    -DCELERITAS_USE_HIP=OFF \\
    -DCELERITAS_USE_Geant4=ON \\
    -DCELERITAS_USE_VecGeom=OFF \\
    -DCELERITAS_USE_OpenMP=OFF \\
    -DCELERITAS_USE_ROOT=OFF \\
    -DCELERITAS_USE_HepMC3=OFF \\
    -DCELERITAS_USE_MPI=OFF \\
    -DCELERITAS_USE_PNG=ON \\
    -DCELERITAS_USE_SWIG=OFF \\
    -DCELERITAS_USE_Python=OFF \\
    -DCELERITAS_BUILTIN_GTest=ON \\
    -DCELERITAS_DEBUG=OFF"""

    def _cmake_flags_fallback(self) -> str:
        return """\\
    -G Ninja \\
    -DCMAKE_BUILD_TYPE=Release \\
    -DCMAKE_CXX_STANDARD=17 \\
    -DCMAKE_CXX_FLAGS="-fsigned-char" \\
    -DBUILD_TESTING=ON \\
    -DCELERITAS_BUILD_TESTS=ON \\
    -DCELERITAS_USE_CUDA=OFF \\
    -DCELERITAS_USE_Geant4=ON \\
    -DCELERITAS_USE_VecGeom=OFF \\
    -DCELERITAS_USE_ROOT=OFF \\
    -DCELERITAS_USE_MPI=OFF"""

    def _cmake_flags_no_geant4(self) -> str:
        return """\\
    -G Ninja \\
    -DCMAKE_BUILD_TYPE=Release \\
    -DCMAKE_CXX_STANDARD=17 \\
    -DCMAKE_CXX_FLAGS="-fsigned-char" \\
    -DBUILD_TESTING=ON \\
    -DCELERITAS_BUILD_TESTS=ON \\
    -DCELERITAS_USE_CUDA=OFF \\
    -DCELERITAS_USE_Geant4=OFF \\
    -DCELERITAS_USE_VecGeom=OFF \\
    -DCELERITAS_USE_ROOT=OFF \\
    -DCELERITAS_USE_MPI=OFF"""

    def files(self) -> list[File]:
        cmake_flags = self._cmake_flags()
        cmake_flags_fallback = self._cmake_flags_fallback()
        cmake_flags_no_geant4 = self._cmake_flags_no_geant4()

        build_and_test = """
cmake_configure() {{
    cmake .. {cmake_flags} 2>/dev/null \\
        || cmake .. {cmake_flags_fallback} 2>/dev/null \\
        || cmake .. {cmake_flags_no_geant4} 2>/dev/null \\
        || cmake .. -G Ninja -DCMAKE_BUILD_TYPE=Release -DCMAKE_CXX_FLAGS="-fsigned-char" -DBUILD_TESTING=ON -DCELERITAS_USE_Geant4=OFF
}}

cmake_build() {{
    if cmake --build . -j $(nproc) 2>&1; then
        return 0
    fi
    echo "Build failed, retrying with Geant4 OFF..."
    rm -rf *
    cmake .. {cmake_flags_no_geant4} 2>/dev/null \\
        || cmake .. -G Ninja -DCMAKE_BUILD_TYPE=Release -DCMAKE_CXX_FLAGS="-fsigned-char" -DBUILD_TESTING=ON -DCELERITAS_USE_Geant4=OFF
    cmake --build . -j $(nproc) || cmake --build . -j 4
}}
""".format(cmake_flags=cmake_flags, cmake_flags_fallback=cmake_flags_fallback, cmake_flags_no_geant4=cmake_flags_no_geant4)

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
rm -rf build
mkdir -p build

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
{build_and_test}
cd /home/{pr.repo}
cd build
cmake_configure
cmake_build
ctest --output-on-failure --timeout 180 || true
""".format(pr=self.pr, build_and_test=build_and_test),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
{build_and_test}
cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
cd build
cmake_configure
cmake_build
ctest --output-on-failure --timeout 180 || true

""".format(pr=self.pr, build_and_test=build_and_test),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
{build_and_test}
cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
cd build
cmake_configure
cmake_build
ctest --output-on-failure --timeout 180 || true

""".format(pr=self.pr, build_and_test=build_and_test),
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


@Instance.register("celeritas-project", "celeritas")
class Celeritas(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return CeleritasImageDefault(self.pr, self._config)

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

        # CTest output format:
        #   1/10 Test  #1: test_name ................   Passed    0.01 sec
        #   2/10 Test  #2: another_test .............***Failed    0.05 sec
        #   3/10 Test  #3: disabled_test ............***Not Run (Disabled)  0.00 sec
        re_passes = [
            re.compile(
                r"^\s*\d+/\d+\s+Test\s+#\d+:\s+(.+?)\s+\.+\s+Passed", re.IGNORECASE
            ),
        ]
        re_fails = [
            re.compile(
                r"^\s*\d+/\d+\s+Test\s+#\d+:\s+(.+?)\s+\.+\*+Failed", re.IGNORECASE
            ),
        ]
        re_skips = [
            # "Not Run (Disabled)" — test was disabled in CMake config
            re.compile(
                r"^\s*\d+/\d+\s+Test\s+#\d+:\s+(.+?)\s+\.+\*+Not Run",
                re.IGNORECASE,
            ),
            re.compile(
                r"^\s*\d+/\d+\s+Test\s+#\d+:\s+(.+?)\s+\.+\*+Disabled",
                re.IGNORECASE,
            ),
            re.compile(
                r"^\s*\d+/\d+\s+Test\s+#\d+:\s+(.+?)\s+\.+\*+Skipped",
                re.IGNORECASE,
            ),
        ]

        for line in test_log.splitlines():
            line = line.strip()
            if not line:
                continue

            for re_pass in re_passes:
                pass_match = re_pass.match(line)
                if pass_match:
                    test = pass_match.group(1).strip()
                    passed_tests.add(test)

            for re_fail in re_fails:
                fail_match = re_fail.match(line)
                if fail_match:
                    test = fail_match.group(1).strip()
                    failed_tests.add(test)

            for re_skip in re_skips:
                skip_match = re_skip.match(line)
                if skip_match:
                    test = skip_match.group(1).strip()
                    skipped_tests.add(test)

        # Remove any overlap (a test name should only appear once)
        failed_tests -= passed_tests
        skipped_tests -= passed_tests
        skipped_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
