from __future__ import annotations

import hashlib
import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)


STATE_SELECTION = [
    ("available", "Available"),
    ("assigned", "Assigned"),
    ("used", "Used"),
    ("bad", "Bad"),
]
TERMINAL_STATES = {"used", "bad"}


OUTCOME_SELECTION = [
    ("submitted", "Yes - Submitted on MultiMango"),
    ("bad", "No - Bad (with remarks)"),
    ("untouched", "No - Untouched"),
]


TIER_SELECTION = [("dense", "Dense"), ("medium", "Medium")]


class LynceusPrompt(models.Model):
    _name = "lynceus.prompt"
    _description = "Lynceus Prompt"
    _order = "create_date desc, id desc"
    _rec_name = "lynceus_id"

    lynceus_id = fields.Char(
        string="Prompt ID",
        required=True,
        index=True,
        copy=False,
        default=lambda self: self.env["ir.sequence"].next_by_code("lynceus.prompt.id.seq") or "/",
        help="Permanent unique ID assigned at pool entry. Never reused.",
    )
    content = fields.Text(string="Prompt Content", required=True)
    content_hash = fields.Char(
        string="Content SHA256",
        required=True,
        index=True,
        copy=False,
        help="SHA256 of normalized content - enforces G4 (no content repeats).",
    )

    batch_id = fields.Many2one(
        "lynceus.batch",
        string="Generation Batch",
        ondelete="set null",
        index=True,
    )
    tier = fields.Selection(TIER_SELECTION, string="Density Tier")
    archetype = fields.Char(string="Archetype")
    categories = fields.Char(
        string="Categories",
        help="Comma-separated CATEGORIES= passed to the LLM for this prompt.",
    )
    seed = fields.Char(string="LLM Seed", copy=False)

    state = fields.Selection(
        STATE_SELECTION,
        string="State",
        required=True,
        default="available",
        copy=False,
        tracking=True,
        index=True,
    )

    assigned_user_id = fields.Many2one(
        "res.users",
        string="Assigned Tasker",
        ondelete="restrict",
        copy=False,
        index=True,
        tracking=True,
    )
    assigned_at = fields.Datetime(string="Assigned At", copy=False, readonly=True)
    reclaimed_at = fields.Datetime(string="Last Reclaimed At", copy=False, readonly=True)
    reclaim_count = fields.Integer(string="Reclaim Count", default=0, copy=False)

    outcome = fields.Selection(
        OUTCOME_SELECTION,
        string="Outcome",
        copy=False,
        tracking=True,
        help="Set when the tasker reports an outcome. Submit/bad are terminal.",
    )
    outcome_at = fields.Datetime(string="Outcome Reported At", copy=False, readonly=True)
    submit_remarks = fields.Text(string="Submission Remarks (optional)", copy=False)
    bad_remarks = fields.Text(string="Bad Prompt Remarks (mandatory)", copy=False)

    company_id = fields.Many2one(
        "res.company",
        string="Company",
        default=lambda self: self.env.company,
        index=True,
    )

    _sql_constraints = [
        (
            "lynceus_prompt_id_uniq",
            "UNIQUE(lynceus_id)",
            "Lynceus prompt ID must be unique forever (G5).",
        ),
        (
            "lynceus_prompt_content_hash_uniq",
            "UNIQUE(content_hash)",
            "Prompt content must never repeat across batches (G4).",
        ),
    ]

    @api.model
    def normalize_content(self, raw: str) -> str:
        if not raw:
            return ""
        return " ".join(raw.strip().lower().split())

    @api.model
    def compute_content_hash(self, raw: str) -> str:
        return hashlib.sha256(self.normalize_content(raw).encode("utf-8")).hexdigest()

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            content = vals.get("content") or ""
            if not vals.get("content_hash"):
                vals["content_hash"] = self.compute_content_hash(content)
        records = super().create(vals_list)
        History = self.env["lynceus.history"].sudo()
        for rec in records:
            History.create({
                "content_hash": rec.content_hash,
                "lynceus_id": rec.lynceus_id,
                "batch_id": rec.batch_id.id if rec.batch_id else False,
            })
        return records

    @api.constrains("state", "assigned_user_id")
    def _check_assignment_consistency(self):
        for rec in self:
            if rec.state == "assigned" and not rec.assigned_user_id:
                raise ValidationError(_(
                    "Prompt %s is in ASSIGNED state but has no tasker (G14)."
                ) % rec.lynceus_id)
            if rec.state == "available" and rec.assigned_user_id:
                raise ValidationError(_(
                    "Prompt %s is AVAILABLE but still has a tasker assigned (G14)."
                ) % rec.lynceus_id)

    @api.constrains("state", "outcome", "bad_remarks")
    def _check_outcome_consistency(self):
        for rec in self:
            if rec.state == "bad" and not rec.bad_remarks:
                raise ValidationError(_(
                    "Prompt %s marked BAD requires bad_remarks (mandatory)."
                ) % rec.lynceus_id)
            if rec.state == "used" and rec.outcome != "submitted":
                raise ValidationError(_(
                    "Prompt %s in USED state must have outcome=submitted."
                ) % rec.lynceus_id)

    def action_submit(self, remarks: str | None = None):
        for rec in self:
            rec._guard_terminal()
            rec._guard_owner()
            rec.write({
                "state": "used",
                "outcome": "submitted",
                "outcome_at": fields.Datetime.now(),
                "submit_remarks": remarks or rec.submit_remarks,
            })
            rec._touch_activity()
        return self._action_back_to_my_queue()

    def action_mark_bad(self, remarks: str):
        if not remarks:
            raise UserError(_("Bad remarks are mandatory."))
        for rec in self:
            rec._guard_terminal()
            rec._guard_owner()
            rec.write({
                "state": "bad",
                "outcome": "bad",
                "outcome_at": fields.Datetime.now(),
                "bad_remarks": remarks,
            })
            rec._touch_activity()
        return self._action_back_to_my_queue()

    def action_mark_untouched(self):
        for rec in self:
            rec._guard_terminal()
            rec._guard_owner()
            rec.write({
                "outcome": "untouched",
                "outcome_at": fields.Datetime.now(),
            })
        return self._action_back_to_my_queue()

    def _action_back_to_my_queue(self):
        action = self.env["ir.actions.act_window"]._for_xml_id(
            "lynceus.action_lynceus_my_queue"
        )
        action["target"] = "current"
        return action

    def _guard_terminal(self):
        if self.state in TERMINAL_STATES:
            raise UserError(_(
                "Prompt %s is in terminal state %s and cannot change (G10)."
            ) % (self.lynceus_id, self.state))

    def _guard_owner(self):
        if self.assigned_user_id and self.assigned_user_id != self.env.user:
            if not self.env.user.has_group("lynceus.group_lynceus_manager"):
                raise UserError(_(
                    "Prompt %s belongs to %s - you cannot act on it (G17)."
                ) % (self.lynceus_id, self.assigned_user_id.display_name))

    def _touch_activity(self):
        if self.assigned_user_id:
            self.assigned_user_id.sudo().lynceus_last_activity_at = fields.Datetime.now()

    @api.model
    def _cron_reclaim_idle(self):
        from ..services import reclaimer
        reclaimer.cron_reclaim(self.env)

    @api.model
    def _cron_pool_depletion_alert(self):
        from ..services import reclaimer
        reclaimer.cron_pool_depletion_alert(self.env)
