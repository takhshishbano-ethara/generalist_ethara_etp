from __future__ import annotations

import json
import logging
import re

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

_logger = logging.getLogger(__name__)

_SHEET_PASSED = "passed"
_SHEET_FAILED = "failed"

_RESOLUTION_MAP = {
    "480p": "854x480",
    "720p": "1280x720",
    "1080p": "1920x1080",
}

_AUDIO_LINE_RE = re.compile(r"(?im)^\s*Audio\s*:\s*(.+)$")
_MAX_FINDINGS_IN_ISSUES = 3


class T2AVSequenceSheetRow(models.Model):
    _name = "t2av.sequence.sheet.row"
    _description = "T2AV Sequence Sheet Row (passed / failed)"
    _order = "create_date desc, id desc"
    _rec_name = "item_id"

    sheet_type = fields.Selection(
        [(_SHEET_PASSED, "Passed (Master)"), (_SHEET_FAILED, "Failed")],
        required=True, readonly=True, copy=False, index=True,
    )

    review_id = fields.Many2one(
        "t2av.video.review", required=True, readonly=True,
        ondelete="cascade", index=True, copy=False,
    )
    attempt_id = fields.Many2one(
        "t2av.attempt", related="review_id.attempt_id",
        store=True, readonly=True, index=True,
    )
    job_id = fields.Many2one(
        "t2av.generation", related="review_id.job_id",
        store=True, readonly=True, index=True,
    )

    item_id = fields.Char(required=True, readonly=True, index=True, copy=False)
    sequence_number = fields.Integer(readonly=True, copy=False, index=True)

    category = fields.Char(readonly=True)
    sub_category = fields.Char(string="Sub-Category", readonly=True)
    style = fields.Char(readonly=True)
    priority = fields.Char(readonly=True)
    complexity = fields.Char(readonly=True)
    language = fields.Char(readonly=True, default="English")
    topic = fields.Char(readonly=True)
    prompt = fields.Text(readonly=True)
    video_file = fields.Char(
        string="Video URL", readonly=True,
        help="Direct (non-presigned) S3 URL for the video file backing this sheet row.",
    )
    duration_seconds = fields.Float(readonly=True)
    resolution = fields.Char(readonly=True)
    fps = fields.Float(readonly=True)
    audio_description = fields.Text(readonly=True)
    contains_dialogue = fields.Boolean(readonly=True)
    speaker_count = fields.Integer(readonly=True)
    dialogue_transcript = fields.Text(readonly=True)
    visual_description = fields.Text(readonly=True)
    issues = fields.Text(
        help="Auto-populated for failed rows from top FATAL/MAJOR findings. "
             "Manager-editable before export.",
    )

    _sql_constraints = [
        ("uniq_review_id", "UNIQUE(review_id)",
         "A sequence-sheet row already exists for this review."),
    ]

    @api.model
    def _split_visual_audio(self, prompt_text):
        if not prompt_text:
            return "", ""
        text = str(prompt_text)
        match = _AUDIO_LINE_RE.search(text)
        if not match:
            return text.strip(), ""
        audio = (match.group(1) or "").strip()
        visual = text[:match.start()].rstrip()
        return visual, audio

    @api.model
    def _issues_from_findings(self, findings_json):
        if not findings_json:
            return ""
        try:
            parsed = json.loads(findings_json)
        except (TypeError, ValueError):
            return ""
        findings = parsed.get("findings") if isinstance(parsed, dict) else None
        if not isinstance(findings, list):
            return ""
        out = []
        for f in findings:
            if not isinstance(f, dict):
                continue
            if (f.get("status") or "").lower() != "fail":
                continue
            sev = (f.get("severity") or "").upper()
            if sev not in ("FATAL", "MAJOR"):
                continue
            evidence = str(f.get("evidence") or "").strip()
            if evidence:
                out.append(evidence)
            if len(out) >= _MAX_FINDINGS_IN_ISSUES:
                break
        return "; ".join(out)

    @api.model
    def _allocate_failure_sequence(self, category):
        code = f"t2av.attempt.{category}_fail"
        raw = self.env["ir.sequence"].next_by_code(code)
        if not raw:
            raise ValidationError(_(
                "Sequence %(code)s not found. Run module upgrade to install failure sequences."
            ) % {"code": code})
        try:
            return int(raw)
        except (TypeError, ValueError):
            raise ValidationError(_(
                "Sequence %(code)s returned non-integer value: %(raw)r"
            ) % {"code": code, "raw": raw})

    @api.model
    def _build_video_url(self, attempt, sheet_type, video_filename):
        connector = attempt.video_s3_bucket
        bucket_url_prefix = ""
        if attempt.video_s3_url and attempt.video_s3_key:
            stored_url = attempt.video_s3_url
            stored_key = attempt.video_s3_key
            if stored_url.endswith(stored_key):
                bucket_url_prefix = stored_url[: -len(stored_key)]
        if not bucket_url_prefix and connector:
            bucket_url_prefix = f"https://{connector}.s3.amazonaws.com/"
        if not bucket_url_prefix:
            return ""
        if sheet_type == _SHEET_PASSED:
            return f"{bucket_url_prefix}{attempt.video_s3_key or ''}"
        existing_key = attempt.video_s3_key or ""
        new_key = existing_key
        if "/master/" in existing_key:
            new_key = existing_key.replace("/master/", "/failures/", 1)
            new_key = new_key.rsplit("/", 1)[0] + "/" + video_filename
        return f"{bucket_url_prefix}{new_key}"

    @api.model
    def create_from_review(self, review, sheet_type):
        if sheet_type not in (_SHEET_PASSED, _SHEET_FAILED):
            raise ValidationError(_(
                "Invalid sheet_type %(t)r"
            ) % {"t": sheet_type})

        existing = self.search([("review_id", "=", review.id)], limit=1)
        if existing:
            return existing

        attempt = review.attempt_id
        job = review.job_id
        if not attempt or not job:
            _logger.warning(
                "T2AV sheet-row: review %s missing attempt/job, skipping",
                review.id,
            )
            return self.browse()

        prompt = job.golden_prompt or job.enriched_prompt or job.prompt or ""
        visual, audio = self._split_visual_audio(prompt)
        resolution = _RESOLUTION_MAP.get(job.resolution or "", job.resolution or "")

        category = job.category or ""
        sub_category = getattr(job, "sub_category", "") or ""
        style = job.style or ""
        priority = job.priority or ""
        complexity = getattr(job, "complexity", "") or ""
        language = getattr(job, "language", None) or "English"
        topic = getattr(job, "topic", "") or ""
        speaker_count = getattr(job, "speaker_count", 0) or 0
        dialogue_transcript = getattr(job, "dialogue_transcript", "") or ""
        fps_value = attempt.fps or 24.0
        try:
            duration_seconds = float(attempt.duration or job.duration or 5)
        except (TypeError, ValueError):
            duration_seconds = 5.0
        contains_dialogue = category == "multi_speaker_dialogue"

        if sheet_type == _SHEET_PASSED:
            sequence_number = attempt.sequence_number or 0
            if sequence_number and category:
                item_id = f"T2AV_{category}_{sequence_number:07d}.mp4"
            else:
                item_id = attempt.video_file or f"review_{review.id}.mp4"
            video_url = self._build_video_url(attempt, _SHEET_PASSED, item_id)
            issues = ""
        else:
            if not category:
                raise ValidationError(_(
                    "Cannot create a failed sheet row without a category on job %(j)s"
                ) % {"j": job.id})
            sequence_number = self._allocate_failure_sequence(category)
            item_id = f"T2AV_{category}_Failure_{sequence_number:07d}.mp4"
            video_url = self._build_video_url(attempt, _SHEET_FAILED, item_id)
            issues = self._issues_from_findings(review.findings_json)

        vals = {
            "sheet_type": sheet_type,
            "review_id": review.id,
            "item_id": item_id,
            "sequence_number": sequence_number,
            "category": category,
            "sub_category": sub_category,
            "style": style,
            "priority": priority,
            "complexity": complexity,
            "language": language,
            "topic": topic,
            "prompt": prompt,
            "video_file": video_url,
            "duration_seconds": duration_seconds,
            "resolution": resolution,
            "fps": fps_value,
            "audio_description": audio,
            "contains_dialogue": contains_dialogue,
            "speaker_count": speaker_count,
            "dialogue_transcript": dialogue_transcript,
            "visual_description": visual,
            "issues": issues,
        }
        return self.create(vals)
