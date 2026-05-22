import base64

from odoo import api, fields, models
from odoo.exceptions import UserError


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    # -- Webhook (callback from Lambda back to Odoo) --
    vegeta_webhook_token = fields.Char(
        string="Webhook Token",
        config_parameter="vegeta.webhook_token",
        help="Shared secret the Lambda sends as X-Vegeta-Token / X-Leviathan-Token "
             "in the extraction-complete callback. If empty, falls back to "
             "VEGETA_WEBHOOK_TOKEN / LEVIATHAN_WEBHOOK_TOKEN env vars on the Odoo "
             "server. Rotate by updating here; no restart needed.",
    )
    vegeta_webhook_url_override = fields.Char(
        string="Webhook URL Override",
        config_parameter="vegeta.webhook_url_override",
        help="Full callback URL the Lambda will POST to. If empty, derived from "
             "web.base.url + /api/v1/vegeta/webhook/extraction-complete. Set this "
             "in local dev when web.base.url is localhost (use "
             "http://host.docker.internal:8069/api/v1/vegeta/webhook/extraction-complete).",
    )

    # -- Extraction Lambda (async invoke) --
    vegeta_lambda_function_name = fields.Char(
        string="Lambda Function Name",
        config_parameter="vegeta.lambda_function_name",
        help="AWS Lambda function name or full ARN for the extraction service. "
             "Used with boto3 lambda:Invoke (InvocationType=Event).",
    )
    vegeta_lambda_region = fields.Char(
        string="Lambda Region",
        config_parameter="vegeta.lambda_region",
        default="ap-south-1",
    )
    vegeta_lambda_local_url = fields.Char(
        string="Lambda Local URL",
        config_parameter="vegeta.lambda_local_url",
        help="If set, extraction requests POST to this URL instead of AWS Lambda. "
             "Used for local development against the AWS Lambda Runtime Interface "
             "Emulator (RIE), e.g. "
             "http://localhost:9000/2015-03-31/functions/function/invocations. "
             "Leave empty in production to use boto3 lambda:Invoke.",
    )
    vegeta_extraction_access_key_id = fields.Char(
        string="Extraction AWS Access Key ID",
        config_parameter="vegeta.extraction_access_key_id",
        help="Leave empty to use EKS pod IAM role (IRSA).",
    )
    vegeta_extraction_secret_access_key = fields.Char(
        string="Extraction AWS Secret Access Key",
        config_parameter="vegeta.extraction_secret_access_key",
    )
    vegeta_batch_concurrency = fields.Integer(
        string="Batch Concurrency",
        config_parameter="vegeta.batch_concurrency",
        default=250,
        help="Max parallel Lambda invocations per batch run. Must not exceed "
             "the Lambda's ReservedConcurrentExecutions setting.",
    )

    # -- Bedrock --
    vegeta_bedrock_inference_arn = fields.Char(
        string="Bedrock Inference ARN",
        config_parameter="vegeta.bedrock_inference_arn",
        help="e.g., arn:aws:bedrock:us-east-1:123456:inference-profile/...",
    )
    vegeta_bedrock_region = fields.Char(
        string="Bedrock Region",
        config_parameter="vegeta.bedrock_region",
        default="us-east-1",
    )
    vegeta_bedrock_access_key_id = fields.Char(
        string="Bedrock Access Key ID",
        config_parameter="vegeta.bedrock_access_key_id",
        help="Leave empty to use EKS pod IAM role (IRSA)",
    )
    vegeta_bedrock_secret_access_key = fields.Char(
        string="Bedrock Secret Access Key",
        config_parameter="vegeta.bedrock_secret_access_key",
    )
    vegeta_max_llm_attempts = fields.Integer(
        string="Max LLM Attempts",
        config_parameter="vegeta.max_llm_attempts",
    )

    # -- S3 --
    vegeta_s3_bucket = fields.Char(
        string="S3 Bucket Name",
        config_parameter="vegeta.s3_bucket",
    )
    vegeta_s3_access_key_id = fields.Char(
        string="S3 Access Key ID",
        config_parameter="vegeta.s3_access_key_id",
    )
    vegeta_s3_secret_access_key = fields.Char(
        string="S3 Secret Access Key",
        config_parameter="vegeta.s3_secret_access_key",
    )
    vegeta_s3_region = fields.Char(
        string="S3 Region",
        config_parameter="vegeta.s3_region",
    )
    vegeta_s3_folder = fields.Char(
        string="S3 Folder",
        config_parameter="vegeta.s3_folder",
        default="vegeta",
    )
    vegeta_s3_cdn_url = fields.Char(
        string="S3 CDN URL",
        config_parameter="vegeta.s3_cdn_url",
        help="e.g., https://cdn.example.com",
    )
    vegeta_s3_endpoint_url = fields.Char(
        string="S3 Endpoint URL",
        config_parameter="vegeta.s3_endpoint_url",
        help="Override the S3 endpoint URL for MinIO/LocalStack/etc. "
             "Leave empty for AWS S3. Example: http://localhost:9001",
    )

    # -- Prompts (file upload) --
    vegeta_prd_prompt_file = fields.Binary(
        string="PRD Prompt File (.md)",
        help="Upload a Markdown file to override the built-in PRD prompt.",
    )
    vegeta_prd_prompt_filename = fields.Char(string="PRD Prompt Filename")
    vegeta_prd_prompt_status = fields.Char(
        string="PRD Prompt Status", compute="_compute_vegeta_prompt_status",
    )
    vegeta_qc_prompt_file = fields.Binary(
        string="QC Prompt File (.md)",
        help="Upload a Markdown file to override the built-in QC prompt.",
    )
    vegeta_qc_prompt_filename = fields.Char(string="QC Prompt Filename")
    vegeta_qc_prompt_status = fields.Char(
        string="QC Prompt Status", compute="_compute_vegeta_prompt_status",
    )

    # -- Limits --
    vegeta_max_jobs_per_user = fields.Integer(
        string="Max Active Jobs per Tasker",
        config_parameter="vegeta.max_jobs_per_user",
        default=5,
        help="Maximum active tasks (draft + extracting + generating + scoring + done) per tasker. 0 = unlimited.",
    )

    @api.depends("vegeta_prd_prompt_filename", "vegeta_qc_prompt_filename")
    def _compute_vegeta_prompt_status(self):
        ICP = self.env["ir.config_parameter"].sudo()
        prd = ICP.get_param("vegeta.prd_system_prompt", "")
        prd_name = ICP.get_param("vegeta.prd_prompt_filename", "")
        qc = ICP.get_param("vegeta.qc_system_prompt", "")
        qc_name = ICP.get_param("vegeta.qc_prompt_filename", "")
        for rec in self:
            if prd and prd.strip():
                rec.vegeta_prd_prompt_status = f"Custom prompt active: {prd_name}" if prd_name else "Custom prompt active"
            else:
                rec.vegeta_prd_prompt_status = "Using built-in default"
            if qc and qc.strip():
                rec.vegeta_qc_prompt_status = f"Custom prompt active: {qc_name}" if qc_name else "Custom prompt active"
            else:
                rec.vegeta_qc_prompt_status = "Using built-in default"

    def get_values(self):
        res = super().get_values()
        ICP = self.env["ir.config_parameter"].sudo()
        # Prompt file names (so UI shows current filename)
        res["vegeta_prd_prompt_filename"] = ICP.get_param(
            "vegeta.prd_prompt_filename", default=""
        )
        res["vegeta_qc_prompt_filename"] = ICP.get_param(
            "vegeta.qc_prompt_filename", default=""
        )
        # Don't load binary into form — just show filename
        res["vegeta_prd_prompt_file"] = False
        res["vegeta_qc_prompt_file"] = False
        return res

    def set_values(self):
        # Guard before super() so the bad config never lands in ICP. Batch
        # dispatch with no function name AND no local URL is silently broken
        # at runtime — every batch click fails late. Reject at save time.
        if (self.vegeta_batch_concurrency or 0) > 0 and not (
            (self.vegeta_lambda_function_name or "").strip()
            or (self.vegeta_lambda_local_url or "").strip()
        ):
            raise UserError(
                "Batch concurrency is configured but neither Lambda Function "
                "Name nor Lambda Local URL is set. Batch dispatch would fail. "
                "Set one of these or set Batch Concurrency to 0."
            )
        super().set_values()
        ICP = self.env["ir.config_parameter"].sudo()

        if self.vegeta_prd_prompt_file:
            try:
                content = base64.b64decode(self.vegeta_prd_prompt_file).decode("utf-8")
            except UnicodeDecodeError as exc:
                raise UserError(
                    "PRD prompt file must be UTF-8 encoded text. "
                    f"Decode failed at byte {exc.start}: {exc.reason}"
                ) from exc
            ICP.set_param("vegeta.prd_system_prompt", content)
            ICP.set_param(
                "vegeta.prd_prompt_filename",
                self.vegeta_prd_prompt_filename or "prd_prompt.md",
            )

        if self.vegeta_qc_prompt_file:
            try:
                content = base64.b64decode(self.vegeta_qc_prompt_file).decode("utf-8")
            except UnicodeDecodeError as exc:
                raise UserError(
                    "QC prompt file must be UTF-8 encoded text. "
                    f"Decode failed at byte {exc.start}: {exc.reason}"
                ) from exc
            ICP.set_param("vegeta.qc_system_prompt", content)
            ICP.set_param(
                "vegeta.qc_prompt_filename",
                self.vegeta_qc_prompt_filename or "qc_prompt.md",
            )

    def action_clear_prd_prompt(self):
        """Reset PRD prompt to built-in default."""
        ICP = self.env["ir.config_parameter"].sudo()
        ICP.set_param("vegeta.prd_system_prompt", "")
        ICP.set_param("vegeta.prd_prompt_filename", "")

    def action_clear_qc_prompt(self):
        """Reset QC prompt to built-in default."""
        ICP = self.env["ir.config_parameter"].sudo()
        ICP.set_param("vegeta.qc_system_prompt", "")
        ICP.set_param("vegeta.qc_prompt_filename", "")
