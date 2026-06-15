"""Import rubric scores from a CSV file into a fenrir.seller.offer.

Wizard reads a CSV with columns (rubric_name, rubric_description, rating,
justification), matches rows to the offer's existing rubric_score_ids by
rubric_name (case-insensitive), and updates the rating + justification
on each matching score.

Rubrics not present on the task are reported as skipped — this wizard does
NOT create new rubrics, only updates scores for existing ones.
"""

import base64
import csv
import io
import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError


_logger = logging.getLogger(__name__)


class FenrirRubricScoreImportWizard(models.TransientModel):
    _name = "fenrir.rubric.score.import.wizard"
    _description = "Fenrir — Import Rubric Scores from CSV"

    seller_offer_id = fields.Many2one(
        comodel_name="fenrir.seller.offer",
        string="Seller Offer",
        required=True,
        readonly=True,
    )
    csv_file = fields.Binary(string="CSV File", required=True)
    csv_filename = fields.Char(string="Filename")
    has_header = fields.Boolean(
        string="First row is header",
        default=True,
        help="Skip the first row (column titles).")

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        active_id = self.env.context.get("active_id")
        active_model = self.env.context.get("active_model")
        if ("seller_offer_id" in fields_list
                and active_model == "fenrir.seller.offer" and active_id):
            res["seller_offer_id"] = active_id
        return res

    def action_import(self):
        self.ensure_one()
        if not self.csv_file:
            raise UserError(_("Upload a CSV file."))
        if not self.seller_offer_id:
            raise UserError(_("No seller offer selected to import into."))

        raw = base64.b64decode(self.csv_file)
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            try:
                text = raw.decode("latin-1")
            except UnicodeDecodeError as exc:
                raise UserError(_(
                    "Cannot decode CSV. Re-export as UTF-8 from your "
                    "spreadsheet and retry.")) from exc

        try:
            dialect = csv.Sniffer().sniff(text[:2048], delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel

        reader = csv.reader(io.StringIO(text), dialect=dialect)
        rows = [r for r in reader if any((c or "").strip() for c in r)]
        if not rows:
            raise UserError(_("CSV is empty."))
        if self.has_header:
            rows = rows[1:]
        if not rows:
            raise UserError(_("CSV has only a header row."))

        # Build a lookup of existing scores keyed by lowercase rubric name.
        scores_by_name = {}
        for score in self.seller_offer_id.rubric_score_ids:
            if score.rubric_name:
                scores_by_name[score.rubric_name.strip().lower()] = score

        updated = 0
        skipped_missing = []
        skipped_bad_rating = []
        for row in rows:
            name = (row[0] or "").strip() if len(row) >= 1 else ""
            if not name:
                continue
            rating_raw = (row[2] or "").strip() if len(row) >= 3 else ""
            justification = (row[3] or "").strip() if len(row) >= 4 else ""

            score = scores_by_name.get(name.lower())
            if not score:
                skipped_missing.append(name)
                continue

            rating = score.rating
            if rating_raw:
                try:
                    rating = int(round(float(rating_raw.replace(",", "."))))
                except ValueError:
                    skipped_bad_rating.append(f"{name} ('{rating_raw}')")
                    continue

            score.write({"rating": rating, "justification": justification})
            updated += 1

        msg_parts = [_("Updated %d rubric score(s).") % updated]
        if skipped_missing:
            msg_parts.append(_("Skipped (rubric not on task): %s")
                             % ", ".join(skipped_missing[:5])
                             + ("…" if len(skipped_missing) > 5 else ""))
        if skipped_bad_rating:
            msg_parts.append(_("Skipped (invalid rating): %s")
                             % ", ".join(skipped_bad_rating[:5])
                             + ("…" if len(skipped_bad_rating) > 5 else ""))

        _logger.info(
            "Fenrir: rubric-score import on seller offer %s — %s",
            self.seller_offer_id.display_name, " | ".join(msg_parts))

        # Try to send a toast via the bus; fall back silently if the API
        # signature differs on this Odoo version. The modal will close
        # either way via the act_window_close return.
        try:
            self.env.user._bus_send("simple_notification", {
                "title": _("Rubric scores imported"),
                "message": " ".join(msg_parts),
                "sticky": False,
                "type": ("success" if updated and not (
                    skipped_missing or skipped_bad_rating) else "warning"),
            })
        except Exception:  # noqa: BLE001
            _logger.info("Fenrir: bus toast skipped (%s)",
                         " | ".join(msg_parts))

        return {"type": "ir.actions.act_window_close"}
