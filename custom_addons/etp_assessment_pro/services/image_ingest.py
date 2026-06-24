# -*- coding: utf-8 -*-
"""Image ingestion for imported / generated questions.

A question image can arrive three ways from an import row:

- a remote ``url`` (http/https)   -> downloaded, then (if S3 is configured)
  re-uploaded to our own bucket so the portal never hot-links a third party;
  otherwise the original URL is kept as-is on ``image_url``.
- a ``data:`` URL or raw base64  -> uploaded to S3 when configured, else the
  binary is kept on the Odoo record (``image`` Binary field).
- nothing                         -> ``(False, False)``.

``ingest`` NEVER raises: on any download / upload failure it logs and falls
back to the least-destructive option (keep the original URL, or keep the
binary) so a single bad image can't abort a whole bank import.
"""
import base64
import binascii
import logging

_logger = logging.getLogger(__name__)

_MAX_BYTES = 15 * 1024 * 1024  # 15 MB safety cap on remote downloads.


def _content_type_for(url, default="image/png"):
    u = (url or "").lower()
    if u.endswith((".jpg", ".jpeg")):
        return "image/jpeg"
    if u.endswith(".gif"):
        return "image/gif"
    if u.endswith(".webp"):
        return "image/webp"
    if u.endswith(".png"):
        return "image/png"
    return default


def _strip_data_url(data):
    """Return (b64_payload, content_type) from a data: URL or raw base64."""
    if not data:
        return None, "image/png"
    data = data.strip()
    if data.startswith("data:"):
        try:
            header, payload = data.split(",", 1)
        except ValueError:
            return None, "image/png"
        ctype = "image/png"
        if ";" in header and ":" in header:
            ctype = header[header.index(":") + 1:header.index(";")] or ctype
        return payload.strip(), ctype
    return data, "image/png"


def _is_valid_b64(payload):
    try:
        base64.b64decode(payload, validate=True)
        return True
    except (binascii.Error, ValueError):
        return False


def _download(url):
    """Fetch a remote image -> base64 string. Returns (b64, content_type) or
    (None, None) on any failure. Uses httpx (a declared dependency)."""
    try:
        import httpx
    except ImportError:
        _logger.warning("httpx not available; cannot download image %s", url)
        return None, None
    try:
        with httpx.Client(timeout=20.0, follow_redirects=True) as client:
            resp = client.get(url)
            resp.raise_for_status()
            content = resp.content
            if not content or len(content) > _MAX_BYTES:
                _logger.warning(
                    "Image %s empty or exceeds %s bytes; skipped download.",
                    url, _MAX_BYTES)
                return None, None
            ctype = resp.headers.get("content-type") or _content_type_for(url)
            return base64.b64encode(content).decode(), ctype.split(";")[0]
    except Exception as exc:  # noqa: BLE001 - never abort an import on one image
        _logger.warning("Image download failed for %s: %s", url, exc)
        return None, None


def ingest(env, url=None, data=None, key_hint="qimg"):
    """Resolve an image spec to ``(image_url, image_b64)``.

    Exactly one of the two return slots is normally populated:
    - ``image_url`` set  -> the portal serves the (S3 / external) URL.
    - ``image_b64`` set  -> the binary is stored on the Odoo record.

    Order of preference: S3 upload (own bucket) > keep external URL > keep
    binary on record. Always safe; never raises.
    """
    from . import s3_service

    url = (url or "").strip() or None
    data = (data or "").strip() or None
    configured = s3_service.is_configured(env)

    # 1) Inline data / base64.
    if data:
        payload, ctype = _strip_data_url(data)
        if payload and _is_valid_b64(payload):
            if configured:
                try:
                    s3_url, _key = s3_service.upload_b64(
                        env, payload, key_hint=key_hint, content_type=ctype)
                    return s3_url, False
                except Exception as exc:  # noqa: BLE001
                    _logger.warning(
                        "S3 upload of inline image failed (%s); keeping "
                        "binary on record.", exc)
            return False, payload  # keep binary on the Odoo record
        _logger.warning("Inline image data for %s not valid base64; skipped.",
                        key_hint)

    # 2) Remote URL.
    if url:
        if url.startswith("data:"):
            return ingest(env, data=url, key_hint=key_hint)
        if not configured:
            # No bucket: just point the portal at the original URL.
            return url, False
        b64, ctype = _download(url)
        if not b64:
            # Download failed but we still have a usable external URL.
            return url, False
        try:
            s3_url, _key = s3_service.upload_b64(
                env, b64, key_hint=key_hint,
                content_type=ctype or _content_type_for(url))
            return s3_url, False
        except Exception as exc:  # noqa: BLE001
            _logger.warning(
                "S3 re-upload of %s failed (%s); keeping external URL.",
                url, exc)
            return url, False

    return False, False
