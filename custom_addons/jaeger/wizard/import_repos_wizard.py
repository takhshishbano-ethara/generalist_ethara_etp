import base64
import csv
import io
import logging

from odoo import fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class ImportReposWizard(models.TransientModel):
    _name = "jaeger.import.repos.wizard"
    _description = "Import Repos from CSV"

    csv_file = fields.Binary(string="CSV File", required=True)
    csv_filename = fields.Char(string="Filename")
    auto_validate = fields.Boolean(
        string="Auto-validate after import", default=False,
    )
    default_language = fields.Selection(
        [
            ("python", "Python"),
            ("java", "Java"),
            ("typescript", "TypeScript"),
            ("javascript", "JavaScript"),
            ("go", "Go"),
            ("rust", "Rust"),
            ("c", "C"),
            ("cpp", "C++"),
        ],
        string="Default Language",
        default="python",
    )
    default_pipeline_mode = fields.Selection(
        [
            ("swe", "SWE (Single-PR Tasks)"),
            ("lht", "LHT (Long-Horizon Tasks)"),
        ],
        string="Default Pipeline Mode",
        default="swe",
    )

    def action_import(self):
        self.ensure_one()
        if not self.csv_file:
            raise UserError("Please upload a CSV file.")

        data = base64.b64decode(self.csv_file).decode("utf-8")
        reader = csv.DictReader(io.StringIO(data))

        Repo = self.env["jaeger.repository"]
        created = 0
        skipped = 0

        for row in reader:
            url = row.get("url", "").strip()
            if not url:
                continue

            existing = Repo.search([("repo_url", "=", url)], limit=1)
            if existing:
                skipped += 1
                continue

            Repo.create(
                {
                    "repo_url": url,
                    "language": row.get("language", self.default_language),
                    "pipeline_mode": row.get(
                        "pipeline_mode", self.default_pipeline_mode,
                    ),
                },
            )
            created += 1

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Import Complete",
                "message": f"Created {created} repos, skipped {skipped} duplicates.",
                "type": "success",
                "sticky": False,
            },
        }
