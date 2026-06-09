# -*- coding: utf-8 -*-
"""API serializers for `etp.assessment.response` and its lines."""

from odoo import models


class EtpAssessmentResponse(models.Model):
    _inherit = "etp.assessment.response"

    def to_api_dict(self, state_labels=None):
        self.ensure_one()
        if state_labels is None:
            state_labels = dict(self._fields["state"].selection)
        return {
            "id": self.id,
            "assessment_id": self.assessment_id.id if self.assessment_id else 0,
            "assessment_name": (
                self.assessment_id.name if self.assessment_id else ""
            ),
            "assessment_evaluator_id": (
                self.assessment_evaluator_id.id
                if self.assessment_evaluator_id else 0
            ),
            "evaluator_id": self.evaluator_id.id if self.evaluator_id else 0,
            "evaluator_name": self.evaluator_id.name if self.evaluator_id else "",
            "question_id": self.question_id.id if self.question_id else 0,
            "question_name": self.question_id.name if self.question_id else "",
            "justification": self.justification or "",
            "state": self.state,
            "state_label": state_labels.get(self.state, ""),
            "score": self.score or 0,
            "max_score": self.max_score or 0,
            "lines": [line.to_api_dict() for line in self.line_ids],
            "create_date": (
                self.create_date.isoformat() if self.create_date else None
            ),
            "write_date": (
                self.write_date.isoformat() if self.write_date else None
            ),
        }


class EtpAssessmentResponseLine(models.Model):
    _inherit = "etp.assessment.response.line"

    def to_api_dict(self):
        self.ensure_one()
        return {
            "id": self.id,
            "dimension_id": self.dimension_id.id if self.dimension_id else 0,
            "dimension_name": (
                self.dimension_id.name if self.dimension_id else ""
            ),
            "selected_option_id": (
                self.selected_option_id.id if self.selected_option_id else 0
            ),
            "selected_option_name": (
                self.selected_option_id.name if self.selected_option_id else ""
            ),
        }
