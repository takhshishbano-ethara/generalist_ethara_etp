# -*- coding: utf-8 -*-
import base64
import csv
import io
import logging
import xml.etree.ElementTree as ET
import zipfile

from odoo import fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

XLSX_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def _extract_urls_from_xlsx(data):
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        shared_strings = []
        if "xl/sharedStrings.xml" in zf.namelist():
            tree = ET.parse(zf.open("xl/sharedStrings.xml"))
            for si in tree.findall(f"{XLSX_NS}si"):
                text_parts = []
                for t in si.iter(f"{XLSX_NS}t"):
                    if t.text:
                        text_parts.append(t.text)
                shared_strings.append("".join(text_parts))

        tree = ET.parse(zf.open("xl/worksheets/sheet1.xml"))
        urls = []
        for row in tree.findall(f".//{XLSX_NS}row"):
            cell = row.find(f"{XLSX_NS}c")
            if cell is None:
                continue
            cell_type = cell.get("t", "")
            val_el = cell.find(f"{XLSX_NS}v")
            if val_el is None or not val_el.text:
                continue
            if cell_type == "s":
                idx = int(val_el.text)
                value = shared_strings[idx] if idx < len(shared_strings) else ""
            else:
                value = val_el.text
            value = value.strip()
            if value and "github.com" in value:
                urls.append(value)
    return urls


def _extract_urls_from_csv(data):
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("latin-1")
    reader = csv.reader(text.splitlines())
    urls = []
    for row in reader:
        if not row:
            continue
        url = row[0].strip()
        if url and "github.com" in url:
            urls.append(url)
    return urls


class ImportReposWizard(models.TransientModel):
    _name = "commit0.import.repos.wizard"
    _description = "Import Repository URLs from CSV/XLSX"

    csv_file = fields.Binary(string="File (CSV or XLSX)", required=True)
    csv_filename = fields.Char(string="Filename")
    result_message = fields.Text(string="Import Result", readonly=True)

    def action_import(self):
        self.ensure_one()
        if not self.csv_file:
            raise UserError("Please upload a file.")

        data = base64.b64decode(self.csv_file)
        filename = (self.csv_filename or "").lower()

        if filename.endswith(".xlsx"):
            try:
                urls = _extract_urls_from_xlsx(data)
            except (zipfile.BadZipFile, KeyError, ET.ParseError) as e:
                raise UserError(f"Failed to read XLSX file: {e}")
        else:
            urls = _extract_urls_from_csv(data)

        if not urls:
            raise UserError(
                "No valid GitHub URLs found. "
                "Ensure the file contains URLs with 'github.com' in the first column."
            )

        Eval = self.env["commit0.repo.evaluation"]
        existing = set(Eval.search([("repo_url", "in", urls)]).mapped("repo_url"))

        vals_list = []
        skipped_urls = []
        for url in urls:
            if url in existing:
                skipped_urls.append(url)
                continue
            vals_list.append(
                {
                    "repo_url": url,
                    "user_id": False,
                }
            )

        if vals_list:
            Eval.create(vals_list)

        msg_parts = [f"Created: {len(vals_list)} task(s)"]
        if skipped_urls:
            msg_parts.append(f"Skipped (duplicate): {len(skipped_urls)}")
            msg_parts.append("Skipped URLs:\n" + "\n".join(skipped_urls))

        self.result_message = "\n".join(msg_parts)

        return {
            "type": "ir.actions.act_window",
            "res_model": self._name,
            "res_id": self.id,
            "view_mode": "form",
            "target": "new",
        }
