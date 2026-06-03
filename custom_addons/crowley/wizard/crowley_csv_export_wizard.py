import base64
import csv
import io
import logging
import re

from odoo import _, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

_XMLID_RE = re.compile(r"[^a-zA-Z0-9_]")


def _sanitize_xmlid(val):
    """Replace non-alphanumeric chars with underscores for External ID use."""
    return _XMLID_RE.sub("_", str(val))


class CrowleyCsvExportWizard(models.TransientModel):
    _name = "crowley.csv.export.wizard"
    _description = "Export Generations for Migration"

    state = fields.Selection(
        [("draft", "Configure"), ("done", "Download")],
        default="draft",
        readonly=True,
    )
    export_file = fields.Binary("Export File", readonly=True, attachment=False)
    export_filename = fields.Char("Filename", readonly=True)
    export_count = fields.Integer("Generations", readonly=True)
    attempt_count = fields.Integer("Attempts", readonly=True)
    log_text = fields.Text("Export Log", readonly=True)

    # ------------------------------------------------------------------
    # CSV builder
    # ------------------------------------------------------------------

    _HEADERS = [
        "id",
        "prompt",
        "original_prompt",
        "negative_prompt",
        "duration",
        "resolution",
        "aspect_ratio",
        "category",
        "sub_category",
        "topic",
        "style",
        "priority",
        "complexity",
        "generate_audio",
        "model_name",
        "seed",
        "attempt_ids/id",
        "attempt_ids/attempt_number",
        "attempt_ids/prompt",
        "attempt_ids/original_prompt",
        "attempt_ids/negative_prompt",
        "attempt_ids/duration",
        "attempt_ids/resolution",
        "attempt_ids/aspect_ratio",
        "attempt_ids/seed",
        "attempt_ids/generate_audio",
        "attempt_ids/model_name",
        "attempt_ids/state",
        "attempt_ids/review_state",
        "attempt_ids/video_s3_url",
        "attempt_ids/video_s3_key",
        "attempt_ids/video_sha256",
        "attempt_ids/video_size_bytes",
        "attempt_ids/cost_usd",
        "attempt_ids/error_code",
        "attempt_ids/error_message",
        "attempt_ids/completed_at",
        "attempt_ids/category",
        "attempt_ids/sequence_number",
    ]

    _GEN_FIELD_COUNT = 16

    @staticmethod
    def _bool_str(val):
        return "True" if val else "False"

    def _gen_fields(self, gen, xmlid):
        return [
            xmlid,
            gen.prompt or "",
            gen.original_prompt or "",
            gen.negative_prompt or "",
            gen.duration or "",
            gen.resolution or "",
            gen.aspect_ratio or "",
            gen.category or "",
            gen.sub_category or "",
            gen.topic or "",
            gen.style or "",
            gen.priority or "",
            gen.complexity or "",
            self._bool_str(gen.generate_audio),
            gen.model_name or "",
            gen.seed or 0,
        ]

    def _att_fields(self, att):
        s3_key = att.video_s3_key or f"id_{att.id}"
        return [
            f"__export__.crowley_att_{_sanitize_xmlid(s3_key)}",
            att.attempt_number or 1,
            att.prompt or "",
            att.original_prompt or "",
            att.negative_prompt or "",
            att.duration or "",
            att.resolution or "",
            att.aspect_ratio or "",
            att.seed or 0,
            self._bool_str(att.generate_audio),
            att.model_name or "",
            att.state or "",
            att.review_state or "",
            att.video_s3_url or "",
            att.video_s3_key or "",
            att.video_sha256 or "",
            att.video_size_bytes or 0,
            att.cost_usd or 0.0,
            att.error_code or "",
            att.error_message or "",
            fields.Datetime.to_string(att.completed_at) if att.completed_at else "",
            att.category or "",
            att.sequence_number or 0,
        ]

    def _build_csv(self, generations, gen_xmlid_map):
        buf = io.StringIO()
        writer = csv.writer(buf, quoting=csv.QUOTE_ALL)
        writer.writerow(self._HEADERS)

        total_attempts = 0
        for gen in generations:
            done_attempts = gen.attempt_ids.filtered(lambda a: a.state == "done")
            xmlid = gen_xmlid_map[gen.id]

            for idx, att in enumerate(done_attempts):
                if idx == 0:
                    row = self._gen_fields(gen, xmlid) + self._att_fields(att)
                else:
                    row = ([""] * self._GEN_FIELD_COUNT) + self._att_fields(att)
                writer.writerow(row)
                total_attempts += 1

        return buf.getvalue(), total_attempts

    # ------------------------------------------------------------------
    # Main action
    # ------------------------------------------------------------------

    def action_export(self):
        self.ensure_one()

        attempts = self.env["crowley.attempt"].search([("state", "=", "done")])
        if not attempts:
            raise UserError(_("No completed attempts found to export."))

        generations = attempts.mapped("job_id")

        gen_xmlid_map = {}
        for gen in generations:
            att = gen.attempt_ids.filtered(lambda a: a.state == "done")[:1]
            key = att.video_s3_key if att else f"id_{gen.id}"
            gen_xmlid_map[gen.id] = f"__export__.crowley_gen_{_sanitize_xmlid(key)}"

        seq_maxes = {}
        for att in attempts:
            if att.category and att.sequence_number:
                seq_maxes[att.category] = max(
                    seq_maxes.get(att.category, 0), att.sequence_number
                )

        csv_data, total_attempts = self._build_csv(generations, gen_xmlid_map)

        log_lines = [
            f"Export complete: {len(generations)} generations, "
            f"{total_attempts} attempts.",
            "",
            "IMPORT ON PROD:",
            "  1. Go to Crowley > Generations (list view)",
            "  2. Gear icon > Import Records",
            "  3. Upload the CSV, verify column mapping, Import",
            "",
            "AFTER IMPORT — fix sequences in Odoo shell:",
            f"  odoo-bin shell -d <dbname>",
            "",
        ]
        for cat, ms in sorted(seq_maxes.items()):
            code = f"crowley.attempt.{cat}"
            log_lines.append(
                f"  env['ir.sequence'].search("
                f"[('code', '=', '{code}')], limit=1)"
                f".sudo().write({{'number_next_actual': {ms + 1}}})"
            )
        if seq_maxes:
            log_lines.append("  env.cr.commit()")

        self.write(
            {
                "state": "done",
                "export_file": base64.b64encode(csv_data.encode("utf-8-sig")),
                "export_filename": "crowley_migration.csv",
                "export_count": len(generations),
                "attempt_count": total_attempts,
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
