"""Shared artifact collection for Phase 2 evaluation.

Used by both the K8s worker (worker/run_evaluation.py) and the local
executor (models/evaluation_executor.py) to upload per-instance artifacts
to S3 and create/update aurora_evaluation_instance DB records.
"""
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Optional

import psycopg2

from . import s3_storage

_logger = logging.getLogger(__name__)

_SERIALIZATION_RETRIES = 3
_SERIALIZATION_BACKOFF = 0.5

_ALLOWED_INSTANCE_COLUMNS = frozenset({
    "status", "resolved", "error_message",
    "f2p_count", "p2p_count", "s2p_count", "n2p_count",
    "image_tag", "image_workdir",
    "tag_start", "tag_end", "pr_numbers",
    "pr_attribution_method", "version_scheme",
    "dockerfile_content", "report_json_content", "fix_patch_content",
    "build_log_tail", "run_log_tail", "test_patch_log_tail", "fix_patch_log_tail",
    "dockerfile_s3_uri", "build_log_s3_uri", "run_log_s3_uri",
    "test_patch_log_s3_uri", "fix_patch_log_s3_uri",
    "report_json_s3_uri", "fix_patch_s3_uri", "oci_tar_s3_uri",
    "dockerfile_local_path", "build_log_local_path", "run_log_local_path",
    "test_patch_log_local_path", "fix_patch_log_local_path",
    "report_json_local_path",
    # Post-eval ECR push phase writes these.
    "ecr_image_uri", "ecr_image_digest",
})

_INLINE_DOCKERFILE_CAP = 16 * 1024
_INLINE_REPORT_CAP = 128 * 1024
_INLINE_FIX_PATCH_CAP = 256 * 1024
_LOG_TAIL_BYTES = 64 * 1024

ECR_RESOURCE_TAGS: list[tuple[str, str]] = [
    ("Environment", "Production"),
    ("Project", "Aurora"),
    ("Owner", "Ethara AI"),
    ("Team", "DevOps"),
    ("ManagedBy", "Ethara-internel"),
    ("CostCenter", "RD-001 / OPS-002"),
    ("map-migrated", "migWBK9WXSX89"),
]


def _ecr_tag_cli_args() -> list[str]:
    return [f"Key={k},Value={v}" for k, v in ECR_RESOURCE_TAGS]


def load_s3_config() -> dict:
    from .pipeline import S3_BUCKET, S3_REGION, S3_AURORA_PREFIX
    return {
        "bucket": S3_BUCKET,
        "region": S3_REGION,
        "access_key": os.environ.get("AURORA_S3_ACCESS_KEY", "").strip(),
        "secret_key": os.environ.get("AURORA_S3_SECRET_KEY", "").strip(),
        "folder": S3_AURORA_PREFIX,
    }


def resolve_run_numbers(s3_config: dict, use_s3: bool, s3_folder: str,
                        phase: str, instances) -> dict:
    result: dict = {}
    for inst in instances:
        key = (inst.pr.org, inst.pr.repo)
        if key in result:
            continue
        if use_s3:
            try:
                result[key] = s3_storage.get_next_run_number(
                    s3_config, key[0], key[1], folder=s3_folder, phase=phase,
                )
            except Exception:
                _logger.warning(
                    "get_next_run_number failed for %s/%s, falling back to 1",
                    key[0], key[1], exc_info=True,
                )
                result[key] = 1
        else:
            result[key] = 1
    return result


