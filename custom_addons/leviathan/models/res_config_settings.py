import base64

from odoo import api, fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    # -- Extraction Service --
    leviathan_extraction_service_url = fields.Char(
        string="Extraction Service URL",
        config_parameter="leviathan.extraction_service_url",
        help="Base URL of the external Leviathan extraction microservice",
    )
    leviathan_extraction_access_key_id = fields.Char(
        string="Extraction AWS Access Key ID",
        config_parameter="leviathan.extraction_access_key_id",
    )
    leviathan_extraction_secret_access_key = fields.Char(
        string="Extraction AWS Secret Access Key",
        config_parameter="leviathan.extraction_secret_access_key",
    )

    # -- Bedrock --
    leviathan_bedrock_inference_arn = fields.Char(
        string="Bedrock Inference ARN",
        config_parameter="leviathan.bedrock_inference_arn",
        help="e.g., arn:aws:bedrock:us-east-1:123456:inference-profile/...",
    )
    leviathan_bedrock_region = fields.Char(
        string="Bedrock Region",
        config_parameter="leviathan.bedrock_region",
        default="us-east-1",
    )
    leviathan_bedrock_access_key_id = fields.Char(
        string="Bedrock Access Key ID",
        config_parameter="leviathan.bedrock_access_key_id",
        help="Leave empty to use EKS pod IAM role (IRSA)",
    )
    leviathan_bedrock_secret_access_key = fields.Char(
        string="Bedrock Secret Access Key",
        config_parameter="leviathan.bedrock_secret_access_key",
    )
    leviathan_max_llm_attempts = fields.Integer(
        string="Max LLM Attempts",
        config_parameter="leviathan.max_llm_attempts",
    )

    # -- S3 --
    leviathan_s3_bucket = fields.Char(
        string="S3 Bucket Name",
        config_parameter="leviathan.s3_bucket",
    )
    leviathan_s3_access_key_id = fields.Char(
        string="S3 Access Key ID",
        config_parameter="leviathan.s3_access_key_id",
    )
    leviathan_s3_secret_access_key = fields.Char(
        string="S3 Secret Access Key",
        config_parameter="leviathan.s3_secret_access_key",
    )
    leviathan_s3_region = fields.Char(
        string="S3 Region",
        config_parameter="leviathan.s3_region",
    )
    leviathan_s3_folder = fields.Char(
        string="S3 Folder",
        config_parameter="leviathan.s3_folder",
        default="leviathan",
    )
    leviathan_s3_cdn_url = fields.Char(
        string="S3 CDN URL",
        config_parameter="leviathan.s3_cdn_url",
        help="e.g., https://cdn.example.com",
    )

    # -- Prompts (file upload) --
    leviathan_prd_prompt_file = fields.Binary(
        string="PRD Prompt File (.md)",
        help="Upload a Markdown file to override the built-in PRD prompt.",
    )
    leviathan_prd_prompt_filename = fields.Char(string="PRD Prompt Filename")
    leviathan_qc_prompt_file = fields.Binary(
        string="QC Prompt File (.md)",
        help="Upload a Markdown file to override the built-in QC prompt.",
    )
    leviathan_qc_prompt_filename = fields.Char(string="QC Prompt Filename")

    # -- Limits --
    leviathan_max_jobs_per_user = fields.Integer(
        string="Max Active Jobs per Tasker",
        config_parameter="leviathan.max_jobs_per_user",
        default=5,
        help="Maximum active tasks (draft + extracting + generating + scoring + done) per tasker. 0 = unlimited.",
    )

    def get_values(self):
        res = super().get_values()
        ICP = self.env["ir.config_parameter"].sudo()
        # Prompt file names (so UI shows current filename)
        res["leviathan_prd_prompt_filename"] = ICP.get_param(
            "leviathan.prd_prompt_filename", default=""
        )
        res["leviathan_qc_prompt_filename"] = ICP.get_param(
            "leviathan.qc_prompt_filename", default=""
        )
        # Don't load binary into form — just show filename
        res["leviathan_prd_prompt_file"] = False
        res["leviathan_qc_prompt_file"] = False
        return res

    def set_values(self):
        super().set_values()
        ICP = self.env["ir.config_parameter"].sudo()

        # PRD prompt file upload
        if self.leviathan_prd_prompt_file:
            content = base64.b64decode(self.leviathan_prd_prompt_file).decode(
                "utf-8", errors="replace"
            )
            ICP.set_param("leviathan.prd_system_prompt", content)
            ICP.set_param(
                "leviathan.prd_prompt_filename",
                self.leviathan_prd_prompt_filename or "prd_prompt.md",
            )

        # QC prompt file upload
        if self.leviathan_qc_prompt_file:
            content = base64.b64decode(self.leviathan_qc_prompt_file).decode(
                "utf-8", errors="replace"
            )
            ICP.set_param("leviathan.qc_system_prompt", content)
            ICP.set_param(
                "leviathan.qc_prompt_filename",
                self.leviathan_qc_prompt_filename or "qc_prompt.md",
            )

    def action_clear_prd_prompt(self):
        """Reset PRD prompt to built-in default."""
        ICP = self.env["ir.config_parameter"].sudo()
        ICP.set_param("leviathan.prd_system_prompt", "")
        ICP.set_param("leviathan.prd_prompt_filename", "")

    def action_clear_qc_prompt(self):
        """Reset QC prompt to built-in default."""
        ICP = self.env["ir.config_parameter"].sudo()
        ICP.set_param("leviathan.qc_system_prompt", "")
        ICP.set_param("leviathan.qc_prompt_filename", "")
