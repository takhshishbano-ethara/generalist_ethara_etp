import base64

from odoo import api, fields, models
from odoo.exceptions import UserError


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    etp_assessment_pro_vertex_project_id = fields.Char(
        string="Vertex Project ID",
        config_parameter="etp_assessment_pro.vertex_project_id",
    )
    etp_assessment_pro_vertex_location = fields.Char(
        string="Vertex Location",
        config_parameter="etp_assessment_pro.vertex_location",
        default="global",
    )
    etp_assessment_pro_vertex_model = fields.Char(
        string="Vertex Model",
        config_parameter="etp_assessment_pro.vertex_model",
        default="gemini-3.1-pro-preview",
    )
    etp_assessment_pro_vertex_image_model = fields.Char(
        string="Vertex Image Model",
        config_parameter="etp_assessment_pro.vertex_image_model",
        default="gemini-3-pro-image",
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

    etp_assessment_pro_skill_gen_prompt = fields.Char(
        string="Skill Generation Prompt",
        config_parameter="etp_assessment_pro.skill_gen_prompt",
    )
    etp_assessment_pro_question_prompt = fields.Char(
        string="Question Generation Prompt",
        config_parameter="etp_assessment_pro.question_prompt",
    )
    skill_gen_prompt_upload = fields.Binary(string="Upload skill_gen.md")
    skill_gen_prompt_upload_filename = fields.Char()
    question_prompt_upload = fields.Binary(string="Upload question.md")
    question_prompt_upload_filename = fields.Char()
    etp_assessment_pro_skill_gen_prompt_filename = fields.Char(
        string="Skill Gen Prompt Filename",
        config_parameter="etp_assessment_pro.skill_gen_prompt_filename",
    )
    etp_assessment_pro_question_prompt_filename = fields.Char(
        string="Question Prompt Filename",
        config_parameter="etp_assessment_pro.question_prompt_filename",
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

    @api.onchange("skill_gen_prompt_upload")
    def _onchange_skill_gen_prompt_upload(self):
        if not self.skill_gen_prompt_upload:
            return
        try:
            text = base64.b64decode(self.skill_gen_prompt_upload).decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise UserError("Skill prompt must be UTF-8 text (%s)." % exc)
        ICP = self.env["ir.config_parameter"].sudo()
        fname = self.skill_gen_prompt_upload_filename or "skill_gen.md"
        ICP.set_param("etp_assessment_pro.skill_gen_prompt", text)
        ICP.set_param("etp_assessment_pro.skill_gen_prompt_filename", fname)
        self.etp_assessment_pro_skill_gen_prompt = text
        self.etp_assessment_pro_skill_gen_prompt_filename = fname

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
