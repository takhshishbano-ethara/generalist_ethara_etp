# -*- coding: utf-8 -*-
import logging
import re
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Tried in order. Driver 17 (and 13) don't suffer from the OpenSSL 3
# "legacy sigalg disallowed" handshake failure that Driver 18 hits when
# the SQL Server presents a SHA-1 / legacy self-signed certificate.
_DRIVER_PREFERENCE = (
    "ODBC Driver 18 for SQL Server",
    "ODBC Driver 17 for SQL Server",
    "ODBC Driver 13 for SQL Server",
)

# Substrings that indicate a TLS/sigalg handshake failure worth retrying
# with an older driver. Anything else (auth, network, db missing) is fatal
# and must surface immediately.
_TLS_FALLBACK_MARKERS = (
    "legacy sigalg",
    "SSL Provider",
    "SSL routines",
    "ssl handshake",
    "TLS",
)

DEVICE_SOURCE_COLUMNS = (
    "DeviceId",
    "DeviceFName",
    "DeviceSName",
    "DeviceDirection",
    "SerialNumber",
    "ConnectionType",
    "IpAddress",
    "LastPing",
    "LastLogDownloadDate",
    "DeviceType",
    "DeviceLocation",
    "Timezone",
    "FaceDeviceType",
    "DeviceActivationCode",
)


