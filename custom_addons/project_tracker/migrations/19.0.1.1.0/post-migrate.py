# -*- coding: utf-8 -*-
"""Backfill the new per-project links added in 19.0.1.1.0.

Before this version 'Project' only sliced allocations. We now give personas a
project (Many2one) and team members their projects (Many2many). Existing
records predate those columns, so derive them from the allocation history:

  * a persona's project  = the project of its current (else first) allocation;
  * a member's projects  = every project they appear in as tasker, PL, or QL.

Idempotent: only touches records that are still unset / re-derives from data.
"""
from odoo import api, SUPERUSER_ID


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})

    # 1. Personas -> the project of their current (else earliest) allocation.
    # SKIPPED when pt_project_id no longer exists: personas were made global in
    # 19.0.1.4.0 and the field was dropped, so this backfill is a historical
    # no-op on any upgrade that lands on the current schema.
    if 'pt_project_id' in env['kensei2.persona']._fields:
        personas = env['kensei2.persona'].search([('pt_project_id', '=', False)])
        for persona in personas:
            alloc = persona.pt_current_allocation_id or persona.pt_allocation_ids[:1]
            if alloc and alloc.project_id:
                persona.pt_project_id = alloc.project_id.id

    # 2. Team members -> every project they touch (as tasker, PL, or QL).
    Alloc = env['project.tracker.allocation']
    Member = env['project.tracker.team.member']
    allocs = Alloc.search([('project_id', '!=', False)])

    projects_by_member = {}          # member.id -> {project.id, ...}
    projects_by_user = {}            # res.users.id (PL/QL) -> {project.id, ...}
    for a in allocs:
        pid = a.project_id.id
        if a.tasker_member_id:
            projects_by_member.setdefault(a.tasker_member_id.id, set()).add(pid)
        for user in (a.assigned_pl_id | a.assigned_ql_id):
            projects_by_user.setdefault(user.id, set()).add(pid)

    # Resolve the PL/QL res.users back to their team-member row via user_id.
    if projects_by_user:
        for member in Member.search([('user_id', 'in', list(projects_by_user))]):
            for pid in projects_by_user.get(member.user_id.id, ()):
                projects_by_member.setdefault(member.id, set()).add(pid)

    for member_id, pids in projects_by_member.items():
        Member.browse(member_id).project_ids = [(6, 0, list(pids))]