def _read_tail(path: Optional[str], max_bytes: int = _LOG_TAIL_BYTES) -> Optional[str]:
    if not path:
        return None
    try:
        size = os.path.getsize(path)
    except OSError:
        return None
    try:
        with open(path, "rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
            data = f.read()
        return data.decode("utf-8", errors="replace")
    except OSError:
        return None


def _read_capped(path: Optional[str], cap: int) -> Optional[str]:
    if not path:
        return None
    try:
        with open(path, "rb") as f:
            data = f.read(cap + 1)
    except OSError:
        return None
    if len(data) > cap:
        return data[:cap].decode("utf-8", errors="replace") + "\n\n[truncated]"
    return data.decode("utf-8", errors="replace")


def _upload_artifact(s3_config: dict, use_s3: bool, local_path: Optional[str],
                     s3_key: str) -> Optional[str]:
    if not (use_s3 and local_path and os.path.isfile(local_path)):
        return None
    try:
        return s3_storage.upload_file(s3_config, local_path, s3_key)
    except Exception:
        _logger.warning("S3 upload failed for %s -> %s", local_path, s3_key, exc_info=True)
        return None


def _build_instance_key(s3_folder: str, phase: str, org: str, repo: str,
                        run_number: int, instance_id: str, filename: str) -> str:
    nested = f"{instance_id}/{filename}"
    return s3_storage.build_s3_key(org, repo, run_number, nested, folder=s3_folder, phase=phase)


def update_instance(conn, instance_id: int, vals: dict[str, Any]) -> None:
    if not vals:
        return
    invalid = set(vals) - _ALLOWED_INSTANCE_COLUMNS
    if invalid:
        raise ValueError(f"Attempted to update disallowed instance columns: {invalid}")
    sets = ", ".join(f"{k} = %s" for k in vals)
    params = list(vals.values()) + [instance_id]
    query = f"UPDATE aurora_evaluation_instance SET {sets} WHERE id = %s"
    for attempt in range(_SERIALIZATION_RETRIES):
        try:
            with conn.cursor() as cur:
                cur.execute(query, params)
            conn.commit()
            return
        except psycopg2.errors.SerializationFailure:
            conn.rollback()
            if attempt < _SERIALIZATION_RETRIES - 1:
                time.sleep(_SERIALIZATION_BACKOFF * (2 ** attempt))
            else:
                _logger.warning(
                    "SerializationFailure updating instance id=%s after %d retries: keys=%s",
                    instance_id, _SERIALIZATION_RETRIES, sorted(vals.keys()),
                )
                return


def ensure_instance(conn, evaluation_id: int, org: str, repo: str,
                    instance_id: str,
                    tag_start: Optional[str] = None,
                    tag_end: Optional[str] = None,
                    pr_numbers: Optional[str] = None,
                    pr_attribution_method: Optional[str] = None,
                    version_scheme: Optional[str] = None,
                    image_tag: Optional[str] = None,
                    image_workdir: Optional[str] = None) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM aurora_evaluation_instance "
            "WHERE evaluation_id = %s AND instance_id = %s",
            [evaluation_id, instance_id],
        )
        row = cur.fetchone()

    if row:
        vals: dict[str, Any] = {}
        for key, val in (
            ("tag_start", tag_start),
            ("tag_end", tag_end),
            ("pr_numbers", pr_numbers),
            ("pr_attribution_method", pr_attribution_method),
            ("version_scheme", version_scheme),
            ("image_tag", image_tag),
            ("image_workdir", image_workdir),
        ):
            if val:
                vals[key] = val
        if vals:
            update_instance(conn, row[0], vals)
        return row[0]

    insert_sql = (
        "INSERT INTO aurora_evaluation_instance "
        "(evaluation_id, org, repo, instance_id, tag_start, tag_end, "
        "pr_numbers, pr_attribution_method, version_scheme, "
        "image_tag, image_workdir, status, resolved, "
        "f2p_count, p2p_count, s2p_count, n2p_count, "
        "create_uid, create_date, write_uid, write_date) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
        "'pending', FALSE, 0, 0, 0, 0, 1, NOW(), 1, NOW()) "
        "RETURNING id"
    )
    insert_params = [
        evaluation_id, org, repo, instance_id, tag_start, tag_end,
        pr_numbers, pr_attribution_method, version_scheme,
        image_tag, image_workdir,
    ]
    for attempt in range(_SERIALIZATION_RETRIES):
        try:
            with conn.cursor() as cur:
                cur.execute(insert_sql, insert_params)
                new_id = cur.fetchone()[0]
            conn.commit()
            return new_id
        except psycopg2.errors.SerializationFailure:
            conn.rollback()
            if attempt < _SERIALIZATION_RETRIES - 1:
                time.sleep(_SERIALIZATION_BACKOFF * (2 ** attempt))
            else:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT id FROM aurora_evaluation_instance "
                        "WHERE evaluation_id = %s AND instance_id = %s",
                        [evaluation_id, instance_id],
                    )
                    again = cur.fetchone()
                if again:
                    return again[0]
                raise
    raise RuntimeError("ensure_instance exhausted retries without result")


