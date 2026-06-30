"""Bulk-import fenrir.task records from an Excel (.xlsx) or CSV spreadsheet.

Each data row becomes one task. Columns are matched by header name
(case/space/punctuation-insensitive), so the column order is irrelevant.

Recognised columns
    Task ID          -> code            (required, must be unique)
    Category         -> category_id     (matched by name; optionally created)
    Title            -> title
    Overview         -> overview
    Scope of Work    -> scope_of_work
    Company Details  -> company_details
    PRD              -> prd_link         (link, stored for reference)
    Assets           -> assets_link      (link, stored for reference)
    Instruction.md   -> instruction_md_link
    Rubrics          -> rubrics_link
"""

import base64
import csv
import io
import logging
import re

from odoo import _, api, fields, models
from odoo.exceptions import UserError


_logger = logging.getLogger(__name__)


def _norm_header(value):
    """Normalise a header cell to a comparison key: lowercase, alnum only."""
    return re.sub(r"[^a-z0-9]", "", (value or "").strip().lower())


# Normalised-header -> task field. Several spellings map to the same field so
# the importer tolerates "Task ID"/"Code", "Scope of Work"/"Scope", etc.
HEADER_MAP = {
    "taskid": "code",
    "code": "code",
    "taskcode": "code",
    "category": "category",
    "title": "title",
    "overview": "overview",
    "scopeofwork": "scope_of_work",
    "scope": "scope_of_work",
    "companydetails": "company_details",
    "company": "company_details",
    "prd": "prd_link",
    "prdlink": "prd_link",
    "assets": "assets_link",
    "assetslink": "assets_link",
    "instructionmd": "instruction_md_link",
    "instructionmdlink": "instruction_md_link",
    "rubrics": "rubrics_link",
    "rubric": "rubrics_link",
    "rubricslink": "rubrics_link",
}

# Plain text fields copied straight from their column to the task.
_TEXT_FIELDS = (
    "code", "title", "overview", "scope_of_work", "company_details",
    "prd_link", "assets_link", "instruction_md_link", "rubrics_link",
)


def _cell_str(value):
    """Coerce a spreadsheet cell (str/int/float/None) to a trimmed string."""
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        # openpyxl reads "1234567" as 1234567.0 — drop the spurious ".0".
        value = int(value)
    return str(value).strip()


