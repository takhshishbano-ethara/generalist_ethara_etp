"""Import rubrics from a CSV file into a fenrir.task.

Wizard accepts a two-column CSV (name, description) and creates
fenrir.rubric records linked to the active task.
"""

import base64
import csv
import io
import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError


_logger = logging.getLogger(__name__)


class FenrirRubricImportWizard(models.TransientModel):
    _name = "fenrir.rubric.import.wizard"
    _description = "Fenrir — Import Rubrics from CSV"

    task_id = fields.Many2one(
        comodel_name="fenrir.task",
        string="Task",
        required=True,
        readonly=True,
    )
    csv_file = fields.Binary(string="CSV File", required=True)
    csv_filename = fields.Char(string="Filename")
    has_header = fields.Boolean(
        string="First row is header",
        default=True,
        help="Skip the first row (it contains column titles like name,description).")
    replace_existing = fields.Boolean(
        string="Replace existing rubrics",
        default=False,
        help="When checked, all existing rubrics on this task are deleted "
             "before importing.")

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        active_id = self.env.context.get("active_id")
        active_model = self.env.context.get("active_model")
        if "task_id" in fields_list and active_model == "fenrir.task" and active_id:
            res["task_id"] = active_id
        return res

    def action_import(self):
        self.ensure_one()
        if not self.csv_file:
            raise UserError(_("Please upload a CSV file."))
        if not self.task_id:
            raise UserError(_("No task selected to import into."))

        raw = base64.b64decode(self.csv_file)
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            try:
                text = raw.decode("latin-1")
            except UnicodeDecodeError as exc:
                raise UserError(_(
                    "Unable to decode CSV file. Expected UTF-8 or Latin-1. "
                    "Re-export from your spreadsheet as 'CSV UTF-8' and retry."
                )) from exc

        # Sniff dialect so comma/semicolon both work.
        try:
            dialect = csv.Sniffer().sniff(text[:2048], delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel  # default to comma

        reader = csv.reader(io.StringIO(text), dialect=dialect)
        rows = [row for row in reader if any((c or "").strip() for c in row)]
        if not rows:
            raise UserError(_("CSV file is empty."))

        if self.has_header:
            rows = rows[1:]
        if not rows:
            raise UserError(_(
                "CSV file has only a header row and no data."))

        if self.replace_existing and self.task_id.rubric_ids:
            self.task_id.rubric_ids.unlink()

        Rubric = self.env["fenrir.rubric"]
        starting_seq = max(
            (r.sequence for r in self.task_id.rubric_ids), default=0)
        to_create = []
        for i, row in enumerate(rows, start=1):
            name = (row[0] or "").strip() if len(row) >= 1 else ""
            description = (row[1] or "").strip() if len(row) >= 2 else ""
            if not name:
                continue
            to_create.append({
                "task_id": self.task_id.id,
                "sequence": starting_seq + 10 * i,
                "name": name,
                "description": description,
            })

        if not to_create:
            raise UserError(_(
                "No valid rubrics found in the CSV. Each non-empty row must "
                "have at least a name in the first column."))

        created = Rubric.create(to_create)
        _logger.info(
            "Fenrir: imported %d rubrics into task %s from %s",
            len(created), self.task_id.code, self.csv_filename or "(no filename)")

        return {
            "type": "ir.actions.act_window",
            "name": _("Task"),
            "res_model": "fenrir.task",
            "view_mode": "form",
            "res_id": self.task_id.id,
            "target": "current",
        }