def instance_id_for(pr) -> str:
    return f"{pr.org}__{pr.repo}-pr-{pr.number}"


def populate_build_artifacts(conn, rec_id: int, workdir: str, instances,
                             s3_config: dict, use_s3: bool,
                             run_numbers: dict, s3_folder: str, phase: str,
                             oci_tar_dir: Optional[str] = None) -> dict:
    """Collect per-instance build artifacts and base image Dockerfile.

    Returns a dict with base image Dockerfile info (may be empty):
        {"base_dockerfile_content": str|None, "base_dockerfile_s3_uri": str|None}
    """
    from ..tools.harness.constant import BUILD_IMAGE_WORKDIR, BUILD_IMAGE_LOG_FILE
    from ..tools.harness.image import Image

    base_result: dict[str, Any] = {}
    collected_base_dirs: set[str] = set()

    for instance in instances:
        pr = instance.pr
        image = instance.dependency()
        image_workdir_name = image.workdir()
        run_number = run_numbers.get((pr.org, pr.repo), 1)
        img_dir = Path(workdir) / pr.org / pr.repo / BUILD_IMAGE_WORKDIR / image_workdir_name
        dockerfile_name = image.dockerfile_name() if hasattr(image, "dockerfile_name") else "Dockerfile"
        dockerfile_path = img_dir / dockerfile_name
        build_log_path = img_dir / BUILD_IMAGE_LOG_FILE
        image_tag_str = image.image_full_name() if hasattr(image, "image_full_name") else image_workdir_name

        iid = instance_id_for(pr)
        db_instance_id = ensure_instance(
            conn, rec_id, pr.org, pr.repo, iid,
            image_tag=image_tag_str,
            image_workdir=image_workdir_name,
        )

        vals: dict[str, Any] = {"status": "built"}
        if dockerfile_path.exists():
            vals["dockerfile_local_path"] = str(dockerfile_path)
            content = _read_capped(str(dockerfile_path), _INLINE_DOCKERFILE_CAP)
            if content is not None:
                vals["dockerfile_content"] = content
            s3_uri = _upload_artifact(
                s3_config, use_s3, str(dockerfile_path),
                _build_instance_key(s3_folder, phase, pr.org, pr.repo, run_number, iid, dockerfile_name),
            )
            if s3_uri:
                vals["dockerfile_s3_uri"] = s3_uri

        if build_log_path.exists():
            vals["build_log_local_path"] = str(build_log_path)
            tail = _read_tail(str(build_log_path))
            if tail is not None:
                vals["build_log_tail"] = tail
            s3_uri = _upload_artifact(
                s3_config, use_s3, str(build_log_path),
                _build_instance_key(s3_folder, phase, pr.org, pr.repo, run_number, iid, BUILD_IMAGE_LOG_FILE),
            )
            if s3_uri:
                vals["build_log_s3_uri"] = s3_uri
        else:
            vals["build_log_tail"] = (
                f"[Docker image {image_tag_str} was already "
                f"cached locally — build_image.log was not written.\n"
                f"To capture a build log, tick 'Force Build' or run: docker rmi {image_tag_str}]"
            )

        if oci_tar_dir:
            safe_name = image_tag_str.replace("/", "_").replace(":", "_")
            tar_path = Path(oci_tar_dir) / f"{safe_name}.tar"
            if tar_path.exists():
                s3_uri = _upload_artifact(
                    s3_config, use_s3, str(tar_path),
                    _build_instance_key(s3_folder, phase, pr.org, pr.repo, run_number, iid, f"{safe_name}.oci.tar"),
                )
                if s3_uri:
                    vals["oci_tar_s3_uri"] = s3_uri

        update_instance(conn, db_instance_id, vals)

        # --- Collect base image Dockerfile (once per unique base) ---
        if not base_result and hasattr(image, "dependency"):
            base_image = image.dependency()
            if isinstance(base_image, Image):
                base_workdir_name = base_image.workdir()
                if base_workdir_name not in collected_base_dirs:
                    collected_base_dirs.add(base_workdir_name)
                    base_dir = Path(workdir) / pr.org / pr.repo / BUILD_IMAGE_WORKDIR / base_workdir_name
                    base_df_name = base_image.dockerfile_name() if hasattr(base_image, "dockerfile_name") else "Dockerfile"
                    base_df_path = base_dir / base_df_name
                    if base_df_path.exists():
                        content = _read_capped(str(base_df_path), _INLINE_DOCKERFILE_CAP)
                        if content is not None:
                            base_result["base_dockerfile_content"] = content
                        base_s3_key = s3_storage.build_s3_key(
                            pr.org, pr.repo, run_number,
                            f"base/{base_df_name}",
                            folder=s3_folder, phase=phase,
                        )
                        s3_uri = _upload_artifact(s3_config, use_s3, str(base_df_path), base_s3_key)
                        if s3_uri:
                            base_result["base_dockerfile_s3_uri"] = s3_uri

    return base_result


