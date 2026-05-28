"""Data-prep helpers mirroring WildClawBench's script/prepare.sh.

Public API:
    download_video(env, url, *, output_dir=None, format='best[ext=mp4]') -> Path
    trim_video(env, src_path, *, start_s, duration_s, output_path) -> Path
    download_modelscope(env, repo_id, *, filename, output_dir=None) -> Path
    download_hf_hub(env, repo_id, *, filename, output_dir=None) -> Path
    extract_archive(env, archive_path, *, output_dir=None) -> Path

All functions register the resulting file as a wildclaw.media.attachment with
source_kind set appropriately (yt_dlp / modelscope / hf_hub / archive_extract).
"""

import hashlib
import logging
import os
import subprocess
import tarfile
import tempfile
import zipfile
from pathlib import Path
from typing import Optional

_logger = logging.getLogger(__name__)


def _prep_dir(env) -> Path:
    base = Path(env["ir.config_parameter"].sudo().get_param("wildclaw.prep_dir", "/tmp/wildclaw_prep"))
    base.mkdir(parents=True, exist_ok=True)
    return base


def _register_file(env, path: Path, mime_type: str, *, source_kind: str, source_url: str = "") -> "models.Model":  # noqa: F821
    from . import media_processor
    data = path.read_bytes()
    rec = media_processor.process_upload(
        env, file_bytes=data, filename=path.name, mime_type=mime_type,
    )
    rec.write({"source_kind": source_kind, "source_url": source_url})
    return rec


def download_video(env, url: str, *, output_dir: Optional[Path] = None,
                   format: str = "best[ext=mp4]/best", register: bool = True):
    target_dir = Path(output_dir) if output_dir else _prep_dir(env) / "videos"
    target_dir.mkdir(parents=True, exist_ok=True)
    out_template = str(target_dir / "%(title)s_%(id)s.%(ext)s")
    try:
        subprocess.run(
            ["yt-dlp", "-f", format, "-o", out_template, "--no-playlist", url],
            check=True, capture_output=True, timeout=600,
        )
    except subprocess.CalledProcessError as exc:
        _logger.error("yt-dlp failed for %s: %s", url, exc.stderr.decode() if exc.stderr else "")
        raise RuntimeError(f"yt-dlp failed for {url}")
    except FileNotFoundError:
        raise RuntimeError("yt-dlp not installed; pip install yt-dlp")

    files = sorted(target_dir.glob("*"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        raise RuntimeError(f"yt-dlp produced no output for {url}")
    downloaded = files[0]
    if register:
        return _register_file(env, downloaded, "video/mp4", source_kind="yt_dlp", source_url=url)
    return downloaded


def trim_video(env, src_path: Path, *, start_s: float, duration_s: float,
               output_path: Optional[Path] = None, register: bool = True):
    src_path = Path(src_path)
    if output_path is None:
        output_path = src_path.parent / f"{src_path.stem}_trim_{int(start_s)}_{int(duration_s)}.mp4"
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-ss", str(start_s), "-i", str(src_path),
             "-t", str(duration_s), "-c", "copy", str(output_path)],
            check=True, capture_output=True, timeout=300,
        )
    except subprocess.CalledProcessError as exc:
        _logger.error("ffmpeg trim failed: %s", exc.stderr.decode() if exc.stderr else "")
        raise RuntimeError("ffmpeg trim failed")
    if register:
        return _register_file(env, output_path, "video/mp4", source_kind="archive_extract",
                              source_url=f"trim:{src_path}@{start_s}+{duration_s}")
    return output_path


def download_modelscope(env, repo_id: str, *, filename: str,
                         output_dir: Optional[Path] = None, register: bool = True):
    target_dir = Path(output_dir) if output_dir else _prep_dir(env) / "weights" / repo_id.replace("/", "_")
    target_dir.mkdir(parents=True, exist_ok=True)
    out_path = target_dir / filename
    if out_path.exists():
        _logger.info("modelscope file already present: %s", out_path)
    else:
        url = f"https://www.modelscope.cn/models/{repo_id}/resolve/master/{filename}"
        try:
            import httpx
            with httpx.stream("GET", url, timeout=600.0, follow_redirects=True) as r:
                r.raise_for_status()
                with open(out_path, "wb") as f:
                    for chunk in r.iter_bytes(chunk_size=1 << 20):
                        f.write(chunk)
        except Exception as exc:
            _logger.error("modelscope download failed for %s/%s: %s", repo_id, filename, exc)
            raise
    if register:
        mime = "application/octet-stream"
        return _register_file(env, out_path, mime, source_kind="modelscope",
                              source_url=f"modelscope://{repo_id}/{filename}")
    return out_path


def download_hf_hub(env, repo_id: str, *, filename: str,
                    output_dir: Optional[Path] = None, register: bool = True):
    target_dir = Path(output_dir) if output_dir else _prep_dir(env) / "weights" / repo_id.replace("/", "_")
    target_dir.mkdir(parents=True, exist_ok=True)
    out_path = target_dir / filename
    if out_path.exists():
        return out_path if not register else _register_file(env, out_path, "application/octet-stream",
                                                            source_kind="hf_hub",
                                                            source_url=f"hf://{repo_id}/{filename}")
    try:
        from huggingface_hub import hf_hub_download
        downloaded = hf_hub_download(repo_id=repo_id, filename=filename, local_dir=str(target_dir))
        out_path = Path(downloaded)
    except ImportError:
        token = env["ir.config_parameter"].sudo().get_param("wildclaw.hf_token", "")
        url = f"https://huggingface.co/{repo_id}/resolve/main/{filename}"
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        try:
            import httpx
            with httpx.stream("GET", url, headers=headers, timeout=600.0, follow_redirects=True) as r:
                r.raise_for_status()
                with open(out_path, "wb") as f:
                    for chunk in r.iter_bytes(chunk_size=1 << 20):
                        f.write(chunk)
        except Exception as exc:
            _logger.error("hf_hub download failed for %s/%s: %s", repo_id, filename, exc)
            raise
    if register:
        return _register_file(env, out_path, "application/octet-stream", source_kind="hf_hub",
                              source_url=f"hf://{repo_id}/{filename}")
    return out_path


def extract_archive(env, archive_path: Path, *, output_dir: Optional[Path] = None,
                    register_files: bool = True) -> Path:
    archive_path = Path(archive_path)
    target = Path(output_dir) if output_dir else _prep_dir(env) / "extracted" / archive_path.stem
    target.mkdir(parents=True, exist_ok=True)
    suffix = archive_path.suffix.lower()
    name = archive_path.name.lower()
    try:
        if name.endswith((".tar.gz", ".tgz")) or suffix == ".tar":
            with tarfile.open(archive_path, "r:*") as tf:
                tf.extractall(target)
        elif suffix == ".zip":
            with zipfile.ZipFile(archive_path) as zf:
                zf.extractall(target)
        else:
            raise ValueError(f"unsupported archive suffix: {suffix}")
    except Exception as exc:
        _logger.error("archive extract failed for %s: %s", archive_path, exc)
        raise
    if register_files:
        for p in target.rglob("*"):
            if p.is_file():
                try:
                    _register_file(env, p, "application/octet-stream", source_kind="archive_extract",
                                   source_url=f"archive:{archive_path}")
                except Exception as exc:
                    _logger.warning("could not register extracted %s: %s", p, exc)
    return target
