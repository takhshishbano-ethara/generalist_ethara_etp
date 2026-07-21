import base64

from odoo import api, fields, models
from odoo.exceptions import UserError

from ..constants import (
    VERTEX_DEFAULT_LOCATION,
    VERTEX_DEFAULT_MODEL,
    VIDEO_DEFAULT_MODEL,
    VIDEO_DEFAULT_LOCATION,
    VIDEO_DEFAULT_DURATION_S,
)


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    etp_assessment_pro_vertex_project_id = fields.Char(
        string="Vertex Project ID",
        config_parameter="etp_assessment_pro.vertex_project_id",
    )
    etp_assessment_pro_vertex_location = fields.Char(
        string="Vertex Location",
        config_parameter="etp_assessment_pro.vertex_location",
        default=VERTEX_DEFAULT_LOCATION,
    )
    etp_assessment_pro_vertex_model = fields.Char(
        string="Vertex Model",
        config_parameter="etp_assessment_pro.vertex_model",
        default=VERTEX_DEFAULT_MODEL,
        help="Default model used for scoring, image rendering, and any task "
             "without its own override.",
    )
    etp_assessment_pro_generation_model = fields.Char(
        string="Question Generation Model",
        config_parameter="etp_assessment_pro.generation_model",
        help="Strong multimodal model used to read the SOP document (with its "
             "images) and author questions directly - e.g. gemini-3-pro or "
             "gemini-2.5-pro. Leave empty to fall back to the Vertex Model.",
    )
    etp_assessment_pro_detection_model = fields.Char(
        string="Image Detection Model",
        config_parameter="etp_assessment_pro.detection_model",
        help="Multimodal model used to detect elements in an image_label "
             "stimulus for the numbered-box overlay. Leave empty to fall back "
             "to the Question Generation Model.",
    )
    etp_assessment_pro_scoring_model = fields.Char(
        string="Subjective Scoring Model",
        config_parameter="etp_assessment_pro.scoring_model",
        help="TEXT reasoning model the subjective judge uses to grade answers "
             "against the golden key (e.g. gemini-2.5-pro). MUST be a text model, "
             "never the image model. Leave empty to fall back to the Question "
             "Generation Model.",
    )
    etp_assessment_pro_image_model = fields.Char(
        string="Image Rendering Model",
        config_parameter="etp_assessment_pro.image_model",
        help="Model used to RENDER image_ab / image_prompt / image_label "
             "pictures (e.g. gemini-3-pro-image = Nano Banana Pro, or the "
             "cheaper gemini-3.1-flash-image = Nano Banana 2). Leave empty to "
             "fall back to the Default Model. Keeping this separate means the "
             "Default Model can be a cheap text model without misrouting image "
             "rendering.",
    )
    etp_assessment_pro_video_model = fields.Char(
        string="Video Generation Model (Veo)",
        config_parameter="etp_assessment_pro.video_model",
        default=VIDEO_DEFAULT_MODEL,
        help="OPTIONAL. Veo model for async video_prompt clip generation, e.g. "
             "veo-3.1-generate-001 (has audio) or the cheaper "
             "veo-3.1-fast-generate-001. Video generation only fires when this "
             "AND Vertex credentials resolve; otherwise video_prompt clips stay "
             "upload-only.",
    )
    etp_assessment_pro_video_location = fields.Char(
        string="Video Generation Location",
        config_parameter="etp_assessment_pro.video_location",
        default=VIDEO_DEFAULT_LOCATION,
        help="Vertex region for Veo. Veo is served on a regional endpoint "
             "(us-central1), NOT 'global' - the gemini default 404s for Veo.",
    )
    etp_assessment_pro_video_default_duration_s = fields.Integer(
        string="Video Clip Duration (s)",
        config_parameter="etp_assessment_pro.video_default_duration_s",
        default=VIDEO_DEFAULT_DURATION_S,
        help="Default generated clip length in seconds when a brief omits one.",
    )
    etp_assessment_pro_vertex_api_key = fields.Char(
        string="Vertex API Key",
        config_parameter="etp_assessment_pro.vertex_api_key",
    )
    etp_assessment_pro_vertex_access_token = fields.Char(
        string="Vertex Access Token",
        config_parameter="etp_assessment_pro.vertex_access_token",
    )
    etp_assessment_pro_vertex_service_account_json = fields.Char(
        string="Service Account JSON (text)",
        config_parameter="etp_assessment_pro.vertex_service_account_json",
    )
    etp_assessment_pro_vertex_service_account_filename = fields.Char(
        string="Service Account Filename",
        config_parameter="etp_assessment_pro.vertex_service_account_filename",
    )
    vertex_sa_upload = fields.Binary(string="Upload Service Account JSON")
    vertex_sa_upload_filename = fields.Char(string="SA Upload Filename")

    etp_assessment_pro_s3_bucket = fields.Char(
        string="S3 Bucket",
        config_parameter="etp_assessment_pro.s3_bucket",
    )
    etp_assessment_pro_s3_region = fields.Char(
        string="S3 Region",
        config_parameter="etp_assessment_pro.s3_region",
        default="us-east-1",
    )
    etp_assessment_pro_s3_access_key_id = fields.Char(
        string="S3 Access Key ID",
        config_parameter="etp_assessment_pro.s3_access_key_id",
    )
    etp_assessment_pro_s3_secret_key = fields.Char(
        string="S3 Secret Access Key",
        config_parameter="etp_assessment_pro.s3_secret_key",
    )
    etp_assessment_pro_s3_folder = fields.Char(
        string="S3 Key Prefix",
        config_parameter="etp_assessment_pro.s3_folder",
        default="etp_assessment",
    )
    etp_assessment_pro_s3_cdn_url = fields.Char(
        string="S3 CDN Base URL",
        config_parameter="etp_assessment_pro.s3_cdn_url",
    )
    etp_assessment_pro_s3_max_retries = fields.Integer(
        string="S3 Max Upload Retries",
        config_parameter="etp_assessment_pro.s3_max_retries",
        default=3,
    )

    etp_assessment_pro_llm_max_attempts = fields.Integer(
        string="LLM Scoring Max Attempts",
        config_parameter="etp_assessment_pro.llm_max_attempts",
        default=3,
        help="After this many failed scoring attempts a response resolves as a "
             "fail instead of staying pending forever.",
    )
    etp_assessment_pro_scoring_batch_size = fields.Integer(
        string="Scoring Batch Size",
        config_parameter="etp_assessment_pro.scoring_batch_size",
        default=8,
        help="Max answers graded in one Vertex call. Large candidates are split "
             "into sub-batches of this size so no request overflows the token "
             "budget. Lower it if you see truncated scoring responses.",
    )
    etp_assessment_pro_daily_llm_cap_usd = fields.Float(
        string="Daily LLM Spend Cap (USD)",
        config_parameter="etp_assessment_pro.daily_llm_cap_usd",
        default=100.0,
        help="H-5: refuse any Vertex call once same-day recorded LLM spend "
             "reaches this. 0 disables the daily cap. Protects against a "
             "cost-DoS (adversarial answers) draining the budget in one cycle.",
    )
    etp_assessment_pro_per_evaluator_llm_cap_usd = fields.Float(
        string="Per-Candidate LLM Spend Cap (USD)",
        config_parameter="etp_assessment_pro.per_evaluator_llm_cap_usd",
        default=5.0,
        help="H-5: refuse further Vertex calls for one candidate once their "
             "recorded LLM spend reaches this. 0 disables the per-candidate "
             "cap. Stops a single candidate consuming the whole budget.",
    )

    @api.constrains("etp_assessment_pro_llm_max_attempts",
                    "etp_assessment_pro_scoring_batch_size")
    def _check_llm_limits(self):
        # H-5 hardening: a compromised/fat-fingered admin setting attempts=100 or
        # batch=1000 would multiply spend / overflow the token budget. Clamp the
        # accepted range at the write path.
        for rec in self:
            if not (1 <= (rec.etp_assessment_pro_llm_max_attempts or 3) <= 5):
                raise UserError(
                    "LLM Scoring Max Attempts must be between 1 and 5.")
            if not (1 <= (rec.etp_assessment_pro_scoring_batch_size or 8) <= 32):
                raise UserError(
                    "Scoring Batch Size must be between 1 and 32.")

    etp_assessment_pro_question_prompt = fields.Char(
        string="Question Generation Prompt",
        config_parameter="etp_assessment_pro.question_prompt",
    )
    etp_assessment_pro_scoring_prompt = fields.Char(
        string="Scoring Prompt",
        config_parameter="etp_assessment_pro.scoring_system_prompt",
    )
    question_prompt_upload = fields.Binary(string="Upload question.md")
    question_prompt_upload_filename = fields.Char()
    scoring_prompt_upload = fields.Binary(string="Upload scoring.md")
    scoring_prompt_upload_filename = fields.Char()
    etp_assessment_pro_question_prompt_filename = fields.Char(
        string="Question Prompt Filename",
        config_parameter="etp_assessment_pro.question_prompt_filename",
    )
    etp_assessment_pro_scoring_prompt_filename = fields.Char(
        string="Scoring Prompt Filename",
        config_parameter="etp_assessment_pro.scoring_prompt_filename",
    )

    @api.onchange("vertex_sa_upload")
    def _onchange_vertex_sa_upload(self):
        if not self.vertex_sa_upload:
            return
        try:
            text = base64.b64decode(self.vertex_sa_upload).decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise UserError(
                "Service account JSON must be a UTF-8 text file (%s)." % exc
            )
        fname = self.vertex_sa_upload_filename or "service-account.json"
        ICP = self.env["ir.config_parameter"].sudo()
        ICP.set_param("etp_assessment_pro.vertex_service_account_json", text)
        ICP.set_param("etp_assessment_pro.vertex_service_account_filename", fname)
        ICP.set_param("etp_assessment_pro.vertex_minted_token", "")
        ICP.set_param("etp_assessment_pro.vertex_minted_token_expires", "")
        self.etp_assessment_pro_vertex_service_account_json = text
        self.etp_assessment_pro_vertex_service_account_filename = fname

    @api.onchange("question_prompt_upload")
    def _onchange_question_prompt_upload(self):
        if not self.question_prompt_upload:
            return
        try:
            text = base64.b64decode(self.question_prompt_upload).decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise UserError("Question prompt must be UTF-8 text (%s)." % exc)
        ICP = self.env["ir.config_parameter"].sudo()
        fname = self.question_prompt_upload_filename or "question.md"
        ICP.set_param("etp_assessment_pro.question_prompt", text)
        ICP.set_param("etp_assessment_pro.question_prompt_filename", fname)
        self.etp_assessment_pro_question_prompt = text
        self.etp_assessment_pro_question_prompt_filename = fname

    @api.onchange("scoring_prompt_upload")
    def _onchange_scoring_prompt_upload(self):
        if not self.scoring_prompt_upload:
            return
        try:
            text = base64.b64decode(self.scoring_prompt_upload).decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise UserError("Scoring prompt must be UTF-8 text (%s)." % exc)
        ICP = self.env["ir.config_parameter"].sudo()
        fname = self.scoring_prompt_upload_filename or "scoring.md"
        ICP.set_param("etp_assessment_pro.scoring_system_prompt", text)
        ICP.set_param("etp_assessment_pro.scoring_prompt_filename", fname)
        self.etp_assessment_pro_scoring_prompt = text
        self.etp_assessment_pro_scoring_prompt_filename = fname

