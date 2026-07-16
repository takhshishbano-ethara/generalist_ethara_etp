from odoo import _, models, fields, api
from odoo.exceptions import UserError, ValidationError

from ._common import QUESTION_TYPE_SELECTION, MCQ_TYPES


TEMPLATE_STATUS_SELECTION = [
    ("draft", "Draft"),
    ("public", "Public"),
    ("archive", "Archive"),
]
TEMPLATE_STATUS_KEYS = {k for k, _ in TEMPLATE_STATUS_SELECTION}


class EtpApplicantAssessmentTemplate(models.Model):
    _name = "etp.applicant.assessment.template"
    _description = "Assessment Template (job-scoped)"
    _order = "name"

    name = fields.Char(required=True)
    active = fields.Boolean(default=True)
    status = fields.Selection(
        TEMPLATE_STATUS_SELECTION,
        default="draft", required=True, index=True,
        help="Draft = editable, not offered to candidates. "
             "Public = available to be assigned to candidates. "
             "Archive = retired, no longer offered.",
    )
    job_id = fields.Many2one(
        "hr.job", string="Job Position", ondelete="cascade",
        help="Job this template is attached to. One template per job (typical).",
    )
    description = fields.Html(
        string="Candidate Instructions",
        help="Shown on the portal instructions screen before Start.",
    )
    duration_minutes = fields.Integer(
        string="Duration (minutes)", default=30,
        help="Hard time limit. 0 = no limit.",
    )
    pass_mark_percent = fields.Float(
        string="Pass Mark (%)", default=60.0,
        help="Final score threshold for PASS.",
    )

    warning_cap = fields.Integer(
        string="Warning Cap",
        default=5,
        help="Auto-submit when this many warnings accumulate. 0 disables auto-submit.",
    )
    penalty_other_person = fields.Float(
        string="Penalty: Other Person (%)", default=10.0,
    )
    penalty_mobile_phone = fields.Float(
        string="Penalty: Mobile Phone (%)", default=10.0,
    )
    penalty_lip_movement = fields.Float(
        string="Penalty: Lip Movement (%)", default=2.0,
    )
    penalty_window_change = fields.Float(
        string="Penalty: Tab/Window Switch (%)", default=5.0,
    )
    penalty_no_face = fields.Float(
        string="Penalty: Candidate Not Visible (%)", default=5.0,
    )
    penalty_look_away = fields.Float(
        string="Penalty: Looking Away (%)", default=3.0,
    )

    require_webcam = fields.Boolean(default=True)
    require_mic = fields.Boolean(default=False)
    require_fullscreen = fields.Boolean(default=True)
    block_copy_paste = fields.Boolean(
        default=True,
        help="Block copy / cut / paste keyboard shortcuts during the test.",
    )
    block_right_click = fields.Boolean(
        default=True,
        help="Suppress the right-click context menu during the test.",
    )
    detect_window_switch = fields.Boolean(
        default=True,
        help="Log a warning when the candidate switches tab / minimizes the window.",
    )
    detect_no_face = fields.Boolean(
        default=True,
        help="AI check: warn when the candidate's face is not visible in the webcam.",
    )
    detect_other_person = fields.Boolean(
        default=True,
        help="AI check: warn when more than one face is detected in the webcam frame.",
    )
    detect_look_away = fields.Boolean(
        default=True,
        help="AI check: warn when the candidate looks away from the screen (yaw / pitch beyond threshold).",
    )
    detect_lip_movement = fields.Boolean(
        default=True,
        help="AI check: warn on sustained lip movement suggesting the candidate is speaking.",
    )
    detect_mobile_phone = fields.Boolean(
        default=True,
        help="AI check: warn when a mobile phone is detected in the webcam frame.",
    )
    shuffle_questions = fields.Boolean(
        default=False,
        help="Randomise question order within each section per candidate. "
             "Section order stays deterministic (by section sequence).",
    )

    # --- Candidate-facing extras (accepted by the create/update API) ---
    instructions = fields.Text(
        string="Pre-start Instructions",
        help="Plain-text instructions shown to the candidate before Start. "
             "Complements the rich 'description' field.",
    )
    consent_text = fields.Text(
        string="Consent / Declaration Text",
        help="Consent / declaration the candidate must accept before starting.",
    )
    attempts_allowed = fields.Integer(
        string="Attempts Allowed", default=1,
        help="How many times a candidate may take this assessment. "
             "0 = unlimited.",
    )
    negative_factor = fields.Float(
        string="Negative Factor (x marks)", default=0.0,
        help="Global fraction of a question's marks deducted for a wrong "
             "answer when 'Enable Negative Marking' is on.",
    )
    available_from = fields.Date(
        string="Available From",
        help="Candidates cannot start the assessment before this date.",
    )
    available_until = fields.Date(
        string="Available Until",
        help="Candidates cannot start the assessment after this date.",
    )
    enable_negative_marking = fields.Boolean(
        string="Enable Negative Marking (global)", default=False,
    )
    show_score_to_candidate = fields.Boolean(
        string="Show Score to Candidate", default=False,
        help="Reveal the final score to the candidate after submission.",
    )
    randomize_section_order = fields.Boolean(
        string="Randomize Section Order", default=False,
        help="Shuffle the order of sections per candidate.",
    )
    shuffle_mcq_options = fields.Boolean(
        string="Shuffle MCQ Options", default=False,
        help="Shuffle the order of options within each MCQ per candidate.",
    )
    sync_to_google_sheet = fields.Boolean(
        string="Sync Responses to Google Sheet", default=False,
        help="Mirror candidate responses to a connected Google Sheet.",
    )

    section_ids = fields.One2many(
        "etp.applicant.assessment.template.section",
        "template_id",
        string="Sections",
    )
    question_ids = fields.One2many(
        "etp.applicant.assessment.template.question",
        "template_id",
        string="Questions",
    )
    question_count = fields.Integer(
        compute="_compute_question_count", store=True,
    )
    max_score = fields.Integer(
        compute="_compute_max_score", store=True,
        string="Total Marks",
    )
    assigned_count = fields.Integer(
        compute="_compute_assigned_count",
        string="Assigned Count",
    )

    @api.depends("question_ids")
    def _compute_question_count(self):
        for rec in self:
            rec.question_count = len(rec.question_ids)

    @api.depends("question_ids.marks")
    def _compute_max_score(self):
        for rec in self:
            rec.max_score = sum(rec.question_ids.mapped("marks"))

    def _compute_assigned_count(self):
        Assessment = self.env["etp.applicant.assessment"]
        grouped = Assessment.read_group(
            [("template_id", "in", self.ids)],
            ["template_id"],
            ["template_id"],
        )
        counts = {g["template_id"][0]: g["template_id_count"] for g in grouped}
        for rec in self:
            rec.assigned_count = counts.get(rec.id, 0)

    @api.constrains("pass_mark_percent")
    def _check_pass_mark(self):
        for rec in self:
            if not 0 <= rec.pass_mark_percent <= 100:
                raise ValidationError("Pass mark must be between 0 and 100.")


