import base64

from odoo import api, fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    # -- Extraction Lambda (async invoke) --
    leviathan_lambda_function_name = fields.Char(
        string="Lambda Function Name",
        config_parameter="leviathan.lambda_function_name",
        help="AWS Lambda function name or full ARN for the extraction service. "
             "Used with boto3 lambda:Invoke (InvocationType=Event).",
    )
    leviathan_lambda_region = fields.Char(
        string="Lambda Region",
        config_parameter="leviathan.lambda_region",
        default="ap-south-1",
    )
    leviathan_extraction_access_key_id = fields.Char(
        string="Extraction AWS Access Key ID",
        config_parameter="leviathan.extraction_access_key_id",
        help="Leave empty to use EKS pod IAM role (IRSA).",
    )
    leviathan_extraction_secret_access_key = fields.Char(
        string="Extraction AWS Secret Access Key",
        config_parameter="leviathan.extraction_secret_access_key",
    )
    leviathan_batch_concurrency = fields.Integer(
        string="Batch Concurrency",
        config_parameter="leviathan.batch_concurrency",
        default=250,
        help="Max parallel Lambda invocations per batch run. Must not exceed "
             "the Lambda's ReservedConcurrentExecutions setting.",
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
    leviathan_prd_prompt_status = fields.Char(
        string="PRD Prompt Status", compute="_compute_prompt_status",
    )
    leviathan_qc_prompt_file = fields.Binary(
        string="QC Prompt File (.md)",
        help="Upload a Markdown file to override the built-in QC prompt.",
    )
    leviathan_qc_prompt_filename = fields.Char(string="QC Prompt Filename")
    leviathan_qc_prompt_status = fields.Char(
        string="QC Prompt Status", compute="_compute_prompt_status",
    )

    # -- Limits --
    leviathan_max_jobs_per_user = fields.Integer(
        string="Max Active Jobs per Tasker",
        config_parameter="leviathan.max_jobs_per_user",
        default=5,
        help="Maximum active tasks (draft + extracting + generating + scoring + done) per tasker. 0 = unlimited.",
    )

    # -- Watchdog & Recovery (LIVE — read each cron tick) --
    leviathan_watchdog_extracting_minutes = fields.Integer(
        string="Extracting Timeout (min)",
        config_parameter="leviathan.watchdog_extracting_minutes",
        default=30,
        help="How long a task can sit in 'extracting' state with a stale "
             "heartbeat before the watchdog auto-retries it. Lambda's hard "
             "cap is 15 min, so anything past 30 min means the callback "
             "didn't land. Live setting — takes effect on next cron tick.",
    )
    leviathan_watchdog_generating_minutes = fields.Integer(
        string="Generating Timeout (min)",
        config_parameter="leviathan.watchdog_generating_minutes",
        default=120,
        help="How long a task can sit in 'generating'/'scoring' state with "
             "a stale heartbeat before the watchdog auto-retries it. With "
             "the heartbeat ticker pulsing every 60s, you can safely set "
             "this to 15. Live setting — takes effect on next cron tick.",
    )
    leviathan_watchdog_auto_retry_max = fields.Integer(
        string="Watchdog Auto-Retry Max",
        config_parameter="leviathan.watchdog_auto_retry_max",
        default=1,
        help="How many times the watchdog silently auto-retries a stuck "
             "job before marking it failed for real. 1 = one free retry "
             "(recommended). 0 = disable auto-retry (mark failed on first "
             "stuck hit). Live — takes effect on next cron tick.",
    )

    # -- Concurrency (RESTART required for changes to take full effect) --
    leviathan_prd_pool_size = fields.Integer(
        string="PRD Pool Size (per process)",
        config_parameter="leviathan.prd_pool_size",
        default=50,
        help="Max concurrent PRD-generation threads PER Odoo worker process. "
             "Real total = this × Odoo workers × pods. Sized against db_maxconn: "
             "(pool × workers × pods × 2 cursors) + 50 reserved < db_maxconn. "
             "REQUIRES POD RESTART (env LEVIATHAN_PRD_POOL_SIZE also needs to "
             "be updated by devops) — ThreadPoolExecutor can't be resized live.",
    )
    leviathan_bedrock_max_concurrent = fields.Integer(
        string="Bedrock Max Concurrent Calls",
        config_parameter="leviathan.bedrock_max_concurrent",
        default=5,
        help="In-process semaphore that caps simultaneous Bedrock API calls. "
             "Prevents AWS adaptive throttle from queuing calls for 30+ min. "
             "Size ≈ TPS_quota × avg_call_seconds. Default 5 is safe for "
             "Bedrock's default 5-10 RPS quota. Raise once devops confirms "
             "higher quota. REQUIRES POD RESTART (env "
             "LEVIATHAN_BEDROCK_MAX_CONCURRENT) — semaphore can't be resized.",
    )
    leviathan_bedrock_inner_retries = fields.Integer(
        string="Bedrock Internal Retries",
        config_parameter="leviathan.bedrock_inner_retries",
        default=2,
        help="Max retry attempts inside a single Bedrock call (adaptive "
             "backoff). With max_llm_attempts=1, worst case is this × 1 = "
             "this calls per phase. Lower = less load. REQUIRES POD RESTART "
             "for cached clients (env LEVIATHAN_BEDROCK_INNER_RETRIES).",
    )

    # -- Webhook (LIVE — read per request) --
    leviathan_webhook_max_bytes = fields.Integer(
        string="Webhook Max Body Bytes",
        config_parameter="leviathan.webhook_max_bytes",
        default=10485760,  # 10 MB
        help="Maximum size of the webhook callback body from Lambda. "
             "Defense against OOM from oversized payloads. Default 10 MB. "
             "Live — takes effect on next request.",
    )

    def _compute_prompt_status(self):
        ICP = self.env["ir.config_parameter"].sudo()
        prd = ICP.get_param("leviathan.prd_system_prompt", "")
        prd_name = ICP.get_param("leviathan.prd_prompt_filename", "")
        qc = ICP.get_param("leviathan.qc_system_prompt", "")
        qc_name = ICP.get_param("leviathan.qc_prompt_filename", "")
        for rec in self:
            if prd and prd.strip():
                rec.leviathan_prd_prompt_status = f"Custom prompt active: {prd_name}" if prd_name else "Custom prompt active"
            else:
                rec.leviathan_prd_prompt_status = "Using built-in default"
            if qc and qc.strip():
                rec.leviathan_qc_prompt_status = f"Custom prompt active: {qc_name}" if qc_name else "Custom prompt active"
            else:
                rec.leviathan_qc_prompt_status = "Using built-in default"

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
