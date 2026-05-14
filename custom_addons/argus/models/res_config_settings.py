# -*- coding: utf-8 -*-
"""Argus Settings-page integration.

Surfaces the five ``argus.*`` ``ir.config_parameter`` keys as proper
form fields under Settings → Argus → Kimi K2.5 Configuration so an
operator can fill them in via the UI instead of hand-crafting rows
under Settings → Technical → Parameters → System Parameters.

Three Char fields and an Integer use the ``config_parameter=``
shortcut.  The fifth, ``argus_qc_system_prompt``, is a ``Text`` field
— res.config.settings' shortcut machinery REJECTS ``Text`` (only
boolean / integer / float / char / selection / many2one / datetime
are accepted, see ``base/models/res_config.py::_get_classified_fields``).
For that one we read/write the underlying ``argus.qc_system_prompt``
``ir.config_parameter`` row ourselves via the ``get_values`` /
``set_values`` override at the bottom of this file.
"""

from odoo import api, fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    argus_kimi_api_key = fields.Char(
        string="Bedrock API Key",
        config_parameter="argus.kimi_api_key",
    )
    argus_kimi_model_arn = fields.Char(
        string="Bedrock Model ARN",
        config_parameter="argus.kimi_model_arn",
        help=(
            "AWS Bedrock inference profile or foundation-model ARN "
            "for Kimi K2.5.  Example: "
            "``arn:aws:bedrock:us-east-1:000000000000:inference-profile/...``"
        ),
    )
    argus_kimi_aws_region = fields.Char(
        string="AWS Region",
        config_parameter="argus.kimi_aws_region",
        default="us-east-1",
        help="AWS region your Bedrock model is deployed in.",
    )
    argus_grammar_score_threshold = fields.Integer(
        string="Approve Threshold (0-100)",
        config_parameter="argus.grammar_score_threshold",
        default=100,
        help=(
            "Score gate for the QC Prompt verdict.  When Kimi returns "
            "a score >= this value, the task is auto-approved; below "
            "it, the task is auto-rejected and the red feedback "
            "banner is shown.\n\n"
            "Default is 100 — strict mode, only a perfect grammar "
            "score auto-approves.  Lower the threshold (e.g. 80) if "
            "you want a more lenient gate."
        ),
    )
    # NOTE: ``Text`` fields cannot use the ``config_parameter=``
    # shortcut on res.config.settings — the framework only accepts
    # boolean / integer / float / char / selection / many2one /
    # datetime there (see base/models/res_config.py:222).  We handle
    # this one manually via get_values / set_values below.
    argus_qc_system_prompt = fields.Text(
        string="QC System Prompt",
        help=(
            "Instructions sent to Kimi as the system prompt for the "
            "QC Prompt grammar check.  Leave blank to use the built-"
            "in default.\n\n"
            "IMPORTANT: whatever you write here MUST instruct the "
            "model to respond with a JSON object containing the keys "
            "``score`` (0-100 int), ``level`` (excellent / good / "
            "fair / poor), ``feedback`` (one paragraph), ``issues`` "
            "(list of strings), and ``corrected_text`` (string).  "
            "If you break that contract, response parsing will fail."
        ),
    )

    # ------------------------------------------------------------------
    # Manual ir.config_parameter round-trip for the Text field.
    # The other four fields above ride the standard
    # ``config_parameter=`` shortcut; this one can't.
    # ------------------------------------------------------------------
    @api.model
    def get_values(self):
        res = super().get_values()
        res["argus_qc_system_prompt"] = (
            self.env["ir.config_parameter"]
            .sudo()
            .get_param("argus.qc_system_prompt", "")
        ) or ""
        return res

    def set_values(self):
        super().set_values()
        self.env["ir.config_parameter"].sudo().set_param(
            "argus.qc_system_prompt",
            (self.argus_qc_system_prompt or "").strip(),
        )
