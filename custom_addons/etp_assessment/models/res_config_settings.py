# -*- coding: utf-8 -*-
from odoo import api, fields, models
from odoo.exceptions import UserError


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    etp_assessment_vertex_project_id = fields.Char(
        string="Vertex Project ID",
        config_parameter="etp_assessment.vertex_project_id",
    )
    etp_assessment_vertex_location = fields.Char(
        string="Vertex Location",
        config_parameter="etp_assessment.vertex_location",
        default="global",
    )
    etp_assessment_vertex_model = fields.Char(
        string="Text / Scoring Model",
        config_parameter="etp_assessment.vertex_model",
        default="gemini-3-pro",
    )
    etp_assessment_vertex_image_model = fields.Char(
        string="Image Model",
        config_parameter="etp_assessment.vertex_image_model",
        default="imagen-4.0-generate-001",
    )
    etp_assessment_vertex_api_key = fields.Char(
        string="Vertex API Key",
        config_parameter="etp_assessment.vertex_api_key",
    )
    etp_assessment_vertex_access_token = fields.Char(
        string="Vertex Access Token",
        config_parameter="etp_assessment.vertex_access_token",
    )

    # Service Account JSON support: a Text-backed config_parameter stores the
    # decoded JSON; a non-stored Binary holds the freshly uploaded file which
    # the onchange handler decodes into the text-backed field. Once set,
    # vertex_questions._minted_bearer signs JWTs and exchanges them for fresh
    # ~1h OAuth bearers (cached + auto-refreshed).
    etp_assessment_vertex_service_account_json = fields.Char(
        string="Service Account JSON (text)",
        config_parameter="etp_assessment.vertex_service_account_json",
    )
    etp_assessment_vertex_service_account_filename = fields.Char(
        string="Service Account Filename",
        config_parameter="etp_assessment.vertex_service_account_filename",
    )
    vertex_sa_upload = fields.Binary(string="Upload Service Account JSON")
    vertex_sa_upload_filename = fields.Char(string="SA Upload Filename")

    seed_prompt_upload = fields.Binary(string="Upload Seed Prompt .md")
    seed_prompt_upload_filename = fields.Char()
    scoring_prompt_upload = fields.Binary(string="Upload Scoring Prompt .md")
    scoring_prompt_upload_filename = fields.Char()

    @api.onchange("seed_prompt_upload")
    def _onchange_seed_prompt_upload(self):
        if not self.seed_prompt_upload:
            return
        import base64
        try:
            text = base64.b64decode(self.seed_prompt_upload).decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise UserError("Seed prompt must be UTF-8 text (%s)." % exc)
        self.etp_assessment_seed_system_prompt = text

    @api.onchange("scoring_prompt_upload")
    def _onchange_scoring_prompt_upload(self):
        if not self.scoring_prompt_upload:
            return
        import base64
        try:
            text = base64.b64decode(
                self.scoring_prompt_upload).decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise UserError(
                "Scoring prompt must be UTF-8 text (%s)." % exc)
        self.etp_assessment_scoring_system_prompt = text

    @api.onchange("vertex_sa_upload")
    def _onchange_vertex_sa_upload(self):
        if not self.vertex_sa_upload:
            return
        import base64
        try:
            text = base64.b64decode(self.vertex_sa_upload).decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise UserError(
                "Service account JSON must be a UTF-8 text file (%s)." % exc
            )
        fname = self.vertex_sa_upload_filename or "service-account.json"
        # Persist immediately to ir.config_parameter so the value survives
        # even if res.config.settings.execute() never runs (page refresh,
        # CSRF expiry, proxy timeout). The Char-via-config_parameter wrapper
        # only persists on Save; this set_param commits during the onchange
        # RPC itself.
        ICP = self.env["ir.config_parameter"].sudo()
        ICP.set_param("etp_assessment.vertex_service_account_json", text)
        ICP.set_param("etp_assessment.vertex_service_account_filename", fname)
        # Wipe any cached minted token so the next LLM call re-mints from the
        # newly-uploaded SA — old credentials must not leak across uploads.
        ICP.set_param("etp_assessment.vertex_minted_token", "")
        ICP.set_param("etp_assessment.vertex_minted_token_expires", "")
        self.etp_assessment_vertex_service_account_json = text
        self.etp_assessment_vertex_service_account_filename = fname

    etp_assessment_pass_threshold = fields.Integer(
        string="Pass Threshold (%)",
        config_parameter="etp_assessment.pass_threshold",
        default=70,
    )
    etp_assessment_subjective_points = fields.Integer(
        string="Subjective Points / Question",
        config_parameter="etp_assessment.subjective_points",
        default=10,
    )
    etp_assessment_subjective_pass_threshold = fields.Float(
        string="Subjective Pass Threshold",
        config_parameter="etp_assessment.subjective_pass_threshold",
        default=0.7,
        digits=(3, 2),
    )

    etp_assessment_s3_bucket = fields.Char(
        string="S3 Bucket",
        config_parameter="etp_assessment.s3_bucket",
    )
    etp_assessment_s3_region = fields.Char(
        string="S3 Region",
        config_parameter="etp_assessment.s3_region",
        default="us-east-1",
    )
    etp_assessment_s3_access_key_id = fields.Char(
        string="S3 Access Key ID",
        config_parameter="etp_assessment.s3_access_key_id",
    )
    etp_assessment_s3_secret_key = fields.Char(
        string="S3 Secret Access Key",
        config_parameter="etp_assessment.s3_secret_key",
    )
    etp_assessment_s3_folder = fields.Char(
        string="S3 Key Prefix",
        config_parameter="etp_assessment.s3_folder",
        default="etp_assessment",
    )
    etp_assessment_s3_cdn_url = fields.Char(
        string="S3 CDN Base URL",
        config_parameter="etp_assessment.s3_cdn_url",
    )
    etp_assessment_s3_max_retries = fields.Integer(
        string="Max Upload Retries",
        config_parameter="etp_assessment.s3_max_retries",
        default=3,
    )

    etp_assessment_seed_system_prompt = fields.Char(
        string="Seed Mode Prompt",
        config_parameter="etp_assessment.seed_system_prompt",
    )
    etp_assessment_scoring_system_prompt = fields.Char(
        string="Scoring Prompt",
        config_parameter="etp_assessment.scoring_system_prompt",
    )
    etp_assessment_skills_system_prompt = fields.Char(
        string="Skills Prompt (legacy)",
        config_parameter="etp_assessment.skills_system_prompt",
    )
    etp_assessment_questions_system_prompt = fields.Char(
        string="Questions Prompt (legacy)",
        config_parameter="etp_assessment.questions_system_prompt",
    )
