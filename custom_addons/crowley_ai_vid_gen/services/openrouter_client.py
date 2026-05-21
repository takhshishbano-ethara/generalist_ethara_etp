# -*- coding: utf-8 -*-
import hashlib
import logging

import requests

from odoo import _, api, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class CrowleyOpenRouterClient(models.AbstractModel):
    _name = "crowley.ai.vid.gen.openrouter.client"
    _description = "OpenRouter Seedance 2.0 HTTP client"

    OPENROUTER_API_BASE_URL = "https://openrouter.ai/api/v1"
    SUBMIT_TIMEOUT = (10, 60)       # (connect, read)
    POLL_TIMEOUT = (10, 30)
    DOWNLOAD_TIMEOUT = (10, 600)    # videos can take a while to fully stream

    @api.model
    def _get_api_key(self):
        key = self.env['ir.config_parameter'].sudo().get_param('crowley_ai_vid_gen.openrouter_api_key')
        if not key:
            raise UserError(_("OpenRouter API key is not configured. Set it in Settings → Crowley AI Vid Gen."))
        return key

    @api.model
    def _get_headers(self, *, include_content_type=True):
        ICP = self.env['ir.config_parameter'].sudo()
        headers = {"Authorization": f"Bearer {self._get_api_key()}"}
        if include_content_type:
            headers["Content-Type"] = "application/json"
        if referer := ICP.get_param('crowley_ai_vid_gen.openrouter_http_referer'):
            headers["HTTP-Referer"] = referer
        if title := ICP.get_param('crowley_ai_vid_gen.openrouter_app_title'):
            headers["X-OpenRouter-Title"] = title
        return headers

    @api.model
    def submit_video_job(self, *, model, prompt, duration, resolution, aspect_ratio,
                         generate_audio=True, seed=None, callback_url=None):
        """Submit an async video-generation job to OpenRouter. Returns the JSON response.

        Expected response shape:
            {"id": "...", "polling_url": "...", "status": "pending"}
        """
        body = {
            "model": model,
            "prompt": prompt,
            "duration": int(duration),
            "resolution": resolution,
            "aspect_ratio": aspect_ratio,
            "generate_audio": bool(generate_audio),
        }
        if seed is not None:
            body["seed"] = int(seed)
        if callback_url:
            body["callback_url"] = callback_url

        url = f"{self.OPENROUTER_API_BASE_URL}/videos"
        _logger.info("Crowley: submitting Seedance job model=%s res=%s dur=%ss", model, resolution, duration)
        try:
            resp = requests.post(url, headers=self._get_headers(), json=body, timeout=self.SUBMIT_TIMEOUT)
        except requests.exceptions.RequestException as e:
            raise UserError(_("Cannot reach OpenRouter: %s") % e) from e

        self._raise_on_http_error(resp, action="submit")
        try:
            data = resp.json()
        except ValueError as e:
            raise UserError(_("OpenRouter returned non-JSON response: %s") % resp.text[:500]) from e
        if not data.get('id') or not data.get('polling_url'):
            raise UserError(_("OpenRouter response missing 'id' or 'polling_url': %s") % data)
        return data

    @api.model
    def poll_job(self, polling_url):
        """Poll an in-flight video job. Returns the JSON response.

        Expected shapes:
          In-flight:  {"status": "pending"|"in_progress", ...}
          Done:       {"status": "completed", "unsigned_urls": ["..."], "usage": {"cost": 1.7}, ...}
          Failed:     {"status": "failed"|"cancelled"|"expired", "error": {...}, ...}
        """
        if not polling_url:
            raise UserError(_("No polling URL — cannot poll job."))
        try:
            resp = requests.get(polling_url, headers=self._get_headers(include_content_type=False),
                                timeout=self.POLL_TIMEOUT)
        except requests.exceptions.RequestException as e:
            raise UserError(_("Cannot reach OpenRouter (poll): %s") % e) from e
        self._raise_on_http_error(resp, action="poll")
        try:
            return resp.json()
        except ValueError as e:
            raise UserError(_("OpenRouter poll returned non-JSON: %s") % resp.text[:500]) from e

    @api.model
    def stream_to_s3(self, content_url, *, bucket, key, mimetype="video/mp4"):
        """Stream OpenRouter video bytes into S3 with byte-perfect SHA-256 verification.

        The bytes are hashed as they are read by boto3's multipart uploader.
        No buffering of the full video; no re-encoding; no transcoding.

        :returns: dict(size, sha256, etag) — etag is what S3 returned.
        """
        s3 = self.env['crowley.ai.vid.gen.s3.storage']

        sha = hashlib.sha256()
        counter = {"size": 0}

        class _HashingStreamWrapper:
            """Wraps a stream so every .read() updates the SHA and byte counter."""
            def __init__(self, inner):
                self._inner = inner

            def read(self, amt=-1):
                chunk = self._inner.read(amt) if amt != -1 else self._inner.read()
                if chunk:
                    sha.update(chunk)
                    counter["size"] += len(chunk)
                return chunk

        _logger.info(
            "Crowley: streaming Seedance content to s3://%s/%s from %s",
            bucket, key, content_url,
        )
        expected_size = None
        api_key_present = bool(
            self.env["ir.config_parameter"].sudo().get_param(
                "crowley_ai_vid_gen.openrouter_api_key"
            )
        )
        if not api_key_present:
            raise UserError(_(
                "OpenRouter API key is not configured — cannot download the generated video. "
                "Set Settings → Crowley AI Vid Gen → OpenRouter API Key."
            ))
        try:
            with requests.get(content_url,
                              headers=self._get_headers(include_content_type=False),
                              stream=True, timeout=self.DOWNLOAD_TIMEOUT) as resp:
                # Log the response status + first chunk of body BEFORE raising so
                # operators always have a diagnostic trail in the Odoo log even
                # when this fails inside a deferred callback.
                if not resp.ok:
                    body_snippet = ""
                    try:
                        body_snippet = resp.text[:500]
                    except Exception:  # noqa: BLE001 — diagnostic best-effort
                        pass
                    _logger.warning(
                        "Crowley download failed: HTTP %s from %s — body: %s",
                        resp.status_code, content_url, body_snippet,
                    )
                self._raise_on_http_error(resp, action="download")
                # decode_content=True only undoes HTTP gzip — does NOT touch the video bitstream
                resp.raw.decode_content = True
                # Capture Content-Length only when the transport is identity-encoded;
                # if OpenRouter ever gzip-encoded the response, the header refers to
                # the compressed size which would not match our hashed (decoded) bytes.
                if not (resp.headers.get("Content-Encoding") or "").strip():
                    try:
                        expected_size = int(resp.headers.get("Content-Length") or 0)
                    except ValueError:
                        expected_size = None
                wrapped = _HashingStreamWrapper(resp.raw)
                etag = s3.upload_fileobj(wrapped, bucket=bucket, key=key, mimetype=mimetype)
        except requests.exceptions.RequestException as e:
            raise UserError(_("Cannot download video from OpenRouter: %s") % e) from e

        if counter["size"] <= 0:
            raise UserError(_("OpenRouter returned an empty video stream."))

        if expected_size and expected_size > 0 and expected_size != counter["size"]:
            raise UserError(
                _(
                    "Truncated download from OpenRouter: %s bytes received, "
                    "Content-Length advertised %s."
                ) % (counter["size"], expected_size)
            )

        return {"size": counter["size"], "sha256": sha.hexdigest(), "etag": etag}

    @api.model
    def _raise_on_http_error(self, resp, *, action):
        """Translate OpenRouter HTTP errors into user-friendly UserError instances."""
        if resp.ok:
            return
        code = resp.status_code
        snippet = (resp.text or '')[:500]
        if code == 401:
            raise UserError(_("OpenRouter: invalid API key. Check Settings → Crowley AI Vid Gen."))
        if code == 402:
            raise UserError(_("OpenRouter: insufficient credits. Top up your account."))
        if code == 403:
            raise UserError(_("OpenRouter: request forbidden (moderation or permissions). Detail: %s") % snippet)
        if code == 429:
            retry_after = resp.headers.get('Retry-After', '?')
            raise UserError(_("OpenRouter: rate-limited. Retry after %s seconds.") % retry_after)
        if code in (502, 503):
            retry_after = resp.headers.get('Retry-After', '?')
            raise UserError(_("OpenRouter upstream unavailable (HTTP %s). Retry after %s seconds.")
                            % (code, retry_after))
        if code == 408:
            raise UserError(_("OpenRouter request timed out (%s).") % action)
        raise UserError(_("OpenRouter %s failed (HTTP %s): %s") % (action, code, snippet))
