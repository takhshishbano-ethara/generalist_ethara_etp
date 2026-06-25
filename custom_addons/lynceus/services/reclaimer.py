from __future__ import annotations

import logging
from datetime import timedelta

from odoo import fields

_logger = logging.getLogger(__name__)


def sweep(env, reclaim_hours: int | None = None) -> int:
    if reclaim_hours is None:
        reclaim_hours = int(
            env["ir.config_parameter"].sudo().get_param("lynceus.reclaim_hours", "12") or "12"
        )
    cutoff = fields.Datetime.now() - timedelta(hours=reclaim_hours)

    Prompt = env["lynceus.prompt"].sudo()
    candidates = Prompt.search([
        ("state", "=", "assigned"),
        ("outcome", "in", (False, "untouched")),
        ("assigned_user_id", "!=", False),
        "|",
        ("assigned_user_id.lynceus_last_activity_at", "=", False),
        ("assigned_user_id.lynceus_last_activity_at", "<", cutoff),
    ])
    if not candidates:
        return 0

    AssignmentLog = env["lynceus.assignment.log"].sudo()
    now = fields.Datetime.now()
    for prompt in candidates:
        AssignmentLog.search([
            ("user_id", "=", prompt.assigned_user_id.id),
            ("prompt_id", "=", prompt.id),
            ("reclaimed_at", "=", False),
        ]).write({"reclaimed_at": now})

    candidates.write({
        "state": "available",
        "assigned_user_id": False,
        "assigned_at": False,
        "outcome": False,
        "outcome_at": False,
        "reclaimed_at": now,
        "reclaim_count": 0,
    })
    for prompt in candidates:
        prompt.reclaim_count = prompt.reclaim_count + 1

    _logger.info("Lynceus reclaim: returned %d prompts to AVAILABLE", len(candidates))
    return len(candidates)


def cron_reclaim(env) -> None:
    sweep(env)


def cron_pool_depletion_alert(env) -> None:
    ICP = env["ir.config_parameter"].sudo()
    threshold = int(ICP.get_param("lynceus.pool_depletion_threshold", "500") or "500")
    available = env["lynceus.prompt"].sudo().search_count([("state", "=", "available")])
    if available < threshold:
        _logger.warning(
            "Lynceus pool depletion: %d AVAILABLE prompts (threshold %d). "
            "Trigger a new generation batch.",
            available, threshold,
        )
