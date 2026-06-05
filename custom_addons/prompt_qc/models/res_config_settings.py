# -*- coding: utf-8 -*-
import base64

from odoo import models, fields


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    # The ONE universal judge system prompt (.md). Uploaded here, applied to every QC run. Stored
    # decoded as ir.config_parameter 'prompt_qc.system_prompt_text'; the Binary widget is just an
    # upload/download surface, reconstructed from that text on load (text is the single source).
    prompt_qc_system_prompt_file = fields.Binary(string="System Prompt (.md)")
    prompt_qc_system_prompt_filename = fields.Char(string="System Prompt Filename")

    prompt_qc_bedrock_region = fields.Char(
        string="Prompt QC: Bedrock Region",
        config_parameter="prompt_qc.bedrock_region",
        default="ap-south-1",
    )
    prompt_qc_bedrock_inference_arn = fields.Char(
        string="Prompt QC: Bedrock Inference ARN",
        config_parameter="prompt_qc.bedrock_inference_arn",
        help="Inference-profile ARN that selects the judge model. The model is chosen by "
             "this ARN, not hardcoded.",
    )
    prompt_qc_bedrock_api_key = fields.Char(
        string="Prompt QC: Bedrock API Key (optional)",
        config_parameter="prompt_qc.bedrock_api_key",
        help="Optional. If empty, the bearer token is read from AWS_BEARER_TOKEN_BEDROCK "
             "in the .env file next to odoo.conf.",
    )

    # --- Bulk Import (Stage 2) ------------------------------------------------------------
    prompt_qc_run_timeout_minutes = fields.Integer(
        string="Prompt QC: Run Timeout (minutes)",
        config_parameter="prompt_qc.run_timeout_minutes",
        default=15,
        help="A run sitting in 'running' longer than this is forced to 'failed' by the reaper "
             "cron, so no run is ever stuck.",
    )
    prompt_qc_max_file_size_mb = fields.Integer(
        string="Prompt QC: Max Upload Size (MB)",
        config_parameter="prompt_qc.max_file_size_mb",
        default=1,
        help="Maximum size of each uploaded rubric (.json) or system prompt (.md) file.",
    )

    # ----- Universal system prompt: persisted by hand (Binary is not a config_parameter) -----
    def get_values(self):
        res = super().get_values()
        ICP = self.env["ir.config_parameter"].sudo()
        text = ICP.get_param("prompt_qc.system_prompt_text") or ""
        filename = ICP.get_param("prompt_qc.system_prompt_filename") or ""
        # Reconstruct the upload widget's file from the stored text (the source of truth).
        file_val = base64.b64encode(text.encode("utf-8")).decode("ascii") if text else False
        res.update(
            prompt_qc_system_prompt_file=file_val,
            prompt_qc_system_prompt_filename=filename or False,
        )
        return res

    def set_values(self):
        super().set_values()
        ICP = self.env["ir.config_parameter"].sudo()
        raw_b64 = self.prompt_qc_system_prompt_file
        if raw_b64:
            # get_values reconstructs this widget from the stored text, so an untouched form posts
            # back the exact same base64. Skip re-validation in that case — otherwise lowering the
            # max upload size would block saving ANY setting, because the unchanged stored prompt
            # would be re-validated against the new (lower) cap and raise.
            stored_text = ICP.get_param("prompt_qc.system_prompt_text") or ""
            incoming_b64 = raw_b64.decode("ascii") if isinstance(raw_b64, bytes) else raw_b64
            stored_b64 = (base64.b64encode(stored_text.encode("utf-8")).decode("ascii")
                          if stored_text else "")
            if stored_text and incoming_b64 == stored_b64:
                # Unchanged file: only (re)store the filename; never re-validate the bytes.
                ICP.set_param("prompt_qc.system_prompt_filename",
                              self.prompt_qc_system_prompt_filename or "system_prompt.md")
                return
            max_bytes = self.env["prompt.qc.run"]._get_max_file_size_mb() * 1024 * 1024
            # Validates extension/size/UTF-8 and raises UserError (blocking the save) on a bad file.
            text = self.env["prompt.qc.run"]._decode_md_text(
                raw_b64, self.prompt_qc_system_prompt_filename, max_bytes)
            ICP.set_param("prompt_qc.system_prompt_text", text)
            ICP.set_param("prompt_qc.system_prompt_filename",
                          self.prompt_qc_system_prompt_filename or "system_prompt.md")
        else:
            # Upload cleared -> fall back to the packaged seed prompt for every run.
            ICP.set_param("prompt_qc.system_prompt_text", "")
            ICP.set_param("prompt_qc.system_prompt_filename", "")
