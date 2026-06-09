# -*- coding: utf-8 -*-
"""API serializers for `etp.assessment.category`."""

from odoo import models


class EtpAssessmentCategory(models.Model):
    _inherit = "etp.assessment.category"

    def to_api_dict(self):
        """Return the JSON payload used by the REST list/detail endpoints."""
        self.ensure_one()
        return {
            "id": self.id,
            "name": self.name or "",
            "sequence": self.sequence or 0,
            "active": bool(self.active),
            "description": self.description or "",
            "question_count": self.question_count or 0,
            "create_date": (
                self.create_date.isoformat() if self.create_date else None
            ),
            "write_date": (
                self.write_date.isoformat() if self.write_date else None
            ),
        }
