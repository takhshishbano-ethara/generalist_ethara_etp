# -*- coding: utf-8 -*-
"""API serializers for the question bank.

Covers:
  - etp.assessment.question
  - etp.assessment.question.dimension
  - etp.assessment.question.dimension.option

The portal payload exposes the master dimension options (not the per-question
ones), since the candidate picks a master option and scoring compares against
the question's `is_correct` flag on the per-question row.
"""

from odoo import models


class EtpAssessmentQuestion(models.Model):
    _inherit = "etp.assessment.question"

    def to_api_dict(self, type_labels=None):
        """Admin-side payload: full question detail with dimensions + options."""
        self.ensure_one()
        if type_labels is None:
            type_labels = dict(self._fields["question_type"].selection)
        return {
            "id": self.id,
            "name": self.name or "",
            "sequence": self.sequence or 0,
            "question_type": self.question_type or "",
            "question_type_label": type_labels.get(self.question_type or "", ""),
            "prompt": self.prompt or "",
            "description": self.description or "",
            "active": bool(self.active),
            "category_id": self.category_id.id if self.category_id else 0,
            "category_name": self.category_id.name if self.category_id else "",
            "image_a_url": self.image_a_url or "",
            "image_b_url": self.image_b_url or "",
            "code_snippet": self.code_snippet or "",
            "code_language": self.code_language or "",
            "video_url": self.video_url or "",
            "dimension_count": len(self.question_dimension_ids),
            "dimensions": [
                qd.to_api_dict()
                for qd in self.question_dimension_ids.sorted("sequence")
            ],
            "create_date": (
                self.create_date.isoformat() if self.create_date else None
            ),
            "write_date": (
                self.write_date.isoformat() if self.write_date else None
            ),
        }

    def to_portal_dict(self):
        """Candidate-facing payload: only the fields needed to render and answer.

        The dimension options here come from the master dimension (so the
        candidate sees `etp.assessment.dimension.option`, not the per-question
        rows). Correctness is computed server-side in `etp.assessment.response`.
        """
        self.ensure_one()
        dimensions = []
        for qd in self.question_dimension_ids.sorted("sequence"):
            dim = qd.dimension_id
            if not dim:
                continue
            dimensions.append({
                "dimension_id": dim.id,
                "name": dim.name or "",
                "sequence": qd.sequence or 0,
                "options": [
                    {
                        "id": opt.id,
                        "name": opt.name or "",
                        "sequence": opt.sequence or 0,
                    }
                    for opt in dim.option_ids.sorted("sequence")
                ],
            })

        return {
            "id": self.id,
            "name": self.name or "",
            "question_type": self.question_type or "",
            "prompt": self.prompt or "",
            "description": self.description or "",
            "image_a_url": self.image_a_url or "",
            "image_b_url": self.image_b_url or "",
            "code_snippet": self.code_snippet or "",
            "code_language": self.code_language or "",
            "video_url": self.video_url or "",
            "dimensions": dimensions,
        }


class EtpAssessmentQuestionDimension(models.Model):
    _inherit = "etp.assessment.question.dimension"

    def to_api_dict(self):
        self.ensure_one()
        return {
            "id": self.id,
            "dimension_id": self.dimension_id.id if self.dimension_id else 0,
            "dimension_name": self.dimension_id.name if self.dimension_id else "",
            "sequence": self.sequence or 0,
            "options": [
                line.to_api_dict()
                for line in self.option_line_ids.sorted("sequence")
            ],
        }


class EtpAssessmentQuestionDimensionOption(models.Model):
    _inherit = "etp.assessment.question.dimension.option"

    def to_api_dict(self):
        self.ensure_one()
        return {
            "id": self.id,
            "master_option_id": (
                self.master_option_id.id if self.master_option_id else 0
            ),
            "name": self.name or "",
            "sequence": self.sequence or 0,
            "is_correct": bool(self.is_correct),
            "score": self.score or 0,
        }
