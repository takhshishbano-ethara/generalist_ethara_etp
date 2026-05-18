import base64

from odoo import api, fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    # -- Spec-compliant API Gateway path (HANDOFF_ODOO.md §4.4 / §4.5) --
    # Optional alternative trigger: action_run_pipeline() POSTs to
    # {lambda_api_url}/run with X-Api-Key; callbacks land on /gohan/webhook
    # with X-Gohan-Signature (hex HMAC-SHA256 of body using hmac_secret).
    gohan_lambda_api_url = fields.Char(
        string="API Gateway Base URL",
        config_parameter="gohan.lambda_api_url",
        help="API Gateway base URL fronting the Gohan Lambda. The spec-aligned "
             "action_run_pipeline() POSTs to {this}/run. Leave empty to disable "
             "the API-Gateway trigger and use the direct boto3 invoke path.",
    )
    gohan_lambda_api_key = fields.Char(
        string="API Gateway X-Api-Key",
        config_parameter="gohan.lambda_api_key",
        help="Value passed in the X-Api-Key header when calling the API "
             "Gateway. Provisioned in AWS API Gateway → Usage Plans.",
    )
    gohan_hmac_secret = fields.Char(
        string="Webhook HMAC Secret",
        config_parameter="gohan.hmac_secret",
        help="Shared secret used by the pipeline to sign /gohan/webhook "
             "callbacks (hex HMAC-SHA256 of the raw body in the "
             "X-Gohan-Signature header). Must match the value baked into "
             "the Lambda's environment.",
    )

    # -- Extraction Lambda (async invoke) --
    gohan_lambda_function_name = fields.Char(
        string="Lambda Function Name",
        config_parameter="gohan.lambda_function_name",
        help="AWS Lambda function name or full ARN for the extraction service. "
             "Used with boto3 lambda:Invoke (InvocationType=Event).",
    )
    gohan_lambda_region = fields.Char(
        string="Lambda Region",
        config_parameter="gohan.lambda_region",
        default="ap-south-1",
    )
    gohan_extraction_access_key_id = fields.Char(
        string="Extraction AWS Access Key ID",
        config_parameter="gohan.extraction_access_key_id",
        help="Leave empty to use EKS pod IAM role (IRSA).",
    )
    gohan_extraction_secret_access_key = fields.Char(
        string="Extraction AWS Secret Access Key",
        config_parameter="gohan.extraction_secret_access_key",
    )
    gohan_batch_concurrency = fields.Integer(
        string="Batch Concurrency",
        config_parameter="gohan.batch_concurrency",
        default=250,
        help="Max parallel Lambda invocations per batch run. Must not exceed "
             "the Lambda's ReservedConcurrentExecutions setting.",
    )

    # -- Bedrock --
    gohan_bedrock_inference_arn = fields.Char(
        string="Bedrock Inference ARN",
        config_parameter="gohan.bedrock_inference_arn",
        help="e.g., arn:aws:bedrock:us-east-1:123456:inference-profile/...",
    )
    gohan_bedrock_region = fields.Char(
        string="Bedrock Region",
        config_parameter="gohan.bedrock_region",
        default="us-east-1",
    )
    gohan_bedrock_access_key_id = fields.Char(
        string="Bedrock Access Key ID",
        config_parameter="gohan.bedrock_access_key_id",
        help="Leave empty to use EKS pod IAM role (IRSA)",
    )
    gohan_bedrock_secret_access_key = fields.Char(
        string="Bedrock Secret Access Key",
        config_parameter="gohan.bedrock_secret_access_key",
    )
    gohan_max_llm_attempts = fields.Integer(
        string="Max LLM Attempts",
        config_parameter="gohan.max_llm_attempts",
    )

    # -- S3 --
    gohan_s3_bucket = fields.Char(
        string="S3 Bucket Name",
        config_parameter="gohan.s3_bucket",
    )
    gohan_s3_access_key_id = fields.Char(
        string="S3 Access Key ID",
        config_parameter="gohan.s3_access_key_id",
    )
    gohan_s3_secret_access_key = fields.Char(
        string="S3 Secret Access Key",
        config_parameter="gohan.s3_secret_access_key",
    )
    gohan_s3_region = fields.Char(
        string="S3 Region",
        config_parameter="gohan.s3_region",
        default="us-east-1",
    )
    gohan_s3_folder = fields.Char(
        string="S3 Folder",
        config_parameter="gohan.s3_folder",
        default="gohan",
    )
    gohan_s3_cdn_url = fields.Char(
        string="S3 CDN URL",
        config_parameter="gohan.s3_cdn_url",
        help="e.g., https://cdn.example.com",
    )

    # -- Prompts (file upload) --
    gohan_prd_prompt_file = fields.Binary(
        string="PRD Prompt File (.md)",
        help="Upload a Markdown file to override the built-in PRD prompt.",
    )
    gohan_prd_prompt_filename = fields.Char(string="PRD Prompt Filename")
    gohan_prd_prompt_status = fields.Char(
        string="PRD Prompt Status", compute="_compute_prompt_status",
    )
    gohan_qc_prompt_file = fields.Binary(
        string="QC Prompt File (.md)",
        help="Upload a Markdown file to override the built-in QC prompt.",
    )
    gohan_qc_prompt_filename = fields.Char(string="QC Prompt Filename")
    gohan_qc_prompt_status = fields.Char(
        string="QC Prompt Status", compute="_compute_prompt_status",
    )

    # -- Limits --
    gohan_max_jobs_per_user = fields.Integer(
        string="Max Active Jobs per Tasker",
        config_parameter="gohan.max_jobs_per_user",
        default=5,
        help="Maximum active tasks (draft + extracting + generating + scoring + done) per tasker. 0 = unlimited.",
    )

    def _compute_prompt_status(self):
        ICP = self.env["ir.config_parameter"].sudo()
        prd = ICP.get_param("gohan.prd_system_prompt", "")
        prd_name = ICP.get_param("gohan.prd_prompt_filename", "")
        qc = ICP.get_param("gohan.qc_system_prompt", "")
        qc_name = ICP.get_param("gohan.qc_prompt_filename", "")
        for rec in self:
            if prd and prd.strip():
                rec.gohan_prd_prompt_status = f"Custom prompt active: {prd_name}" if prd_name else "Custom prompt active"
            else:
                rec.gohan_prd_prompt_status = "Using built-in default"
            if qc and qc.strip():
                rec.gohan_qc_prompt_status = f"Custom prompt active: {qc_name}" if qc_name else "Custom prompt active"
            else:
                rec.gohan_qc_prompt_status = "Using built-in default"

    def get_values(self):
        res = super().get_values()
        ICP = self.env["ir.config_parameter"].sudo()
        # Prompt file names (so UI shows current filename)
        res["gohan_prd_prompt_filename"] = ICP.get_param(
            "gohan.prd_prompt_filename", default=""
        )
        res["gohan_qc_prompt_filename"] = ICP.get_param(
            "gohan.qc_prompt_filename", default=""
        )
        # Don't load binary into form — just show filename
        res["gohan_prd_prompt_file"] = False
        res["gohan_qc_prompt_file"] = False
        return res

    def set_values(self):
        super().set_values()
        ICP = self.env["ir.config_parameter"].sudo()

        # PRD prompt file upload
        if self.gohan_prd_prompt_file:
            content = base64.b64decode(self.gohan_prd_prompt_file).decode(
                "utf-8", errors="replace"
            )
            ICP.set_param("gohan.prd_system_prompt", content)
            ICP.set_param(
                "gohan.prd_prompt_filename",
                self.gohan_prd_prompt_filename or "prd_prompt.md",
            )

        # QC prompt file upload
        if self.gohan_qc_prompt_file:
            content = base64.b64decode(self.gohan_qc_prompt_file).decode(
                "utf-8", errors="replace"
            )
            ICP.set_param("gohan.qc_system_prompt", content)
            ICP.set_param(
                "gohan.qc_prompt_filename",
                self.gohan_qc_prompt_filename or "qc_prompt.md",
            )

    def action_clear_prd_prompt(self):
        """Reset PRD prompt to built-in default."""
        ICP = self.env["ir.config_parameter"].sudo()
        ICP.set_param("gohan.prd_system_prompt", "")
        ICP.set_param("gohan.prd_prompt_filename", "")

    def action_clear_qc_prompt(self):
        """Reset QC prompt to built-in default."""
        ICP = self.env["ir.config_parameter"].sudo()
        ICP.set_param("gohan.qc_system_prompt", "")
        ICP.set_param("gohan.qc_prompt_filename", "")
