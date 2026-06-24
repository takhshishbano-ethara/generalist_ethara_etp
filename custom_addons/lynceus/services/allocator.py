from __future__ import annotations

import logging

from odoo import fields

_logger = logging.getLogger(__name__)


def allocate_to_user(env, user_id: int, quota: int) -> dict:
    result = {
        "allocated": 0,
        "had_before": 0,
        "wanted_delta": 0,
        "pool_was_empty": False,
    }

    if quota <= 0:
        return result

    env.flush_all()

    env.cr.execute(
        """
        SELECT COUNT(*) FROM lynceus_prompt
         WHERE assigned_user_id = %s AND state = 'assigned'
        """,
        (user_id,),
    )
    had_before = env.cr.fetchone()[0] or 0
    result["had_before"] = had_before

    delta = max(0, quota - had_before)
    result["wanted_delta"] = delta
    if delta == 0:
        return result

    env.cr.execute(
        """
        SELECT p.id
          FROM lynceus_prompt p
         WHERE p.state = 'available'
           AND NOT EXISTS (
                 SELECT 1
                   FROM lynceus_assignment_log a
                  WHERE a.user_id = %s
                    AND a.prompt_id = p.id
           )
         ORDER BY p.id
           FOR UPDATE OF p SKIP LOCKED
         LIMIT %s
        """,
        (user_id, delta),
    )
    rows = env.cr.fetchall()
    if not rows:
        result["pool_was_empty"] = True
        return result

    prompt_ids = [r[0] for r in rows]
    if len(prompt_ids) < delta:
        result["pool_was_empty"] = True

    now = fields.Datetime.now()

    env["lynceus.prompt"].sudo().browse(prompt_ids).write({
        "state": "assigned",
        "assigned_user_id": user_id,
        "assigned_at": now,
    })

    AssignmentLog = env["lynceus.assignment.log"].sudo()
    AssignmentLog.create([
        {"user_id": user_id, "prompt_id": pid, "allocated_at": now}
        for pid in prompt_ids
    ])

    result["allocated"] = len(prompt_ids)
    return result


def allocate_to_users(env, user_quota_map: dict[int, int]) -> dict[int, dict]:
    results: dict[int, dict] = {}
    for user_id, quota in user_quota_map.items():
        results[user_id] = allocate_to_user(env, user_id, quota)
    return results
