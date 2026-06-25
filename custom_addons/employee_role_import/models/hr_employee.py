from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

ROLE_GROUP_MAP = {
    "tasker": ("etp_user_roles.group_tasker", "Tasker"),
    "ql": ("etp_user_roles.group_quality_lead", "Quality Lead"),
    "qr": ("etp_user_roles.group_quality_reviewer", "Quality Reviewer"),
    "pl": ("etp_user_roles.group_project_lead", "Project Lead"),
    "dm": ("etp_user_roles.group_delivery_manager", "Delivery Manager"),
    "tpm": ("etp_user_roles.group_tpm", "TPM"),
    "cto": ("etp_user_roles.group_cto", "CTO"),
    "hr_admin": ("etp_user_roles.group_hr_admin", "HR Admin"),
    "it_admin": ("etp_user_roles.group_it_admin", "IT Admin"),
}

ROLE_SELECTION = [(k, v[1]) for k, v in ROLE_GROUP_MAP.items()]

# Senior-first lookup order so a user holding both TPM and PL reports as TPM.
ROLE_PRIORITY = ["cto", "tpm", "dm", "pl", "ql", "qr", "tasker", "hr_admin", "it_admin"]

# QL and QR are the SAME level in the hierarchy and must be treated
# identically: neither can be assigned for the other (same level), and both
# require the same set of higher reporting roles.
ROLE_LEVEL = {
    "tasker": 1,
    "qr": 2,
    "ql": 2,
    "pl": 4,
    "dm": 4,
    "tpm": 5,
    "cto": 6,
    "hr_admin": 6,
    "it_admin": 6,
}

ROLE_DEFAULT_PARENT_ROLE = {
    "tasker": "qr",
    "qr": "ql",
    "ql": "pl",
    "pl": "tpm",
    "dm": "tpm",
    "tpm": "cto",
}

# TPM/CTO sit at the top - no hierarchy fields. dm/hr_admin/it_admin are
# off-hierarchy and intentionally empty (not a bug).
ROLE_HIERARCHY_FIELDS = {
    "tasker": ("ql", "pl", "tpm"),
    # QR is the same level as QL, so it reports up the same way QL does
    # (to PL / TPM) and is NOT assigned a quality-tier (QL/QR) manager.
    "qr": ("pl", "tpm"),
    "ql": ("pl", "tpm"),
    "pl": ("tpm",),
    "tpm": (),
    "cto": (),
    "dm": (),
    "hr_admin": (),
    "it_admin": (),
}

# The reporting tier that is MANDATORY for each role (the immediate tier above
# it). QL and QR are the same level, so both require a PL. The value is
# (tier_key, stored_field_to_check).  Roles not listed (tpm, cto, dm, hr_admin,
# it_admin) require nothing.
ROLE_REQUIRED_MANAGER = {
    "tasker": ("ql", "task_forge_ql_id"),
    "qr": ("pl", "task_forge_pl_id"),
    "ql": ("pl", "task_forge_pl_id"),
    "pl": ("tpm", "task_forge_tpm_id"),
}

# --- Role-assignment hierarchy ----------------------------------------------
# A user may ASSIGN only roles strictly HIGHER than their own level (QL and QR
# share a level, so neither can assign the other). Admin-type roles may assign
# everything so the import tool stays usable for HR/CTO/admins.
ASSIGNABLE_ROLE_KEYS = ("tasker", "qr", "ql", "pl", "tpm", "cto")
ADMIN_ASSIGNER_ROLES = frozenset({"admin", "hr", "cto", "hr_admin", "it_admin"})


