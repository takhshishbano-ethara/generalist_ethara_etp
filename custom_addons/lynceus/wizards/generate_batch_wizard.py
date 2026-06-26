from __future__ import annotations

from odoo import _, fields, models
from odoo.exceptions import UserError


MAX_TARGET_N = 10000


class LynceusGenerateBatchWizard(models.TransientModel):
    _name = "lynceus.generate.batch.wizard"
    _description = "Lynceus Generate Batch Wizard"

    target_n = fields.Integer(
        string="Target Prompts (N)",
        required=True,
        default=lambda self: int(
            self.env["ir.config_parameter"].sudo().get_param("lynceus.default_batch_size", "3000") or "3000"
        ),
        help="Net unique prompts to add to the pool. Internally the "
             "orchestrator issues batched Gemini calls (each returns N "
             "prompts), de-duplicates, and creates one record per unique "
             "prompt. Hard cap: %d." % MAX_TARGET_N,
    )

    def action_generate(self):
        self.ensure_one()
        if self.target_n <= 0:
            raise UserError(_("Target N must be a positive integer."))
        if self.target_n > MAX_TARGET_N:
            raise UserError(
                _("Target N cannot exceed %d.") % MAX_TARGET_N
            )

        ICP = self.env["ir.config_parameter"].sudo()
        if not ICP.get_param("lynceus.vertex_api_key", ""):
            raise UserError(_(
                "Vertex AI API key is not configured. Set it in "
                "Settings -> Lynceus first."
            ))

        batch = self.env["lynceus.batch"].create({
            "target_n": self.target_n,
            "state": "pending",
        })

        self.env.user._bus_send("simple_notification", {
            "title": _("Batch Queued"),
            "message": _(
                "Batch %s is pending. It will start shortly. "
                "You can leave this page \u2014 progress is saved as it runs."
            ) % batch.name,
            "type": "success",
            "sticky": False,
        })

        return {
            "type": "ir.actions.act_window",
            "name": _("Generation Batch"),
            "res_model": "lynceus.batch",
            "res_id": batch.id,
            "view_mode": "form",
            "target": "current",
        }