def populate_run_artifacts(conn, rec_id: int, workdir: str, instances,
                           s3_config: dict, use_s3: bool,
                           run_numbers: dict, s3_folder: str, phase: str) -> None:
    from ..tools.harness.constant import (
        EVALUATION_WORKDIR, RUN_LOG_FILE,
        TEST_PATCH_RUN_LOG_FILE, FIX_PATCH_RUN_LOG_FILE,
    )

    for instance in instances:
        pr = instance.pr
        image_workdir_name = instance.dependency().workdir()
        run_number = run_numbers.get((pr.org, pr.repo), 1)
        eval_dir = Path(workdir) / pr.org / pr.repo / EVALUATION_WORKDIR / image_workdir_name

        iid = instance_id_for(pr)
        db_instance_id = ensure_instance(conn, rec_id, pr.org, pr.repo, iid)

        vals: dict[str, Any] = {"status": "running"}
        for attr_local, attr_tail, attr_s3, fname in [
            ("run_log_local_path", "run_log_tail", "run_log_s3_uri", RUN_LOG_FILE),
            ("test_patch_log_local_path", "test_patch_log_tail", "test_patch_log_s3_uri", TEST_PATCH_RUN_LOG_FILE),
            ("fix_patch_log_local_path", "fix_patch_log_tail", "fix_patch_log_s3_uri", FIX_PATCH_RUN_LOG_FILE),
        ]:
            p = eval_dir / fname
            if p.exists():
                vals[attr_local] = str(p)
                tail = _read_tail(str(p))
                if tail is not None:
                    vals[attr_tail] = tail
                s3_uri = _upload_artifact(
                    s3_config, use_s3, str(p),
                    _build_instance_key(s3_folder, phase, pr.org, pr.repo, run_number, iid, fname),
                )
                if s3_uri:
                    vals[attr_s3] = s3_uri

        fix_patch_path = eval_dir / "fix.patch"
        if fix_patch_path.exists():
            content = _read_capped(str(fix_patch_path), _INLINE_FIX_PATCH_CAP)
            if content is not None:
                vals["fix_patch_content"] = content
            s3_uri = _upload_artifact(
                s3_config, use_s3, str(fix_patch_path),
                _build_instance_key(s3_folder, phase, pr.org, pr.repo, run_number, iid, "fix.patch"),
            )
            if s3_uri:
                vals["fix_patch_s3_uri"] = s3_uri
        update_instance(conn, db_instance_id, vals)


