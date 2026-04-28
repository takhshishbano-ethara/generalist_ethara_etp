import json
import logging

from odoo import fields, models

_logger = logging.getLogger(__name__)

_MAX_PREVIEW_LINES = 50
_MAX_CHARS_PER_LINE = 600


class AuroraPipelinePreview(models.TransientModel):
    _name = "aurora.pipeline.preview"
    _description = "JSONL Preview"

    phase_label = fields.Char(readonly=True)
    preview_text = fields.Text(readonly=True)
    record_count = fields.Integer(readonly=True)
    preview_count = fields.Integer(readonly=True)

    @staticmethod
    def _build_preview(file_path, total_count):
        lines = []
        idx = -1
        with open(file_path, "r") as fh:
            for idx, raw_line in enumerate(fh):
                if idx >= _MAX_PREVIEW_LINES:
                    remaining = (total_count or idx) - _MAX_PREVIEW_LINES
                    if remaining > 0:
                        lines.append(f"… and {remaining} more records")
                    break
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    obj = json.loads(raw_line)
                    pretty = json.dumps(obj, indent=2, ensure_ascii=False)
                    if len(pretty) > _MAX_CHARS_PER_LINE:
                        pretty = pretty[:_MAX_CHARS_PER_LINE] + "\n  …(truncated)"
                    lines.append(pretty)
                except json.JSONDecodeError:
                    lines.append(raw_line[:_MAX_CHARS_PER_LINE])

        preview_count = min(idx + 1, _MAX_PREVIEW_LINES) if idx >= 0 else 0
        return "\n---\n".join(lines), preview_count
