# -*- coding: utf-8 -*-
"""API serializer for `etp.assessment`."""

from odoo import models


def _pct(part, whole):
    if not whole:
        return 0.0
    return round((part / whole) * 100.0, 2)


class EtpAssessment(models.Model):
    _inherit = "etp.assessment"

    def to_api_dict(self, state_labels=None):
        """Return the JSON payload used by the assessments endpoints."""
        self.ensure_one()
        if state_labels is None:
            state_labels = dict(self._fields["state"].selection)
        total = len(self.assessment_evaluator_ids)
        done = sum(
            1 for ev in self.assessment_evaluator_ids
            if ev.state == "submitted"
        )
        return {
            "id": self.id,
            "name": self.name or "",
            "state": self.state,
            "state_label": state_labels.get(self.state, ""),
            "category_id": self.category_id.id if self.category_id else 0,
            "category_name": self.category_id.name if self.category_id else "",
            "question_limit": self.question_limit or 0,
            "total_questions_available": self.total_questions_available or 0,
            "duration_minutes": self.duration_minutes or 0,
            "start_date": self.start_date.isoformat() if self.start_date else None,
            "end_date": self.end_date.isoformat() if self.end_date else None,
            "deadline": self.deadline.isoformat() if self.deadline else None,
            "question_ids": self.question_ids.ids,
            "candidate_ids": self.evaluator_ids.ids,
            "evaluators_total": total,
            "evaluators_done": done,
            "progress_percent": _pct(done, total),
            "response_count": self.response_count or 0,
            "create_date": (
                self.create_date.isoformat() if self.create_date else None
            ),
            "write_date": (
                self.write_date.isoformat() if self.write_date else None
            ),
        }

    def to_brief_dict(self):
        """Minimal payload used by the candidate portal (no candidate roster)."""
        self.ensure_one()
        return {
            "id": self.id,
            "name": self.name or "",
            "duration_minutes": self.duration_minutes or 0,
            "state": self.state,
        }
