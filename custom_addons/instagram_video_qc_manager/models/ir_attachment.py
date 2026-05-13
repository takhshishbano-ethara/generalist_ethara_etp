# -*- coding: utf-8 -*-
"""Lightweight ir.attachment extension to track which version an asset belongs to."""

from odoo import fields, models


class IrAttachment(models.Model):
    _inherit = "ir.attachment"

    video_task_id = fields.Many2one(
        "video.task",
        string="Video Task",
        index=True,
        ondelete="set null",
    )
    video_version_id = fields.Many2one(
        "video.task.version",
        string="Video Version",
        index=True,
        ondelete="set null",
    )
    video_asset_kind = fields.Selection(
        [
            ("original", "Original"),
            ("edited", "Edited"),
            ("preview", "Preview"),
            ("thumbnail", "Thumbnail"),
        ],
        string="Video Asset Kind",
    )
