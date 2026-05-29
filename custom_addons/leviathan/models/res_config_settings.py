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

    # -- LLM image attachment (opt-in; off by default) --
    #
    # Attaching screenshots to Bedrock calls is a known failure-rate
    # amplifier: long-output PRD generation + image content blocks
    # produces 4xx rejections far more often than text-only requests.
    # Default: both OFF — matches the historical hardcoded behaviour
    # (`screenshot_blocks = []` and `screenshot_blocks accepted but
    # ignored` in qc_service). Opt in per-environment when you want to
    # trade reliability for marginally better visual grounding.
    leviathan_prd_include_images = fields.Boolean(
        string="Attach Screenshots to PRD Generation",
        config_parameter="leviathan.prd_include_images",
        default=False,
        help=(
            "When ON, the PRD generation Bedrock call receives the first "
            "N screenshots (capped by Attach — Max Images, default 4) "
            "as image content blocks. When OFF (default and recommended), "
            "PRD generation is text-only: the extraction Lambda's "
            "`prd_prompt` already encodes a textual description of the "
            "visual extraction, and text-only requests are much less "
            "likely to hit Bedrock 4xx rejections."
        ),
    )
    leviathan_qc_include_images = fields.Boolean(
        string="Attach Screenshots to QC Alignment Check",
        config_parameter="leviathan.qc_include_images",
        default=False,
        help=(
            "When ON, the QC alignment-check Bedrock call receives the "
            "first N screenshots (same cap as PRD generation). When OFF "
            "(default and recommended), QC compares the PRD against the "
            "extraction-JSON summary text — not screenshots. Turn ON "
            "only if reviewers report 'PRD says hero is dark but "
            "screenshot shows light'-style misalignments slipping "
            "through."
        ),
    )
    leviathan_prd_max_images = fields.Integer(
        string="Attach — Max Images",
        config_parameter="leviathan.prd_max_images",
        default=4,
        help=(
            "Maximum number of screenshots to attach when either "
            "`prd_include_images` or `qc_include_images` is ON. Each "
            "image is resized to fit Bedrock's 8000-px-per-side limit "
            "before upload. Higher values give the model more visual "
            "context but also raise the 4xx-rejection rate — 4 is the "
            "observed sweet spot on Claude."
        ),
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

    # -- PRD-queue two-gate reconcile (LIVE) --
    #
    # The drainer's stale-recovery step uses TWO gates so a worker that's
    # genuinely alive-but-slow does NOT get reclaimed (and the same job
    # billed on Bedrock twice). See `_prd_queue_recover_stale` docstring.
    leviathan_prd_stale_minutes = fields.Integer(
        string="PRD Stale Heartbeat (long gate, min)",
        config_parameter="leviathan.prd_stale_minutes",
        default=15,
        help="Long / unconditional gate. A row with no heartbeat for "
             "this many minutes is recovered no matter what. Set above "
             "the worst-case real PRD pipeline duration (Bedrock + QC "
             "+ S3 upload ≈ 5 min worst case; 15 leaves wide margin). "
             "Live — takes effect on next drainer tick.",
    )
    leviathan_prd_short_stale_minutes = fields.Integer(
        string="PRD Stale Heartbeat (short gate, min)",
        config_parameter="leviathan.prd_short_stale_minutes",
        default=5,
        help="Short / fast gate. Combined with the failure-count "
             "threshold below: a row is recovered if its heartbeat is "
             "older than THIS many minutes AND its "
             "heartbeat_failure_count is at or above the threshold. "
             "Lower = faster recovery when the worker is *demonstrably* "
             "failing to pulse. Live — takes effect on next drainer tick.",
    )
    leviathan_prd_heartbeat_failure_threshold = fields.Integer(
        string="Heartbeat Failure Threshold",
        config_parameter="leviathan.prd_heartbeat_failure_threshold",
        default=3,
        help="How many consecutive heartbeat-write failures must be "
             "observed before the short-stale gate trips. The counter "
             "resets on every successful pulse, so this is 'failures "
             "in a row,' not 'failures ever.' 3 means a worker has "
             "missed three full heartbeat cycles in a row (~180s by "
             "default) before short-gate recovery fires.",
    )

    # -- Concurrency (LIVE THROTTLE-DOWN; restart required to raise above boot cap) --
    leviathan_prd_pool_size = fields.Integer(
        string="PRD Pool Size (per process)",
        config_parameter="leviathan.prd_pool_size",
        default=100,
        help="Max concurrent PRD-generation threads PER worker process. "
             "Real total = this × worker pods. Sized against db_maxconn: "
             "(pool × pods × 2 cursors) + 50 reserved < db_maxconn. "
             "LIVE-TUNABLE: lowering this in Settings takes effect on the "
             "next drainer tick (≤5s) and immediately caps how many jobs "
             "the worker claims. RAISING above the env "
             "LEVIATHAN_PRD_POOL_SIZE has no effect — the Python "
             "ThreadPoolExecutor cap is set at boot. To raise, bump the "
             "env var AND restart the worker pod.",
    )
    leviathan_bedrock_max_concurrent = fields.Integer(
        string="Bedrock Max Concurrent Calls",
        config_parameter="leviathan.bedrock_max_concurrent",
        default=22,
        help="In-process semaphore that caps simultaneous Bedrock API calls. "
             "Prevents AWS adaptive throttle from queuing calls for 30+ min. "
             "Size ≈ TPS_quota × avg_call_seconds. Default 22 assumes a "
             "cluster-wide Bedrock quota of ~220 concurrent calls split "
             "across 10 worker pods; raise once devops confirms higher "
             "quota. LIVE-TUNABLE downwards: lowering throttles within "
             "one drainer tick. RAISING above the env "
             "LEVIATHAN_BEDROCK_MAX_CONCURRENT has no effect — the "
             "Python Semaphore's cap is set at construction. To raise, "
             "bump the env var AND restart the pod.",
    )
    leviathan_bedrock_inner_retries = fields.Integer(
        string="Bedrock Internal Retries",
        config_parameter="leviathan.bedrock_inner_retries",
        default=2,
        help="Max retry attempts inside a single Bedrock call (adaptive "
             "backoff) for transient failures. Each pipeline phase (PRD "
             "gen, QC) makes one call, so worst case is this many Bedrock "
             "requests per phase. Lower = less load. REQUIRES POD RESTART "
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

    # -- K8s worker auto-scaler (worker mode only) --
    #
    # Settings ported from vegeta v19.0.2.6.0 — see
    # ``services/k8s_scaler.py`` for the patching logic. Scaler is a no-op
    # unless ``leviathan.prd_queue_enabled=True`` AND
    # ``leviathan.prd_execution_mode=worker``.
    leviathan_worker_deployment_name = fields.Char(
        string="Worker Deployment Name",
        config_parameter="leviathan.worker_deployment_name",
        default="leviathan-prd-worker",
        help="Kubernetes Deployment that the scaler patches. Must exist "
             "in the namespace below. Matches the manifest in deploy/.",
    )
    leviathan_k8s_namespace = fields.Char(
        string="K8s Namespace",
        config_parameter="leviathan.k8s_namespace",
        default="leviathan",
        help="Namespace where the worker Deployment lives. The Odoo "
             "backend ServiceAccount needs get/patch RBAC on "
             "deployments + deployments/scale in this namespace.",
    )
    leviathan_worker_min_replicas = fields.Integer(
        string="Worker Min Replicas",
        config_parameter="leviathan.worker_min_replicas",
        default=0,
        help="Floor for the auto-scaler. 0 = scale-to-zero between bursts "
             "(saves $$). 1 = keep one warm pod for instant claim "
             "(shaves ~60-90s cold start).",
    )
    leviathan_worker_max_replicas = fields.Integer(
        string="Worker Max Replicas",
        config_parameter="leviathan.worker_max_replicas",
        default=10,
        help="Burst ceiling. Sized against (Bedrock TPM quota) ÷ "
             "(per_pod Bedrock concurrency × avg call seconds). Going "
             "too high risks Bedrock throttle queueing.",
    )
    leviathan_worker_target_concurrency = fields.Integer(
        string="Worker Target Concurrency",
        config_parameter="leviathan.worker_target_concurrency",
        default=100,
        help="Jobs each pod is sized to handle in parallel. Matches the "
             "LEVIATHAN_PRD_POOL_SIZE env var on the worker pod. The "
             "scaler computes desired_pods = ceil(queue_depth / this).",
    )
    leviathan_worker_scale_down_cooldown_s = fields.Integer(
        string="Scale-Down Cooldown (s)",
        config_parameter="leviathan.worker_scale_down_cooldown_s",
        default=600,
        help="Minimum seconds between any scale-down patch. Asymmetric "
             "hysteresis — scale-up is immediate (burst response), "
             "scale-down waits this long. Don't go below 120s, risks "
             "flapping when a burst comes in waves.",
    )

    # -- Worker loop knobs (ICP > env > default) --
    #
    # Promoted from env-only to ICP so an operator can re-tune live from
    # Odoo Settings. The standalone worker re-reads these every poll-tick
    # (see ``worker/run_prd.py::_resolve_int_setting``); ``LEVIATHAN_*``
    # env vars on the Deployment are still honoured as fallback.
    leviathan_worker_poll_s = fields.Integer(
        string="Worker Poll Interval (s)",
        config_parameter="leviathan.worker_poll_s",
        default=5,
        help="How often each worker pod polls the queue for claimable "
             "rows. Lower = faster burst response, higher DB load. "
             "Don't go below 2s on a shared RDS. Live — picked up on "
             "next tick, no pod restart.",
    )
    leviathan_worker_claim_fail_limit = fields.Integer(
        string="Worker Claim-Failure Limit",
        config_parameter="leviathan.worker_claim_fail_limit",
        default=5,
        help="Consecutive drainer-tick failures before the worker pod "
             "exits non-zero so K8s replaces it with a fresh registry. "
             "Recovers automatically from RDS failover or stale "
             "connections. Live — picked up on next tick.",
    )
    leviathan_worker_shutdown_timeout_s = fields.Integer(
        string="Worker Shutdown Drain (s)",
        config_parameter="leviathan.worker_shutdown_timeout_s",
        default=1800,
        help="Bounded drain budget on SIGTERM. Worker stops claiming new "
             "jobs and waits this long for in-flight PRDs to finish; "
             "anything still running past the budget is abandoned to "
             "SIGKILL and stale-heartbeat recovery re-claims it. MUST be "
             "less than the pod's terminationGracePeriodSeconds.",
    )

    @api.depends("leviathan_prd_prompt_filename", "leviathan_qc_prompt_filename")
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
