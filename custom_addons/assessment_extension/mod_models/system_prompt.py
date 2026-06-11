from odoo import api, fields, models
from odoo.exceptions import ValidationError


class ModAssessmentSystemPrompt(models.Model):
    _name = "etp.assessment.system.prompt"
    _description = "Assessment System Prompt"
    _order = "is_current desc, version_label desc, id desc"

    version_label = fields.Char(
        string="Version Label",
        required=True,
        help="Human-readable version chip, e.g. 'Generalist v3'.",
    )
    body = fields.Text(
        string="Prompt Body",
        required=True,
        help="Verbatim text shown in MOD-View-System-Prompt drawer.",
    )
    is_current = fields.Boolean(
        string="Is Current",
        default=False,
        help=(
            "Exactly one active prompt may be marked current; the API "
            "endpoint /system_prompt/current returns this record."
        ),
    )
    is_active = fields.Boolean(
        string="Active",
        default=True,
    )
    notes = fields.Text(
        string="Internal Notes",
        help="Operator-only notes. NEVER exposed via the API surface.",
    )

    @api.constrains("is_current", "is_active")
    def _check_single_current(self):
        for rec in self:
            if not (rec.is_current and rec.is_active):
                continue
            others = self.search([
                ("id", "!=", rec.id),
                ("is_current", "=", True),
                ("is_active", "=", True),
            ], limit=1)
            if others:
                raise ValidationError(
                    "Only one active System Prompt can be marked "
                    "'is_current' at a time. Demote '%s' first."
                    % others.version_label
                )

    def to_api_dict(self):
        self.ensure_one()
        return {
            "id": self.id,
            "version_label": self.version_label or "",
            "body": self.body or "",
            "is_current": bool(self.is_current),
            "is_active": bool(self.is_active),
        }
