# -*- coding: utf-8 -*-
"""Wizard for attaching / refining a prompt against a version."""

from odoo import _, api, fields, models


class VideoPromptWizard(models.TransientModel):
    _name = "video.prompt.wizard"
    _description = "Attach Prompt to Video Version"

    version_id = fields.Many2one("video.task.version", required=True)
    prompt_text = fields.Text(required=True)
    prompt_response = fields.Text()

    @api.model
    def default_get(self, fields_list):
        values = super().default_get(fields_list)
        active_id = self.env.context.get("active_id")
        active_model = self.env.context.get("active_model")
        if active_model == "video.task.version" and active_id:
            version = self.env["video.task.version"].browse(active_id)
            values["version_id"] = version.id
            values.setdefault("prompt_text", version.prompt_text or "")
            values.setdefault("prompt_response", version.prompt_response or "")
        return values

    def action_save(self):
        self.ensure_one()
        self.version_id.write(
            {"prompt_text": self.prompt_text, "prompt_response": self.prompt_response}
        )
        self.version_id.message_post(body=_("Prompt updated."))
        return {"type": "ir.actions.act_window_close"}
