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

Design notes:

* Anonymous HTTPS GET only. The production S3 bucket is publicly readable,
  per product decision. No boto3 dependency here.
* Cache key is ``sha1(url)`` to avoid collisions between different runs that
  happen to share a filename (e.g. two pipelines both wrote ``lht_dataset.jsonl``).
* Download is atomic: write to ``<target>.part`` then ``os.replace`` — a crash
  mid-download never leaves a truncated cached file in place.
* A per-URL ``threading.Lock`` (keyed by sha1) serialises concurrent downloads
  within the same process so two parallel evaluation workers don't both
  download the same dataset.
* The cache lives under ``{aurora.output_dir}/dataset_cache/`` which is the
  same root everything else in this addon writes to.
"""
from __future__ import annotations

import hashlib
import logging
import os
import shutil
import tempfile
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
    """Return True if ``path`` is a URL we need to download before reading.

    Recognises ``http://``, ``https://``, and ``s3://`` schemes. Plain local
    paths (``/tmp/x.jsonl``, ``./x.jsonl``, ``C:\\x.jsonl``) return False.
    """
    if not path:
        return False
    scheme = urlparse(path).scheme.lower()
    return scheme in ("http", "https", "s3")


def _cache_root(output_dir_param: str) -> str:
    """Return ``{aurora.output_dir}/dataset_cache`` — created on demand."""
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
    """Atomically download ``url`` to ``target`` via anonymous HTTPS GET.

    Writes to ``target + ".part"`` first, then ``os.replace`` for atomicity.
    Raises on non-2xx response or network error — callers should translate
    to a user-facing ``UserError``.
    """
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
        # Best-effort cleanup of the partial file; ignore cleanup errors.
        try:
            if os.path.exists(part):
                os.remove(part)
        except OSError:
            pass
        raise


def _download_s3(url: str, target: str) -> None:
    """Download an ``s3://bucket/key`` URL as anonymous HTTPS via the
    regional virtual-hosted URL.

    This exists so users who paste an ``s3://`` URI (matching the AWS CLI
    style) get the same behaviour as pasting the ``https://…amazonaws.com/…``
    form. Requires the bucket to be publicly readable, per product decision.
    """
    parsed = urlparse(url)
    bucket = parsed.netloc
    key = parsed.path.lstrip("/")
    if not bucket or not key:
        raise ValueError(f"Invalid s3:// URL (missing bucket or key): {url!r}")
    https_url = f"https://{bucket}.s3.amazonaws.com/{key}"
    _download_http(https_url, target)


def _get_output_dir(env_or_cr: Any) -> str:
    """Read ``aurora.output_dir`` from config, supporting both Odoo env and
    a raw psycopg2 cursor (used by background workers that don't have an
    Environment yet).

    Falls back to ``/tmp/aurora_output`` to mirror the rest of this addon.
    """
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

    * If ``path`` is already local, returned unchanged (even if it doesn't
      exist — the caller's ``os.path.isfile`` check will surface that).
    * If ``path`` is a remote URL, download once into
      ``{aurora.output_dir}/dataset_cache/<sha1(url)>/<basename>`` and return
      that local path. Subsequent calls with the same URL return the cached
      path without re-downloading.

    ``env_or_cr`` may be either an Odoo ``Environment`` (UI thread) or a
    raw psycopg2 cursor (background worker before ``api.Environment`` is
    constructed). It's only used to read the ``aurora.output_dir`` config
    parameter.
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
        # Re-check after acquiring the lock — another thread may have just
        # finished downloading while we were waiting.
        if os.path.isfile(target) and os.path.getsize(target) > 0:
            return target
        scheme = urlparse(path).scheme.lower()
        if scheme == "s3":
            _download_s3(path, target)
        else:
            _download_http(path, target)
        return target


def clear_cache(env_or_cr: Any, url: str | None = None) -> None:
    """Remove cached datasets. Intended for admin tooling / tests.

    If ``url`` is given, drop only that entry; otherwise drop the whole cache.
    Does NOT raise on missing files — idempotent.
    """
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
