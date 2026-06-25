import hmac
import json
import logging
import time

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)

_API_TOKEN_PARAM = "essl_pull_api.token"


class EsslPullApiController(http.Controller):

    @http.route(
        "/essl/api/pull",
        type="http",
        auth="public",
        methods=["GET"],
        csrf=False,
    )
    def pull_all_devices(self, token=None, **kwargs):
        if not self._is_token_valid(token):
            return self._json_response({"status": "error", "message": "Invalid or missing token"}, 401)

        started_at = time.monotonic()
        Device = request.env["essl.device"].sudo()
        Log = request.env["essl.attendance.log"].sudo()
        devices = Device.search([
            ("active", "=", True),
            ("sync_enabled", "=", True),
            ("server_id.active", "=", True),
        ])

        empty_device_set = Device.browse()
        devices_by_server = {}
        for device in devices:
            devices_by_server.setdefault(device.server_id, empty_device_set)
            devices_by_server[device.server_id] |= device

        per_device = []
        totals = {"created": 0, "processed": 0, "failed": 0}
        employee_map = Log.build_employee_map()

        for server, server_devices in devices_by_server.items():
            conn = None
            try:
                conn = server.sudo()._connect()
            except Exception as exc:
                _logger.exception("ESSL pull API: server %s connect failed", server.name)
                for device in server_devices:
                    per_device.append({
                        "device_id": device.id,
                        "device_name": device.display_name,
                        "status": "error",
                        "message": "server connect failed: %s" % str(exc)[:300],
                    })
                continue

            try:
                for device in server_devices:
                    entry = {"device_id": device.id, "device_name": device.display_name}
                    try:
                        result = device._pull_attendance(conn=conn, employee_map=employee_map)
                        request.env.cr.commit()
                        entry.update({"status": "ok", **result})
                        for key in totals:
                            totals[key] += int(result.get(key, 0))
                    except Exception as exc:
                        request.env.cr.rollback()
                        _logger.exception("ESSL pull API: device %s failed", device.display_name)
                        entry.update({"status": "error", "message": str(exc)[:500]})
                    per_device.append(entry)
            finally:
                try:
                    conn.close()
                except Exception:
                    pass

        return self._json_response({
            "status": "ok",
            "devices_attempted": len(devices),
            "servers_used": len(devices_by_server),
            "totals": totals,
            "devices": per_device,
            "duration_ms": int((time.monotonic() - started_at) * 1000),
        })

    @http.route(
        "/essl/api/pull-devices",
        type="http",
        auth="public",
        methods=["GET"],
        csrf=False,
    )
    def pull_devices(self, token=None, server_id=None, **kwargs):
        if not self._is_token_valid(token):
            return self._json_response({"status": "error", "message": "Invalid or missing token"}, 401)

        started_at = time.monotonic()
        Server = request.env["essl.server"].sudo()
        if server_id:
            try:
                sid = int(server_id)
            except (TypeError, ValueError):
                return self._json_response(
                    {"status": "error", "message": "Invalid server_id"}, 400
                )
            servers = Server.search([("active", "=", True), ("id", "=", sid)])
        else:
            servers = Server.search([("active", "=", True)])

        per_server = []
        totals = {"created": 0, "updated": 0, "unchanged": 0, "failed": 0}
        for srv in servers:
            entry = {"server_id": srv.id, "server_name": srv.name}
            try:
                result = srv._pull_devices()
                request.env.cr.commit()
                entry.update({"status": "ok", **result})
                for key in totals:
                    totals[key] += int(result.get(key, 0))
            except Exception as exc:
                request.env.cr.rollback()
                _logger.exception("ESSL pull-devices API: server %s failed", srv.name)
                entry.update({"status": "error", "message": str(exc)[:500]})
            per_server.append(entry)

        return self._json_response({
            "status": "ok",
            "servers_attempted": len(servers),
            "totals": totals,
            "servers": per_server,
            "duration_ms": int((time.monotonic() - started_at) * 1000),
        })

    def _is_token_valid(self, supplied_token):
        if not supplied_token:
            auth_header = request.httprequest.headers.get("Authorization", "")
            if auth_header.startswith("Bearer "):
                supplied_token = auth_header[7:]
        if not supplied_token:
            return False
        configured = request.env["ir.config_parameter"].sudo().get_param(_API_TOKEN_PARAM, "")
        if not configured:
            return False
        return hmac.compare_digest(str(supplied_token), str(configured))

    def _json_response(self, payload, status=200):
        return request.make_response(
            json.dumps(payload, default=str),
            status=status,
            headers=[("Content-Type", "application/json")],
        )
