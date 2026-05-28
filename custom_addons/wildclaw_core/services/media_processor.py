"""Multimedia processing: video frame extraction, PDF text extraction, S3 upload.

Public API used by wildclaw_core.controllers.media_upload and by wrappers:

    process_upload(env, file_bytes, filename, mime_type) -> wildclaw.media.attachment record
    extract_video_frames(env, attachment) -> int
    extract_pdf_text(env, attachment) -> str
    replace_inline_media_with_s3(env, jsonl_messages: list) -> list

Storage backends are configured via ir.config_parameter wildclaw.s3_bucket / prefix / region.
"""

import hashlib
import io
import logging
import os
import tempfile
from pathlib import Path
from typing import Optional

_logger = logging.getLogger(__name__)

_MEDIA_MIME_KIND_MAP = {
    "image/png": "image", "image/jpeg": "image", "image/jpg": "image", "image/webp": "image",
    "image/gif": "image", "image/bmp": "image", "image/heic": "image", "image/svg+xml": "image",
    "video/mp4": "video", "video/webm": "video", "video/quicktime": "video",
    "audio/mpeg": "audio", "audio/wav": "audio", "audio/ogg": "audio", "audio/mp4": "audio",
    "application/pdf": "pdf",
    "text/plain": "text", "text/markdown": "text", "text/csv": "text", "text/html": "text",
    "application/json": "text",
}


def _kind_for_mime(mime: str) -> str:
    return _MEDIA_MIME_KIND_MAP.get((mime or "").lower(), "other")


def _s3_client(env):
    try:
        import boto3
    except ImportError:
        raise RuntimeError("boto3 not installed; pip install boto3")
    region = env["ir.config_parameter"].sudo().get_param("wildclaw.s3_region", "us-east-1")
    return boto3.client("s3", region_name=region)


def _s3_bucket(env) -> str:
    return env["ir.config_parameter"].sudo().get_param("wildclaw.s3_bucket", "")


def _s3_prefix(env) -> str:
    return env["ir.config_parameter"].sudo().get_param("wildclaw.s3_prefix", "wildclaw") or "wildclaw"


def process_upload(env, file_bytes: bytes, filename: str, mime_type: str,
                   *, sandbox_model: Optional[str] = None,
                   sandbox_id_int: Optional[int] = None,
                   task_id_str: Optional[str] = None):
    sha256_hex = hashlib.sha256(file_bytes).hexdigest()
    byte_size = len(file_bytes)
    media_kind = _kind_for_mime(mime_type)

    bucket = _s3_bucket(env)
    s3_key = f"{_s3_prefix(env)}/uploads/{sha256_hex[:2]}/{sha256_hex}_{filename}"
    s3_url = ""
    if bucket:
        try:
            client = _s3_client(env)
            client.put_object(Bucket=bucket, Key=s3_key, Body=file_bytes, ContentType=mime_type or "application/octet-stream")
            s3_url = f"https://{bucket}.s3.amazonaws.com/{s3_key}"
        except Exception as exc:
            _logger.warning("S3 upload failed for %s: %s", filename, exc)

    rec = env["wildclaw.media.attachment"].sudo().create({
        "name": filename,
        "mime_type": mime_type,
        "media_kind": media_kind,
        "byte_size": byte_size,
        "s3_url": s3_url,
        "s3_key": s3_key,
        "sha256_hex": sha256_hex,
        "sandbox_model": sandbox_model or "",
        "sandbox_id_int": sandbox_id_int or 0,
        "task_id_str": task_id_str or "",
        "uploaded_by_id": env.user.id,
    })

    if media_kind == "image":
        _populate_image_dimensions(rec, file_bytes)
    elif media_kind == "video":
        extract_video_frames(env, rec, file_bytes=file_bytes)
    elif media_kind == "pdf":
        extract_pdf_text(env, rec, file_bytes=file_bytes)
    elif media_kind == "audio":
        probe_audio_metadata(env, rec, file_bytes=file_bytes)
    return rec


