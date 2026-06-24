from __future__ import annotations

from odoo import _, fields, models
from odoo.exceptions import UserError


class LynceusGenerateBatchWizard(models.TransientModel):
    _name = "lynceus.generate.batch.wizard"
    _description = "Lynceus Generate Batch Wizard"

    target_n = fields.Integer(
        string="Target Prompts (N)",
        required=True,
        default=lambda self: int(
            self.env["ir.config_parameter"].sudo().get_param("lynceus.default_batch_size", "3000") or "3000"
        ),
        help="Net unique prompts to add to the pool. The orchestrator will keep "
             "calling Anthropic until N unique prompts pass content-hash dedup "
             "(or it hits the internal attempt cap of N*3 calls).",
    )

    def action_generate(self):
        self.ensure_one()
        if self.target_n <= 0:
            raise UserError(_("Target N must be a positive integer."))

        ICP = self.env["ir.config_parameter"].sudo()
        provider = ICP.get_param("lynceus.provider", "anthropic")
        if provider == "openrouter":
            if not ICP.get_param("lynceus.openrouter_api_key", ""):
                raise UserError(_(
                    "OpenRouter API key is not configured. Set it in "
                    "Settings -> Lynceus first (provider is currently OpenRouter)."
                ))
        else:
            if not ICP.get_param("lynceus.anthropic_api_key", ""):
                raise UserError(_(
                    "Anthropic API key is not configured. Set it in "
                    "Settings -> Lynceus first (provider is currently Anthropic)."
                ))

        batch = self.env["lynceus.batch"].create({
            "target_n": self.target_n,
            "state": "pending",
        })

        self.env.user._bus_send("simple_notification", {
            "title": _("Batch Queued"),
            "message": _(
                "Batch %s is pending. It will start within 1 minute. "
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