def populate_report_artifacts(conn, rec_id: int, workdir: str, output_dir: str,
                              instances, s3_config: dict, use_s3: bool,
                              run_numbers: dict, s3_folder: str, phase: str) -> None:
    from ..tools.harness.constant import EVALUATION_WORKDIR, REPORT_FILE

    final_report_path = Path(output_dir) / "final_report.json"
    resolved_set: set = set()
    error_set: set = set()
    if final_report_path.exists():
        try:
            with open(final_report_path, "r", encoding="utf-8") as f:
                fr = json.load(f)
            resolved_set = set(fr.get("resolved_ids", []) or [])
            error_set = set(fr.get("error_ids", []) or [])
        except Exception:
            _logger.debug("Failed to parse final_report.json", exc_info=True)

    for instance in instances:
        pr = instance.pr
        image_workdir_name = instance.dependency().workdir()
        run_number = run_numbers.get((pr.org, pr.repo), 1)
        eval_dir = Path(workdir) / pr.org / pr.repo / EVALUATION_WORKDIR / image_workdir_name

        iid = instance_id_for(pr)
        db_instance_id = ensure_instance(conn, rec_id, pr.org, pr.repo, iid)
        pr_id = f"{pr.org}/{pr.repo}:pr-{pr.number}"

        vals: dict[str, Any] = {}
        if pr_id in error_set:
            vals["status"] = "error"
            vals["resolved"] = False
        elif pr_id in resolved_set:
            vals["status"] = "resolved"
            vals["resolved"] = True
        else:
            vals["status"] = "unresolved"
            vals["resolved"] = False

        report_path = eval_dir / REPORT_FILE
        if report_path.exists():
            vals["report_json_local_path"] = str(report_path)
            raw = _read_capped(str(report_path), _INLINE_REPORT_CAP)
            if raw is not None:
                try:
                    parsed = json.loads(raw)
                    vals["report_json_content"] = json.dumps(parsed, indent=2, sort_keys=True)
                except Exception:
                    vals["report_json_content"] = raw
            try:
                rpt = json.loads(Path(report_path).read_text("utf-8"))
                vals["f2p_count"] = len(rpt.get("f2p_tests", {}) or {})
                vals["p2p_count"] = len(rpt.get("p2p_tests", {}) or {})
                vals["s2p_count"] = len(rpt.get("s2p_tests", {}) or {})
                vals["n2p_count"] = len(rpt.get("n2p_tests", {}) or {})
                err_msg = rpt.get("error_msg") or ""
                if err_msg:
                    vals["error_message"] = err_msg[:4000]
            except Exception:
                _logger.debug("Failed to parse per-instance report for instance=%s", iid, exc_info=True)
            s3_uri = _upload_artifact(
                s3_config, use_s3, str(report_path),
                _build_instance_key(s3_folder, phase, pr.org, pr.repo, run_number, iid, REPORT_FILE),
            )
            if s3_uri:
                vals["report_json_s3_uri"] = s3_uri

        update_instance(conn, db_instance_id, vals)