class EtpApplicantAssessmentTemplateQuestion(models.Model):
    _name = "etp.applicant.assessment.template.question"
    _description = "Assessment Template Question"
    _order = "sequence, id"

    template_id = fields.Many2one(
        "etp.applicant.assessment.template",
        required=True, ondelete="cascade",
    )
    section_id = fields.Many2one(
        "etp.applicant.assessment.template.section",
        string="Section", ondelete="set null", index=True,
    )
    sequence = fields.Integer(default=10)
    prompt = fields.Text(required=True, string="Question")
    question_type = fields.Selection(
        QUESTION_TYPE_SELECTION,
        default="mcq_single",
        required=True,
    )
    marks = fields.Integer(default=1, required=True)
    negative_marks = fields.Integer(
        default=0, required=True,
        help="Marks deducted on a wrong answer (0 disables negative marking).",
    )
    bank_question_id = fields.Many2one(
        "etp.applicant.assessment.question.bank",
        string="Source Bank Question",
        ondelete="set null", index=True,
        help="If this row was cloned from the question bank, this is the source. "
             "Editing the bank does NOT propagate to this row.",
    )
    option_ids = fields.One2many(
        "etp.applicant.assessment.template.option",
        "question_id",
        string="Options",
    )
    option_count = fields.Integer(
        compute="_compute_option_count", store=True,
    )

    @api.depends("option_ids")
    def _compute_option_count(self):
        for rec in self:
            rec.option_count = len(rec.option_ids)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get("template_id") and vals.get("section_id"):
                section = self.env[
                    "etp.applicant.assessment.template.section"
                ].browse(vals["section_id"])
                if section.exists():
                    vals["template_id"] = section.template_id.id
        return super().create(vals_list)

    @api.onchange("section_id")
    def _onchange_section_id(self):
        for rec in self:
            if rec.section_id and not rec.template_id:
                rec.template_id = rec.section_id.template_id

    @api.constrains("marks")
    def _check_marks(self):
        for rec in self:
            if rec.marks <= 0:
                raise ValidationError("Marks must be a positive integer.")

    @api.constrains("negative_marks")
    def _check_negative_marks(self):
        for rec in self:
            if rec.negative_marks < 0:
                raise ValidationError("Negative marks cannot be less than 0.")

    @api.constrains("question_type", "option_ids")
    def _check_options_for_mcq(self):
        for rec in self:
            if rec.question_type in MCQ_TYPES:
                if len(rec.option_ids) < 2:
                    raise ValidationError(
                        "MCQ-style questions need at least two options."
                    )
                if not any(o.is_correct for o in rec.option_ids):
                    raise ValidationError(
                        "MCQ-style questions need at least one correct option."
                    )

    def action_promote_to_bank(self):
        self.ensure_one()
        if self.bank_question_id:
            raise UserError(
                _("This question is already linked to bank entry %r.")
                % (self.bank_question_id.name or self.bank_question_id.id)
            )
        Bank = self.env["etp.applicant.assessment.question.bank"]
        first_line = (self.prompt or "").strip().split("\n", 1)[0].strip()
        name = first_line[:80] or _("Untitled bank question")
        bank_q = Bank.create({
            "name": name,
            "prompt": self.prompt or "",
            "question_type": self.question_type,
            "marks": self.marks,
            "negative_marks": self.negative_marks,
            "option_ids": [
                (0, 0, {
                    "sequence": opt.sequence,
                    "label": opt.label,
                    "is_correct": opt.is_correct,
                })
                for opt in self.option_ids.sorted("sequence")
            ],
        })
        self.bank_question_id = bank_q.id
        return {
            "type": "ir.actions.act_window",
            "name": _("Bank Question"),
            "res_model": "etp.applicant.assessment.question.bank",
            "res_id": bank_q.id,
            "view_mode": "form",
            "target": "current",
        }


