from __future__ import annotations

from odoo import fields, models


class LynceusAssignmentLog(models.Model):
    _name = "lynceus.assignment.log"
    _description = "Lynceus Assignment History (G1 enforcement)"
    _order = "allocated_at desc"
    _rec_name = "id"

    user_id = fields.Many2one(
        "res.users",
        string="Tasker",
        required=True,
        ondelete="restrict",
        index=True,
    )
    prompt_id = fields.Many2one(
        "lynceus.prompt",
        string="Prompt",
        required=True,
        ondelete="restrict",
        index=True,
    )
    allocated_at = fields.Datetime(
        string="Allocated At",
        required=True,
        default=fields.Datetime.now,
        index=True,
    )
    reclaimed_at = fields.Datetime(
        string="Reclaimed At",
        copy=False,
        help="Set when the prompt was reclaimed back to the pool from this "
             "tasker. Does NOT delete the row - G1 still applies forever.",
    )

    _sql_constraints = [
        (
            "lynceus_assignment_log_user_prompt_uniq",
            "UNIQUE(user_id, prompt_id)",
            "A tasker can only be linked to a given prompt once - ever.",
        ),
    ]
