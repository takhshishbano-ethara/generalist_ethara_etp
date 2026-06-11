"""Role-profile infrastructure (v1.1).

Pins the four contracts of the multi-role slice:

* the **seed**: exactly one shipped role (Head of Engineering), active,
  non-legacy, carrying competence guidance + a starter tech-date table;
* the **creation lock**: ``create()`` raises for everyone — plain users,
  managers, even sudo — unless XML data loading (``install_mode``), the
  migration context (``iris_role_migration``) or the
  ``iris.enable_role_creation`` config parameter unlocks it. ``copy()``
  routes through the same guard;
* **prompt resolution order**: role override → ICP override → bundled file
  (``get_prompt(env, name, role=...)``), end-to-end through a screening run;
* **role-aware prompt assembly**: competence guidance lands in the screening
  INPUTS (never the system prompt), the questions user text gains the
  TARGET ROLE line, and ``default_tech_date_reference`` snapshots onto the
  candidate at creation.
"""

from odoo.exceptions import AccessError, UserError
from odoo.tests.common import tagged

from .common import IrisCase, mock_llm
from odoo.addons.iris.services.prompt_loader import get_prompt

LOCK_ICP = "iris.enable_role_creation"

#: Prompt names that have a per-role FULL override field on the profile.
ROLE_PROMPT_NAMES = ("screening", "questions", "scorecard", "batch_consistency")


