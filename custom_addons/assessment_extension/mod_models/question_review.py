from odoo import api, fields, models


class ModAssessmentQuestionReview(models.Model):
    _name = "etp.assessment.question.review"
    _description = "Per-Assessment Question Review"
    _order = "assessment_id, day_number, day_sequence, id"
    _sql_constraints = [
        (
            "review_question_unique_per_assessment",
            "UNIQUE(assessment_id, question_id)",
            "A question can only be reviewed once per assessment.",
        ),
        (
            "review_slot_unique_per_assessment",
            "UNIQUE(assessment_id, day_number, day_sequence)",
            "Day-sequence slots must be unique within an assessment.",
        ),
    ]

    assessment_id = fields.Many2one(
        "etp.assessment",
        string="Assessment",
        required=True,
        ondelete="cascade",
        index=True,
    )
    question_id = fields.Many2one(
        "etp.assessment.question",
        string="Question",
        required=True,
        ondelete="cascade",
        index=True,
    )
    review_state = fields.Selection(
        [
            ("draft", "Draft"),
            ("approved", "Approved"),
            ("regenerating", "Regenerating"),
        ],
        default="draft",
        required=True,
        string="Review State",
    )
    approved_by_id = fields.Many2one(
        "res.users",
        string="Approved By",
        readonly=True,
    )
    approved_at = fields.Datetime(
        string="Approved At",
        readonly=True,
    )
    day_number = fields.Integer(
        string="Day #",
        default=1,
        help="1-based day index inside the assessment window.",
    )
    day_sequence = fields.Integer(
        string="Seq",
        default=1,
        help="1-based question sequence within the day.",
    )

    def _to_question_code(self):
        self.ensure_one()
        return "D{day}-Q{seq:02d}".format(
            day=self.day_number or 1,
            seq=self.day_sequence or 1,
        )

    def _set_state(self, state, by_user=None):
        self.ensure_one()
        vals = {"review_state": state}
        if state == "approved":
            vals["approved_by_id"] = (
                by_user.id if by_user else self.env.uid
            )
            vals["approved_at"] = fields.Datetime.now()
        else:
            vals["approved_by_id"] = False
            vals["approved_at"] = False
        self.write(vals)

    def to_api_dict(self):
        self.ensure_one()
        approved_by = self.approved_by_id
        return {
            "id": self.id,
            "assessment_id": self.assessment_id.id if self.assessment_id else 0,
            "question_id": self.question_id.id if self.question_id else 0,
            "question_code": self._to_question_code(),
            "question_type": (
                self.question_id.question_type
                if self.question_id else False
            ),
            "review_state": self.review_state,
            "day_number": self.day_number or 0,
            "day_sequence": self.day_sequence or 0,
            "approved_by": (
                {"id": approved_by.id, "name": approved_by.name}
                if approved_by else False
            ),
            "approved_at": (
                self.approved_at.isoformat()
                if self.approved_at else None
            ),
        }