def probe_audio_metadata(env, attachment, *, file_bytes=None) -> dict:
    import subprocess
    import json as _json

    with tempfile.TemporaryDirectory() as td:
        audio_path = Path(td) / (attachment.name or "audio.bin")
        if file_bytes is None:
            bucket = _s3_bucket(env)
            if not bucket or not attachment.s3_key:
                return {}
            try:
                client = _s3_client(env)
                obj = client.get_object(Bucket=bucket, Key=attachment.s3_key)
                audio_path.write_bytes(obj["Body"].read())
            except Exception as exc:
                _logger.warning("S3 fetch for audio probe failed: %s", exc)
                return {}
        else:
            audio_path.write_bytes(file_bytes)
        try:
            r = subprocess.run(
                ["ffprobe", "-v", "error", "-print_format", "json",
                 "-show_format", "-show_streams", str(audio_path)],
                capture_output=True, text=True, timeout=30,
            )
            data = _json.loads(r.stdout or "{}")
            streams = [s for s in data.get("streams", []) if s.get("codec_type") == "audio"]
            duration = float(data.get("format", {}).get("duration", 0) or 0)
            rate = int(streams[0].get("sample_rate", 0)) if streams else 0
            channels = int(streams[0].get("channels", 0)) if streams else 0
            attachment.write({
                "audio_duration_s": duration,
                "audio_sample_rate": rate,
                "audio_channels": channels,
            })
            return {"duration_s": duration, "sample_rate": rate, "channels": channels}
        except Exception as exc:
            _logger.warning("ffprobe audio probe failed: %s", exc)
            return {}


def transcribe_audio(env, attachment) -> str:
    provider = env["ir.config_parameter"].sudo().get_param("wildclaw.audio_transcription_provider", "")
    if not provider:
        _logger.info("audio transcription disabled (wildclaw.audio_transcription_provider unset)")
        return ""
    if provider == "openai_whisper":
        return _transcribe_openai_whisper(env, attachment)
    if provider == "bedrock":
        return _transcribe_bedrock(env, attachment)
    _logger.warning("unknown audio transcription provider: %s", provider)
    return ""


def _transcribe_openai_whisper(env, attachment) -> str:
    api_key = env["ir.config_parameter"].sudo().get_param("wildclaw.openai_api_key", "")
    if not api_key or not attachment.s3_key:
        return ""
    try:
        import httpx
        bucket = _s3_bucket(env)
        client = _s3_client(env)
        obj = client.get_object(Bucket=bucket, Key=attachment.s3_key)
        audio_bytes = obj["Body"].read()
        with httpx.Client(timeout=120.0) as h:
            r = h.post(
                "https://api.openai.com/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {api_key}"},
                data={"model": "whisper-1"},
                files={"file": (attachment.name, audio_bytes, attachment.mime_type or "audio/mpeg")},
            )
            r.raise_for_status()
            text = r.json().get("text", "")
            attachment.write({"audio_transcript": text[:1_000_000]})
            return text
    except Exception as exc:
        _logger.warning("openai whisper transcription failed: %s", exc)
        return ""


def _transcribe_bedrock(env, attachment) -> str:
    _logger.info("bedrock audio transcription not yet implemented; configure provider=openai_whisper instead")
    return ""


def _populate_image_dimensions(attachment, file_bytes: bytes) -> None:
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(file_bytes))
        attachment.write({"image_width": img.width, "image_height": img.height})
    except Exception as exc:
        _logger.info("image dimension probe failed: %s", exc)


