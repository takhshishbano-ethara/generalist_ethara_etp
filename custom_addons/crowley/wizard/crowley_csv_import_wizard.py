import base64
import csv
import io
import logging
import re
from datetime import datetime, timezone

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)

_PROMPT_MAX = 2000

_RESOLUTION_MAP = {
    "1280 x 720": "720p",
    "1920 x 1080": "1080p",
    "854 x 480": "480p",
    "640 x 480": "480p",
}

# CSV writes "16:09" — normalise to Odoo selection value "16:9".
_ASPECT_RATIO_NORM = re.compile(r"^0*(\d+):0*(\d+)$")

# Extracts trailing 6-digit sequence from filenames like T2AV_human_activities_000873.mp4
_SEQ_RE = re.compile(r"_(\d{4,6})\.mp4$")


class CrowleyCsvImportWizard(models.TransientModel):
    _name = "crowley.csv.import.wizard"
    _description = "Import Completed Video Generations from CSV"

    csv_file = fields.Binary(string="CSV File", required=True)
    csv_filename = fields.Char(string="Filename")
    import_count = fields.Integer(string="Imported", readonly=True)
    skip_count = fields.Integer(string="Skipped", readonly=True)
    error_count = fields.Integer(string="Errors", readonly=True)
    log_text = fields.Text(string="Import Log", readonly=True)
    state = fields.Selection(
        [("draft", "Upload"), ("done", "Done")],
        default="draft",
        readonly=True,
    )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _clean(row, key):
        return (row.get(key) or "").strip()

    @classmethod
    def _normalise_resolution(cls, raw):
        return _RESOLUTION_MAP.get(raw, "720p")

    @classmethod
    def _normalise_aspect_ratio(cls, raw):
        m = _ASPECT_RATIO_NORM.match(raw)
        if m:
            return f"{m.group(1)}:{m.group(2)}"
        return raw or "16:9"

    @staticmethod
    def _extract_sequence(s3_key):
        """Return (int) sequence from T2AV/{cat}/T2AV_{cat}_{seq}.mp4."""
        m = _SEQ_RE.search(s3_key)
        if not m:
            return None
        return int(m.group(1))

    @staticmethod
    def _parse_completed_at(raw):
        if not raw:
            return False
        try:
            dt = datetime.fromisoformat(raw)
            if dt.tzinfo:
                dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
            return fields.Datetime.to_string(dt)
        except (ValueError, TypeError):
            return False

    @staticmethod
    def _safe_int(val, default=0):
        try:
            return int(val)
        except (ValueError, TypeError):
            return default

    @staticmethod
    def _safe_float(val, default=0.0):
        try:
            return float(val)
        except (ValueError, TypeError):
            return default

    @staticmethod
    def _clamp_duration(seconds):
        """Clamp to valid crowley.generation duration selection (4-15)."""
        clamped = max(4, min(15, seconds))
        return str(clamped)

    @staticmethod
    def _truncate_prompt(text, label="prompt"):
        """Truncate to _PROMPT_MAX chars, return (truncated_text, warning|None)."""
        if not text:
            return text, None
        if len(text) <= _PROMPT_MAX:
            return text, None
        return text[
            :_PROMPT_MAX
        ], f"{label} truncated from {len(text)} to {_PROMPT_MAX} chars"

    # ------------------------------------------------------------------
    # Main action
    # ------------------------------------------------------------------

    def action_import(self):
        self.ensure_one()
        if not self.csv_file:
            raise UserError(_("Please upload a CSV file."))

        try:
            raw = base64.b64decode(self.csv_file).decode("utf-8-sig")
        except Exception as exc:
            raise UserError(_("Cannot decode CSV file: %s", exc)) from exc

        reader = csv.DictReader(io.StringIO(raw))
        if not reader.fieldnames:
            raise UserError(_("CSV file appears empty or has no header row."))

        Generation = self.env["crowley.generation"]
        Attempt = self.env["crowley.attempt"]
        Sequence = self.env["ir.sequence"]

        imported = 0
        skipped = 0
        errors = 0
        log_lines = []
        seq_maxes = {}

        for row_idx, row in enumerate(reader, start=2):
            try:
                s3_key = self._clean(row, "s3_key")
                if not s3_key:
                    log_lines.append(f"Row {row_idx}: skipped — empty s3_key")
                    errors += 1
                    continue

                if Attempt.search_count([("video_s3_key", "=", s3_key)]):
                    log_lines.append(f"Row {row_idx}: skipped — s3_key already exists")
                    skipped += 1
                    continue

                seq_num = self._extract_sequence(s3_key)
                if seq_num is None:
                    log_lines.append(
                        f"Row {row_idx}: skipped — cannot extract sequence from '{s3_key}'"
                    )
                    errors += 1
                    continue

                category = self._clean(row, "Category")
                sub_category = self._clean(row, "Sub_Category")
                original_prompt = self._clean(row, "Prompt")
                enriched_prompt = self._clean(row, "enriched_prompt")
                prompt = enriched_prompt or original_prompt

                if not prompt:
                    log_lines.append(f"Row {row_idx}: skipped — no prompt")
                    errors += 1
                    continue

                prompt, pw = self._truncate_prompt(prompt, "enriched_prompt")
                original_prompt, opw = self._truncate_prompt(
                    original_prompt, "original_prompt"
                )
                warnings = [w for w in (pw, opw) if w]

                dur_sec = self._safe_int(self._clean(row, "duration_seconds"), 0)
                duration = self._clamp_duration(dur_sec) if dur_sec else "5"
                resolution = self._normalise_resolution(self._clean(row, "resolution"))
                aspect_ratio = self._normalise_aspect_ratio(
                    self._clean(row, "Aspect Ratio")
                )

                completed_at = self._parse_completed_at(
                    self._clean(row, "completed_at")
                )

                gen_vals = {
                    "user_id": self.env.user.id,
                    "prompt": prompt,
                    "original_prompt": original_prompt or False,
                    "duration": duration,
                    "resolution": resolution,
                    "aspect_ratio": aspect_ratio,
                    "category": category or False,
                    "sub_category": sub_category or False,
                    "topic": self._clean(row, "Topic") or False,
                    "style": self._clean(row, "Style") or False,
                    "priority": self._clean(row, "Priority") or False,
                    "complexity": self._clean(row, "Complexity") or False,
                }
                generation = Generation.create(gen_vals)

                attempt_vals = {
                    "job_id": generation.id,
                    "attempt_number": 1,
                    "prompt": prompt,
                    "original_prompt": original_prompt or False,
                    "duration": duration,
                    "resolution": resolution,
                    "aspect_ratio": aspect_ratio,
                    "state": "done",
                    "review_state": "pending",
                    "video_s3_url": self._clean(row, "s3_url") or False,
                    "video_s3_key": s3_key,
                    "video_sha256": self._clean(row, "sha256") or False,
                    "video_size_bytes": self._safe_int(self._clean(row, "size_bytes")),
                    "cost_usd": self._safe_float(self._clean(row, "cost_usd")),
                    "completed_at": completed_at,
                    "category": category,
                    "sequence_number": seq_num,
                }

                error_code = self._clean(row, "error_code")
                if error_code:
                    attempt_vals["error_code"] = error_code
                error_msg = self._clean(row, "error_message")
                if error_msg:
                    attempt_vals["error_message"] = error_msg

                Attempt.create(attempt_vals)

                if category:
                    seq_maxes[category] = max(seq_maxes.get(category, 0), seq_num)

                imported += 1
                line = f"Row {row_idx}: imported → {generation.name}"
                if warnings:
                    line += f" (warnings: {'; '.join(warnings)})"
                log_lines.append(line)

            except (ValidationError, UserError) as exc:
                errors += 1
                log_lines.append(f"Row {row_idx}: VALIDATION ERROR — {exc.args[0]}")
                _logger.warning("CSV import row %d validation error: %s", row_idx, exc)
            except Exception as exc:
                errors += 1
                log_lines.append(f"Row {row_idx}: ERROR — {exc}")
                _logger.exception("CSV import row %d failed", row_idx)

        for category, max_seq in seq_maxes.items():
            seq_code = f"crowley.attempt.{category}"
            seq_rec = Sequence.search([("code", "=", seq_code)], limit=1)
            if seq_rec and seq_rec.number_next_actual <= max_seq:
                seq_rec.sudo().write({"number_next_actual": max_seq + 1})
                log_lines.append(f"Sequence '{seq_code}': advanced to {max_seq + 1}")

        summary = (
            f"Import complete: {imported} imported, "
            f"{skipped} skipped (duplicates), {errors} errors."
        )
        log_lines.insert(0, summary)

        self.write(
            {
                "state": "done",
                "import_count": imported,
                "skip_count": skipped,
                "error_count": errors,
                "log_text": "\n".join(log_lines),
            }
        )

        return {
            "type": "ir.actions.act_window",
            "res_model": self._name,
            "res_id": self.id,
            "view_mode": "form",
            "target": "new",
        }
