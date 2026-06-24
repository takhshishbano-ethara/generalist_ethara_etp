from __future__ import annotations

from odoo import api, fields, models


class ResUsers(models.Model):
    _inherit = "res.users"

    lynceus_last_activity_at = fields.Datetime(
        string="Lynceus Last Activity",
        copy=False,
        help="Last time this user reported a Submitted or Bad outcome. "
             "Resets the 24h reclaim timer. Marking 'Untouched' does NOT reset.",
    )
    lynceus_daily_quota = fields.Integer(
        string="Lynceus Daily Quota",
        default=20,
        help="Maximum number of prompts to allocate to this user per Active List import.",
    )
    lynceus_active_today = fields.Boolean(
        string="Active Today (Lynceus)",
        copy=False,
        default=False,
        help="Set by the Import Active Taskers wizard. Cleared at midnight by cron.",
    )
    lynceus_assigned_count = fields.Integer(
        string="Lynceus Open Queue",
        compute="_compute_lynceus_assigned_count",
        help="Number of prompts currently in ASSIGNED state for this user.",
    )

    @api.depends_context("uid")
    def _compute_lynceus_assigned_count(self):
        Prompt = self.env["lynceus.prompt"].sudo()
        for user in self:
            user.lynceus_assigned_count = Prompt.search_count([
                ("assigned_user_id", "=", user.id),
                ("state", "=", "assigned"),
            ])
