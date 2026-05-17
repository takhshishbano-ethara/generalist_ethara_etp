# -*- coding: utf-8 -*-
import base64
import csv
import io
import logging

from odoo import models, fields, api
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

VALID_LANGUAGES = {"python", "java", "go", "typescript", "rust"}


class ImportCsvWizard(models.TransientModel):
    _name = "kaiju.commit0.import.wizard"
    _description = "Import CSV/Excel for Bulk Builds"

    # ── Two-screen state ─────────────────────────────────────────────────────
    # 'upload' = initial file upload screen
    # 'result' = post-import summary with per-row status table
    state = fields.Selection(
        [("upload", "Upload"), ("result", "Result")],
        default="upload",
        readonly=True,
    )

    file = fields.Binary(string="CSV File", required=True)
    filename = fields.Char(string="Filename")

    # ── Result summary (populated after action_import) ───────────────────────
    result_line_ids = fields.One2many(
        "kaiju.commit0.import.wizard.line",
        "wizard_id",
        string="Import Results",
        readonly=True,
    )
    total_rows = fields.Integer(string="Total Rows", readonly=True)
    queued_count = fields.Integer(
        string="Queued", compute="_compute_counts", store=True
    )
    imported_count = fields.Integer(
        string="Imported", compute="_compute_counts", store=True
    )
    failed_count = fields.Integer(
        string="Failed", compute="_compute_counts", store=True
    )
    skipped_count = fields.Integer(
        string="Skipped", compute="_compute_counts", store=True
    )

    @api.depends("result_line_ids.status")
    def _compute_counts(self):
        for wiz in self:
            wiz.queued_count = sum(
                1 for l in wiz.result_line_ids if l.status == "queued"
            )
            wiz.imported_count = sum(
                1 for l in wiz.result_line_ids if l.status == "imported"
            )
            wiz.failed_count = sum(
                1 for l in wiz.result_line_ids if l.status == "failed"
            )
            wiz.skipped_count = sum(
                1 for l in wiz.result_line_ids if l.status == "skipped"
            )

    # ── Main action ──────────────────────────────────────────────────────────

    def action_import(self):
        """Parse the uploaded file, create + start builds, then switch to
        the result screen showing per-row outcome (imported / failed / skipped).
        """
        self.ensure_one()
        if not self.file:
            raise UserError("Please upload a file.")

        if not self.filename or not self.filename.lower().endswith(
            (".csv", ".xlsx", ".xls")
        ):
            raise UserError("Only CSV and Excel files are supported.")

        raw = base64.b64decode(self.file)

        # ── Phase 1: parse rows (preserves invalid rows as 'skipped' entries) ──
        if self.filename.lower().endswith(".csv"):
            parsed = self._parse_csv_with_validation(raw)
        else:
            parsed = self._parse_excel_with_validation(raw)

        if not parsed:
            raise UserError("No rows found in the file.")

        # ── Phase 2: create build records ONLY (no Argo submit here) ──
        # Argo submit happens asynchronously via _cron_submit_pending_builds.
        # This keeps the wizard sub-second even for 400-row uploads (avoids
        # HTTP timeout) and bounds the burst on Argo + DB.
        Build = self.env["kaiju.commit0"]
        line_vals = []
        for row in parsed:
            line = {
                "wizard_id": self.id,
                "repo_name": row.get("repo_name") or "(empty)",
                "language": row.get("language") or "(default)",
                "status": "skipped",
                "error_message": "",
                "build_id": False,
            }

            # Skip invalid rows up-front
            if row.get("_skip_reason"):
                line["error_message"] = row["_skip_reason"]
                line_vals.append((0, 0, line))
                continue

            # Dedup pre-check: hard-error rows that conflict with active builds
            # (only pending/running block — done/failed don't, so re-runs work).
            existing = Build.search(
                [
                    ("repo_name", "=", row["repo_name"]),
                    ("language", "=", row["language"]),
                    ("build_status", "in", ("pending", "running")),
                ],
                limit=1,
            )
            if existing:
                line["status"] = "failed"
                line["error_message"] = (
                    f"Duplicate — build {existing.name} already "
                    f"{existing.build_status} for this repo/language."
                )
                line["build_id"] = existing.id
                line_vals.append((0, 0, line))
                continue

            # Create the build record only — cron will submit it to Argo.
            try:
                with self.env.cr.savepoint():
                    build = Build.create(
                        {
                            "repo_name": row["repo_name"],
                            "language": row["language"],
                            "build_status": "pending",
                        }
                    )
                line["status"] = "queued"
                line["build_id"] = build.id
            except Exception as e:  # noqa: BLE001 — surface in result table
                line["status"] = "failed"
                line["error_message"] = str(e)[:255]
                _logger.warning(
                    "Bulk import: failed to create build for %s: %s",
                    row.get("repo_name"),
                    e,
                )

            line_vals.append((0, 0, line))

        # ── Phase 3: write result lines and switch to result screen ──
        total = len(parsed)
        self.write(
            {
                "state": "result",
                "total_rows": total,
                "result_line_ids": line_vals,
            }
        )

        # Stay on the wizard (target=new in act_window keeps the modal open)
        return {
            "type": "ir.actions.act_window",
            "res_model": "kaiju.commit0.import.wizard",
            "res_id": self.id,
            "view_mode": "form",
            "target": "new",
        }

    # ── View Imported Builds button ──────────────────────────────────────────

    def action_view_imported_builds(self):
        """Navigate to a filtered Builds list showing only successfully imported rows."""
        self.ensure_one()
        build_ids = [
            l.build_id.id
            for l in self.result_line_ids
            if l.build_id and l.status in ("queued", "imported")
        ]
        if not build_ids:
            raise UserError("No builds were successfully created.")

        return {
            "type": "ir.actions.act_window",
            "name": "Imported / Queued Builds",
            "res_model": "kaiju.commit0",
            "view_mode": "list,form",
            "domain": [("id", "in", build_ids)],
            "target": "current",
        }

    # ── Reset to upload screen ───────────────────────────────────────────────

    def action_import_another(self):
        """Clear current state and return to the upload screen for another file."""
        self.ensure_one()
        self.write(
            {
                "state": "upload",
                "file": False,
                "filename": False,
                "result_line_ids": [(5, 0, 0)],
                "total_rows": 0,
                "imported_count": 0,
                "failed_count": 0,
                "skipped_count": 0,
                "queued_count": 0,
            }
        )
        return {
            "type": "ir.actions.act_window",
            "res_model": "kaiju.commit0.import.wizard",
            "res_id": self.id,
            "view_mode": "form",
            "target": "new",
        }

    # ── Parsing helpers (preserve invalid rows so user sees what was skipped) ─

    def _parse_csv_with_validation(self, raw):
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError as e:
            raise UserError(f"CSV file must be UTF-8 encoded: {e}")

        reader = csv.DictReader(io.StringIO(text))
        return self._normalize_rows(reader)

    def _parse_excel_with_validation(self, raw):
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

        dict_rows = []
        for row_values in rows_iter:
            row_dict = {}
            for i, val in enumerate(row_values):
                if i < len(headers) and headers[i]:
                    row_dict[headers[i]] = str(val).strip() if val is not None else ""
            # Skip fully empty rows
            if any(v for v in row_dict.values()):
                dict_rows.append(row_dict)

        return self._normalize_rows(dict_rows)

    def _normalize_rows(self, rows_iterable):
        """Normalize and validate rows. Invalid rows are kept with `_skip_reason`
        so the result screen can show *why* a row was skipped.
        """
        normalized = []
        for i, row in enumerate(rows_iterable, 1):
            cleaned = {
                (k or "").strip().lower(): (str(v).strip() if v is not None else "")
                for k, v in row.items()
                if k
            }
            repo_name = cleaned.get("repo_name", "")
            language = cleaned.get("language", "python").lower() or "python"

            entry = {"repo_name": repo_name, "language": language}

            if not repo_name:
                entry["_skip_reason"] = "Missing repo_name"
            elif "/" not in repo_name:
                entry["_skip_reason"] = (
                    f"Invalid repo_name '{repo_name}' (expected 'owner/repo' format)"
                )
            elif language not in VALID_LANGUAGES:
                # Auto-fallback to python but record the original for transparency
                entry["language"] = "python"
                entry["_skip_reason"] = f"Unsupported language '{language}', skipped"

            normalized.append(entry)

        return normalized


class ImportCsvWizardLine(models.TransientModel):
    _name = "kaiju.commit0.import.wizard.line"
    _description = "Per-row import result for the Bulk Builds wizard"
    _order = "status desc, id"

    wizard_id = fields.Many2one(
        "kaiju.commit0.import.wizard",
        string="Wizard",
        required=True,
        ondelete="cascade",
    )
    repo_name = fields.Char(string="Repository", readonly=True)
    language = fields.Char(string="Language", readonly=True)
    status = fields.Selection(
        [
            ("queued", "Queued"),
            ("imported", "Imported"),
            ("failed", "Failed"),
            ("skipped", "Skipped"),
        ],
        string="Status",
        readonly=True,
    )
    error_message = fields.Char(string="Reason / Error", readonly=True)
    build_id = fields.Many2one(
        "kaiju.commit0",
        string="Build",
        readonly=True,
        ondelete="set null",
    )
