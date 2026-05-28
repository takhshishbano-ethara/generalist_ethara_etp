# -*- coding: utf-8 -*-
from odoo import fields, models


class VideoEditorProcessingLog(models.Model):
    _name = "video.editor.processing.log"
    _description = "Video Editor S3 processing log"
    _order = "id desc"
    _rec_name = "operation"

    project_id = fields.Many2one(
        "video.editor.project",
        string="Project",
        ondelete="cascade",
        index=True,
    )
    job_id = fields.Many2one(
        "video.editor.job",
        string="Job",
        ondelete="cascade",
        index=True,
    )
    level = fields.Selection(
        [("info", "Info"), ("warning", "Warning"), ("error", "Error")],
        default="info",
        required=True,
    )
    operation = fields.Char(string="Operation", required=True, index=True)
    message = fields.Text(string="Message")
    duration_ms = fields.Integer(string="Duration (ms)")
    ffmpeg_command = fields.Text(string="Command")