def extract_video_frames(env, attachment, *, file_bytes: Optional[bytes] = None,
                          frame_count: Optional[int] = None) -> int:
    import subprocess
    frame_count = frame_count or int(env["ir.config_parameter"].sudo().get_param("wildclaw.media_video_frame_count", "8"))
    bucket = _s3_bucket(env)
    if not bucket:
        return 0

    with tempfile.TemporaryDirectory() as td:
        video_path = Path(td) / (attachment.name or "video.mp4")
        if file_bytes is None:
            if not attachment.s3_url:
                return 0
            try:
                client = _s3_client(env)
                obj = client.get_object(Bucket=bucket, Key=attachment.s3_key)
                video_path.write_bytes(obj["Body"].read())
            except Exception as exc:
                _logger.warning("S3 fetch for frame extract failed: %s", exc)
                return 0
        else:
            video_path.write_bytes(file_bytes)

        duration_s = _probe_video_duration(video_path)
        fps = max(1.0, frame_count / max(duration_s, 0.1))
        out_pattern = Path(td) / "frame_%04d.jpg"
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-i", str(video_path), "-vf", f"fps={fps:.4f}",
                 "-frames:v", str(frame_count), str(out_pattern)],
                check=True, capture_output=True, timeout=120,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as exc:
            _logger.warning("ffmpeg frame extract failed: %s", exc)
            return 0

        client = _s3_client(env)
        uploaded = 0
        for i in range(1, frame_count + 1):
            frame_path = Path(td) / f"frame_{i:04d}.jpg"
            if not frame_path.exists():
                break
            data = frame_path.read_bytes()
            key = f"{_s3_prefix(env)}/frames/{attachment.sha256_hex}/frame_{i:04d}.jpg"
            try:
                client.put_object(Bucket=bucket, Key=key, Body=data, ContentType="image/jpeg")
                uploaded += 1
            except Exception as exc:
                _logger.warning("frame upload %s failed: %s", key, exc)

        attachment.write({
            "video_duration_s": duration_s,
            "video_fps": fps,
            "frame_extract_count": uploaded,
        })
        return uploaded


def _probe_video_duration(video_path: Path) -> float:
    import subprocess
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(video_path)],
            capture_output=True, text=True, timeout=30,
        )
        return float(result.stdout.strip() or "0")
    except Exception:
        return 0.0


def extract_pdf_text(env, attachment, *, file_bytes: Optional[bytes] = None) -> str:
    try:
        from PyPDF2 import PdfReader
    except ImportError:
        _logger.info("PyPDF2 not installed; skipping PDF text extraction")
        return ""

    if file_bytes is None:
        bucket = _s3_bucket(env)
        if not bucket or not attachment.s3_key:
            return ""
        try:
            client = _s3_client(env)
            obj = client.get_object(Bucket=bucket, Key=attachment.s3_key)
            file_bytes = obj["Body"].read()
        except Exception as exc:
            _logger.warning("S3 fetch for PDF extract failed: %s", exc)
            return ""

    try:
        reader = PdfReader(io.BytesIO(file_bytes))
        pages = [p.extract_text() or "" for p in reader.pages]
        text = "\n\n".join(pages)
        attachment.write({"pdf_page_count": len(pages), "pdf_text": text[:1_000_000]})
        return text
    except Exception as exc:
        _logger.warning("PDF text extraction failed: %s", exc)
        return ""


def replace_inline_media_with_s3(env, messages: list) -> list:
    bucket = _s3_bucket(env)
    if not bucket:
        return messages
    return _walk_replace(env, messages, bucket)


def _walk_replace(env, node, bucket):
    if isinstance(node, list):
        return [_walk_replace(env, n, bucket) for n in node]
    if isinstance(node, dict):
        if node.get("type") in ("image", "input_image") and isinstance(node.get("source"), dict):
            src = node["source"]
            if src.get("type") == "base64" and src.get("data"):
                url = _upload_base64(env, src["data"], src.get("media_type", "image/png"), bucket)
                if url:
                    node["source"] = {"type": "url", "url": url}
        return {k: _walk_replace(env, v, bucket) for k, v in node.items()}
    return node


def _upload_base64(env, data_b64: str, mime: str, bucket: str) -> Optional[str]:
    import base64, uuid
    try:
        data = base64.b64decode(data_b64)
        ext = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "image/gif": "gif"}.get(mime, "bin")
        key = f"{_s3_prefix(env)}/inline/{uuid.uuid4().hex}.{ext}"
        client = _s3_client(env)
        client.put_object(Bucket=bucket, Key=key, Body=data, ContentType=mime)
        return f"https://{bucket}.s3.amazonaws.com/{key}"
    except Exception as exc:
        _logger.warning("inline media upload failed: %s", exc)
        return None
