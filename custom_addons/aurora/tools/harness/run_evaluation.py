import concurrent.futures
import glob
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional

from dataclasses_json import dataclass_json
from tqdm import tqdm

from .constant import (
    BUILD_IMAGE_LOG_FILE,
    BUILD_IMAGE_WORKDIR,
    EVALUATION_WORKDIR,
    FIX_PATCH_RUN_LOG_FILE,
    REPORT_FILE,
    RUN_EVALUATION_LOG_FILE,
    RUN_LOG_FILE,
    TEST_PATCH_RUN_LOG_FILE,
)
from .dataset import Dataset
from . import docker_util
from . import git_util
from .gen_report import ReportCliArgs, setup_logger
from .image import Config, DockerfileEnhancer, Image
from .instance import Instance
from .pull_request import PullRequestBase, Repository


def get_non_propagate_logger(log_dir, log_file, log_level, log_to_console):
    # Use log_dir in logger name so each image gets its own FileHandler
    safe_dir = str(log_dir).replace("/", ".").replace("\\", ".")
    logger = logging.getLogger(f"aurora.harness.img.{safe_dir}.{log_file}")
    logger.setLevel(getattr(logging, log_level, logging.INFO))
    logger.propagate = False
    if not logger.handlers:
        fh = logging.FileHandler(str(log_dir / log_file))
        fh.setLevel(getattr(logging, log_level, logging.INFO))
        formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
        fh.setFormatter(formatter)
        logger.addHandler(fh)
    return logger


@dataclass_json
@dataclass
class RepoCommits(Repository):
    commits: dict[str, int] = field(default_factory=dict)


@dataclass_json
@dataclass
class Patch(PullRequestBase):
    fix_patch: str

    def __post_init__(self):
        if not isinstance(self.fix_patch, str):
            raise ValueError(f"Invalid patch: {self.fix_patch}")