class EsslServer(models.Model):
    _name = "essl.server"
    _description = "ESSL SQL Server (eTimeTrackLite database)"
    _order = "name"

    name = fields.Char(string="Name", required=True)
    active = fields.Boolean(default=True)

    db_host = fields.Char(string="Server Host", required=True)
    db_port = fields.Integer(string="Port", default=1433)
    db_name = fields.Char(string="Database Name", required=True)
    db_username = fields.Char(string="Username", required=True)
    db_password = fields.Char(string="Password")
    db_timeout = fields.Integer(string="Timeout (s)", default=10)

    devices_table = fields.Char(
        string="Devices Table", default="Devices", required=True,
        help="Source-of-truth master device table on the eSSL SQL Server.",
    )
    logs_table = fields.Char(
        string="Logs Table", default="DeviceLogs", required=True,
    )
    logs_user_id_column = fields.Char(
        string="Logs: User ID Column", default="UserId", required=True,
    )
    logs_timestamp_column = fields.Char(
        string="Logs: Timestamp Column", default="LogDate", required=True,
    )
    logs_direction_column = fields.Char(
        string="Logs: Direction Column", default="Direction",
        help="Optional. If blank, every punch is treated as a check-in.",
    )
    logs_device_column = fields.Char(
        string="Logs: Device FK Column", default="DeviceId", required=True,
        help="Foreign-key column in the logs table that joins back to Devices.",
    )

    last_device_sync_at = fields.Datetime(string="Last Device Sync At", readonly=True)
    last_device_sync_count = fields.Integer(string="Devices Touched (last run)", readonly=True)
    last_device_sync_error = fields.Text(string="Last Device Sync Error", readonly=True)

    device_ids = fields.One2many("essl.device", "server_id", string="Devices")
    device_count = fields.Integer(string="Device Count", compute="_compute_device_count")

    @api.depends("device_ids")
    def _compute_device_count(self):
        data = self.env["essl.device"].read_group(
            [("server_id", "in", self.ids)], ["server_id"], ["server_id"]
        )
        count_map = {row["server_id"][0]: row["server_id_count"] for row in data}
        for srv in self:
            srv.device_count = count_map.get(srv.id, 0)

    def _validate_identifier(self, name, label):
        if not name or not _IDENT_RE.match(name):
            raise UserError(_("Invalid identifier for %s: %r") % (label, name))

    def _connect(self):
        self.ensure_one()
        try:
            import pyodbc  # noqa: PLC0415
        except ImportError:
            raise UserError(_(
                "pyodbc is not installed on the Odoo server. "
                "Install it with: pip install pyodbc (also requires ODBC Driver 18 for SQL Server)"
            ))

        installed = {d.strip() for d in pyodbc.drivers()}
        candidates = [d for d in _DRIVER_PREFERENCE if d in installed]
        if not candidates:
            raise UserError(_(
                "No supported SQL Server ODBC driver found on the Odoo host. "
                "Installed drivers: %s. Install msodbcsql18 (or 17)."
            ) % (", ".join(sorted(installed)) or _("none")))

        timeout = self.db_timeout or 10
        last_exc = None
        for driver in candidates:
            conn_str = (
                "DRIVER={%s};"
                "SERVER=%s,%d;"
                "DATABASE=%s;"
                "UID=%s;"
                "PWD=%s;"
                "Encrypt=no;"
                "TrustServerCertificate=yes;"
                "Connection Timeout=%d;"
            ) % (
                driver,
                self.db_host,
                self.db_port or 1433,
                self.db_name,
                self.db_username,
                self.db_password or "",
                timeout,
            )
            try:
                conn = pyodbc.connect(conn_str, timeout=timeout)
            except Exception as exc:
                last_exc = exc
                msg = str(exc)
                if any(marker in msg for marker in _TLS_FALLBACK_MARKERS):
                    _logger.warning(
                        "ESSL %s: driver %r failed TLS handshake (%s); trying next driver",
                        self.name, driver, msg.splitlines()[0][:200],
                    )
                    continue
                raise UserError(_("Connection failed: %s") % exc)
            if driver != candidates[0]:
                _logger.info(
                    "ESSL %s: connected via fallback driver %r", self.name, driver,
                )
            conn.timeout = timeout
            return conn

        raise UserError(_(
            "Connection failed for all installed drivers (%s). Last error: %s"
        ) % (", ".join(candidates), last_exc))

    def action_test_connection(self):
        self.ensure_one()
        self._validate_identifier(self.devices_table, "Devices Table")
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM [%s]" % self.devices_table)
            row = cur.fetchone()
            cnt = int(row[0]) if row else 0
            cur.close()
        except Exception as exc:
            raise UserError(_("Query failed: %s") % exc)
        finally:
            try:
                conn.close()
            except Exception:
                pass
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("ESSL Server"),
                "message": _("Connection OK. %d device(s) in the source table.") % cnt,
                "sticky": False,
                "type": "success",
            },
        }

    def action_pull_devices_now(self):
        self.ensure_one()
        res = self._pull_devices()
        msg = _(
            "Devices synced. Created: %(created)d, Updated: %(updated)d, "
            "Unchanged: %(unchanged)d, Failed: %(failed)d."
        ) % res
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("ESSL Device Sync"),
                "message": msg,
                "sticky": False,
                "type": "success",
            },
        }

    def _pull_devices(self):
        self.ensure_one()
        self._validate_identifier(self.devices_table, "Devices Table")
        try:
            conn = self._connect()
        except UserError as exc:
            self.sudo().write({"last_device_sync_error": str(exc)[:1000]})
            raise
        try:
            cur = conn.cursor()
            sql = "SELECT %s FROM [%s]" % (
                ", ".join("[%s]" % c for c in DEVICE_SOURCE_COLUMNS),
                self.devices_table,
            )
            cur.execute(sql)
            cols = [c[0] for c in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
            cur.close()
        except Exception as exc:
            self.sudo().write({"last_device_sync_error": str(exc)[:1000]})
            raise UserError(_("Devices query failed: %s") % exc)
        finally:
            try:
                conn.close()
            except Exception:
                pass

        Device = self.env["essl.device"].sudo()
        existing = {
            d.external_device_id: d
            for d in Device.search([("server_id", "=", self.id)])
        }
        created = updated = unchanged = failed = 0
        stale_cutoff = fields.Datetime.now() - timedelta(days=365)

        for row in rows:
            ext_raw = row.get("DeviceId")
            if ext_raw is None:
                continue
            try:
                ext_id = int(ext_raw)
            except (TypeError, ValueError):
                continue

            vals = self._map_source_row(row, ext_id)

            try:
                rec = existing.get(ext_id)
                if rec:
                    changes = {k: v for k, v in vals.items() if rec[k] != v}
                    if changes:
                        rec.write(changes)
                        updated += 1
                    else:
                        unchanged += 1
                else:
                    vals["sync_enabled"] = self._default_sync_enabled(vals, stale_cutoff)
                    Device.create(vals)
                    created += 1
            except Exception as exc:
                failed += 1
                _logger.warning(
                    "ESSL device upsert failed for DeviceId %s on server %s: %s",
                    ext_id, self.name, exc,
                )

        self.sudo().write({
            "last_device_sync_at": fields.Datetime.now(),
            "last_device_sync_count": created + updated,
            "last_device_sync_error": False,
        })
        _logger.info(
            "ESSL %s: pulled devices — created=%d updated=%d unchanged=%d failed=%d",
            self.name, created, updated, unchanged, failed,
        )
        return {
            "created": created,
            "updated": updated,
            "unchanged": unchanged,
            "failed": failed,
        }

    def _map_source_row(self, row, ext_id):
        def _s(key):
            v = row.get(key)
            if v is None:
                return False
            v = str(v).strip()
            return v or False

        def _ts(key):
            v = row.get(key)
            if not v:
                return False
            if hasattr(v, "year") and v.year < 1990:
                return False
            return v

        return {
            "server_id": self.id,
            "external_device_id": ext_id,
            "name": _s("DeviceFName") or ("Device %s" % ext_id),
            "short_name": _s("DeviceSName"),
            "direction_mode": _s("DeviceDirection"),
            "serial_number": _s("SerialNumber"),
            "connection_type": _s("ConnectionType"),
            "ip_address": _s("IpAddress"),
            "last_ping_at": _ts("LastPing"),
            "last_log_download_at": _ts("LastLogDownloadDate"),
            "device_type": _s("DeviceType"),
            "device_location": _s("DeviceLocation"),
            "timezone_minutes": int(row.get("Timezone") or 0),
            "face_device_type": _s("FaceDeviceType"),
            "activation_code": _s("DeviceActivationCode"),
        }

    @staticmethod
    def _default_sync_enabled(vals, stale_cutoff):
        if (vals.get("device_type") or "").lower() == "canteen":
            return False
        if not vals.get("serial_number"):
            return False
        last_ping = vals.get("last_ping_at")
        if last_ping and stale_cutoff and last_ping < stale_cutoff:
            return False
        return True

    @api.model
    def _cron_pull_devices_all_servers(self):
        for srv in self.search([("active", "=", True)]):
            try:
                srv._pull_devices()
            except Exception as exc:
                srv.sudo().write({"last_device_sync_error": str(exc)[:1000]})
                _logger.exception("ESSL: pull-devices cron failed for %s", srv.name)
