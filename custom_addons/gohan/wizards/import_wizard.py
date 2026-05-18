import base64
import csv
import io

from odoo import fields, models
from odoo.exceptions import UserError


class GohanImportWizard(models.TransientModel):
    _name = "gohan.import.wizard"
    _description = "Import URLs as Tasks"

    csv_file = fields.Binary(string="CSV File", required=True)
    csv_filename = fields.Char(string="Filename")

    def action_import(self):
        """Parse CSV and create tasks in not_assigned state."""
        self.ensure_one()
        if not self.csv_file:
            raise UserError("Please upload a CSV file.")

        try:
            content = base64.b64decode(self.csv_file).decode("utf-8-sig")
        except Exception:
            raise UserError("Could not read the file. Ensure it is a valid UTF-8 CSV.")

        reader = csv.DictReader(io.StringIO(content))
        if not reader.fieldnames or "url" not in [f.strip().lower() for f in reader.fieldnames]:
            raise UserError(
                'CSV must have a "url" column. '
                "Optional columns: category"
            )

        # Normalize field names
        field_map = {f.strip().lower(): f for f in reader.fieldnames}
        url_col = field_map.get("url")
        cat_col = field_map.get("category")

        # Pre-load categories for matching
        categories = {
            c.name.lower(): c.id
            for c in self.env["gohan.category"].search([])
        }

        tasks_created = 0
        errors = []
        Job = self.env["gohan.job"]

        for i, row in enumerate(reader, start=2):
            url = (row.get(url_col) or "").strip()
            if not url:
                errors.append(f"Row {i}: empty URL, skipped")
                continue

            # Ensure URL has scheme
            if not url.startswith(("http://", "https://")):
                url = "https://" + url

            # Duplicate check: skip if same URL already exists and not submitted
            existing = Job.sudo().search_count([
                ("url", "=", url),
                ("state", "not in", ["submitted", "cancelled"]),
            ])
            if existing:
                errors.append(f"Row {i}: duplicate URL '{url}', skipped")
                continue

            vals = {
                "url": url,
                "state": "not_assigned",
                "user_id": False,
            }

            # Match category
            if cat_col:
                cat_name = (row.get(cat_col) or "").strip()
                if cat_name:
                    cat_id = categories.get(cat_name.lower())
                    if cat_id:
                        vals["category_id"] = cat_id
                    else:
                        errors.append(
                            f"Row {i}: category '{cat_name}' not found, task created without category"
                        )

            try:
                Job.create(vals)
                tasks_created += 1
            except Exception as exc:
                errors.append(f"Row {i}: {exc}")

        # Build result message
        msg = f"Created {tasks_created} task(s). Taskers can now pick them up via Start Task."
        if errors:
            msg += f"\n\n{len(errors)} warning(s):\n" + "\n".join(errors[:20])
            if len(errors) > 20:
                msg += f"\n... and {len(errors) - 20} more"

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Import Complete",
                "message": msg,
                "type": "success" if not errors else "warning",
                "sticky": bool(errors),
            },
        }