def push_resolved_images_to_ecr(conn, rec_id: int, workdir: str, output_dir: str,
                                instances, run_numbers: dict,
                                ecr_registry: str, ecr_region: str,
                                s3_config: dict, use_s3: bool, s3_folder: str,
                                phase: str) -> dict:
    """Push each resolved instance's OCI image to ECR via skopeo.

    Source: ``oci:<workdir>/oci_tars/<safe_name>.tar.d`` only (multi-arch
    manifest list). docker-daemon source is forbidden because the multi-arch
    build only loads the native arch into the daemon — pushing from daemon
    would silently drop the other arch.

    Also writes ``ecr_manifest.json`` to ``<output_dir>/`` and uploads it to
    S3 alongside ``final_report.json`` for downstream discovery.

    Returns: ``{"ecr_pushed_count": N, "ecr_repository": "<host>/<repo>",
                "ecr_manifest_s3_uri": "<url or empty>"}``.
    """
    import subprocess
    # Registry kind: defaults to 'ecr' (production). Setting
    # AURORA_REGISTRY_KIND=local opts out of AWS auth, ECR repo precreate,
    # and TLS verification — used for local-k8s development against a
    # bring-your-own OCI registry (e.g. registry:2 in the aurora namespace).
    kind = (os.environ.get("AURORA_REGISTRY_KIND") or "ecr").strip().lower()

    final_report_path = Path(output_dir) / "final_report.json"
    if not final_report_path.exists():
        raise RuntimeError(
            f"final_report.json not found at {final_report_path}; "
            "cannot determine resolved instances."
        )
    with open(final_report_path, "r", encoding="utf-8") as f:
        report = json.load(f)
    resolved_ids = set(report.get("resolved_ids") or [])
    if not resolved_ids:
        _logger.warning("No resolved_ids in final_report.json — nothing to push.")
        return {"ecr_pushed_count": 0, "ecr_repository": "", "ecr_manifest_s3_uri": ""}

    if not ecr_registry:
        raise RuntimeError(
            "AURORA_ECR_REGISTRY is not set; cannot push images. For prod, set "
            "the ECR host (<account>.dkr.ecr.<region>.amazonaws.com). For local, "
            "set the in-cluster registry host (e.g. registry.aurora.svc.cluster.local:5000)."
        )

    ecr_token = ""
    if kind == "ecr":
        if not ecr_region:
            raise RuntimeError(
                "AURORA_ECR_REGION is required when AURORA_REGISTRY_KIND=ecr "
                "(or unset, which defaults to ecr)."
            )
        try:
            token_proc = subprocess.run(
                ["aws", "ecr", "get-login-password", "--region", ecr_region],
                capture_output=True, text=True, check=True, timeout=60,
            )
            ecr_token = token_proc.stdout.strip()
        except FileNotFoundError as exc:
            raise RuntimeError(
                "`aws` CLI not found in worker image. Add awscli to Dockerfile.worker."
            ) from exc
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(
                f"`aws ecr get-login-password` failed: {exc.stderr or exc}. "
                "Check IRSA (prod) or AWS credentials (local) are reachable "
                "from the worker pod."
            ) from exc
    else:
        _logger.info(
            "AURORA_REGISTRY_KIND=%s — using non-AWS push path (no token, no "
            "ECR repo precreate, TLS verification disabled).",
            kind,
        )

    # ``resolved_ids`` in final_report.json uses the harness format
    # ``<org>/<repo>:pr-<number>`` (per PullRequestBase.id). The DB-side
    # `instance_id` column uses ``<org>__<repo>-pr-<number>`` (per
    # instance_id_for). Index by the harness format here, derive the DB
    # format only when we need to write the row.
    by_id: dict[str, Any] = {}
    for inst in instances:
        try:
            pr = inst.pr
            harness_id = f"{pr.org}/{pr.repo}:pr-{pr.number}"
            by_id[harness_id] = inst
        except Exception:
            _logger.debug("Failed to index instance for ecr push", exc_info=True)

    org_for_repo = ""
    repo_for_repo = ""
    pushed_count = 0
    manifest_entries: list[dict[str, Any]] = []
    oci_tars_root = Path(workdir) / "oci_tars"
    import tempfile as _tempfile

    ecr_repo_path = "aurora"
    if kind == "ecr" and resolved_ids:
        try:
            ensure_ecr_repository(ecr_registry, ecr_region, ecr_repo_path)
        except Exception as exc:
            _logger.warning(
                "Failed to ensure ECR repository %s exists: %s. "
                "Push will fail if the repo isn't pre-created.",
                ecr_repo_path, exc,
            )

    for iid in sorted(resolved_ids):
        inst = by_id.get(iid)
        if not inst:
            _logger.warning("Resolved id %r not found in instances map; skipping ECR push", iid)
            continue
        pr = inst.pr
        org, repo = pr.org, pr.repo
        org_for_repo = org_for_repo or org
        repo_for_repo = repo_for_repo or repo
        try:
            image = inst.dependency()
            image_tag_str = image.image_full_name()
        except Exception:
            _logger.exception("Cannot determine image tag for instance %s", iid)
            continue
        safe_name = image_tag_str.replace("/", "_").replace(":", "_")
        oci_dir = oci_tars_root / f"{safe_name}.tar.d"
        if not oci_dir.is_dir():
            _logger.error(
                "OCI dir %s missing — multi-arch build did not produce the layout. "
                "Skipping ECR push for %s.",
                oci_dir, iid,
            )
            continue

        pr_number = getattr(pr, "number", None)
        if not pr_number:
            _logger.error(
                "PR number missing for %s; refusing to push (would collide on the shared aurora repo).",
                iid,
            )
            continue
        # Tag from PR number (mutable). The digest captured below is the
        # immutable handle stored alongside on the instance row.
        raw_tag = f"{org}__{repo}__pr-{pr_number}".lower()
        ecr_tag = re.sub(r"[^a-zA-Z0-9_.-]", "_", raw_tag)[:300]
        ecr_ref = f"{ecr_registry}/{ecr_repo_path}:{ecr_tag}"

        # Capture the manifest digest reliably via --digestfile. Skopeo's
        # stdout/stderr only contain per-blob sha256 lines during transfer;
        # those are blob digests, not the manifest digest we need for the
        # immutable `<ref>@sha256:...` reference in the pull script.
        digest = ""
        with _tempfile.NamedTemporaryFile(
            prefix="skopeo_digest_", suffix=".txt", delete=False,
        ) as df:
            digestfile_path = df.name
        try:
            cmd = [
                "skopeo", "copy", "--multi-arch", "all",
                "--digestfile", digestfile_path,
            ]
            if kind == "ecr":
                cmd.extend(["--dest-creds", f"AWS:{ecr_token}"])
            else:
                # registry:2 (or similar) running plain HTTP inside the cluster.
                cmd.append("--dest-tls-verify=false")
            cmd.extend([f"oci:{oci_dir}", f"docker://{ecr_ref}"])
            try:
                subprocess.run(
                    cmd, capture_output=True, text=True, check=True, timeout=1800,
                )
            except subprocess.CalledProcessError as exc:
                _logger.error(
                    "skopeo push failed for %s -> %s: %s",
                    oci_dir, ecr_ref, exc.stderr or exc,
                )
                continue
            except FileNotFoundError as exc:
                raise RuntimeError(
                    "`skopeo` not found in worker image. Add skopeo to Dockerfile.worker."
                ) from exc
            try:
                with open(digestfile_path, "r", encoding="utf-8") as df_in:
                    digest = df_in.read().strip()
                    if not digest.startswith("sha256:"):
                        digest = ""
            except OSError:
                _logger.debug("Could not read digestfile %s", digestfile_path, exc_info=True)
        finally:
            try:
                os.unlink(digestfile_path)
            except OSError:
                pass

        run_number = run_numbers.get((org, repo), 1)
        # DB row uses the underscore-dash form (instance_id_for) so look up
        # / update by that — not by the harness id we used for the by_id map.
        # Defensive try/except: a per-instance DB hiccup must not kill the
        # entire batch's subsequent pushes (we already paid for the push).
        try:
            db_iid = instance_id_for(pr)
            db_instance_id = ensure_instance(conn, rec_id, org, repo, db_iid)
            update_instance(conn, db_instance_id, {
                "ecr_image_uri": ecr_ref,
                "ecr_image_digest": digest,
            })
        except Exception:
            _logger.exception(
                "DB write failed for %s after successful push to %s — "
                "image is in the registry but DB row was not updated.",
                iid, ecr_ref,
            )
            continue
        manifest_entries.append({
            "instance_id": iid,             # harness id (as in final_report)
            "org": org,
            "repo": repo,
            "pr_number": pr_number,
            "ecr_image_uri": ecr_ref,
            "ecr_image_digest": digest,
            "status": "resolved",
        })
        pushed_count += 1
        _logger.info("Pushed %s -> %s (%s)", iid, ecr_ref, digest or "no-digest")

    ecr_repository_uri = f"{ecr_registry}/{ecr_repo_path}".lower() if resolved_ids else ""

    manifest_s3_uri = ""
    if manifest_entries:
        manifest_doc = {
            "evaluation_id": rec_id,
            "exported_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "ecr_repository": ecr_repository_uri,
            "image_count": len(manifest_entries),
            "images": manifest_entries,
        }
        manifest_path = Path(output_dir) / "ecr_manifest.json"
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(manifest_doc, f, indent=2)
        run_number = run_numbers.get((org_for_repo, repo_for_repo), 1) if org_for_repo else 1
        manifest_s3_uri = _upload_artifact(
            s3_config, use_s3, str(manifest_path),
            s3_storage.build_s3_key(
                org_for_repo or "unknown", repo_for_repo or "unknown",
                run_number, "ecr_manifest.json",
                folder=s3_folder, phase=phase,
            ),
        ) or ""

    # Signal failure when at least one resolved instance was expected but
    # nothing actually pushed — keeps the eval row's oci_export_status truthful.
    all_failed = bool(resolved_ids) and pushed_count == 0
    return {
        "ecr_pushed_count": pushed_count,
        "ecr_repository": ecr_repository_uri,
        "ecr_manifest_s3_uri": manifest_s3_uri,
        "ecr_all_failed": all_failed,
    }


