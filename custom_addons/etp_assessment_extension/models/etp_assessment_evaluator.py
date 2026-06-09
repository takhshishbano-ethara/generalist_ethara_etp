# -*- coding: utf-8 -*-
"""API serializers for `etp.assessment.evaluator` (candidate assignment)."""

from odoo import models


def _pct(part, whole):
    if not whole:
        return 0.0
    return round((part / whole) * 100.0, 2)


class EtpAssessmentEvaluator(models.Model):
    _inherit = "etp.assessment.evaluator"

    def to_api_dict(self, state_labels=None):
        """Admin-side payload: full assignment detail + score + violation info."""
        self.ensure_one()
        if state_labels is None:
            state_labels = dict(self._fields["state"].selection)
        emp = self.employee_id
        return {
            "id": self.id,
            "assessment_id": self.assessment_id.id if self.assessment_id else 0,
            "employee_id": emp.id if emp else 0,
            "employee_name": emp.name if emp else "",
            "employee_email": (
                (emp.work_email or emp.private_email) if emp else ""
            ),
            "state": self.state,
            "state_label": state_labels.get(self.state, ""),
            "access_token": self.access_token or "",
            "started_at": (
                self.started_at.isoformat() if self.started_at else None
            ),
            "deadline_datetime": (
                self.deadline_datetime.isoformat()
                if self.deadline_datetime else None
            ),
            "total_questions": self.total_questions or 0,
            "answered_count": self.answered_count or 0,
            "progress_percent": _pct(self.answered_count, self.total_questions),
            "total_score": self.total_score or 0,
            "max_possible_score": self.max_possible_score or 0,
            "is_locked": bool(self.is_locked),
            "is_violated": bool(self.is_violated),
            "violation_reason": self.violation_reason or "",
            "violation_datetime": (
                self.violation_datetime.isoformat()
                if self.violation_datetime else None
            ),
        }

    def to_portal_dict(self):
        """Candidate-facing payload: no access_token, no violation_datetime.

        Used by `/portal/<token>/...` responses where the candidate already
        proved ownership of the token — we don't need to echo it back.
        """
        self.ensure_one()
        emp = self.employee_id
        return {
            "id": self.id,
            "name": emp.name if emp else "",
            "email": (
                (emp.work_email or emp.private_email or "") if emp else ""
            ),
            "state": self.state,
            "started_at": (
                self.started_at.isoformat() if self.started_at else None
            ),
            "deadline_iso": (
                self.deadline_datetime.strftime("%Y-%m-%dT%H:%M:%SZ")
                if self.deadline_datetime else None
            ),
            "total_questions": self.total_questions or 0,
            "answered_count": self.answered_count or 0,
            "progress_percent": _pct(self.answered_count, self.total_questions),
            "is_locked": bool(self.is_locked),
            "is_violated": bool(self.is_violated),
            "violation_reason": self.violation_reason or "",
        }