class FenrirTaskImportWizard(models.TransientModel):
    _name = "fenrir.task.import.wizard"
    _description = "Fenrir — Import Tasks from Spreadsheet"

    data_file = fields.Binary(string="Spreadsheet", required=True)
    data_filename = fields.Char(string="Filename")
    has_header = fields.Boolean(
        string="First row is header",
        default=True,
        help="The first row holds the column titles (Task ID, Category, ...).")
    create_missing_categories = fields.Boolean(
        string="Create missing categories",
        default=True,
        help="When a Category name is not found, create it. If unchecked, "
             "the task is created with no category instead.")

    # ------------------------------------------------------------------ parse
    def _parse_rows(self):
        """Return a list of row-lists (each a list of cell strings)."""
        raw = base64.b64decode(self.data_file)
        name = (self.data_filename or "").lower()
        is_xlsx = name.endswith(".xlsx") or raw[:2] == b"PK"

        if is_xlsx:
            try:
                import openpyxl
            except ImportError as exc:
                raise UserError(_(
                    "Reading .xlsx files needs the 'openpyxl' Python library, "
                    "which is not installed. Either install it, or re-save the "
                    "spreadsheet as CSV and upload that.")) from exc
            try:
                wb = openpyxl.load_workbook(
                    io.BytesIO(raw), read_only=True, data_only=True)
            except Exception as exc:  # noqa: BLE001
                raise UserError(_(
                    "Could not read the spreadsheet: %s") % exc) from exc
            ws = wb.active
            rows = [[_cell_str(c) for c in row]
                    for row in ws.iter_rows(values_only=True)]
            wb.close()
        else:
            try:
                text = raw.decode("utf-8-sig")
            except UnicodeDecodeError:
                try:
                    text = raw.decode("latin-1")
                except UnicodeDecodeError as exc:
                    raise UserError(_(
                        "Unable to decode the CSV file. Re-export it as "
                        "'CSV UTF-8' and retry.")) from exc
            try:
                dialect = csv.Sniffer().sniff(text[:2048], delimiters=",;\t")
            except csv.Error:
                dialect = csv.excel
            rows = [[_cell_str(c) for c in row]
                    for row in csv.reader(io.StringIO(text), dialect=dialect)]

        # Drop fully-empty rows.
        return [r for r in rows if any(c for c in r)]

    # ----------------------------------------------------------------- import
    def action_import(self):
        self.ensure_one()
        if not self.data_file:
            raise UserError(_("Please upload a spreadsheet."))

        rows = self._parse_rows()
        if not rows:
            raise UserError(_("The spreadsheet is empty."))

        if not self.has_header:
            raise UserError(_(
                "A header row is required so columns can be matched by name. "
                "Tick 'First row is header'."))

        header = rows[0]
        data_rows = rows[1:]
        if not data_rows:
            raise UserError(_(
                "The spreadsheet has a header row but no data rows."))

        # Map each column index to a task field via its header.
        col_to_field = {}
        for idx, cell in enumerate(header):
            field = HEADER_MAP.get(_norm_header(cell))
            if field:
                col_to_field[idx] = field

        if "code" not in col_to_field.values():
            raise UserError(_(
                "No 'Task ID' column found. Detected headers: %s"
            ) % ", ".join(h for h in header if h))

        Task = self.env["fenrir.task"]
        Category = self.env["fenrir.category"]
        existing_codes = set(
            Task.with_context(active_test=False).search([]).mapped("code"))
        category_cache = {}  # normalised name -> category id

        def resolve_category(name):
            key = name.strip().lower()
            if not key:
                return False
            if key in category_cache:
                return category_cache[key]
            cat = Category.search([("name", "=ilike", name.strip())], limit=1)
            if not cat and self.create_missing_categories:
                cat = Category.create({"name": name.strip()})
            category_cache[key] = cat.id if cat else False
            return category_cache[key]

        to_create = []
        seen_codes = set()
        skipped_dup = []
        skipped_nocode = 0

        for row in data_rows:
            row_vals = {}
            category_name = ""
            for idx, field in col_to_field.items():
                value = row[idx] if idx < len(row) else ""
                if field == "category":
                    category_name = value
                elif field in _TEXT_FIELDS:
                    row_vals[field] = value

            code = (row_vals.get("code") or "").strip()
            if not code:
                skipped_nocode += 1
                continue
            if code in existing_codes or code in seen_codes:
                skipped_dup.append(code)
                continue
            seen_codes.add(code)

            row_vals["code"] = code
            cat_id = resolve_category(category_name)
            if cat_id:
                row_vals["category_id"] = cat_id
            # Drop empty strings so we don't overwrite defaults with "".
            row_vals = {k: v for k, v in row_vals.items()
                        if k == "code" or v not in ("", None, False)}
            to_create.append(row_vals)

        if not to_create:
            raise UserError(_(
                "No tasks were created. %(dup)d row(s) had a Task ID that "
                "already exists, and %(blank)d row(s) had no Task ID."
            ) % {"dup": len(skipped_dup), "blank": skipped_nocode})

        created = Task.create(to_create)
        _logger.info(
            "Fenrir: imported %d tasks from %s (skipped %d duplicates, "
            "%d blank) ", len(created), self.data_filename or "(no name)",
            len(skipped_dup), skipped_nocode)

        summary = _("%d created") % len(created)
        if skipped_dup:
            summary += _(", %d skipped (duplicate Task ID)") % len(skipped_dup)
        if skipped_nocode:
            summary += _(", %d skipped (no Task ID)") % skipped_nocode

        return {
            "type": "ir.actions.act_window",
            "name": _("Imported Tasks — %s") % summary,
            "res_model": "fenrir.task",
            "view_mode": "list,form",
            "domain": [("id", "in", created.ids)],
            "context": {"from_all_tasks": True},
            "target": "current",
        }
