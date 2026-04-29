import logging
import os
import platform as _platform
import shlex
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Optional, Union

import docker

docker_client = docker.from_env(timeout=600)


def exists(image_name: str) -> bool:
    try:
        docker_client.images.get(image_name)
        return True
    except docker.errors.ImageNotFound:
        return False


def build(
    workdir: Path,
    dockerfile_name: str,
    image_full_name: str,
    logger: logging.Logger,
    buildargs: dict[str, str] | None = None,
    platform: str | None = None,
    output_tar: Path | None = None,
    base_image_context: str | None = None,
):
    workdir = str(workdir)
    logger.info(
        f"Start building image `{image_full_name}`, working directory is `{workdir}`"
    )

    if platform:
        abs_output_tar = output_tar.resolve() if output_tar else None
        _build_with_buildx(
            workdir,
            dockerfile_name,
            image_full_name,
            logger,
            buildargs=buildargs,
            platform=platform,
            output_tar=abs_output_tar,
            base_image_context=base_image_context,
        )
    else:
        _build_with_sdk(
            workdir,
            dockerfile_name,
            image_full_name,
            logger,
            buildargs=buildargs,
        )


def _build_with_sdk(
    workdir: str,
    dockerfile_name: str,
    image_full_name: str,
    logger: logging.Logger,
    buildargs: dict[str, str] | None = None,
):
    try:
        build_logs = docker_client.api.build(
            path=workdir,
            dockerfile=dockerfile_name,
            tag=image_full_name,
            rm=True,
            forcerm=True,
            decode=True,
            encoding="utf-8",
            buildargs=buildargs or {},
        )

        for log in build_logs:
            if "stream" in log:
                logger.info(log["stream"].strip())
            elif "error" in log:
                error_message = log["error"].strip()
                logger.error(f"Docker build error: {error_message}")
                raise RuntimeError(f"Docker build failed: {error_message}")
            elif "status" in log:
                logger.info(log["status"].strip())
            elif "aux" in log:
                logger.info(log["aux"].get("ID", "").strip())

        logger.info(f"image({workdir}) build success: {image_full_name}")
    except docker.errors.BuildError as e:
        logger.error(f"build error: {e}")
        raise e
    except Exception as e:
        logger.error(f"Unknown build error occurred: {e}")
        raise e


def _detect_native_platform() -> str:
    machine = _platform.machine().lower()
    if machine in ("arm64", "aarch64"):
        return "linux/arm64"
    return "linux/amd64"


def _run_buildx(
    cmd: list[str],
    workdir: str,
    logger: logging.Logger,
    label: str = "",
):
    cmd_str = shlex.join(cmd)
    logger.info(f"Running buildx{f' ({label})' if label else ''}: {cmd_str}")

    try:
        process = subprocess.Popen(
            cmd,
            cwd=workdir,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

        for line in process.stdout:
            logger.info(line.rstrip())

        returncode = process.wait()
        if returncode != 0:
            raise RuntimeError(
                f"docker buildx build failed with exit code {returncode}"
            )
    except FileNotFoundError:
        raise RuntimeError(
            "docker buildx not found. Install with: docker buildx install"
        )
    except Exception as e:
        logger.error(f"buildx error: {e}")
        raise e


def _build_with_buildx(
    workdir: str,
    dockerfile_name: str,
    image_full_name: str,
    logger: logging.Logger,
    buildargs: dict[str, str] | None = None,
    platform: str = "linux/amd64",
    output_tar: Path | None = None,
    base_image_context: str | None = None,
):
    platforms = [p.strip() for p in platform.split(",")]
    is_multi_platform = len(platforms) > 1

    cmd = [
        "docker", "buildx", "build",
        "--platform", platform,
        "-f", dockerfile_name,
        "-t", image_full_name,
        "--provenance=false",
        "--sbom=false",
    ]

    for key, value in (buildargs or {}).items():
        cmd.extend(["--build-arg", f"{key}={value}"])

    if base_image_context:
        cmd.extend(["--build-context", base_image_context])

    if output_tar:
        cmd.extend(["--output", f"type=oci,dest={output_tar}"])
        if not is_multi_platform:
            cmd.append("--load")
    else:
        cmd.append("--load")

    cmd.append(".")

    _run_buildx(cmd, workdir, logger)
    logger.info(f"image({workdir}) buildx success: {image_full_name}")

    if output_tar:
        oci_dir = Path(str(output_tar) + ".d")
        oci_dir.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["tar", "-xf", str(output_tar), "-C", str(oci_dir)],
            check=True,
        )
        logger.info(f"Extracted OCI tar to {oci_dir}")

    if output_tar and is_multi_platform:
        native = _detect_native_platform()
        logger.info(
            f"Loading native platform ({native}) into daemon from buildx cache..."
        )

        load_cmd = [
            "docker", "buildx", "build",
            "--platform", native,
            "-f", dockerfile_name,
            "-t", image_full_name,
            "--provenance=false",
            "--sbom=false",
        ]
        for key, value in (buildargs or {}).items():
            load_cmd.extend(["--build-arg", f"{key}={value}"])
        if base_image_context:
            load_cmd.extend(["--build-context", base_image_context])
        load_cmd.append("--load")
        load_cmd.append(".")
        _run_buildx(load_cmd, workdir, logger, label="load native")
        logger.info(f"Native platform image loaded into daemon: {image_full_name}")


def run(
    image_full_name: str,
    run_command: str,
    output_path: Optional[Path] = None,
    global_env: Optional[list[str]] = None,
    volumes: Optional[Union[dict[str, str], list[str]]] = None,
) -> str:
    container = None
    try:
        container = docker_client.containers.run(
            image=image_full_name,
            command=run_command,
            remove=False,
            detach=True,
            stdout=True,
            stderr=True,
            environment=global_env,
            volumes=volumes,
        )

        output = ""
        if output_path:
            with open(output_path, "w", encoding="utf-8") as f:
                for line in container.logs(stream=True, follow=True):
                    line_decoded = line.decode("utf-8")
                    f.write(line_decoded)
                    output += line_decoded
        else:
            container.wait()
            output = container.logs().decode("utf-8")

        return output
    finally:
        if container:
            try:
                container.remove(force=True)
            except Exception as e:
                logging.warning(f"Failed to remove container: {e}")


def copy_source_code(source_code_dir: Path, image, dst_dir: Path):
    source_code_path = source_code_dir / image.pr.org / image.pr.repo

    if not source_code_path.exists():
        raise FileNotFoundError(
            f"The source code directory `{source_code_path}` does not exist."
        )

    if not dst_dir.exists():
        dst_dir.mkdir(parents=True, exist_ok=True)

    destination_path = dst_dir / source_code_path.name

    def remove_readonly(func, path, _):
        os.chmod(path, stat.S_IWRITE)
        func(path)

    if os.path.exists(destination_path):
        shutil.rmtree(destination_path, onerror=remove_readonly)

    shutil.copytree(source_code_path, destination_path)
