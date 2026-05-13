# -*- coding: utf-8 -*-
"""Granular audit row for a single editing action on a version."""

from odoo import fields, models


class VideoTaskEditHistory(models.Model):
    _name = "video.task.edit.history"
    _description = "Video Task Edit History"
    _order = "create_date desc, id desc"

    version_id = fields.Many2one(
        "video.task.version",
        string="Version",
        required=True,
        ondelete="cascade",
        index=True,
    )
    task_id = fields.Many2one(
        "video.task",
        related="version_id.task_id",
        store=True,
        index=True,
    )

    action_type = fields.Selection(
        [
            ("trim", "Trim"),
            ("crop", "Crop"),
            ("rotate", "Rotate"),
            ("resize", "Resize"),
            ("mute", "Mute / Unmute"),
            ("brightness", "Brightness"),
            ("contrast", "Contrast"),
            ("saturation", "Saturation"),
            ("export", "Export / Render"),
            ("other", "Other"),
        ],
        required=True,
    )
    action_data = fields.Text(string="Parameters (JSON)")
    notes = fields.Char(string="Notes")
    created_by = fields.Many2one(
        "res.users",
        string="By",
        default=lambda self: self.env.user,
        readonly=True,
    )
    created_on = fields.Datetime(default=fields.Datetime.now, readonly=True)
