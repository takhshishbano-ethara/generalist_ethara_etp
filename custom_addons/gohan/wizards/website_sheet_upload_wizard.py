import base64
import csv
import io
import re

from odoo import fields, models
from odoo.exceptions import UserError


_HEADER_ALIASES = {
    "category": {"category", "website category", "cat"},
    "url": {"url", "website url", "website", "site"},
}


def _slugify_code(name):
    """Derive a gohan.category.code from a display name.

    "Travel & Booking" -> "travel_booking", "AI / ML" -> "ai_ml".
    Returns "category" as a fallback if the name is all punctuation.
    """
    slug = re.sub(r"[^a-z0-9]+", "_", (name or "").lower()).strip("_")
    return slug or "category"


def _resolve_columns(fieldnames):
    """Map canonical -> actual CSV header for the two columns we accept."""
    if not fieldnames:
        return {}
    norm = {f: f.strip().lower() for f in fieldnames}
    resolved = {}
    for canonical, aliases in _HEADER_ALIASES.items():
        for raw, lower in norm.items():
            if lower in aliases:
                resolved[canonical] = raw
                break
    return resolved


class GohanWebsiteSheetUploadWizard(models.TransientModel):
    _name = "gohan.website.sheet.upload.wizard"
    _description = "Upload Website Assignment Sheet"

    name = fields.Char(
        string="Sheet Name",
        help="Optional label. Auto-generated if left empty.",
    )
    csv_file = fields.Binary(string="CSV File", required=True)
    csv_filename = fields.Char(string="Filename")

    def action_upload(self):
        self.ensure_one()
        if not self.csv_file:
            raise UserError("Please choose a CSV file.")

        try:
            content = base64.b64decode(self.csv_file).decode("utf-8-sig")
        except Exception:
            raise UserError(
                "Could not read the file. Ensure it is a valid UTF-8 CSV."
            )

        reader = csv.DictReader(io.StringIO(content))
        cols = _resolve_columns(reader.fieldnames)
        missing = [c for c in ("category", "url") if c not in cols]
        if missing:
            raise UserError(
                "CSV is missing required column(s): %s.\n"
                "Expected headers: category, url."
                % ", ".join(missing)
            )

        # Preload categories: lookup by name (case-insensitive) and by code.
        categories = self.env["gohan.category"].sudo().search([])
        cat_by_name = {c.name.strip().lower(): c.id for c in categories}
        cat_by_code = {
            (c.code or "").strip().lower(): c.id for c in categories if c.code
        }

        # Build the sheet first so all lines hang off one parent.
        sheet_vals = {
            "csv_file": self.csv_file,
            "csv_filename": self.csv_filename,
        }
        if self.name:
            sheet_vals["name"] = self.name
        sheet = self.env["gohan.website.sheet"].create(sheet_vals)

        line_vals = []
        seen_urls = set()
        errors = []
        # Track categories auto-created from the CSV so we can mention them
        # in the post-upload notification.
        created_categories = []
        Category = self.env["gohan.category"].sudo()

        for i, row in enumerate(reader, start=2):
            cat_raw = (row.get(cols["category"]) or "").strip()
            url_raw = (row.get(cols["url"]) or "").strip()

            if not url_raw:
                errors.append(f"Row {i}: empty URL — skipped.")
                continue
            if not cat_raw:
                errors.append(f"Row {i}: empty category — skipped.")
                continue

            url = url_raw
            if not url.startswith(("http://", "https://")):
                url = "https://" + url

            if url in seen_urls:
                errors.append(f"Row {i}: duplicate URL '{url}' within file — skipped.")
                continue
            seen_urls.add(url)

            cat_id = (
                cat_by_name.get(cat_raw.lower())
                or cat_by_code.get(cat_raw.lower())
            )
            if not cat_id:
                # Auto-create a new gohan.category from the CSV value.
                # Derive a code by slugifying the name; if the slug
                # collides with an existing code, reuse that one instead
                # of erroring on the UNIQUE(code) constraint.
                slug = _slugify_code(cat_raw)
                if slug in cat_by_code:
                    cat_id = cat_by_code[slug]
                else:
                    new_cat = Category.create({
                        "name": cat_raw,
                        "code": slug,
                    })
                    cat_id = new_cat.id
                    cat_by_name[cat_raw.lower()] = new_cat.id
                    cat_by_code[slug] = new_cat.id
                    created_categories.append(cat_raw)

            line_vals.append({
                "sheet_id": sheet.id,
                "category_id": cat_id,
                "url": url,
                "status": "unassigned",
            })

        if not line_vals:
            sheet.unlink()
            raise UserError(
                "No valid rows in the CSV. Errors:\n" + "\n".join(errors[:20])
            )

        self.env["gohan.website.sheet.line"].create(line_vals)

        msg = f"Imported {len(line_vals)} website(s) into sheet '{sheet.name}'."
        if created_categories:
            unique_new = sorted(set(created_categories))
            msg += (
                f"\n\nCreated {len(unique_new)} new categor"
                f"{'y' if len(unique_new) == 1 else 'ies'}: "
                + ", ".join(unique_new)
            )
        if errors:
            msg += (
                f"\n\n{len(errors)} warning(s):\n" + "\n".join(errors[:20])
            )
            if len(errors) > 20:
                msg += f"\n... and {len(errors) - 20} more"

        # Open the newly-created sheet so the admin lands on its detail page.
        return {
            "type": "ir.actions.act_window",
            "name": sheet.name,
            "res_model": "gohan.website.sheet",
            "res_id": sheet.id,
            "view_mode": "form",
            "target": "current",
            "context": {"upload_result_message": msg},
        }
