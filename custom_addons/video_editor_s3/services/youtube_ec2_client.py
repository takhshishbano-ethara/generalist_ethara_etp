# -*- coding: utf-8 -*-
import logging
import random
import time

import requests

from odoo import _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

_CONNECT_TIMEOUT = 15
_READ_TIMEOUT = 30
_RETRYABLE_HTTP_STATUSES = {408, 425, 429, 500, 502, 503, 504, 509}
_MAX_ATTEMPTS = 3


class _RetryableHTTP(Exception):
    def __init__(self, status, body):
        super().__init__("HTTP %s: %s" % (status, (body or "")[:400]))
        self.status = status
        self.body = body


def _is_retryable(exc):
    if isinstance(exc, _RetryableHTTP):
        return True
    if isinstance(exc, (requests.ConnectionError, requests.Timeout)):
        return True
    return False


def submit_youtube_job(*, base_url, payload):
    """POST a YouTube ingest job to the EC2 FastAPI service.

    Returns the parsed JSON acknowledgement on success (or an empty dict if
    the body is not JSON). Raises ``UserError`` if all retries are exhausted
    or the server returns a non-retryable 4xx/5xx.
    """
    if not base_url:
        raise UserError(_("YouTube EC2 base URL is not configured."))
    endpoint = base_url.rstrip("/") + "/download"

    last_exc = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            resp = requests.post(
                endpoint,
                json=payload,
                timeout=(_CONNECT_TIMEOUT, _READ_TIMEOUT),
                headers={"Content-Type": "application/json"},
            )
            if resp.status_code in _RETRYABLE_HTTP_STATUSES:
                raise _RetryableHTTP(resp.status_code, resp.text)
            if resp.status_code >= 400:
                raise UserError(_(
                    "YouTube EC2 dispatch failed (HTTP %d): %s"
                ) % (resp.status_code, (resp.text or "")[:400]))
            try:
                return resp.json() if resp.content else {}
            except ValueError:
                return {}
        except UserError:
            raise
        except Exception as exc:
            last_exc = exc
            if attempt >= _MAX_ATTEMPTS or not _is_retryable(exc):
                if isinstance(exc, _RetryableHTTP):
                    raise UserError(_(
                        "YouTube EC2 dispatch failed after %d attempts (HTTP %s): %s"
                    ) % (_MAX_ATTEMPTS, exc.status, (exc.body or "")[:400])) from exc
                raise UserError(_(
                    "YouTube EC2 dispatch failed after %d attempts: %s"
                ) % (_MAX_ATTEMPTS, exc)) from exc
            delay = min(30.0, (2 ** attempt) + random.random())
            _logger.warning(
                "youtube_ec2 retry %d/%d in %.1fs (%s)",
                attempt, _MAX_ATTEMPTS, delay, exc.__class__.__name__,
            )
            time.sleep(delay)

    raise UserError(_(
        "YouTube EC2 dispatch failed after %d attempts: %s"
    ) % (_MAX_ATTEMPTS, last_exc))
