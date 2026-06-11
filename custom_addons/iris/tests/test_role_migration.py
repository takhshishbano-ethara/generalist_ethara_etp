"""v1.0 → v1.1 candidate role migration helper, exercised in isolation.

``IrisRoleProfile._migrate_legacy_target_roles(cr)`` is the worker behind
``migrations/19.0.1.1.0/end-migrate.py``. These tests synthesize the
pre-migration shape INSIDE the test transaction — a nullable ``role_id``
plus a ``target_role_legacy`` column with free-text labels — then call the
helper directly. All DDL rolls back with the test transaction.

Pinned mapping rules (labels are NEVER changed):

* "Head of Engineering" (case-insensitive) → the seeded role;
* every other distinct label → ONE archived ``is_legacy`` role with the
  label preserved verbatim;
* blank labels → the "Unknown Role (pre-v1.1)" placeholder;
* re-runs are idempotent: existing legacy roles are reused, never
  duplicated;
* the helper no-ops when the legacy column is absent (fresh installs).
"""

from odoo.tests.common import tagged

from .common import IrisCase


@tagged("post_install", "-at_install", "iris")
class TestRoleMigration(IrisCase):
    # ------------------------------------------------------------------
    # Synthetic pre-migration schema (transaction-local DDL)
    # ------------------------------------------------------------------
    def _install_legacy_schema(self):
        """Make ``role_id`` nullable + add the ``target_role_legacy`` column."""
        self.env.flush_all()
        cr = self.env.cr
        cr.execute(
            "ALTER TABLE iris_candidate ALTER COLUMN role_id DROP NOT NULL"
        )
        cr.execute("""
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'iris_candidate'
              AND column_name = 'target_role_legacy'
        """)
        if not cr.fetchone():
            cr.execute(
                "ALTER TABLE iris_candidate ADD COLUMN target_role_legacy varchar"
            )

    def _make_legacy_candidate(self, label, name="Legacy Person"):
        """A candidate as the pre-migrate script leaves it: NULL ``role_id``
        + the original free-text role in ``target_role_legacy``."""
        candidate = self._make_candidate(name=name)
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE iris_candidate"
            "   SET role_id = NULL, target_role_legacy = %s"
            " WHERE id = %s",
            (label, candidate.id),
        )
        self.env.invalidate_all()
        return candidate

    def _run_helper(self):
        self.env["iris.role.profile"]._migrate_legacy_target_roles(self.env.cr)
        self.env.flush_all()

    def _role_count(self):
        return self.env["iris.role.profile"].with_context(
            active_test=False,
        ).search_count([])

    def _has_legacy_column(self):
        self.env.cr.execute("""
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'iris_candidate'
              AND column_name = 'target_role_legacy'
        """)
        return bool(self.env.cr.fetchone())

    # ------------------------------------------------------------------
    # Fresh installs: no legacy column → no-op
    # ------------------------------------------------------------------
    def test_helper_noops_without_legacy_column(self):
        if self._has_legacy_column():
            self.skipTest("database carries a real target_role_legacy column")
        before = self._role_count()
        self._run_helper()  # must not raise
        self.assertEqual(self._role_count(), before)

    # ------------------------------------------------------------------
    # Mapping rules
    # ------------------------------------------------------------------
    def test_hoe_label_maps_to_seed_case_insensitive(self):
        self._install_legacy_schema()
        variants = [
            self._make_legacy_candidate("Head of Engineering", name="Exact Case"),
            self._make_legacy_candidate("head of engineering", name="Lower Case"),
            self._make_legacy_candidate("HEAD OF ENGINEERING", name="Upper Case"),
        ]
        before = self._role_count()
        self._run_helper()

        for candidate in variants:
            self.assertEqual(candidate.role_id, self.role_hoe)
            self.assertEqual(candidate.target_role, "Head of Engineering")
        # No legacy role was created for the seed-matching labels.
        self.assertEqual(self._role_count(), before)

    def test_other_labels_become_archived_legacy_roles(self):
        self._install_legacy_schema()
        ml_one = self._make_legacy_candidate("Senior ML Engineer", name="ML One")
        ml_two = self._make_legacy_candidate("Senior ML Engineer", name="ML Two")
        infra = self._make_legacy_candidate("Staff Infra Engineer", name="Infra")
        before = self._role_count()
        self._run_helper()

        # One role per DISTINCT label — same-label candidates share it.
        self.assertEqual(self._role_count(), before + 2)
        self.assertEqual(ml_one.role_id, ml_two.role_id)
        self.assertNotEqual(ml_one.role_id, infra.role_id)

        for candidate, label in (
            (ml_one, "Senior ML Engineer"),
            (infra, "Staff Infra Engineer"),
        ):
            role = candidate.role_id
            self.assertEqual(role.name, label, "label must be preserved verbatim")
            self.assertTrue(role.is_legacy)
            self.assertFalse(role.active, "legacy roles are archived")
            # target_role (stored related) reads the unchanged label, so the
            # pre-v1.1 API string is byte-identical.
            self.assertEqual(candidate.target_role, label)

        self.assertEqual(ml_one.role_id.code, "senior_ml_engineer")

    def test_blank_label_maps_to_unknown_placeholder(self):
        self._install_legacy_schema()
        blank = self._make_legacy_candidate("   ", name="Blank Label")
        missing = self._make_legacy_candidate(None, name="Null Label")
        self._run_helper()

        self.assertEqual(blank.role_id, missing.role_id)
        role = blank.role_id
        self.assertEqual(role.name, "Unknown Role (pre-v1.1)")
        self.assertTrue(role.is_legacy)
        self.assertFalse(role.active)

    def test_legacy_roles_never_selectable_for_new_candidates(self):
        self._install_legacy_schema()
        legacy = self._make_legacy_candidate("Retired Role", name="Old Timer")
        self._run_helper()
        role = legacy.role_id
        # Archived + legacy: both the candidate-form domain and the API's
        # role resolution filter on these two flags.
        self.assertTrue(role.is_legacy)
        self.assertFalse(role.active)

    # ------------------------------------------------------------------
    # Idempotency
    # ------------------------------------------------------------------
    def test_rerun_without_pending_rows_is_noop(self):
        self._install_legacy_schema()
        self._make_legacy_candidate("Senior ML Engineer", name="Once")
        self._run_helper()
        count = self._role_count()
        self._run_helper()  # nothing left to map — must not raise
        self.assertEqual(self._role_count(), count)

    def test_rerun_reuses_existing_legacy_role(self):
        self._install_legacy_schema()
        first = self._make_legacy_candidate("Senior ML Engineer", name="First")
        self._run_helper()
        mapped_role = first.role_id
        count = self._role_count()

        # A crashed/partial migration re-run: the same label shows up again.
        self.env.cr.execute(
            "ALTER TABLE iris_candidate ALTER COLUMN role_id DROP NOT NULL"
        )
        second = self._make_legacy_candidate("Senior ML Engineer", name="Second")
        self._run_helper()

        self.assertEqual(second.role_id, mapped_role,
                         "re-runs must reuse the existing legacy role")
        self.assertEqual(self._role_count(), count,
                         "re-runs must never duplicate legacy roles")
