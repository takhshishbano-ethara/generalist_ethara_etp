import json
import logging
import os

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)

_MODULE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DATA_FILE = os.path.join(_MODULE_DIR, "data", "Clinical_Data", "patients.json")
_DZI_DIR = os.path.join(_MODULE_DIR, "static", "src", "wsi", "dzi")


def _json_response(payload, status=200):
    return http.Response(
        json.dumps(payload, default=str),
        content_type="application/json",
        status=status,
    )


def _load_patients():
    """Return parsed patients.json, or a placeholder structure if missing."""
    if not os.path.exists(_DATA_FILE):
        return {
            "schema_version": 1,
            "generated_at": None,
            "patients": [],
            "_warning": "patients.json not found. Drop Excel files into "
                        "data/Clinical_Data/Patient_*/structured/ and run "
                        "tools/ingest_excel.py to generate it.",
        }
    with open(_DATA_FILE, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _has_dzi():
    if not os.path.isdir(_DZI_DIR):
        return False
    for _root, _dirs, files in os.walk(_DZI_DIR):
        for fname in files:
            if fname.endswith(".dzi"):
                return True
    return False


class LokiDashboard2Controller(http.Controller):
    @http.route("/loki2", type="http", auth="public", website=True, sitemap=True)
    def dashboard_page(self, **kw):
        ICP = request.env["ir.config_parameter"].sudo()
        title = ICP.get_param("loki_dashboard_2.title", "") or "Clinical Dashboard"
        values = {
            "dashboard_title": title,
            "has_data": os.path.exists(_DATA_FILE),
        }
        return request.render("loki_dashboard_2.portal_dashboard", values)

    @http.route("/loki2/api/patients", type="http", auth="public", cors="*")
    def api_patients(self, **kw):
        try:
            payload = _load_patients()
            return _json_response(payload, status=200)
        except json.JSONDecodeError as e:
            _logger.exception("loki_dashboard_2: malformed patients.json")
            return _json_response(
                {"error": "invalid_json", "detail": str(e)}, status=500,
            )
        except Exception as e:  # pragma: no cover - defensive
            _logger.exception("loki_dashboard_2: failed to load patients.json")
            return _json_response(
                {"error": "load_failed", "detail": str(e)}, status=500,
            )

    @http.route("/loki2/api/patient/<string:pid>", type="http", auth="public", cors="*")
    def api_patient(self, pid, **kw):
        try:
            payload = _load_patients()
        except Exception as e:
            _logger.exception("loki_dashboard_2: failed to load patients.json")
            return _json_response(
                {"error": "load_failed", "detail": str(e)}, status=500,
            )
        for p in payload.get("patients", []):
            if str(p.get("id")) == pid or str(p.get("code")) == pid:
                return _json_response(p, status=200)
        return _json_response({"error": "not_found", "pid": pid}, status=404)

    @http.route("/loki2/api/health", type="http", auth="public", cors="*")
    def api_health(self, **kw):
        loaded = 0
        try:
            payload = _load_patients()
            loaded = len(payload.get("patients", []))
        except Exception:
            loaded = -1
        return _json_response({
            "ok": loaded >= 0,
            "patients_loaded": loaded,
            "data_dir": os.path.dirname(_DATA_FILE),
            "has_dzi": _has_dzi(),
        }, status=200)
