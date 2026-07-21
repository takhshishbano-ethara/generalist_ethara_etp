# -*- coding: utf-8 -*-
"""Shared SSRF guard for any server-side fetch of a caller-supplied URL.

Both the image ingester (services/image_ingest._download) and the headless-
Chromium DOM capture (services/dom_capture.capture_and_annotate) open URLs that
can originate from an admin form field OR from LLM output (a prompt-injected SOP
can make the model emit a source_url). Without a guard those reach cloud metadata
(169.254.169.254 / metadata.google.internal), loopback, or private-range hosts —
classic SSRF that can steal the GCP service-account token.

This module is the ONE place that decides whether a URL is safe to fetch. It:
  * allows only http/https,
  * resolves the host to its IPs and rejects private / loopback / link-local /
    reserved / multicast ranges (defeats DNS-rebinding to an internal IP),
  * blocks the well-known metadata hostnames by name as belt-and-suspenders.

Pure stdlib, no Odoo import, so it is unit-testable in isolation.
"""
import ipaddress
import logging
import socket
from urllib.parse import urlparse

_logger = logging.getLogger(__name__)

_BLOCKED_HOSTNAMES = {
    "metadata.google.internal",
    "metadata",
    "localhost",
}


def _ip_is_blocked(ip_str):
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return True  # unparseable -> refuse
    return (
        ip.is_private or ip.is_loopback or ip.is_link_local
        or ip.is_reserved or ip.is_multicast or ip.is_unspecified
    )


def is_safe_url(url, *, allow_data=False):
    """True only when ``url`` is safe to open server-side. http(s) hosts are
    resolved and must be entirely public/non-metadata. Fail-closed on anything
    unparseable.

    ``allow_data``: when True, a ``data:`` URL is permitted. A data: URL is
    inline content with NO host to reach, so it cannot be an SSRF vector — the
    DOM-capture path renders self-contained data: HTML and needs this. It stays
    OFF by default so a plain image/file fetch never accepts one."""
    raw = (url or "").strip()
    if allow_data and raw[:5].lower() == "data:":
        return True
    try:
        parsed = urlparse(raw)
    except (ValueError, TypeError):
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = parsed.hostname
    if not host:
        return False
    if host.lower() in _BLOCKED_HOSTNAMES:
        return False
    # A bare IP literal in the URL: check it directly.
    try:
        ipaddress.ip_address(host)
        return not _ip_is_blocked(host)
    except ValueError:
        pass
    # A hostname: resolve every A/AAAA record and reject if ANY is internal
    # (stops a name that resolves to 169.254.169.254 / 10.x / ::1).
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    if not infos:
        return False
    for _family, _type, _proto, _canon, sockaddr in infos:
        if _ip_is_blocked(sockaddr[0]):
            return False
    return True


def assert_safe_url(url, *, context="", allow_data=False):
    """Raise ValueError if ``url`` is not safe to fetch server-side."""
    if not is_safe_url(url, allow_data=allow_data):
        _logger.warning("SSRF guard blocked URL %r%s", url,
                        (" (%s)" % context) if context else "")
        raise ValueError(
            "Refusing to fetch %r: not a public http(s) URL (SSRF guard)%s"
            % (url, (" [%s]" % context) if context else ""))