@tagged("post_install", "-at_install", "iris")
class TestRoleProfile(IrisCase):
    # ------------------------------------------------------------------
    # Seed
    # ------------------------------------------------------------------
    def test_seed_role_exists_active_non_legacy(self):
        role = self.env.ref("iris.role_head_of_engineering")
        self.assertEqual(role.name, "Head of Engineering")
        self.assertEqual(role.code, "head_of_engineering")
        self.assertTrue(role.active)
        self.assertFalse(role.is_legacy)
        self.assertTrue((role.competence_guidance or "").strip())
        self.assertIn(
            "| Technology | GA Date | Source |",
            role.default_tech_date_reference,
        )

    def test_seed_is_the_only_selectable_role(self):
        roles = self.env["iris.role.profile"].search([
            ("is_legacy", "=", False),
        ])
        self.assertEqual(roles, self.role_hoe)

    # ------------------------------------------------------------------
    # The creation lock (Python guard — must hold under sudo)
    # ------------------------------------------------------------------
    def _assert_create_locked(self, model):
        with self.assertRaises(UserError) as ctx:
            model.create({"name": "Locked Role", "code": "locked_role"})
        self.assertIn("locked", str(ctx.exception))
        self.assertIn(LOCK_ICP, str(ctx.exception))

    def test_create_locked_for_iris_user(self):
        self._assert_create_locked(
            self.env["iris.role.profile"].with_user(self.user_iris),
        )

    def test_create_locked_for_iris_manager(self):
        self._assert_create_locked(
            self.env["iris.role.profile"].with_user(self.user_manager),
        )

    def test_create_locked_even_under_sudo(self):
        self._assert_create_locked(self.env["iris.role.profile"].sudo())

    def test_copy_routes_through_the_lock(self):
        with self.assertRaises(UserError):
            self.role_hoe.copy()

    def test_icp_flag_unlocks_creation(self):
        icp = self.env["ir.config_parameter"].sudo()
        icp.set_param(LOCK_ICP, "1")
        role = self.env["iris.role.profile"].with_user(self.user_manager).create({
            "name": "Staff Platform Engineer",
            "code": "staff_platform_engineer",
        })
        self.assertTrue(role.active)
        self.assertFalse(role.is_legacy)

        icp.set_param(LOCK_ICP, "true")
        self.env["iris.role.profile"].with_user(self.user_manager).create({
            "name": "Truthy Variant",
            "code": "truthy_variant",
        })

    def test_falsy_icp_values_keep_the_lock(self):
        icp = self.env["ir.config_parameter"].sudo()
        for value in ("0", "false", "no", "  "):
            icp.set_param(LOCK_ICP, value)
            self._assert_create_locked(self.env["iris.role.profile"].sudo())

    def test_unlocked_plain_user_still_blocked_by_acl(self):
        self.env["ir.config_parameter"].sudo().set_param(LOCK_ICP, "1")
        with self.assertRaises(AccessError):
            self.env["iris.role.profile"].with_user(self.user_iris).create({
                "name": "ACL Probe",
                "code": "acl_probe",
            })

    def test_migration_context_bypasses_lock(self):
        role = self._make_role(name="Migration Bypass Probe")
        self.assertTrue(role.exists())
        self.assertFalse(role.is_legacy)

    def test_code_is_immutable_after_create(self):
        role = self._make_role(name="Immutable Code Probe")
        with self.assertRaises(UserError):
            role.write({"code": "renamed_code"})
        # Writing the SAME code back is a no-op, not an error.
        role.write({"code": role.code})

    # ------------------------------------------------------------------
    # Prompt resolution order: role override → ICP override → bundled file
    # ------------------------------------------------------------------
    def test_prompt_resolution_order_for_all_role_prompt_names(self):
        role = self._make_role(name="Resolution Probe")
        icp = self.env["ir.config_parameter"].sudo()
        for name in ROLE_PROMPT_NAMES:
            role_text = f"ROLE OVERRIDE {name.upper()}"
            icp_text = f"ICP OVERRIDE {name.upper()}"

            role.write({f"{name}_prompt": role_text})
            icp.set_param(f"iris.prompt_{name}", icp_text)
            self.assertEqual(get_prompt(self.env, name, role=role), role_text)

            role.write({f"{name}_prompt": False})
            self.assertEqual(get_prompt(self.env, name, role=role), icp_text)

            icp.set_param(f"iris.prompt_{name}", "")
            bundled = get_prompt(self.env, name, role=role)
            self.assertTrue(bundled.strip())
            self.assertNotIn("OVERRIDE", bundled)

    def test_whitespace_role_override_falls_through(self):
        role = self._make_role(name="Whitespace Probe")
        role.write({"screening_prompt": "   \n\t  "})
        content = get_prompt(self.env, "screening", role=role)
        self.assertIn("Forensic Ladder", content)

    def test_role_layer_skipped_without_role(self):
        self.role_hoe.write({"screening_prompt": "SEED ROLE OVERRIDE"})
        # role=None and an empty recordset both skip the role layer.
        empty = self.env["iris.role.profile"]
        self.assertIn("Forensic Ladder", get_prompt(self.env, "screening"))
        self.assertIn(
            "Forensic Ladder", get_prompt(self.env, "screening", role=empty),
        )

    def test_names_without_role_field_fall_through_silently(self):
        role = self._make_role(name="No Field Probe")
        # jd_critique has no <name>_prompt field on the profile — the role
        # layer must fall through to the bundled file without raising.
        content = get_prompt(self.env, "jd_critique", role=role)
        self.assertTrue(content.strip())

    def test_role_screening_prompt_reaches_the_llm_call(self):
        role = self._make_role(
            name="System Prompt Probe",
            screening_prompt="ROLE OVERRIDE SYSTEM PROMPT",
        )
        candidate = self._make_candidate(role_id=role.id)
        candidate.action_screen()
        with mock_llm(self.VALID_SHIP_RECORD) as mocked:
            self._run_llm_queue()
        self.assertEqual(
            mocked.call_args.kwargs["system_prompt"],
            "ROLE OVERRIDE SYSTEM PROMPT",
        )

    # ------------------------------------------------------------------
    # Competence guidance → screening INPUTS (never the system prompt)
    # ------------------------------------------------------------------
    def test_competence_guidance_in_screening_inputs(self):
        candidate = self._make_candidate()  # seeded Head of Engineering role
        screening = self._screen(candidate, self.VALID_SHIP_RECORD)
        prompt_input = screening.llm_prompt_input
        self.assertIn(
            "ROLE COMPETENCE GUIDANCE (maintained by the hiring team):",
            prompt_input,
        )
        self.assertIn("ROLE CALIBRATION — Head of Engineering", prompt_input)

    def test_competence_guidance_stays_out_of_system_prompt(self):
        candidate = self._make_candidate()
        candidate.action_screen()
        with mock_llm(self.VALID_SHIP_RECORD) as mocked:
            self._run_llm_queue()
        system_prompt = mocked.call_args.kwargs["system_prompt"]
        self.assertNotIn("ROLE CALIBRATION", system_prompt)

    def test_no_guidance_no_marker(self):
        role = self._make_role(name="No Guidance Probe")
        candidate = self._make_candidate(role_id=role.id)
        screening = self._screen(candidate, self.VALID_SHIP_RECORD)
        self.assertNotIn("ROLE COMPETENCE GUIDANCE", screening.llm_prompt_input)

    # ------------------------------------------------------------------
    # Questions user text gains the TARGET ROLE line
    # ------------------------------------------------------------------
    def test_questions_user_text_has_target_role_line(self):
        candidate = self._make_candidate()
        self._screen(candidate, self.VALID_SHIP_RECORD)
        candidate.action_generate_guide()
        with mock_llm("# Interview Guide"):
            self._run_llm_queue()
        interview = candidate.interview_ids
        self.assertEqual(interview.llm_status, "done")
        self.assertIn(
            "TARGET ROLE / LEVEL:   Head of Engineering",
            interview.llm_prompt_input,
        )

    # ------------------------------------------------------------------
    # default_tech_date_reference snapshot at candidate creation
    # ------------------------------------------------------------------
    def test_tech_date_snapshot_on_candidate_create(self):
        original = self.role_hoe.default_tech_date_reference
        candidate = self._make_candidate()
        self.assertEqual(candidate.tech_date_reference, original)

        # Snapshot semantics: later role edits never rewrite the candidate.
        self.role_hoe.write({
            "default_tech_date_reference": "| Changed | 2026-01-01 | x |",
        })
        self.assertEqual(candidate.tech_date_reference, original)

    def test_explicit_tech_date_wins_over_snapshot(self):
        candidate = self._make_candidate(tech_date_reference="MY OWN TABLE")
        self.assertEqual(candidate.tech_date_reference, "MY OWN TABLE")

    def test_no_default_no_snapshot(self):
        role = self._make_role(name="No Default Probe")
        candidate = self._make_candidate(role_id=role.id)
        self.assertFalse(candidate.tech_date_reference)

    # ------------------------------------------------------------------
    # target_role stays the role name (stored related, v1.0 field name)
    # ------------------------------------------------------------------
    def test_target_role_is_related_to_role_name(self):
        candidate = self._make_candidate()
        self.assertEqual(candidate.target_role, "Head of Engineering")

        role = self._make_role(name="Custom Role Name")
        other = self._make_candidate(name="Other Person", role_id=role.id)
        self.assertEqual(other.target_role, "Custom Role Name")
