# -*- coding: utf-8 -*-
import base64
import csv
import io
import logging

from odoo import models, fields
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

VALID_LANGUAGES = {"python", "java", "go", "typescript", "rust"}


class ImportCsvWizard(models.TransientModel):
    _name = "kaiju.commit0.import.wizard"
    _description = "Import CSV/Excel for Bulk Builds"

    file = fields.Binary(string="CSV File", required=True)
    filename = fields.Char(string="Filename")

    def action_import(self):
        self.ensure_one()
        if not self.file:
            raise UserError("Please upload a file.")

        if not self.filename or not self.filename.lower().endswith(
            (".csv", ".xlsx", ".xls")
        ):
            raise UserError("Only CSV and Excel files are supported.")

        raw = base64.b64decode(self.file)

        if self.filename.lower().endswith(".csv"):
            rows = self._parse_csv(raw)
        else:
            rows = self._parse_excel(raw)

        if not rows:
            raise UserError("No valid rows found in the file.")

        created = self._create_records(rows)

        # Auto-validate and auto-trigger build for each record
        failed = []
        for rec in created:
            try:
                rec.action_validate_config()
                rec.action_run_build()
            except (UserError, RuntimeError) as e:
                failed.append(f"{rec.repo_name}: {e}")
                _logger.warning(
                    "Bulk import: failed to start build for %s: %s",
                    rec.repo_name,
                    e,
                )

        title = (
            f"Imported {len(created)} Builds — {len(created) - len(failed)} building"
        )
        if failed:
            title += f", {len(failed)} failed"
            _logger.warning("Bulk import failures:\n%s", "\n".join(failed))

        return {
            "type": "ir.actions.act_window",
            "name": title,
            "res_model": "kaiju.commit0",
            "view_mode": "list,form",
            "domain": [("id", "in", created.ids)],
            "target": "current",
        }

    def _parse_csv(self, raw):
        text = raw.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(text))
        rows = []
        for row in reader:
            cleaned = {k.strip().lower(): v.strip() for k, v in row.items() if k}
            if cleaned.get("repo_name"):
                rows.append(cleaned)
        return rows

    def _parse_excel(self, raw):
        try:
            import openpyxl
        except ImportError:
            raise UserError("openpyxl is required for Excel import. Use CSV instead.")

        wb = openpyxl.load_workbook(io.BytesIO(raw), read_only=True)
        ws = wb.active
        rows_iter = ws.iter_rows(values_only=True)

        headers = [str(h).strip().lower() if h else "" for h in next(rows_iter, [])]
        if "repo_name" not in headers:
            raise UserError("Excel file must have a 'repo_name' column header.")

        rows = []
        for row_values in rows_iter:
            row_dict = {}
            for i, val in enumerate(row_values):
                if i < len(headers) and headers[i]:
                    row_dict[headers[i]] = str(val).strip() if val else ""
            if row_dict.get("repo_name"):
                rows.append(row_dict)

        return rows

    def _create_records(self, rows):
        vals_list = []
        for i, row in enumerate(rows, 1):
            repo_name = row.get("repo_name", "")
            if not repo_name or "/" not in repo_name:
                _logger.warning(
                    "Row %d: invalid repo_name '%s', skipping", i, repo_name
                )
                continue

            language = row.get("language", "python").lower()
            if language not in VALID_LANGUAGES:
                language = "python"

            branch_name = row.get("branch_name", "").strip() or "commit0_combined"

            vals_list.append(
                {
                    "repo_name": repo_name,
                    "language": language,
                    "branch_name": branch_name,
                }
            )

        if not vals_list:
            raise UserError(
                "No valid rows to import. Ensure repo_name column has 'owner/repo' format."
            )

        return self.env["kaiju.commit0"].create(vals_list)