class EtpApplicantAssessmentTemplateOption(models.Model):
    _name = "etp.applicant.assessment.template.option"
    _description = "Assessment Template Option"
    _order = "sequence, id"

    question_id = fields.Many2one(
        "etp.applicant.assessment.template.question",
        required=True, ondelete="cascade",
    )
    sequence = fields.Integer(default=10)
    label = fields.Char(required=True)
    is_correct = fields.Boolean(default=False)


class EtpApplicantAssessmentTemplateSection(models.Model):
    _name = "etp.applicant.assessment.template.section"
    _description = "Assessment Template Section"
    _order = "sequence, id"

    template_id = fields.Many2one(
        "etp.applicant.assessment.template",
        required=True, ondelete="cascade", index=True,
    )
    sequence = fields.Integer(default=10)
    name = fields.Char(required=True, string="Section Title")
    description = fields.Char(
        string="Section Description",
        help="Short blurb shown above the section's questions on the test screen.",
    )
    question_ids = fields.One2many(
        "etp.applicant.assessment.template.question",
        "section_id",
        string="Questions",
    )
    question_count = fields.Integer(
        compute="_compute_rollups", store=True,
    )
    total_marks = fields.Integer(
        compute="_compute_rollups", store=True,
    )

    @api.depends("question_ids", "question_ids.marks")
    def _compute_rollups(self):
        for rec in self:
            rec.question_count = len(rec.question_ids)
            rec.total_marks = sum(rec.question_ids.mapped("marks"))
