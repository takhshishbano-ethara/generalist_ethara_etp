from . import models
from . import controllers


def _map_kensei2_roles(env):
    """Seed the Project Tracker's group ladder from the Kensei2 one.

    The Tracker's access is independent of Kensei2 (its groups are a separate
    privilege in Settings / Users), but on the day it is installed nobody has
    a Tracker role yet. Each user's explicit Kensei2 role is copied ONCE to
    the matching Tracker role so existing teams keep working; the two ladders
    are managed separately afterwards.
    """
    Users = env["res.users"].with_context(active_test=False)
    pairs = [
        ("kensei2.group_kensei2_admin", "project_tracker.group_project_tracker_admin"),
        ("kensei2.group_kensei2_pl", "project_tracker.group_project_tracker_pl"),
        ("kensei2.group_kensei2_ql", "project_tracker.group_project_tracker_ql"),
        ("kensei2.group_kensei2_tasker", "project_tracker.group_project_tracker_tasker"),
    ]
    for src_xmlid, dst_xmlid in pairs:
        src = env.ref(src_xmlid, raise_if_not_found=False)
        dst = env.ref(dst_xmlid, raise_if_not_found=False)
        if not src or not dst:
            continue
        # Explicit memberships only — implied ones re-derive in the new ladder.
        users = Users.search([("group_ids", "in", src.id)])
        if users:
            users.write({"group_ids": [(4, dst.id)]})


def _copy_kensei2_tracker_data(env):
    """One-time import of kensei2's built-in tracker data into this module.

    Project Tracker is a standalone addon with its OWN models and tables;
    kensei2's embedded tracker keeps its data untouched. On a database that
    already used that tracker, starting empty would read as data loss — so
    the team roster and the full allocation chains are copied across once.
    A non-empty Project Tracker is never copied into (idempotent), and the
    copy is skipped entirely where kensei2's tracker models don't exist.
    """
    if "kensei2.tracker.allocation" not in env:
        return
    DstMember = env["project.tracker.team.member"]
    DstAlloc = env["project.tracker.allocation"]
    if DstMember.with_context(active_test=False).search_count([]) or \
            DstAlloc.with_context(active_test=False).search_count([]):
        return

    ctx = {"project_tracker_skip_toast": True, "tracking_disable": True,
           "mail_create_nolog": True, "mail_notrack": True,
           "active_test": False}
    SrcMember = env["kensei2.tracker.team.member"].with_context(**ctx)
    SrcAlloc = env["kensei2.tracker.allocation"].with_context(**ctx)
    DstMember = DstMember.with_context(**ctx)
    DstAlloc = DstAlloc.with_context(**ctx)

    # ---- team roster --------------------------------------------------
    member_map = {}
    for m in SrcMember.search([]):
        member_map[m.id] = DstMember.create({
            "member_name": m.member_name,
            "email": m.email,
            "user_id": m.user_id.id,
            "assigned_pl_id": m.assigned_pl_id.id,
            "assigned_ql_id": m.assigned_ql_id.id,
            "status": m.status,
            "notes": m.notes,
            "active": m.active,
        }).id

    # ---- projects from kensei2's free-text project column --------------
    Project = env["project.tracker.project"]
    projects = {}
    for name, in SrcAlloc._read_group([("project", "!=", False)], ["project"]):
        name = (name or "").strip()
        if name and name not in projects:
            projects[name] = Project.create({"name": name}).id

    # ---- allocations, parents before children so the chain remaps ------
    # Copy every stored input field the two models share; computed values
    # (status, labels, stage mirrors) re-derive from those on their own.
    skip = {
        "id", "parent_id", "tasker_member_id", "project_id", "project",
        "create_uid", "create_date", "write_uid", "write_date",
        "message_ids", "message_follower_ids", "message_partner_ids",
        "activity_ids", "rating_ids", "website_message_ids",
    }
    copyable = [
        name for name, f in SrcAlloc._fields.items()
        if name not in skip
        and name in DstAlloc._fields
        and f.store and f.type not in ("one2many", "many2many")
        and (not f.compute or f.readonly is False)
    ]
    alloc_map = {}
    for a in SrcAlloc.search([], order="stage_no asc, id asc"):
        vals = {}
        for name in copyable:
            v = a[name]
            f = SrcAlloc._fields[name]
            vals[name] = v.id if f.type == "many2one" else v
        vals["tasker_member_id"] = member_map.get(a.tasker_member_id.id)
        vals["parent_id"] = alloc_map.get(a.parent_id.id)
        vals["project_id"] = projects.get((a.project or "").strip())
        alloc_map[a.id] = DstAlloc.create(vals).id


def post_init_hook(env):
    _map_kensei2_roles(env)
    _copy_kensei2_tracker_data(env)
