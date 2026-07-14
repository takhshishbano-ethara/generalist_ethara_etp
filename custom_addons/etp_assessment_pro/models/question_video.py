# -*- coding: utf-8 -*-
"""Video attachments for video-evaluation questions; ``slot`` identifies each clip.

Videos get their OWN model (not question.image): mp4 is large and streams from a
URL/CDN (``video_url`` preferred), with a small Binary as a dev-only fallback.
"""
from odoo import api, models, fields


class EtpAssessmentQuestionVideo(models.Model):
    _name = "etp.assessment.pro.question.video"
    _description = "Assessment Question Video"
    _order = "sequence, id"

    question_id = fields.Many2one(
        "etp.assessment.pro.question",
        string="Question",
        required=True,
        ondelete="cascade",
    )
    label = fields.Char(
        string="Label",
        help="Shown to the candidate, e.g. 'Reference', 'Output'.")
    slot = fields.Selection(
        [
            ("reference", "Reference"),
            ("output", "Output"),
            ("single", "Single"),
        ],
        string="Slot",
        default="single",
        help="video_prompt uses Reference + Output; Single for a lone clip.")
    video_url = fields.Char(
        string="Video URL",
        help="S3/CDN URL, the preferred storage. When set, the portal streams "
             "this instead of the stored binary.")
    video = fields.Binary(
        string="Video", attachment=True,
        help="Small/dev fallback; production stores the clip at video_url.")
    video_filename = fields.Char(string="Video Filename")
    poster_url = fields.Char(
        string="Poster URL",
        help="Optional still frame shown before the clip plays.")
    duration_s = fields.Float(string="Duration (s)")
    has_audio = fields.Boolean(string="Has Audio")
    sequence = fields.Integer(default=10)

    @api.depends("label", "slot")
    def _compute_display_name(self):
        for rec in self:
            rec.display_name = rec.label or dict(
                self._fields["slot"].selection).get(rec.slot, "Video")
