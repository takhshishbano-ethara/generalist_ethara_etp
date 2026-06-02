# -*- coding: utf-8 -*-
import logging
import os
import urllib.parse

from odoo import _
from odoo.exceptions import UserError

import requests

_logger = logging.getLogger(__name__)

_CONNECT_TIMEOUT_SECONDS = 15
_READ_TIMEOUT_SECONDS = 900
_CHUNK_SIZE = 1024 * 1024


def _decode_header(value):
    if not value:
        return ""
    try:
        return urllib.parse.unquote(value)
    except (TypeError, ValueError):
        return value


def download_clip_via_local_extractor(
    base_url,
    youtube_url,
    *,
    tier,
    start_seconds=0.0,
    end_seconds=0.0,
    target_dir,
    progress_cb=None,
    cancel_event=None,
    cancel_exception=None,
    max_size_bytes=None,
):
    if not base_url:
        raise UserError(_("Local extractor URL is not configured."))
    if not youtube_url:
        raise UserError(_("YouTube URL is required."))
    if not os.path.isdir(target_dir):
        os.makedirs(target_dir, exist_ok=True)

    cancel_exc = cancel_exception or InterruptedError
    endpoint = base_url.rstrip("/") + "/download"
    payload = {
        "url": youtube_url,
        "tier": tier or "2160p",
        "start_seconds": float(start_seconds or 0.0),
        "end_seconds": float(end_seconds or 0.0),
    }

    try:
        resp = requests.post(
            endpoint,
            json=payload,
            stream=True,
            timeout=(_CONNECT_TIMEOUT_SECONDS, _READ_TIMEOUT_SECONDS),
        )
    except requests.RequestException as exc:
        raise UserError(_(
            "Local extractor at %(url)s is unreachable: %(err)s"
        ) % {"url": endpoint, "err": exc}) from exc

    if resp.status_code != 200:
        try:
            body = resp.json()
            err = body.get("error") if isinstance(body, dict) else None
        except ValueError:
            err = (resp.text or "")[:500]
        resp.close()
        raise UserError(_(
            "Local extractor failed (HTTP %(code)s): %(err)s"
        ) % {"code": resp.status_code, "err": err or "(no body)"})

    content_type = (resp.headers.get("Content-Type") or "").lower()
    if "video/mp4" not in content_type and "application/octet-stream" not in content_type:
        resp.close()
        raise UserError(_(
            "Local extractor returned unexpected Content-Type %s; expected video/mp4."
        ) % (content_type or "(none)"))

    advertised_total = 0
    try:
        advertised_total = int(resp.headers.get("Content-Length") or 0)
    except (TypeError, ValueError):
        advertised_total = 0
    if max_size_bytes and advertised_total and advertised_total > int(max_size_bytes):
        resp.close()
        raise UserError(_(
            "Local extractor reported %(adv).1f MB which exceeds the configured "
            "max of %(cap).1f MB."
        ) % {
            "adv": advertised_total / (1024 * 1024),
            "cap": int(max_size_bytes) / (1024 * 1024),
        })

    metadata = {
        "video_id": _decode_header(resp.headers.get("X-Video-Id")),
        "title": _decode_header(resp.headers.get("X-Video-Title")),
        "channel": _decode_header(resp.headers.get("X-Video-Channel")),
        "filename": _decode_header(resp.headers.get("X-Video-Filename")) or "youtube.mp4",
    }
    try:
        metadata["duration_seconds"] = float(resp.headers.get("X-Video-Duration-Seconds") or 0.0)
    except (TypeError, ValueError):
        metadata["duration_seconds"] = 0.0

    safe_name = os.path.basename(metadata["filename"]) or "youtube.mp4"
    if not safe_name.lower().endswith(".mp4"):
        safe_name = safe_name + ".mp4"
    local_path = os.path.join(target_dir, safe_name)

    written = 0
    try:
        with open(local_path, "wb") as fh:
            for chunk in resp.iter_content(chunk_size=_CHUNK_SIZE):
                if cancel_event is not None and cancel_event.is_set():
                    raise cancel_exc()
                if not chunk:
                    continue
                fh.write(chunk)
                written += len(chunk)
                if max_size_bytes and written > int(max_size_bytes):
                    raise UserError(_(
                        "Local extractor download exceeded the configured max "
                        "of %(cap).1f MB (got %(got).1f MB so far)."
                    ) % {
                        "cap": int(max_size_bytes) / (1024 * 1024),
                        "got": written / (1024 * 1024),
                    })
                if progress_cb is not None:
                    try:
                        progress_cb(written, advertised_total or 0, "downloading")
                    except Exception:
                        _logger.debug("local extractor progress callback raised", exc_info=True)
    except Exception:
        try:
            if os.path.isfile(local_path):
                os.unlink(local_path)
        except OSError:
            pass
        raise
    finally:
        resp.close()

    if written == 0:
        raise UserError(_("Local extractor returned an empty response body."))

    _logger.info(
        "local_extractor: downloaded %d bytes via %s -> %s",
        written, endpoint, local_path,
    )
    return local_path, metadata


def health_check(base_url, timeout_seconds=10):
    if not base_url:
        raise UserError(_("Local extractor URL is not configured."))
    endpoint = base_url.rstrip("/") + "/health"
    try:
        resp = requests.get(endpoint, timeout=timeout_seconds)
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as exc:
        raise UserError(_(
            "Local extractor health check failed: %s"
        ) % exc) from exc
