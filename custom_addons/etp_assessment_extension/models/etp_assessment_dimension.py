# -*- coding: utf-8 -*-
"""API serializers for `etp.assessment.dimension` and its options."""

from odoo import models


class EtpAssessmentDimension(models.Model):
    _inherit = "etp.assessment.dimension"

    def to_api_dict(self, with_options=True):
        """Return the JSON payload used by the dimensions endpoints.

        Set `with_options=False` to omit the nested options array (useful for
        list endpoints that already return `option_count`).
        """
        self.ensure_one()
        data = {
            "id": self.id,
            "name": self.name or "",
            "sequence": self.sequence or 0,
            "active": bool(self.active),
            "option_count": self.option_count or 0,
            "options_display": self.options_display or "",
        }
        if with_options:
            data["options"] = [
                opt.to_api_dict()
                for opt in self.option_ids.sorted("sequence")
            ]
        return data


class EtpAssessmentDimensionOption(models.Model):
    _inherit = "etp.assessment.dimension.option"

    def to_api_dict(self):
        self.ensure_one()
        return {
            "id": self.id,
            "name": self.name or "",
            "sequence": self.sequence or 0,
            "dimension_id": self.dimension_id.id if self.dimension_id else 0,
        }
