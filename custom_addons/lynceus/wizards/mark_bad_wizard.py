from __future__ import annotations

from odoo import _, fields, models
from odoo.exceptions import UserError


class LynceusMarkBadWizard(models.TransientModel):
    _name = "lynceus.mark.bad.wizard"
    _description = "Lynceus Mark Prompt Bad Wizard"

    prompt_id = fields.Many2one(
        "lynceus.prompt",
        string="Prompt",
        required=True,
        default=lambda self: self.env.context.get("default_prompt_id"),
    )
    bad_remarks = fields.Text(
        string="Bad Remarks (mandatory)",
        required=True,
    )

    def action_confirm(self):
        self.ensure_one()
        if not self.bad_remarks or not self.bad_remarks.strip():
            raise UserError(_("Bad remarks are mandatory - describe the MultiMango issue."))
        return self.prompt_id.action_mark_bad(self.bad_remarks.strip())
