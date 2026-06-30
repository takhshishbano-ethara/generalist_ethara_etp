import base64

from odoo import api, fields, models
from odoo.exceptions import UserError

from ..constants import (
    VERTEX_DEFAULT_LOCATION,
    VERTEX_DEFAULT_MODEL,
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
        help="Single model used for ALL tasks: skill extraction, question "
             "generation, subjective scoring AND image rendering.",
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

    # ---- Scoring (equal-marks, 0-100 threshold model) ----
    etp_assessment_pro_pass_threshold = fields.Float(
        string="Overall Pass Threshold %",
        config_parameter="etp_assessment_pro.pass_threshold",
        default=70.0,
        help="A candidate passes the assessment when their score percent is "
             ">= this value (0-100).",
    )
    etp_assessment_pro_subjective_pass_threshold = fields.Float(
        string="Per-Question Subjective Pass %",
        config_parameter="etp_assessment_pro.subjective_pass_threshold",
        default=70.0,
        help="An LLM-graded answer (subjective / image) earns its single mark "
             "when its 0-100 score is >= this value.",
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

    etp_assessment_pro_skill_gen_prompt = fields.Char(
        string="Skill Generation Prompt",
        config_parameter="etp_assessment_pro.skill_gen_prompt",
    )
    etp_assessment_pro_question_prompt = fields.Char(
        string="Question Generation Prompt",
        config_parameter="etp_assessment_pro.question_prompt",
    )
    etp_assessment_pro_scoring_prompt = fields.Char(
        string="Scoring Prompt",
        config_parameter="etp_assessment_pro.scoring_system_prompt",
    )
    skill_gen_prompt_upload = fields.Binary(string="Upload skill_gen.md")
    skill_gen_prompt_upload_filename = fields.Char()
    question_prompt_upload = fields.Binary(string="Upload question.md")
    question_prompt_upload_filename = fields.Char()
    scoring_prompt_upload = fields.Binary(string="Upload scoring.md")
    scoring_prompt_upload_filename = fields.Char()
    etp_assessment_pro_skill_gen_prompt_filename = fields.Char(
        string="Skill Gen Prompt Filename",
        config_parameter="etp_assessment_pro.skill_gen_prompt_filename",
    )
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

    def set_values(self):
        """Persist settings, then — when a scoring threshold changed — re-decide
        every already-scored answer LIVE against the new threshold. Pass/fail and
        the earned mark are computed fields off the immutable llm_raw_100, so we
        only need to nudge a recompute; no answer is re-sent to the LLM. This is
        what makes 'change the threshold in Settings -> results flip' work."""
        ICP = self.env["ir.config_parameter"].sudo()
        before_subj = ICP.get_param(
            "etp_assessment_pro.subjective_pass_threshold")
        before_overall = ICP.get_param("etp_assessment_pro.pass_threshold")
        res = super().set_values()
        after_subj = ICP.get_param(
            "etp_assessment_pro.subjective_pass_threshold")
        after_overall = ICP.get_param("etp_assessment_pro.pass_threshold")
        if before_subj != after_subj or before_overall != after_overall:
            self._recompute_scoring_after_threshold_change()
        return res

    def _recompute_scoring_after_threshold_change(self):
        """Force a live recompute of the subjective marks + result rollups for
        every scored/error response so the new threshold takes effect at once."""
        Response = self.env["etp.assessment.pro.response"].sudo()
        scored = Response.search([("llm_state", "in", ("scored", "error"))])
        if not scored:
            return
        # Recompute per-answer marks (pass/fail, earned mark) from the stored
        # raw score against the new threshold, then let the stored-compute
        # dependency chain refresh evaluator rollups, score%, result, and day
        # scores. modified() invalidates the computed cache so the new threshold
        # is read on the next access.
        scored.modified(["llm_raw_100", "llm_state"])
        scored._compute_subjective_marks()