def ensure_ecr_repository(ecr_registry: str, region: str, repo_path: str) -> None:
    import subprocess
    import json as _json

    repo_arn: Optional[str] = None

    try:
        result = subprocess.run(
            ["aws", "ecr", "describe-repositories",
             "--region", region, "--repository-names", repo_path,
             "--output", "json"],
            capture_output=True, text=True, check=True, timeout=30,
        )
        data = _json.loads(result.stdout or "{}")
        repos = data.get("repositories") or []
        if repos:
            repo_arn = repos[0].get("repositoryArn")
    except subprocess.CalledProcessError:
        pass  # repo doesn't exist — fall through to create
    except FileNotFoundError:
        return  # aws CLI missing; let the push surface the real error

    if repo_arn is None:
        try:
            result = subprocess.run(
                ["aws", "ecr", "create-repository",
                 "--region", region, "--repository-name", repo_path,
                 "--image-scanning-configuration", "scanOnPush=true",
                 "--tags", *_ecr_tag_cli_args(),
                 "--output", "json"],
                capture_output=True, text=True, check=True, timeout=30,
            )
            data = _json.loads(result.stdout or "{}")
            repo_arn = (data.get("repository") or {}).get("repositoryArn")
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or "").lower()
            if not ("repositoryalreadyexists" in stderr or "already exists" in stderr):
                raise
            # race: another caller created it — re-describe to recover the ARN
            try:
                result = subprocess.run(
                    ["aws", "ecr", "describe-repositories",
                     "--region", region, "--repository-names", repo_path,
                     "--output", "json"],
                    capture_output=True, text=True, check=True, timeout=30,
                )
                repos = (_json.loads(result.stdout or "{}").get("repositories") or [])
                if repos:
                    repo_arn = repos[0].get("repositoryArn")
            except Exception:
                pass

    if repo_arn:
        try:
            subprocess.run(
                ["aws", "ecr", "tag-resource",
                 "--region", region, "--resource-arn", repo_arn,
                 "--tags", *_ecr_tag_cli_args()],
                capture_output=True, text=True, check=True, timeout=30,
            )
        except subprocess.CalledProcessError as exc:
            _logger.warning(
                "ecr tag-resource failed for %s: %s",
                repo_path, (exc.stderr or "").strip(),
            )
