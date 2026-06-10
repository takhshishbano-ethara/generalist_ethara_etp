from __future__ import annotations

import base64
import csv
import io
import logging

from odoo import _, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


REQUIRED_HEADERS = (
    "project_type",
    "instruction",
    "original_image_url",
    "edited_image_url",
)
OPTIONAL_HEADERS = (
    "edit_only_instructed",
    "images_aligned",
    "free_of_ai_slop",
    "assigned_user_login",
)


class I2IImportWizard(models.TransientModel):
    _name = "i2i.import.wizard"
    _description = "I2I Import Wizard (CSV / XLSX)"

    file = fields.Binary(string="Upload File (CSV or XLSX)", required=True)
    filename = fields.Char(string="Filename")
    skip_header = fields.Boolean(string="File has header row", default=True)
    created_count = fields.Integer(readonly=True)
    error_log = fields.Text(readonly=True)

    def action_import(self):
        self.ensure_one()
        if not self.file:
            raise UserError(_("Please upload a CSV or XLSX file."))
        filename = (self.filename or "").lower()
        raw = base64.b64decode(self.file)
        if filename.endswith(".xlsx"):
            rows = self._read_xlsx(raw)
        else:
            rows = self._read_csv(raw)

        if not rows:
            raise UserError(_("File contains no data rows."))

        header = [str(c or "").strip().lower() for c in rows[0]]
        data_rows = rows[1:] if self.skip_header else rows
        missing = [h for h in REQUIRED_HEADERS if h not in header]
        if missing and self.skip_header:
            raise UserError(_(
                "Missing required columns: %s. Required: %s"
            ) % (", ".join(missing), ", ".join(REQUIRED_HEADERS)))

        Item = self.env["i2i.item"]
        Users = self.env["res.users"]
        created = 0
        errors = []
        for idx, row in enumerate(data_rows, start=2 if self.skip_header else 1):
            try:
                values = self._row_to_vals(header, row, Users)
                Item.create(values)
                created += 1
            except Exception as exc:  # noqa: BLE001
                errors.append(f"Row {idx}: {exc}")
                _logger.warning("I2I import row %d failed: %s", idx, exc)

        self.write({
            "created_count": created,
            "error_log": "\n".join(errors[:50]) or False,
        })
        if errors:
            return {
                "type": "ir.actions.act_window",
                "name": _("Import I2I Items - Results"),
                "res_model": self._name,
                "res_id": self.id,
                "view_mode": "form",
                "target": "new",
            }
        message = _("%d items imported successfully.") % created
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Import Successful"),
                "message": message,
                "type": "success",
                "sticky": False,
                "next": {"type": "ir.actions.act_window_close"},
            },
        }

    def _row_to_vals(self, header, row, Users):
        get = lambda key: (str(row[header.index(key)]).strip() if key in header and header.index(key) < len(row) else "")
        vals = {}
        for key in REQUIRED_HEADERS + OPTIONAL_HEADERS:
            val = get(key) if self.skip_header else ""
            if key == "assigned_user_login":
                if val:
                    user = Users.search([("login", "=", val)], limit=1)
                    if user:
                        vals["assigned_user_id"] = user.id
                continue
            if val:
                vals[key] = val
        if not self.skip_header:
            if len(row) >= 4:
                vals["project_type"] = (str(row[0]).strip() or "i2i")
                vals["instruction"] = str(row[1]).strip()
                vals["original_image_url"] = str(row[2]).strip()
                vals["edited_image_url"] = str(row[3]).strip()
        for required in ("instruction", "original_image_url", "edited_image_url"):
            if not vals.get(required):
                raise UserError(_("Missing required field: %s") % required)
        return vals

    def _read_csv(self, raw):
        text = raw.decode("utf-8-sig", errors="replace")
        reader = csv.reader(io.StringIO(text))
        return [list(r) for r in reader]

    def _read_xlsx(self, raw):
        try:
            from openpyxl import load_workbook
        except ImportError as exc:
            raise UserError(_(
                "openpyxl is required for XLSX import. Install: pip install openpyxl"
            )) from exc
        wb = load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
        ws = wb.active
        rows = []
        for row in ws.iter_rows(values_only=True):
            rows.append(list(row))
        wb.close()
        return rows
