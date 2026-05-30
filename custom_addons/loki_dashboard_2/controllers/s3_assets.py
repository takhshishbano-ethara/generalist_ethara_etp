"""S3-backed asset routes for /loki2.

Serves WSI tiles and clinical PDFs via 302-redirect to presigned S3 URLs.
The bucket layout is:
    <prefix>/wsi/patients/<pid>/<basename>.dzi
    <prefix>/wsi/patients/<pid>/<basename>_files/<level>/<col>_<row>.jpeg
    <prefix>/docs/patients/<pid>/<category>/<filename>
"""
import logging
import re
import threading

import werkzeug

from odoo import http
from odoo.http import request

from .main import _load_patients

_logger = logging.getLogger(__name__)

_TILE_RE = re.compile(r"^(?P<base>[^/]+)_files/\d+/\d+_\d+\.jpe?g$", re.IGNORECASE)
_MANIFEST_RE = re.compile(r"^(?P<base>[^/]+)\.dzi$", re.IGNORECASE)

_client_lock = threading.Lock()
_client_cache = {}


def _s3_settings():
    ICP = request.env["ir.config_parameter"].sudo()
    return {
        "bucket": ICP.get_param("loki_dashboard_2.s3_bucket") or "",
        "region": ICP.get_param("loki_dashboard_2.s3_region") or "us-east-1",
        "prefix": (ICP.get_param("loki_dashboard_2.s3_prefix") or "loki_dashboard").strip("/"),
        "endpoint": ICP.get_param("loki_dashboard_2.s3_endpoint") or None,
        "ttl_wsi": int(ICP.get_param("loki_dashboard_2.s3_ttl_wsi") or 3600),
        "ttl_doc": int(ICP.get_param("loki_dashboard_2.s3_ttl_doc") or 300),
    }


def _get_s3_client(region, endpoint):
    key = (region, endpoint or "")
    with _client_lock:
        client = _client_cache.get(key)
        if client is not None:
            return client
        import boto3
        from botocore.config import Config
        cfg = Config(
            region_name=region,
            signature_version="s3v4",
            s3={"addressing_style": "virtual"},
            retries={"max_attempts": 3, "mode": "standard"},
        )
        client = boto3.client("s3", endpoint_url=endpoint, config=cfg)
        _client_cache[key] = client
        return client


def _find_patient(pid):
    """Return the patient dict matching `pid` against id or code, or None."""
    try:
        payload = _load_patients()
    except Exception:
        _logger.exception("loki2 assets: cannot load patients.json")
        return None
    pid_s = str(pid)
    for p in payload.get("patients", []):
        if str(p.get("id")) == pid_s or str(p.get("code")) == pid_s:
            return p
    return None


def _slide_basenames(patient):
    """Return the set of allowed slide basenames for a patient.

    Reads from `wsi_slides[].dzi_path` and uses the filename stem.
    """
    out = set()
    for slide in patient.get("wsi_slides") or []:
        path = slide.get("dzi_path") or ""
        if not path:
            continue
        base = path.rsplit("/", 1)[-1]
        if base.lower().endswith(".dzi"):
            base = base[:-4]
        if base:
            out.add(base)
    return out


_NEW_DOC_PREFIX = "loki2/asset/doc/"
_OLD_DOC_PREFIX = "loki_dashboard_2/static/docs/"


def _doc_keys(patient):
    """Return the set of allowed `<category>/[<subcategory>/]<filename>` tails for a patient.

    Supports arbitrary depth under the patient folder (e.g. imaging/usg/x.pdf).
    Accepts both new (/loki2/asset/doc/<pid>/...) and legacy URL forms.
    """
    out = set()
    for doc in patient.get("documents") or []:
        url = (doc.get("url") or "").lstrip("/")
        tail = None
        for prefix in (_NEW_DOC_PREFIX, _OLD_DOC_PREFIX):
            if url.startswith(prefix):
                rest = url[len(prefix):]  # "<pid>/<category>/.../<filename>"
                slash = rest.find("/")
                if slash >= 0:
                    tail = rest[slash + 1:]
                break
        if tail:
            out.add(tail)
    return out


class LokiDashboard2Assets(http.Controller):
    """Public asset routes that 302 to presigned S3 URLs.

    Validation is intentionally tight: only keys that match the patient's
    own slides / documents are allowed. This stops the route from being a
    generic presigner for arbitrary objects under the prefix.
    """

    @http.route(
        "/loki2/asset/wsi/<string:pid>/<path:key>",
        type="http",
        auth="public",
        csrf=False,
    )
    def asset_wsi(self, pid, key, **kw):
        patient = _find_patient(pid)
        if patient is None:
            return request.not_found()

        m = _MANIFEST_RE.match(key) or _TILE_RE.match(key)
        if not m or m.group("base") not in _slide_basenames(patient):
            return request.not_found()

        s = _s3_settings()
        if not s["bucket"]:
            _logger.error("loki2 assets: S3 bucket not configured")
            return request.not_found()

        s3_key = f"{s['prefix']}/wsi/patients/{pid}/{key}"
        try:
            url = _get_s3_client(s["region"], s["endpoint"]).generate_presigned_url(
                "get_object",
                Params={"Bucket": s["bucket"], "Key": s3_key},
                ExpiresIn=s["ttl_wsi"],
            )
        except Exception:
            _logger.exception("loki2 assets: presign failed for %s", s3_key)
            return request.not_found()

        return werkzeug.utils.redirect(url, code=302)

    @http.route(
        "/loki2/asset/doc/<string:pid>/<path:key>",
        type="http",
        auth="public",
        csrf=False,
    )
    def asset_doc(self, pid, key, **kw):
        patient = _find_patient(pid)
        if patient is None:
            return request.not_found()

        if key not in _doc_keys(patient):
            return request.not_found()

        s = _s3_settings()
        if not s["bucket"]:
            _logger.error("loki2 assets: S3 bucket not configured")
            return request.not_found()

        s3_key = f"{s['prefix']}/docs/patients/{pid}/{key}"
        try:
            url = _get_s3_client(s["region"], s["endpoint"]).generate_presigned_url(
                "get_object",
                Params={"Bucket": s["bucket"], "Key": s3_key},
                ExpiresIn=s["ttl_doc"],
            )
        except Exception:
            _logger.exception("loki2 assets: presign failed for %s", s3_key)
            return request.not_found()

        return werkzeug.utils.redirect(url, code=302)
