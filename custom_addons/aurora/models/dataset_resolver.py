"""Resolve remote dataset URLs to local files for Aurora Phase-2 evaluation.

Phase-1 (collect pipeline) uploads the LHT dataset to S3 and stores the public
HTTPS URL in ``aurora.pipeline.step6_file`` (see ``pipeline_executor.py``
``dataset_url = f"https://{bucket}.s3.{region}.amazonaws.com/{key}"``).

Phase-2 (evaluation + harness staging) reads that value from
``aurora.evaluation.dataset_file`` / ``aurora.harness.staging.dataset_file`` and
historically treated it as a **local filesystem path** — ``os.path.isfile``,
``open()``, ``glob.glob`` downstream in ``EvalConfig`` / ``ReportCliArgs``.
That fails in production with "Dataset file not found: https://…s3…/…jsonl".

This module bridges the gap: given a value that may be a local path OR a
remote URL, it returns a usable local path, downloading once into a
content-addressed cache so repeated runs are cheap.
"""
from __future__ import annotations

import hashlib
import logging
import os
import shutil
import threading
from typing import Any
from urllib.parse import urlparse

_logger = logging.getLogger(__name__)

_download_locks: dict[str, threading.Lock] = {}
_download_locks_guard = threading.Lock()

_DOWNLOAD_CHUNK_BYTES = 1024 * 1024

# (connect_timeout, read_timeout): connect stays tight to fail fast on
# unreachable hosts; read is generous because datasets can exceed 100 MB.
_HTTP_TIMEOUT = (10, 300)


def is_remote(path: str | None) -> bool:
    if not path:
        return False
    scheme = urlparse(path).scheme.lower()
    return scheme in ("http", "https", "s3")


def _cache_root(output_dir_param: str) -> str:
    root = os.path.join(output_dir_param, "dataset_cache")
    os.makedirs(root, exist_ok=True)
    return root


def _cache_key(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest()


def _target_path(cache_root: str, url: str) -> str:
    key = _cache_key(url)
    basename = os.path.basename(urlparse(url).path) or "dataset.jsonl"
    bucket = os.path.join(cache_root, key)
    os.makedirs(bucket, exist_ok=True)
    return os.path.join(bucket, basename)


def _get_download_lock(url: str) -> threading.Lock:
    key = _cache_key(url)
    with _download_locks_guard:
        lock = _download_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _download_locks[key] = lock
        return lock


def _download_http(url: str, target: str) -> None:
    import requests

    part = target + ".part"
    _logger.info("Aurora dataset_resolver: downloading %s -> %s", url, target)
    try:
        with requests.get(url, stream=True, timeout=_HTTP_TIMEOUT) as resp:
            resp.raise_for_status()
            with open(part, "wb") as fh:
                for chunk in resp.iter_content(chunk_size=_DOWNLOAD_CHUNK_BYTES):
                    if chunk:
                        fh.write(chunk)
        os.replace(part, target)
        _logger.info(
            "Aurora dataset_resolver: downloaded %.1f MB to %s",
            os.path.getsize(target) / (1024 * 1024), target,
        )
    except Exception:
        try:
            if os.path.exists(part):
                os.remove(part)
        except OSError:
            pass
        raise


def _download_s3(url: str, target: str) -> None:
    parsed = urlparse(url)
    bucket = parsed.netloc
    key = parsed.path.lstrip("/")
    if not bucket or not key:
        raise ValueError(f"Invalid s3:// URL (missing bucket or key): {url!r}")
    https_url = f"https://{bucket}.s3.amazonaws.com/{key}"
    _download_http(https_url, target)


def _get_output_dir(env_or_cr: Any) -> str:
    default = "/tmp/aurora_output"
    if hasattr(env_or_cr, "__getitem__") and not hasattr(env_or_cr, "execute"):
        try:
            ICP = env_or_cr["ir.config_parameter"].sudo()
            return ICP.get_param("aurora.output_dir", default)
        except Exception:
            _logger.debug("dataset_resolver: failed env lookup, using default", exc_info=True)
            return default
    if hasattr(env_or_cr, "execute"):
        try:
            env_or_cr.execute(
                "SELECT value FROM ir_config_parameter WHERE key = %s",
                ("aurora.output_dir",),
            )
            row = env_or_cr.fetchone()
            if row and row[0]:
                return row[0]
        except Exception:
            _logger.debug("dataset_resolver: raw cursor lookup failed", exc_info=True)
    return default


def resolve_to_local(env_or_cr: Any, path: str) -> str:
    """Return a local filesystem path for ``path``.

    * If ``path`` is already local, returned unchanged.
    * If ``path`` is a remote URL, download once into
      ``{aurora.output_dir}/dataset_cache/<sha1(url)>/<basename>`` and return
      that local path. Subsequent calls with the same URL return the cached
      path without re-downloading.
    """
    if not path:
        return path
    if not is_remote(path):
        return path

    output_dir = _get_output_dir(env_or_cr)
    cache_root = _cache_root(output_dir)
    target = _target_path(cache_root, path)

    if os.path.isfile(target) and os.path.getsize(target) > 0:
        _logger.debug("Aurora dataset_resolver: cache hit for %s -> %s", path, target)
        return target

    lock = _get_download_lock(path)
    with lock:
        if os.path.isfile(target) and os.path.getsize(target) > 0:
            return target
        scheme = urlparse(path).scheme.lower()
        if scheme == "s3":
            _download_s3(path, target)
        else:
            _download_http(path, target)
        return target


def clear_cache(env_or_cr: Any, url: str | None = None) -> None:
    output_dir = _get_output_dir(env_or_cr)
    cache_root = os.path.join(output_dir, "dataset_cache")
    if not os.path.isdir(cache_root):
        return
    if url:
        bucket = os.path.join(cache_root, _cache_key(url))
        if os.path.isdir(bucket):
            shutil.rmtree(bucket, ignore_errors=True)
        return
    shutil.rmtree(cache_root, ignore_errors=True)
