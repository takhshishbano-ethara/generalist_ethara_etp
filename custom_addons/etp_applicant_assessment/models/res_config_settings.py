from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    eaa_llm_qc_prompt = fields.Char(
        string='Assessment LLM QC Seed Prompt',
        config_parameter='etp_applicant_assessment.llm_qc_prompt',
    )
    eaa_llm_api_key = fields.Char(
        string="LLM API Key",
        config_parameter="ethara_hrms.llm_api_key",
    )
    eaa_llm_base_url = fields.Char(
        string="LLM Base URL",
        config_parameter="ethara_hrms.llm_base_url",
        default="https://api.groq.com/openai/v1",
    )
    eaa_llm_model = fields.Char(
        string="LLM Model",
        config_parameter="ethara_hrms.llm_model",
        default="llama-3.3-70b-versatile",
    )
    eaa_llm_timeout = fields.Integer(
        string="LLM Timeout (seconds)",
        config_parameter="ethara_hrms.llm_timeout",
        default=60,
    )
