# -*- coding: utf-8 -*-
"""Image ingestion for imported / generated questions."""
import base64
import binascii
import logging

_logger = logging.getLogger(__name__)

_MAX_BYTES = 15 * 1024 * 1024


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
    # L-6: bound the decode. An admin-supplied data: URL / raw base64 is decoded
    # here to validate it; without a cap a multi-hundred-MB payload blows up
    # worker memory. _MAX_BYTES base64 is ~4/3 larger, so cap the STRING length.
    if payload and len(payload) > (_MAX_BYTES // 3) * 4 + 4:
        _logger.warning("Inline image base64 too large (%d chars); rejected.",
                        len(payload))
        return False
    try:
        base64.b64decode(payload, validate=True)
        return True
    except (binascii.Error, ValueError):
        return False


def _download(url):
    """Fetch a remote image -> (b64, content_type), or (None, None) on failure."""
    try:
        import httpx
    except ImportError:
        _logger.warning("httpx not available; cannot download image %s", url)
        return None, None
    # H-14: SSRF guard. `url` can come from an admin field OR from LLM output
    # (a prompt-injected SOP can plant a source_url). Validate BEFORE the request
    # and re-validate every redirect hop so an external URL can't 302 into cloud
    # metadata / loopback / a private range and exfiltrate the SA token.
    from . import net_guard
    if not net_guard.is_safe_url(url):
        _logger.warning("Refusing image download from unsafe URL: %s", url)
        return None, None
    try:
        with httpx.Client(timeout=20.0, follow_redirects=False) as client:
            hop = url
            for _redirect in range(5):
                with client.stream("GET", hop) as resp:
                    if resp.status_code in (301, 302, 303, 307, 308):
                        nxt = resp.headers.get("location")
                        if not nxt or not net_guard.is_safe_url(nxt):
                            _logger.warning(
                                "Image %s redirected to unsafe URL %s; aborted.",
                                url, nxt)
                            return None, None
                        hop = nxt
                        continue
                    resp.raise_for_status()
                    chunks, total = [], 0
                    for chunk in resp.iter_bytes():
                        total += len(chunk)
                        if total > _MAX_BYTES:
                            _logger.warning(
                                "Image %s exceeds %s bytes; aborted download.",
                                url, _MAX_BYTES)
                            return None, None
                        chunks.append(chunk)
                    content = b"".join(chunks)
                    if not content:
                        _logger.warning("Image %s empty; skipped.", url)
                        return None, None
                    ctype = (resp.headers.get("content-type")
                             or _content_type_for(url))
                    return base64.b64encode(content).decode(), ctype.split(";")[0]
            _logger.warning("Image %s exceeded redirect limit; aborted.", url)
            return None, None
    except Exception as exc:  # noqa: BLE001 - never abort an import on one image
        _logger.warning("Image download failed for %s: %s", url, exc)
        return None, None


def download_bytes(env, url):
    """Fetch a remote image -> (b64, content_type), or (None, None) on failure.

    An object living in OUR configured S3 bucket is fetched via an AUTHENTICATED
    server-side S3 GET (a plain public GET 403s on a private bucket); any other
    URL (external site / CDN) uses the unsigned HTTP GET."""
    from . import s3_service
    key = s3_service.object_key_from_url(env, url)
    if key:
        raw, ctype = s3_service.download(env, key)
        if raw:
            return base64.b64encode(raw).decode(), ctype or _content_type_for(url)
    return _download(url)


def ingest(env, url=None, data=None, key_hint="qimg", content_type=None):
    """Resolve an image/video spec to ``(url, b64)``. Never raises.

    ``content_type`` lets a caller declare the MIME of RAW base64 ``data`` (e.g.
    ``video/mp4`` for a clip upload) so the S3 object gets the right ext/type; it
    is ignored for a ``data:`` URL, whose embedded MIME wins. Left None for the
    image path, which keeps the historical image/png default."""
    from . import s3_service

    url = (url or "").strip() or None
    data = (data or "").strip() or None
    configured = s3_service.is_configured(env)

    if data:
        payload, ctype = _strip_data_url(data)
        if content_type and not data.startswith("data:"):
            ctype = content_type
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
            return False, payload
        _logger.warning("Inline image data for %s not valid base64; skipped.",
                        key_hint)

    if url:
        if url.startswith("data:"):
            return ingest(env, data=url, key_hint=key_hint)
        if not configured:
            return url, False
        b64, ctype = download_bytes(env, url)
        if not b64:
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
