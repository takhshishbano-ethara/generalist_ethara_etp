import base64

from odoo import api, fields, models


SENSITIVE_FIELDS = {
    "gohan_webhook_token":                "gohan.webhook_token",
    "gohan_lambda_api_key":               "gohan.lambda_api_key",
    "gohan_hmac_secret":                  "gohan.hmac_secret",
    "gohan_lambda_function_name":         "gohan.lambda_function_name",
    "gohan_extraction_access_key_id":     "gohan.extraction_access_key_id",
    "gohan_extraction_secret_access_key": "gohan.extraction_secret_access_key",
    "gohan_bedrock_inference_arn":        "gohan.bedrock_inference_arn",
    "gohan_bedrock_access_key_id":        "gohan.bedrock_access_key_id",
    "gohan_bedrock_secret_access_key":    "gohan.bedrock_secret_access_key",
    "gohan_s3_access_key_id":             "gohan.s3_access_key_id",
    "gohan_s3_secret_access_key":         "gohan.s3_secret_access_key",
}


def _mask_status(value):
    if not value:
        return "Not set"
    if len(value) <= 4:
        return "Configured (••••)"
    return f"Configured (••••{value[-4:]})"


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
    # Sensitive fields below intentionally OMIT ``config_parameter=`` so the
    # framework cannot auto-load the stored value into the form. Reads happen
    # in ``get_values`` (always blank); writes happen in ``set_values`` (only
    # when the field is non-empty). The ``_status`` siblings render a masked
    # "Configured (••••last4)" badge so operators can confirm a value exists
    # without exposing it.
    gohan_lambda_api_key = fields.Char(
        string="API Gateway X-Api-Key",
        help="Rotate-only: leave blank to keep the existing value. Set in "
             "AWS API Gateway → Usage Plans.",
    )
    gohan_lambda_api_key_status = fields.Char(
        string="API Key Status", compute="_compute_sensitive_status",
    )
    gohan_hmac_secret = fields.Char(
        string="Webhook HMAC Secret",
        help="Rotate-only. Shared secret used by the pipeline to sign "
             "/gohan/webhook callbacks (hex HMAC-SHA256 of the raw body in the "
             "X-Gohan-Signature header). Must match the value baked into the "
             "Lambda's environment.",
    )
    gohan_hmac_secret_status = fields.Char(
        string="HMAC Secret Status", compute="_compute_sensitive_status",
    )
    gohan_webhook_token = fields.Char(
        string="Legacy Webhook Token (X-Gohan-Token)",
        help="Shared token verified by the legacy /api/v1/gohan/webhook/* "
             "endpoints (X-Gohan-Token header). Rotate-only field; leave "
             "blank to keep the existing value.",
    )
    gohan_webhook_token_status = fields.Char(
        string="Webhook Token Status", compute="_compute_sensitive_status",
    )

    # -- Extraction Lambda (async invoke) --
    gohan_lambda_function_name = fields.Char(
        string="Lambda Function Name / ARN",
        help="Rotate-only. AWS Lambda function name or full ARN for the "
             "extraction service. Used with boto3 lambda:Invoke "
             "(InvocationType=Event).",
    )
    gohan_lambda_function_name_status = fields.Char(
        string="Lambda ARN Status", compute="_compute_sensitive_status",
    )
    gohan_lambda_region = fields.Char(
        string="Lambda Region",
        config_parameter="gohan.lambda_region",
        default="ap-south-1",
    )
    gohan_lambda_local_url = fields.Char(
        string="Lambda Local URL (dev only)",
        config_parameter="gohan.lambda_local_url",
        help="If set, extraction requests POST to this URL instead of AWS Lambda. "
             "Used for local development against the AWS Lambda Runtime Interface "
             "Emulator (RIE), e.g. "
             "http://localhost:9000/2015-03-31/functions/function/invocations. "
             "Leave empty in production to use boto3 lambda:Invoke.",
    )
    gohan_extraction_access_key_id = fields.Char(
        string="Extraction AWS Access Key ID",
        help="Rotate-only. Leave empty to use EKS pod IAM role (IRSA).",
    )
    gohan_extraction_access_key_id_status = fields.Char(
        string="Extraction Access Key ID Status",
        compute="_compute_sensitive_status",
    )
    gohan_extraction_secret_access_key = fields.Char(
        string="Extraction AWS Secret Access Key",
        help="Rotate-only field.",
    )
    gohan_extraction_secret_access_key_status = fields.Char(
        string="Extraction Secret Status", compute="_compute_sensitive_status",
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
        help="Rotate-only. e.g., arn:aws:bedrock:us-east-1:123456:"
             "inference-profile/...",
    )
    gohan_bedrock_inference_arn_status = fields.Char(
        string="Bedrock ARN Status", compute="_compute_sensitive_status",
    )
    gohan_bedrock_region = fields.Char(
        string="Bedrock Region",
        config_parameter="gohan.bedrock_region",
        default="us-east-1",
    )
    gohan_bedrock_access_key_id = fields.Char(
        string="Bedrock Access Key ID",
        help="Rotate-only. Leave empty to use EKS pod IAM role (IRSA).",
    )
    gohan_bedrock_access_key_id_status = fields.Char(
        string="Bedrock Access Key ID Status",
        compute="_compute_sensitive_status",
    )
    gohan_bedrock_secret_access_key = fields.Char(
        string="Bedrock Secret Access Key",
        help="Rotate-only field.",
    )
    gohan_bedrock_secret_access_key_status = fields.Char(
        string="Bedrock Secret Status", compute="_compute_sensitive_status",
    )

    # -- S3 --
    gohan_s3_bucket = fields.Char(
        string="S3 Bucket Name",
        config_parameter="gohan.s3_bucket",
    )
    gohan_s3_access_key_id = fields.Char(
        string="S3 Access Key ID",
        help="Rotate-only field.",
    )
    gohan_s3_access_key_id_status = fields.Char(
        string="S3 Access Key ID Status", compute="_compute_sensitive_status",
    )
    gohan_s3_secret_access_key = fields.Char(
        string="S3 Secret Access Key",
        help="Rotate-only field.",
    )
    gohan_s3_secret_access_key_status = fields.Char(
        string="S3 Secret Status", compute="_compute_sensitive_status",
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
        string="PRD Prompt Status", compute="_compute_gohan_prompt_status",
    )
    gohan_qc_prompt_file = fields.Binary(
        string="QC Prompt File (.md)",
        help="Upload a Markdown file to override the built-in QC prompt.",
    )
    gohan_qc_prompt_filename = fields.Char(string="QC Prompt Filename")
    gohan_qc_prompt_status = fields.Char(
        string="QC Prompt Status", compute="_compute_gohan_prompt_status",
    )

    # -- Limits --
    gohan_max_jobs_per_user = fields.Integer(
        string="Max Active Jobs per Tasker",
        config_parameter="gohan.max_jobs_per_user",
        default=5,
        help="Maximum active tasks (draft + extracting + generating + scoring + done) per tasker. 0 = unlimited.",
    )

    def _compute_gohan_prompt_status(self):
        ICP = self.env["ir.config_parameter"].sudo()
        prd = (ICP.get_param("gohan.prd_system_prompt", "") or "").strip()
        prd_name = ICP.get_param("gohan.prd_prompt_filename", "") or ""
        qc = (ICP.get_param("gohan.qc_system_prompt", "") or "").strip()
        qc_name = ICP.get_param("gohan.qc_prompt_filename", "") or ""
        for rec in self:
            rec.gohan_prd_prompt_status = (
                (f"Custom prompt active: {prd_name}" if prd_name else "Custom prompt active")
                if prd else "Using built-in default"
            )
            rec.gohan_qc_prompt_status = (
                (f"Custom prompt active: {qc_name}" if qc_name else "Custom prompt active")
                if qc else "Using built-in default"
            )

    def _compute_sensitive_status(self):
        ICP = self.env["ir.config_parameter"].sudo()
        cache = {icp_key: ICP.get_param(icp_key, "") for icp_key in SENSITIVE_FIELDS.values()}
        for rec in self:
            for form_field, icp_key in SENSITIVE_FIELDS.items():
                rec[f"{form_field}_status"] = _mask_status(cache[icp_key])

    def get_values(self):
        res = super().get_values()
        ICP = self.env["ir.config_parameter"].sudo()
        res["gohan_prd_prompt_filename"] = ICP.get_param("gohan.prd_prompt_filename", default="")
        res["gohan_qc_prompt_filename"] = ICP.get_param("gohan.qc_prompt_filename", default="")
        res["gohan_prd_prompt_file"] = False
        res["gohan_qc_prompt_file"] = False
        # SECURITY: never round-trip stored secret/ARN values back to the UI.
        # The form input always renders blank; operators rotate by entering a
        # new value (empty = keep existing, enforced in set_values).
        for form_field in SENSITIVE_FIELDS:
            res[form_field] = ""
        return res

    def set_values(self):
        super().set_values()
        ICP = self.env["ir.config_parameter"].sudo()

        # Rotate-only write: only persist non-empty entries so a blank field
        # never wipes an existing secret. There is no UI affordance to clear
        # a secret on purpose — operators delete the ICP row directly if that
        # is truly intended (rare; usually they rotate to a new value).
        for form_field, icp_key in SENSITIVE_FIELDS.items():
            new_value = (getattr(self, form_field, "") or "").strip()
            if new_value:
                ICP.set_param(icp_key, new_value)

        if self.gohan_prd_prompt_file:
            content = base64.b64decode(self.gohan_prd_prompt_file).decode(
                "utf-8", errors="replace"
            )
            ICP.set_param("gohan.prd_system_prompt", content)
            ICP.set_param(
                "gohan.prd_prompt_filename",
                self.gohan_prd_prompt_filename or "prd_prompt.md",
            )

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
        ICP = self.env["ir.config_parameter"].sudo()
        ICP.set_param("gohan.prd_system_prompt", "")
        ICP.set_param("gohan.prd_prompt_filename", "")

    def action_clear_qc_prompt(self):
        ICP = self.env["ir.config_parameter"].sudo()
        ICP.set_param("gohan.qc_system_prompt", "")
        ICP.set_param("gohan.qc_prompt_filename", "")
