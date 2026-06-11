"""Iris role profile — the multi-role infrastructure (v1.1).

One row per hireable role. Carries the per-role screening calibration
(``competence_guidance`` injected into the screening INPUTS block — NOT the
system prompt, which protects the verdict parse anchor), a starter tech-date
table snapshotted onto candidates, and optional FULL per-role prompt
overrides.

v1.1 ships with exactly ONE seeded role (Head of Engineering,
``data/iris_role_profile_data.xml``). Creating additional roles is LOCKED
behind the ``iris.enable_role_creation`` config parameter — the v1.2 unlock
is one config flip, no code change. The lock is enforced in Python (not
ACL) because controllers run via ``sudo()`` and admins bypass ACLs.
"""

import logging
import re

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

#: ICP feature flag that unlocks role creation (the v1.2 unlock).
_ROLE_CREATION_ICP = "iris.enable_role_creation"

#: ICP string values treated as "enabled".
_TRUTHY = ("1", "true", "yes", "on")


class IrisRoleProfile(models.Model):
    _name = "iris.role.profile"
    _description = "Iris Role Profile"
    _inherit = ["mail.thread"]
    _order = "sequence, id"

    _unique_code = models.Constraint(
        "UNIQUE(code)",
        message="Role code must be unique — it is the stable API identifier.",
    )

    # ------------------------------------------------------------------
    # Fields
    # ------------------------------------------------------------------
    name = fields.Char(string="Role Name", required=True, tracking=True)
    code = fields.Char(
        string="Code", required=True, tracking=True,
        help="Stable API identifier (slug), e.g. head_of_engineering. "
             "Cannot be changed after creation.",
    )
    sequence = fields.Integer(string="Sequence", default=10)
    active = fields.Boolean(default=True, tracking=True)
    is_legacy = fields.Boolean(
        string="Legacy (Migration)", readonly=True, default=False,
        help="Set only by the v1.1 migration for pre-existing free-text "
             "roles. Legacy roles are archived and never selectable for "
             "new candidates.",
    )
    description = fields.Text(string="Description")
    competence_guidance = fields.Text(
        string="Competence Guidance",
        help="Role-specific calibration injected into the screening INPUTS "
             "block as ROLE COMPETENCE GUIDANCE (NOT into the system prompt, "
             "which protects the verdict parse anchor).",
    )
    default_tech_date_reference = fields.Text(
        string="Default Tech Date Reference",
        help="Starter GA-date markdown table copied onto a candidate at "
             "creation when the candidate has none (snapshot semantics).",
    )
    screening_prompt = fields.Text(
        string="Screening Prompt Override",
        help="FULL per-role replacement for prompts/SCREENING.md. Empty = "
             "fall through to the Settings override, then the bundled file.",
    )
    questions_prompt = fields.Text(
        string="Questions Prompt Override",
        help="FULL per-role replacement for prompts/QUESTIONS.md.",
    )
    scorecard_prompt = fields.Text(
        string="Scorecard Prompt Override",
        help="FULL per-role replacement for prompts/SCORECARD.md.",
    )
    batch_consistency_prompt = fields.Text(
        string="Batch Consistency Prompt Override",
        help="FULL per-role replacement for prompts/BATCH_CONSISTENCY.md.",
    )
    candidate_count = fields.Integer(
        string="Candidates", compute="_compute_candidate_count",
    )

    # ------------------------------------------------------------------
    # Computes
    # ------------------------------------------------------------------
    def _compute_candidate_count(self):
        groups = self.env["iris.candidate"].with_context(active_test=False)._read_group(
            [("role_id", "in", self.ids)], ["role_id"], ["__count"],
        )
        counts = {role.id: count for role, count in groups}
        for rec in self:
            rec.candidate_count = counts.get(rec.id, 0)

    # ------------------------------------------------------------------
    # The lock (Python create() guard — ACLs don't hold under sudo)
    # ------------------------------------------------------------------
    def _role_creation_allowed(self):
        """True when role creation is permitted in this context.

        Allowed for: XML data loading (``install_mode`` — the seed),
        the v1.1 migration (``iris_role_migration`` context), or when the
        ``iris.enable_role_creation`` config parameter is truthy.
        """
        if self.env.context.get("install_mode"):
            return True
        if self.env.context.get("iris_role_migration"):
            return True
        icp = self.env["ir.config_parameter"].sudo()
        value = (icp.get_param(_ROLE_CREATION_ICP) or "").strip().lower()
        return value in _TRUTHY

    @api.model_create_multi
    def create(self, vals_list):
        # copy() routes through here without a bypass context, so duplicates
        # are locked too — no separate guard needed.
        if not self._role_creation_allowed():
            raise UserError(_(
                "Additional role profiles are locked in iris v1.1. "
                "Set the '%s' system parameter to enable new roles."
            ) % _ROLE_CREATION_ICP)
        return super().create(vals_list)

    def write(self, vals):
        if "code" in vals:
            for rec in self:
                if rec.code and vals["code"] != rec.code:
                    raise UserError(_(
                        "The role code is a stable API identifier and "
                        "cannot be changed after creation."
                    ))
        return super().write(vals)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------
    def action_view_candidates(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Candidates — %s") % self.name,
            "res_model": "iris.candidate",
            "view_mode": "list,form",
            "domain": [("role_id", "=", self.id)],
            "context": {"default_role_id": self.id},
        }

    # ------------------------------------------------------------------
    # v1.1 migration helper (called by migrations/19.0.1.1.0/end-migrate.py)
    # ------------------------------------------------------------------
    @api.model
    def _slugify_code(self, name):
        """Derive a unique role ``code`` slug from ``name``."""
        base = re.sub(r"[^a-z0-9]+", "_", (name or "").lower()).strip("_") or "role"
        slug = base
        suffix = 1
        Role = self.with_context(active_test=False)
        while Role.search_count([("code", "=", slug)]):
            suffix += 1
            slug = f"{base}_{suffix}"
        return slug

    @api.model
    def _migrate_legacy_target_roles(self, cr):
        """Map pre-v1.1 free-text ``target_role`` strings onto role profiles.

        Called by the end-migration script (after the seed data has loaded).
        Testable in isolation: idempotent, no-ops when the legacy column is
        absent (fresh installs).

        Mapping rules (locked decision — labels are NEVER changed):

        * strings matching "Head of Engineering" case-insensitive → the
          seeded ``iris.role_head_of_engineering`` role;
        * every other distinct string → an ARCHIVED ``is_legacy`` role with
          the label unchanged, created ``with_context(iris_role_migration=
          True)`` (reused on re-run for idempotency);
        * finally ``role_id`` is made NOT NULL at the SQL level.
        """
        cr.execute("""
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'iris_candidate'
              AND column_name = 'target_role_legacy'
        """)
        if not cr.fetchone():
            _logger.info(
                "Iris role migration: no target_role_legacy column — "
                "nothing to migrate."
            )
            return

        seed = self.env.ref("iris.role_head_of_engineering", raise_if_not_found=False)
        Role = self.with_context(iris_role_migration=True, active_test=False)
        Candidate = self.env["iris.candidate"].with_context(
            active_test=False, tracking_disable=True,
        )

        cr.execute("""
            SELECT id, target_role_legacy FROM iris_candidate
            WHERE role_id IS NULL
        """)
        by_label = {}
        for cand_id, legacy in cr.fetchall():
            label = (legacy or "").strip() or "Unknown Role (pre-v1.1)"
            by_label.setdefault(label, []).append(cand_id)

        for label, cand_ids in by_label.items():
            role = None
            if seed and label.lower() == "head of engineering":
                role = seed
            if role is None:
                # Reuse an existing role with the same label (idempotency:
                # a crashed/re-run migration never duplicates legacy roles).
                role = Role.search([("name", "=ilike", label)], limit=1)
            if not role:
                role = Role.create({
                    "name": label,
                    "code": self._slugify_code(label),
                    "active": False,
                    "is_legacy": True,
                    "description": _(
                        "Created automatically by the iris v1.1 migration "
                        "from the legacy free-text target role."
                    ),
                })
                _logger.info(
                    "Iris role migration: created archived legacy role %r "
                    "(code %s) for %d candidate(s).",
                    label, role.code, len(cand_ids),
                )
            # ORM write so the stored related target_role recomputes.
            Candidate.browse(cand_ids).write({"role_id": role.id})

        cr.execute("SELECT COUNT(*) FROM iris_candidate WHERE role_id IS NULL")
        remaining = cr.fetchone()[0]
        if remaining:
            _logger.error(
                "Iris role migration: %d candidate(s) still have no role_id "
                "— NOT NULL constraint skipped.", remaining,
            )
            return
        cr.execute(
            "ALTER TABLE iris_candidate ALTER COLUMN role_id SET NOT NULL"
        )
        _logger.info(
            "Iris role migration: mapped %d legacy label(s); role_id is "
            "now NOT NULL.", len(by_label),
        )
