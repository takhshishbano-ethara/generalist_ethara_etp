# -*- coding: utf-8 -*-
"""Long-form processing log captured by the FFmpeg / downloader pipeline."""

from odoo import fields, models


class VideoTaskProcessingLog(models.Model):
    _name = "video.task.processing.log"
    _description = "Video Task Processing Log"
    _order = "create_date desc, id desc"

    task_id = fields.Many2one(
        "video.task",
        string="Task",
        ondelete="cascade",
        required=True,
        index=True,
    )
    version_id = fields.Many2one(
        "video.task.version",
        string="Version",
        ondelete="set null",
    )
    level = fields.Selection(
        [
            ("info", "Info"),
            ("warning", "Warning"),
            ("error", "Error"),
        ],
        default="info",
        required=True,
    )
    operation = fields.Selection(
        [
            ("download", "Download"),
            ("probe", "Probe"),
            ("render", "Render"),
            ("preview", "Preview"),
            ("thumbnail", "Thumbnail"),
            ("cleanup", "Cleanup"),
            ("other", "Other"),
        ],
        default="other",
        required=True,
    )
    message = fields.Text(required=True)
    duration_ms = fields.Integer(string="Duration (ms)")
    ffmpeg_command = fields.Text()
