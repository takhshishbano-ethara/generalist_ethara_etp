from __future__ import annotations

from odoo import fields, models


class LynceusHistory(models.Model):
    _name = "lynceus.history"
    _description = "Lynceus Prompt Content History (SHA256 dedup registry)"
    _order = "create_date desc"
    _rec_name = "content_hash"

    content_hash = fields.Char(
        string="Content SHA256",
        required=True,
        index=True,
        copy=False,
        help="SHA256 hex digest of the normalized prompt content. "
             "Used to reject duplicate LLM output even across batches and "
             "even with different generated IDs.",
    )
    lynceus_id = fields.Char(
        string="Lynceus Prompt ID",
        index=True,
        copy=False,
        help="The unique ID assigned to the prompt when it first entered "
             "the pool. Recorded here so a USED/BAD prompt can still be "
             "traced back if its content fingerprint is rejected later.",
    )
    batch_id = fields.Many2one(
        "lynceus.batch",
        string="Origin Batch",
        ondelete="set null",
        index=True,
    )
    rejected_as_duplicate = fields.Boolean(
        string="Rejected as Duplicate",
        default=False,
        help="True when this hash was seen during a later batch and the "
             "duplicate output was discarded. Useful for tracking dedup "
             "rejection rate over time.",
    )

    _sql_constraints = [
        (
            "lynceus_history_content_hash_uniq",
            "UNIQUE(content_hash)",
            "Content hash must be unique across the entire history.",
        ),
    ]