def assignable_role_keys(actor_role):
    """Return the role keys an actor with role ``actor_role`` may assign.

    ``actor_role`` uses the task-forge vocabulary returned by
    ``hr.employee._get_task_forge_role()`` ('admin','tpm','pl','qr','ql','hr',
    'tasker') OR an import role key. Admin-type actors get every role;
    everyone else gets only hierarchy roles strictly above their level;
    an unknown/empty actor gets nothing (defensive)."""
    if actor_role in ADMIN_ASSIGNER_ROLES:
        return list(ROLE_GROUP_MAP.keys())
    actor_level = ROLE_LEVEL.get(actor_role)
    if not actor_level:
        return []
    return [k for k in ASSIGNABLE_ROLE_KEYS if ROLE_LEVEL.get(k, 0) > actor_level]


class HrEmployee(models.Model):
    _inherit = "hr.employee"

    employee_code = fields.Char(
        string="Employee ID",
        index=True,
        copy=False,
        help="External employee identifier supplied at import time "
        "(maps to the `employee_id` column in the CSV).",
    )

    @api.constrains("employee_code")
    def _check_employee_code_unique(self):
        for emp in self:
            code = (emp.employee_code or "").strip()
            if not code:
                continue
            dup = self.with_context(active_test=False).sudo().search(
                [("id", "!=", emp.id), ("employee_code", "=ilike", code)],
                limit=1,
            )
            if dup:
                raise ValidationError(_(
                    "Employee ID '%(code)s' already exists "
                    "(used by %(name)s%(archived)s). "
                    "Pick a different ID or update that record instead."
                ) % {
                    "code": code,
                    "name": dup.name or _("an archived record"),
                    "archived": "" if dup.active else _(" — archived"),
                })

    @api.constrains("work_email")
    def _check_work_email_unique(self):
        for emp in self:
            email = (emp.work_email or "").strip().lower()
            if not email:
                continue
            dup = self.with_context(active_test=False).sudo().search(
                [("id", "!=", emp.id), ("work_email", "=ilike", email)],
                limit=1,
            )
            if dup:
                raise ValidationError(_(
                    "Work Email '%(email)s' already exists "
                    "(used by %(name)s%(archived)s). "
                    "Pick a different email or update that record instead."
                ) % {
                    "email": email,
                    "name": dup.name or _("an archived record"),
                    "archived": "" if dup.active else _(" — archived"),
                })

    def _collect_linked_users_to_unlink(self):
        protected_ids = {self.env.user.id}
        for xml_id in ("base.user_admin", "base.user_root"):
            protected = self.env.ref(xml_id, raise_if_not_found=False)
            if protected:
                protected_ids.add(protected.id)
        users = self.env["res.users"]
        for emp in self.with_context(active_test=False):
            if emp.user_id and emp.user_id.id not in protected_ids:
                users |= emp.user_id
        return users

    def unlink(self):
        users_to_unlink = self._collect_linked_users_to_unlink()
        result = super().unlink()
        if users_to_unlink:
            users_to_unlink.exists().sudo().with_context(active_test=False).unlink()
        return result

    def action_delete_with_user(self):
        emp_count = len(self)
        users_to_unlink = self._collect_linked_users_to_unlink()
        self.unlink()
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Records removed"),
                "message": _(
                    "%(emp)s employee(s) and %(usr)s login user(s) were permanently deleted."
                ) % {"emp": emp_count, "usr": len(users_to_unlink)},
                "type": "success",
                "sticky": False,
            },
        }

    role = fields.Selection(
        selection=ROLE_SELECTION,
        string="Role",
        compute="_compute_role",
        inverse="_inverse_role",
        store=True,
        help="ETP role of the linked user. Changing this rewrites the user's "
        "group membership (the previous role is removed, the new one is added).",
    )

    @api.depends("user_id", "user_id.group_ids")
    def _compute_role(self):
        priority_groups = [
            (key, self.env.ref(ROLE_GROUP_MAP[key][0], raise_if_not_found=False))
            for key in ROLE_PRIORITY
        ]
        for emp in self:
            if not emp.user_id:
                # No linked user => no security groups to derive the role from.
                # Preserve any role that was set explicitly (e.g. by the
                # employee-role import, which creates employees without a login
                # user) instead of blanking it. Without this guard the stored
                # role of every user-less imported employee was forced to False
                # and showed blank in the View List.
                continue
            current = False
            user_group_ids = set(emp.user_id.group_ids.ids)
            for key, group in priority_groups:
                if group and group.id in user_group_ids:
                    current = key
                    break
            emp.role = current

    @api.constrains("role", "parent_id")
    def _check_reports_to_role(self):
        for emp in self:
            if not emp.parent_id or not emp.role:
                continue
            parent_role = emp.parent_id.role
            if not parent_role:
                continue
            emp_level = ROLE_LEVEL.get(emp.role, 0)
            parent_level = ROLE_LEVEL.get(parent_role, 0)
            if parent_level <= emp_level:
                raise ValidationError(
                    _(
                        "Invalid Reports To for %(emp)s: a %(emp_role)s cannot "
                        "report to %(parent)s (%(parent_role)s). The manager's "
                        "role must be senior to the employee's role.",
                        emp=emp.name,
                        emp_role=dict(ROLE_SELECTION).get(emp.role, emp.role),
                        parent=emp.parent_id.name,
                        parent_role=dict(ROLE_SELECTION).get(parent_role, parent_role),
                    )
                )

    @api.constrains("role", "task_forge_ql_id", "task_forge_pl_id",
                    "task_forge_tpm_id", "parent_id")
    def _check_required_hierarchy(self):
        # Batch import wires the hierarchy in a SECOND pass (a member's manager
        # may be a later row in the same file), so the hard constraint cannot
        # pass mid-commit. During import the wizard flags rows missing a
        # required manager instead; skip the constraint when that context is set.
        if self.env.context.get("etp_importing"):
            return
        labels = dict(ROLE_SELECTION)
        tier_labels = {"ql": "QL/QR", "pl": "PL", "tpm": "TPM"}
        for emp in self:
            req = ROLE_REQUIRED_MANAGER.get(emp.role or "")
            if not req:
                continue
            tier, field = req
            if not emp[field]:
                raise ValidationError(_(
                    "%(name)s is a %(role)s and must have a %(tier)s assigned "
                    "in the Task Force hierarchy."
                ) % {"name": emp.name or emp.employee_code or _("Employee"),
                     "role": labels.get(emp.role, emp.role),
                     "tier": tier_labels.get(tier, tier)})

    def _inverse_role(self):
        all_role_groups = {
            key: self.env.ref(xml_id, raise_if_not_found=False)
            for key, (xml_id, _label) in ROLE_GROUP_MAP.items()
        }
        internal = self.env.ref("base.group_user", raise_if_not_found=False)
        for emp in self:
            if not emp.user_id:
                # No linked user to carry security groups. The role is still
                # stored on the employee record itself (the framework persists
                # the written value); there are simply no groups to sync. This
                # lets the import assign a role to employees created without a
                # login user instead of rejecting them. If a user is linked
                # later, _compute_role re-derives the role from that user's
                # groups.
                continue
            commands = []
            for key, group in all_role_groups.items():
                if not group:
                    continue
                if key == emp.role:
                    commands.append((4, group.id))
                elif group.id in emp.user_id.group_ids.ids:
                    commands.append((3, group.id))
            if emp.role and internal and internal.id not in emp.user_id.group_ids.ids:
                commands.append((4, internal.id))
            if commands:
                emp.user_id.sudo().write({"group_ids": commands})
        self._etp_sync_job_title()

    # ── Task Forge hierarchy auto-mapping ────────────────────────────────────
    # The TPM / PL / QL / QR fields are defined as plain stored Many2one in
    # task_forge_bridge and were only filled by the importer, leaving them
    # blank for anyone set up outside it.  We re-declare the SAME fields here
    # (employee_role_import depends on task_forge_bridge, so this wins) as
    # computed+stored so they auto-populate from the reporting (`parent_id`)
    # chain - no new field/column is added, the existing fields are reused.
    #
    # ``store=True`` keeps them searchable and persisted; ``readonly=False``
    # keeps them writable so the importer can still set an explicit value.
    # QL and QR are one quality tier, so both fields resolve to the SAME value
    # (the nearest Quality Lead or Quality Reviewer in the chain).
    task_forge_tpm_id = fields.Many2one(
        compute="_compute_task_forge_hierarchy", store=True, readonly=False,
    )
    task_forge_pl_id = fields.Many2one(
        compute="_compute_task_forge_hierarchy", store=True, readonly=False,
    )
    task_forge_ql_id = fields.Many2one(
        compute="_compute_task_forge_hierarchy", store=True, readonly=False,
    )
    task_forge_qr_id = fields.Many2one(
        compute="_compute_task_forge_hierarchy", store=True, readonly=False,
    )

    @api.depends(
        "parent_id",
        "role",
        "parent_id.role",
        "parent_id.task_forge_tpm_id",
        "parent_id.task_forge_pl_id",
        "parent_id.task_forge_qr_id",
    )
    def _compute_task_forge_hierarchy(self):
        """Resolve TPM / PL / QL-QR from the reporting (`parent_id`) chain.

        Each value is the nearest ancestor whose own role matches.  The walk
        reads each ancestor's stored ``role`` directly (not a neighbour's
        computed field), so it is correct regardless of recompute order.  The
        ``parent_id.task_forge_*`` dependencies above are kept only as cascade
        triggers, so re-parenting a manager refreshes every descendant.
        """
        for emp in self:
            parent = emp.parent_id
            # Resolve each tier from the parent_id chain; when a tier is not
            # present in the ancestry, inherit it from the immediate manager's
            # own resolved chain. This makes employee-role mapping fill the
            # whole chain: a Tasker linked to its QL/QR automatically picks up
            # that manager's PL and TPM (and a QL/QR picks up its PL's TPM).
            tpm = emp._tf_walk_ancestor(("tpm",)) or parent.task_forge_tpm_id
            pl = emp._tf_walk_ancestor(("pl",)) or parent.task_forge_pl_id
            # QL and QR are one quality tier -> both fields get the same value.
            quality = emp._tf_walk_ancestor(("ql", "qr")) or parent.task_forge_qr_id
            emp.task_forge_tpm_id = tpm
            emp.task_forge_pl_id = pl
            emp.task_forge_qr_id = quality
            emp.task_forge_ql_id = quality

    @api.onchange("task_forge_qr_id", "task_forge_ql_id")
    def _onchange_quality_fills_chain(self):
        """When the quality manager (QL/QR) is picked in the form, auto-fill the
        PL and TPM from that manager's own resolved chain, so a Tasker's PL/TPM
        become visible immediately on assignment."""
        for emp in self:
            mgr = emp.task_forge_qr_id or emp.task_forge_ql_id
            if not mgr:
                continue
            if not emp.task_forge_pl_id:
                emp.task_forge_pl_id = mgr.task_forge_pl_id
            if not emp.task_forge_tpm_id:
                emp.task_forge_tpm_id = (
                    mgr.task_forge_tpm_id
                    or (emp.task_forge_pl_id and emp.task_forge_pl_id.task_forge_tpm_id)
                )

    @api.onchange("task_forge_pl_id")
    def _onchange_pl_fills_tpm(self):
        """Picking the PL auto-fills its TPM when not already set."""
        for emp in self:
            if emp.task_forge_pl_id and not emp.task_forge_tpm_id:
                emp.task_forge_tpm_id = emp.task_forge_pl_id.task_forge_tpm_id

    def _tf_walk_ancestor(self, target_roles):
        """Nearest manager (or empty) up the parent_id chain whose role matches."""
        seen = set()
        node = self.parent_id
        while node and node.id not in seen:
            seen.add(node.id)
            if node.role in target_roles:
                return node
            node = node.parent_id
        return self.browse()

    # ── Role-assignment permissions ──────────────────────────────────────────
    @api.model
    def _current_user_assigner_role(self):
        """Task-forge role key for the CURRENT user (admin-aware)."""
        user = self.env.user
        if (user._is_admin()
                or user.has_group("etp_user_roles.group_cto")
                or user.has_group("etp_user_roles.group_hr_admin")
                or user.has_group("etp_user_roles.group_it_admin")):
            return "admin"
        emp = user.employee_id
        if emp:
            return emp._get_task_forge_role()
        return "tasker"

    @api.model
    def _current_user_assignable_roles(self):
        """Role keys the current user is allowed to assign (import vocab)."""
        return assignable_role_keys(self._current_user_assigner_role())

    @api.model
    def _assignable_role_selection(self):
        """ROLE_SELECTION filtered to what the current user may assign."""
        allowed = set(self._current_user_assignable_roles())
        return [(k, lbl) for (k, lbl) in ROLE_SELECTION if k in allowed]

    # ── Employee ↔ User auto-mapping ─────────────────────────────────────────
    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        records._etp_link_user()
        records._etp_sync_job_title()
        records._etp_sync_quality_tier()
        return records

    def write(self, vals):
        res = super().write(vals)
        if not self.env.context.get("etp_skip_eu_sync") and (
            "work_email" in vals or "user_id" in vals
        ):
            self._etp_link_user()
        if not self.env.context.get("etp_skip_eu_sync") and "user_id" in vals:
            self._etp_sync_job_title()
        if not self.env.context.get("etp_skip_eu_sync") and (
            "task_forge_ql_id" in vals or "task_forge_qr_id" in vals
            or "parent_id" in vals
        ):
            self._etp_sync_quality_tier()
        return res

    def _etp_sync_quality_tier(self):
        """QL and QR are ONE quality tier, so task_forge_ql_id and
        task_forge_qr_id must always be equal. Direct writes (import,
        change_assignment) may set only one (or wipe the other), so mirror them
        here on every create/write — the Task Force tab shows task_forge_qr_id."""
        if self.env.context.get("etp_skip_eu_sync"):
            return
        for emp in self:
            ql, qr = emp.task_forge_ql_id, emp.task_forge_qr_id
            if ql.id != qr.id:
                val = (ql or qr).id
                emp.with_context(etp_skip_eu_sync=True).write({
                    "task_forge_ql_id": val,
                    "task_forge_qr_id": val,
                })

    def _etp_link_user(self):
        """Link each employee to the res.users whose login/email matches its
        work_email, when not already linked. Never creates a user; never steals a
        user already linked to a different employee. Idempotent."""
        if self.env.context.get("etp_skip_eu_sync"):
            return
        Users = self.env["res.users"].sudo()
        for emp in self:
            if emp.user_id or not emp.work_email:
                continue
            email = emp.work_email.strip()
            if not email:
                continue
            user = Users.search(
                ["&", ("share", "=", False),
                 "|", ("login", "=ilike", email), ("email", "=ilike", email)],
                limit=2,
            )
            if len(user) != 1:
                continue  # zero or ambiguous -> do not guess
            # don't steal a user already tied to another employee
            other = self.env["hr.employee"].sudo().search(
                [("user_id", "=", user.id), ("id", "!=", emp.id)], limit=1)
            if other:
                continue
            emp.with_context(etp_skip_eu_sync=True).user_id = user.id

    def _etp_sync_job_title(self):
        """Keep Job Title aligned with the (group-based) role: a PL's title is
        'Project Lead', a QL's is 'Quality Lead', etc. Only employees that have a
        role are touched; role-less employees are left untouched."""
        labels = dict(ROLE_SELECTION)
        for emp in self:
            if not emp.role:
                continue
            title = labels.get(emp.role)
            if title and emp.job_title != title:
                emp.with_context(etp_skip_eu_sync=True).job_title = title