@dataclass_json
@dataclass
class EvalConfig:
    mode: str = "evaluation"
    workdir: Optional[Path] = None
    patch_files: Optional[list[str]] = None
    dataset_files: Optional[list[str]] = None
    force_build: bool = False
    output_dir: Optional[Path] = None
    specifics: Optional[set[str]] = None
    skips: Optional[set[str]] = None
    repo_dir: Optional[Path] = None
    need_clone: bool = True
    global_env: Optional[list[str]] = None
    clear_env: bool = True
    stop_on_error: bool = True
    max_workers: int = 8
    max_workers_build_image: int = 8
    max_workers_run_instance: int = 8
    fix_patch_run_cmd: str = ""
    log_dir: Optional[Path] = None
    log_level: str = "INFO"
    log_to_console: bool = True
    human_mode: bool = True
    dataset_generation: bool = False
    platform: Optional[str] = None
    output_tar: Optional[Path] = None
    instance_limit: int = 0

    def __post_init__(self):
        if isinstance(self.workdir, str):
            self.workdir = Path(self.workdir)
        if isinstance(self.repo_dir, str):
            self.repo_dir = Path(self.repo_dir)
        if isinstance(self.output_dir, str):
            self.output_dir = Path(self.output_dir)
        if isinstance(self.log_dir, str):
            self.log_dir = Path(self.log_dir)
        if isinstance(self.output_tar, str):
            self.output_tar = Path(self.output_tar)

        if self.log_dir and not self.log_dir.exists():
            self.log_dir.mkdir(parents=True, exist_ok=True)
        if self.output_dir and not self.output_dir.exists():
            self.output_dir.mkdir(parents=True, exist_ok=True)

        self._expand_patch_files()
        self._expand_dataset_files()

    def _expand_patch_files(self):
        self._patch_files: list[Path] = []
        if self.patch_files:
            for file_pattern in self.patch_files:
                matched_files = glob.glob(file_pattern)
                self._patch_files.extend([Path(f) for f in matched_files])

    def _expand_dataset_files(self):
        self._dataset_files: list[Path] = []
        if self.dataset_files:
            for file_pattern in self.dataset_files:
                matched_files = glob.glob(file_pattern)
                self._dataset_files.extend([Path(f) for f in matched_files])

    def _load_repos_individually(self):
        import importlib
        repos_dir = Path(__file__).parent / "repos"
        if not repos_dir.is_dir():
            return
        base_pkg = "odoo.addons.aurora.tools.harness.repos"
        for lang_dir in sorted(repos_dir.iterdir()):
            if not lang_dir.is_dir() or lang_dir.name.startswith("_"):
                continue
            for org_dir in sorted(lang_dir.iterdir()):
                if not org_dir.is_dir() or org_dir.name.startswith("_"):
                    continue
                for py_file in sorted(org_dir.glob("*.py")):
                    if py_file.name.startswith("_"):
                        continue
                    mod_name = f"{base_pkg}.{lang_dir.name}.{org_dir.name}.{py_file.stem}"
                    try:
                        importlib.import_module(mod_name)
                    except Exception:
                        pass

    def _load_external_registry(self):
        import importlib
        try:
            from ..harness_bridge.phase2_docker_build import (
                _HARNESS_REPOS_ROOT,
                _ensure_harness_importable,
            )
        except Exception:
            return
        if not _HARNESS_REPOS_ROOT.is_dir():
            return
        _ensure_harness_importable()
        for lang_dir in sorted(_HARNESS_REPOS_ROOT.iterdir()):
            if not lang_dir.is_dir() or lang_dir.name.startswith("_"):
                continue
            for org_dir in sorted(lang_dir.iterdir()):
                if not org_dir.is_dir() or org_dir.name.startswith("_"):
                    continue
                for py_file in sorted(org_dir.glob("*.py")):
                    if py_file.name.startswith("_"):
                        continue
                    mod_name = (
                        f"multi_swe_bench.harness.repos"
                        f".{lang_dir.name}.{org_dir.name}.{py_file.stem}"
                    )
                    try:
                        importlib.import_module(mod_name)
                    except Exception:
                        pass

    @property
    def logger(self) -> logging.Logger:
        if not hasattr(self, "_logger"):
            self._logger = setup_logger(
                self.log_dir,
                RUN_EVALUATION_LOG_FILE,
                self.log_level,
                self.log_to_console,
            )
        return self._logger

    @property
    def patches(self) -> dict[str, Patch]:
        if not hasattr(self, "_patches"):
            self._patches: dict[str, Patch] = {}
            for file_path in self._patch_files:
                with open(file_path, "r", encoding="utf-8") as f:
                    for line in f:
                        if line.strip() == "":
                            continue
                        patch = Patch.from_json(line)
                        self._patches[patch.id] = patch
        return self._patches

    @property
    def patch_numbers(self) -> set[int]:
        if not hasattr(self, "_patch_numbers"):
            self._patch_numbers: set[int] = set()
            for patch in self.patches.values():
                self._patch_numbers.add(patch.number)
        return self._patch_numbers

    @property
    def dataset(self) -> Dict[str, "Dataset"]:
        if not hasattr(self, "_dataset"):
            self.logger.info("Loading datasets...")
            self._dataset: dict[str, Dataset] = {}
            self._dataset_parse_failures: list[tuple[str, int, str]] = []
            self._dataset_total_lines: int = 0
            for file_path in self._dataset_files:
                with open(file_path, "r", encoding="utf-8") as f:
                    lines = f.readlines()
                for i, line in enumerate(lines):
                    if line.strip() == "":
                        continue
                    self._dataset_total_lines += 1
                    try:
                        ds = Dataset.from_json(line)
                    except Exception as e:
                        err = f"{type(e).__name__}: {e}"
                        self.logger.error(f"Dataset.from_json failed: {err}")
                        self._dataset_parse_failures.append((str(file_path), i + 1, err))
                        continue
                    if not self.check_specific(ds.id):
                        continue
                    if self.check_skip(ds.id):
                        continue
                    self._dataset[ds.id] = ds
            self.logger.info(f"Loaded {len(self._dataset)} valid datasets")
        return self._dataset

    @property
    def instances(self) -> list[Instance]:
        # Import repos to trigger @Instance.register() decorators
        if not Instance._registry:
            import importlib
            try:
                importlib.import_module("odoo.addons.aurora.tools.harness.repos")
            except Exception as e:
                self.logger.warning(f"Bulk repo import failed: {e}")
                self._load_repos_individually()

        # Also scan the external harness registry root (GitHub-synced repos
        # that live outside the aurora addon tree, e.g. on EKS pods).
        self._load_external_registry()

        def list_to_dict(env: Optional[list[str]]) -> Optional[dict[str, str]]:
            if env is None or len(env) == 0:
                return None
            result = {}
            for item in env:
                key_value = item.split("=")
                if len(key_value) == 2:
                    result[key_value[0]] = key_value[1]
            return result

        if not hasattr(self, "_instances"):
            self.logger.info("Creating instances...")
            instances: list[Instance] = []
            config = Config(
                need_clone=self.need_clone,
                global_env=list_to_dict(self.global_env),
                clear_env=self.clear_env,
            )

            for pr in self.dataset.values():
                try:
                    instance: Instance = Instance.create(pr, config)
                    if not self.check_specific(instance.pr.id):
                        continue
                    if self.check_skip(instance.pr.id):
                        continue
                    instances.append(instance)
                except Exception as e:
                    self.logger.error(f"Error creating instance for {pr.id}: {e}")

            self._instances = [
                inst for inst in instances
                if inst.pr.number in self.patch_numbers
            ]

            if self.instance_limit and self.instance_limit > 0:
                self._instances = self._instances[:self.instance_limit]

            self.logger.info(f"Loaded {len(self._instances)} valid instances.")

        return self._instances

    @property
    def repo_commits(self) -> dict[Repository, RepoCommits]:
        if not hasattr(self, "_repo_commits"):
            self.logger.info("Loading repo commits...")
            self._repo_commits: dict[Repository, RepoCommits] = {}

            for instance in self.instances:
                repo = Repository(org=instance.pr.org, repo=instance.pr.repo)
                if repo not in self._repo_commits:
                    self._repo_commits[repo] = RepoCommits(
                        org=instance.pr.org, repo=instance.pr.repo
                    )
                self._repo_commits[repo].commits[instance.pr.base.sha] = (
                    instance.pr.number
                )

            for repo, rc in self._repo_commits.items():
                self.logger.debug(
                    f"Repo: {repo.repo_full_name}, commits: {len(rc.commits)}"
                )

        return self._repo_commits

    def check_specific(self, name: str) -> bool:
        if self.specifics and not any(
            name in specific or specific in name for specific in self.specifics
        ):
            return False
        return True

    def check_skip(self, name: str) -> bool:
        if self.skips and any(name in skip or skip in name for skip in self.skips):
            return True
        return False

    def check_commit_hashes(self):
        error_happened = False
        for repo, repo_commits in tqdm(
            self.repo_commits.items(), desc="Checking commit hashes"
        ):
            repo_dir = self.repo_dir / repo.repo_full_name
            if not git_util.exists(repo_dir):
                self.logger.warning(f"Repository not found: {repo_dir}")
                git_util.clone_repository(
                    self.repo_dir / repo.org, repo.org, repo.repo
                )

            is_clean, error_msg = git_util.is_clean(repo_dir)
            if not is_clean:
                git_util.clean(repo_dir)
                is_clean, error_msg = git_util.is_clean(repo_dir)
            if not is_clean:
                self.logger.error(error_msg)
                error_happened = True
                continue

            commit_hashes = git_util.get_all_commit_hashes(repo_dir, self.logger)
            if len(commit_hashes) == 0:
                self.logger.error(f"No commit hashes found in {repo.repo_full_name}")
                error_happened = True
                continue

            for commit_hash, pr_number in tqdm(
                repo_commits.commits.items(),
                desc=f"Checking commit hashes for {repo.repo_full_name}",
            ):
                if commit_hash not in commit_hashes:
                    self.logger.error(
                        f"Commit hash not found in {repo.repo_full_name}:pr-{pr_number}: {commit_hash}"
                    )
                    error_happened = True

        if error_happened:
            raise ValueError("Check commit hashes failed, please check the logs.")

    def build_image(self, image: Image):
        per_image_tar = None
        if self.output_tar:
            self.output_tar.mkdir(parents=True, exist_ok=True)
            safe_name = image.image_full_name().replace("/", "_").replace(":", "_")
            per_image_tar = self.output_tar / f"{safe_name}.tar"

        tar_missing = per_image_tar is not None and not per_image_tar.exists()
        if (
            not self.force_build
            and docker_util.exists(image.image_full_name())
            and not tar_missing
        ):
            self.logger.debug(
                f"Image {image.image_full_name()} already exists, skipping..."
            )
            return

        workdir = self.workdir / image.pr.org / image.pr.repo / BUILD_IMAGE_WORKDIR
        image_dir = workdir / image.workdir()
        image_dir.mkdir(parents=True, exist_ok=True)

        if self.repo_dir and image.need_copy_code:
            docker_util.copy_source_code(self.repo_dir, image, image_dir)

        dockerfile_path = image_dir / image.dockerfile_name()
        dockerfile_path.parent.mkdir(parents=True, exist_ok=True)
        with open(dockerfile_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(
                DockerfileEnhancer.enhance(
                    image, dataset_generation=self.dataset_generation
                )
            )

        for file in image.files():
            file_path = image_dir / file.dir / file.name
            file_path.parent.mkdir(parents=True, exist_ok=True)
            with open(file_path, "w", encoding="utf-8", newline="\n") as f:
                f.write(file.content)

        buildargs = {}
        if self.dataset_generation:
            dep = image.dependency()
            if isinstance(dep, str):
                buildargs["REPO_URL"] = (
                    f"https://github.com/{image.pr.org}/{image.pr.repo}.git"
                )
                buildargs["BASE_COMMIT"] = image.pr.base.sha

        self.logger.info(f"Building image {image.image_full_name()}...")
        base_image_context = None
        dep_img = image.dependency()
        if isinstance(dep_img, Image) and self.output_tar:
            safe_dep_name = dep_img.image_full_name().replace("/", "_").replace(":", "_")
            oci_dir = self.output_tar / f"{safe_dep_name}.tar.d"
            if oci_dir.exists():
                base_image_context = (
                    f"{dep_img.image_full_name()}=oci-layout://{oci_dir.resolve()}"
                )
        docker_util.build(
            image_dir,
            image.dockerfile_name(),
            image.image_full_name(),
            get_non_propagate_logger(
                image_dir,
                BUILD_IMAGE_LOG_FILE,
                self.log_level,
                False,
            ),
            buildargs=buildargs,
            platform=self.platform,
            output_tar=per_image_tar,
            base_image_context=base_image_context,
        )
        self.logger.info(f"Image {image.image_full_name()} built successfully.")

    def run_mode_image(self):
        self.logger.info("Building images...")
        self.check_commit_hashes()

        external_images: set[str] = set()
        images: dict[str, set[Image]] = {}
        for instance in self.instances:
            required_image = instance.dependency()
            while isinstance(required_image, Image):
                parent_image = required_image.dependency()

                if isinstance(parent_image, Image):
                    parent_image_name = parent_image.image_full_name()
                else:
                    parent_image_name = parent_image
                    external_images.add(parent_image_name)

                if parent_image_name not in images:
                    images[parent_image_name] = set()
                images[parent_image_name].add(required_image)

                required_image = parent_image

        image_count = sum(len(imgs) for imgs in images.values())
        self.logger.info(f"Total images: {image_count}")

        building_images: set[Image] = set()
        for external_name in external_images:
            for image in images[external_name]:
                building_images.add(image)

        with tqdm(total=image_count, desc="Building images") as building_bar:
            while building_images:
                with concurrent.futures.ThreadPoolExecutor(
                    max_workers=self.max_workers_build_image
                ) as executor:
                    futures = {
                        executor.submit(self.build_image, image): image
                        for image in building_images
                    }

                    failed_images: set[Image] = set()
                    for future in concurrent.futures.as_completed(futures):
                        image = futures[future]
                        try:
                            future.result()
                        except Exception as e:
                            self.logger.error(
                                f"Error building image {image.image_full_name()}: {e}"
                            )
                            failed_images.add(image)
                            if self.stop_on_error:
                                executor.shutdown(wait=False)
                                sys.exit(1)
                        finally:
                            building_bar.update(1)

                new_building_images: set[Image] = set()
                for image in building_images:
                    if image in failed_images:
                        continue
                    if image.image_full_name() not in images:
                        continue
                    for new_image in images[image.image_full_name()]:
                        new_building_images.add(new_image)
                building_images = new_building_images

        self.logger.info("Images built successfully.")

    def run_instance(self, instance: Instance):
        instance_dir = (
            self.workdir
            / instance.pr.org
            / instance.pr.repo
            / EVALUATION_WORKDIR
            / instance.dependency().workdir()
        )
        instance_dir.mkdir(parents=True, exist_ok=True)

        fix_patch_path = instance_dir.absolute() / "fix.patch"
        with open(fix_patch_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(self.patches[instance.pr.id].fix_patch)

        report_path = instance_dir / REPORT_FILE
        if report_path.exists():
            self.logger.info(
                f"Report already exists for {instance.name()}, skipping..."
            )
            return

        _local_mode = os.environ.get("AURORA_LOCAL_MODE") == "1"

        def run_and_save_output(
            image_full_name: str, run_command: str, output_path: Path
        ):
            wrapped_command = f"bash -c '{{ {run_command}; }} 2>&1; true'"
            self.logger.info(
                f"Running {image_full_name} with command: {run_command}..."
            )
            volumes = None if _local_mode else {
                str(fix_patch_path): {
                    "bind": instance.dependency().fix_patch_path(),
                    "mode": "rw",
                }
            }
            try:
                output = docker_util.run(
                    image_full_name,
                    wrapped_command,
                    output_path,
                    self.global_env,
                    volumes=volumes,
                )
            except Exception as exc:
                if volumes and ("not a directory" in str(exc).lower()
                                or "overlay" in str(exc).lower()):
                    self.logger.warning(
                        "Bind-mount failed for %s (%s), retrying without volumes "
                        "(fix.patch was COPYed at build time).",
                        image_full_name, exc,
                    )
                    output = docker_util.run(
                        image_full_name,
                        wrapped_command,
                        output_path,
                        self.global_env,
                        volumes=None,
                    )
                else:
                    raise
            return output

        run_and_save_output(
            instance.name(),
            instance.run(),
            instance_dir / RUN_LOG_FILE,
        )

        run_and_save_output(
            instance.name(),
            instance.test_patch_run(),
            instance_dir / TEST_PATCH_RUN_LOG_FILE,
        )

        run_and_save_output(
            instance.name(),
            instance.fix_patch_run(self.fix_patch_run_cmd),
            instance_dir / FIX_PATCH_RUN_LOG_FILE,
        )

    def run_mode_instance_only(self):
        self.logger.info("Running instances...")

        with tqdm(total=len(self.instances), desc="Running instances") as running_bar:
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=self.max_workers_run_instance
            ) as executor:
                futures = {
                    executor.submit(self.run_instance, instance): instance
                    for instance in self.instances
                }

                for future in concurrent.futures.as_completed(futures):
                    instance = futures[future]
                    try:
                        future.result()
                    except docker_util.DockerDaemonLostError as e:
                        self.logger.error(
                            f"Docker daemon lost during {instance.pr.id}: {e}. "
                            "Aborting remaining instances."
                        )
                        executor.shutdown(wait=False, cancel_futures=True)
                        break
                    except Exception as e:
                        self.logger.error(
                            f"Error running instance {instance.pr.id}: {e}"
                        )
                        if self.stop_on_error:
                            executor.shutdown(wait=False)
                            sys.exit(1)
                    finally:
                        running_bar.update(1)

        self.logger.info("Instances run successfully.")

    def run_mode_instance(self):
        self.run_mode_image()
        self.run_mode_instance_only()

    def run_mode_evaluation(self):
        self.run_mode_instance()
        self.logger.info("Running evaluation...")
        ReportCliArgs(
            mode="evaluation",
            workdir=self.workdir,
            output_dir=self.output_dir,
            specifics=self.specifics,
            skips=self.skips,
            raw_dataset_files=None,
            dataset_files=[str(f) for f in self._dataset_files] if self._dataset_files else self.dataset_files,
            max_workers=self.max_workers,
            log_dir=self.log_dir,
            log_level=self.log_level,
            log_to_console=self.log_to_console,
        ).run()

    def run(self):
        if self.mode == "image":
            self.run_mode_image()
        elif self.mode == "instance_only":
            self.run_mode_instance_only()
        elif self.mode == "instance":
            self.run_mode_instance()
        elif self.mode == "evaluation":
            self.run_mode_evaluation()
        else:
            raise ValueError(f"Invalid mode: {self.mode}")
