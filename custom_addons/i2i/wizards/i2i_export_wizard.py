from __future__ import annotations

import base64
import csv
import io
from datetime import datetime

from odoo import _, fields, models
from odoo.exceptions import UserError


EXPORT_COLUMNS = [
    ("name", "Reference"),
    ("project_id.name", "Project"),
    ("project_type", "Project Type"),
    ("instruction", "Instruction"),
    ("original_image_url", "Original Image URL"),
    ("edited_image_url", "Edited Image URL"),
    ("edit_only_instructed", "Edit Only Instructed (Human)"),
    ("images_aligned", "Images Aligned (Human)"),
    ("free_of_ai_slop", "Free of AI Slop (Human)"),
    ("llm_edit_only_instructed", "Edit Only Instructed (LLM)"),
    ("llm_images_aligned", "Images Aligned (LLM)"),
    ("llm_free_of_ai_slop", "Free of AI Slop (LLM)"),
    ("llm_reasoning", "LLM Reasoning"),
    ("llm_status", "LLM Status"),
    ("llm_tokens_used", "LLM Tokens"),
    ("llm_cost_usd", "LLM Cost USD"),
    ("state", "QC State"),
    ("qc_verdict", "QC Verdict"),
    ("qc_remark", "QC Remark"),
    ("qc_reviewer_id.login", "QC Reviewer"),
    ("qc_date", "QC Date"),
    ("user_id.login", "Created By"),
    ("create_date", "Created At"),
]

_MAX_EXPORT_ROWS = 50000


class I2IExportWizard(models.TransientModel):
    _name = "i2i.export.wizard"
    _description = "I2I Export Wizard (XLSX / CSV)"

    project_id = fields.Many2one("i2i.project", string="Project (optional)")
    state_filter = fields.Selection(
        [
            ("all", "All"),
            ("draft", "Draft"),
            ("human_qc", "Pending Human QC"),
            ("approved", "Approved"),
            ("rejected", "Rejected"),
        ],
        string="QC State",
        default="all",
        required=True,
    )
    date_from = fields.Date(string="From")
    date_to = fields.Date(string="To")
    file_format = fields.Selection(
        [("xlsx", "XLSX"), ("csv", "CSV")],
        default="xlsx",
        required=True,
    )

    row_count = fields.Integer(readonly=True)
    exported_file = fields.Binary(readonly=True, attachment=False)
    exported_filename = fields.Char(readonly=True)

    def _build_domain(self):
        domain = []
        if self.project_id:
            domain.append(("project_id", "=", self.project_id.id))
        if self.state_filter and self.state_filter != "all":
            domain.append(("state", "=", self.state_filter))
        if self.date_from:
            domain.append(("create_date", ">=", str(self.date_from)))
        if self.date_to:
            domain.append(("create_date", "<=", str(self.date_to) + " 23:59:59"))
        return domain

    def action_export(self):
        self.ensure_one()
        Item = self.env["i2i.item"]
        items = Item.search(self._build_domain(), limit=_MAX_EXPORT_ROWS)
        if not items:
            raise UserError(_("No items match the selected filters."))

        if self.file_format == "csv":
            payload, filename = self._build_csv(items)
        else:
            payload, filename = self._build_xlsx(items)

        self.write({
            "exported_file": base64.b64encode(payload),
            "exported_filename": filename,
            "row_count": len(items),
        })
        return {
            "type": "ir.actions.act_window",
            "res_model": self._name,
            "res_id": self.id,
            "view_mode": "form",
            "target": "new",
        }

    def _cell(self, record, path):
        value = record
        for part in path.split("."):
            if value is False or value is None:
                return ""
            value = value[part] if not callable(getattr(value, part, None)) else getattr(value, part)
        if value is False or value is None:
            return ""
        if isinstance(value, datetime):
            return value.strftime("%Y-%m-%d %H:%M:%S")
        return value

    def _build_csv(self, items):
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow([h for _, h in EXPORT_COLUMNS])
        for rec in items:
            writer.writerow([self._cell(rec, p) for p, _ in EXPORT_COLUMNS])
        today = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
        return buf.getvalue().encode("utf-8-sig"), f"i2i-export-{today}.csv"

    def _build_xlsx(self, items):
        try:
            from openpyxl import Workbook
        except ImportError as exc:
            raise UserError(_(
                "openpyxl is required for XLSX export. Install: pip install openpyxl"
            )) from exc
        wb = Workbook(write_only=True)
        ws = wb.create_sheet(title="I2I Items")
        ws.append([h for _, h in EXPORT_COLUMNS])
        for rec in items:
            ws.append([self._cell(rec, p) for p, _ in EXPORT_COLUMNS])
        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)
        today = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
        return buf.getvalue(), f"i2i-export-{today}.xlsx"
