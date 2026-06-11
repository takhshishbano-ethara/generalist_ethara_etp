"""Tests for ``iris.tech.date.reference`` (P1-4, versioned central table).

Contract under test:

* ``get_reference_markdown()`` renders ACTIVE rows as the trusted
  ``| Technology | GA Date | Source |`` table (ISO dates, pipe-escaped
  cells) and ``""`` when empty;
* at most one ACTIVE row per technology (case-insensitive) — archived
  duplicates are the version history;
* the screening prompt consumes the table TRUSTED/unfenced, falls back to
  the candidate's legacy free-text field FENCED, else "none provided";
* plain iris users are read-only (managers maintain the table);
* the quarterly cron schedules a To-Do review activity on the latest row
  and no-ops (with a log line) on an empty table.
"""

from datetime import date

from odoo.exceptions import AccessError, ValidationError
from odoo.tests.common import tagged

from .common import IrisCase


@tagged("post_install", "-at_install", "iris")
class TestTechDateReference(IrisCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Reference = cls.env["iris.tech.date.reference"]

    def _make_row(self, technology="ChatGPT", ga_date=date(2022, 11, 30),
                  **overrides):
        vals = {
            "technology": technology,
            "ga_date": ga_date,
            "source_url": "https://example.com/chatgpt",
        }
        vals.update(overrides)
        return self.Reference.create(vals)

    def _build_screening_inputs(self, candidate):
        screening = self.env["iris.screening"].create({
            "candidate_id": candidate.id,
        })
        _system, user_text = screening._llm_build_messages()
        return user_text

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------
    def test_reference_markdown_renders_active_rows(self):
        self._make_row("ChatGPT", date(2022, 11, 30))
        self._make_row(
            "MCP (Model Context Protocol)", date(2024, 11, 25),
            source_url="https://example.com/mcp",
        )
        md = self.Reference.get_reference_markdown()
        lines = md.split("\n")
        self.assertEqual(lines[0], "| Technology | GA Date | Source |")
        self.assertEqual(lines[1], "| --- | --- | --- |")
        self.assertIn("| ChatGPT | 2022-11-30 | https://example.com/chatgpt |", md)
        self.assertIn(
            "| MCP (Model Context Protocol) | 2024-11-25 | "
            "https://example.com/mcp |",
            md,
        )

    def test_reference_markdown_escapes_pipes_and_newlines(self):
        self._make_row("Weird|Tech", note=False)
        md = self.Reference.get_reference_markdown()
        self.assertIn("Weird\\|Tech", md)

    def test_reference_markdown_excludes_archived_rows(self):
        keep = self._make_row("ChatGPT")
        gone = self._make_row("MCP", date(2024, 11, 25))
        gone.action_archive()
        md = self.Reference.get_reference_markdown()
        self.assertIn("ChatGPT", md)
        self.assertNotIn("MCP", md)

        keep.action_archive()
        self.assertEqual(self.Reference.get_reference_markdown(), "")

    def test_reference_markdown_empty_without_rows(self):
        self.assertEqual(self.Reference.get_reference_markdown(), "")

    # ------------------------------------------------------------------
    # Versioning constraint
    # ------------------------------------------------------------------
    def test_second_active_row_per_technology_rejected(self):
        self._make_row("ChatGPT")
        with self.assertRaises(ValidationError):
            self._make_row("ChatGPT", date(2023, 1, 1))

    def test_constraint_is_case_and_space_insensitive(self):
        self._make_row("ChatGPT")
        with self.assertRaises(ValidationError):
            self._make_row("  chatgpt ", date(2023, 1, 1))

    def test_archived_duplicates_allowed_as_version_history(self):
        old = self._make_row("ChatGPT")
        old.action_archive()
        new = self._make_row("ChatGPT", date(2022, 12, 1))
        self.assertTrue(new.active)
        # Re-activating the archived version would create a second active
        # row → rejected; archive-first is the documented update path.
        with self.assertRaises(ValidationError):
            old.action_unarchive()

    # ------------------------------------------------------------------
    # Screening prompt consumption (trusted table / fenced legacy / none)
    # ------------------------------------------------------------------
    def test_screening_uses_central_table_trusted_when_rows_exist(self):
        self._make_row("MCP (Model Context Protocol)", date(2024, 11, 25))
        candidate = self._make_candidate()
        user_text = self._build_screening_inputs(candidate)
        self.assertIn(
            "TECH DATE REFERENCE (authoritative, maintained by the hiring "
            "team):",
            user_text,
        )
        self.assertIn("| MCP (Model Context Protocol) | 2024-11-25 |", user_text)
        # The legacy fenced fallback is NOT used even though the candidate
        # carries snapshotted legacy text from the role profile.
        self.assertTrue((candidate.tech_date_reference or "").strip())
        self.assertNotIn("TECH DATE REFERENCE (legacy free text", user_text)
        self.assertNotIn("TECH DATE REFERENCE (LEGACY)", user_text)

    def test_screening_falls_back_to_fenced_legacy_text(self):
        # No central rows; the candidate's free-text field (snapshotted
        # from the seeded role profile at creation) is the fenced fallback.
        candidate = self._make_candidate()
        self.assertIn("Model Context Protocol", candidate.tech_date_reference)
        user_text = self._build_screening_inputs(candidate)
        self.assertIn(
            "TECH DATE REFERENCE (legacy free text — untrusted data):",
            user_text,
        )
        self.assertIn("BEGIN TECH DATE REFERENCE (LEGACY)>>>", user_text)
        self.assertIn("END TECH DATE REFERENCE (LEGACY)>>>", user_text)
        self.assertNotIn("TECH DATE REFERENCE (authoritative", user_text)

    def test_screening_renders_none_provided_without_any_source(self):
        candidate = self._make_candidate()
        candidate.write({"tech_date_reference": False})
        user_text = self._build_screening_inputs(candidate)
        self.assertIn("TECH DATE REFERENCE:   none provided", user_text)
        self.assertNotIn("TECH DATE REFERENCE (LEGACY)", user_text)
        self.assertNotIn("TECH DATE REFERENCE (authoritative", user_text)

    # ------------------------------------------------------------------
    # ACL: user read-only, manager maintains
    # ------------------------------------------------------------------
    def test_plain_user_is_read_only(self):
        row = self._make_row("ChatGPT")
        as_user = row.with_user(self.user_iris)
        self.assertEqual(as_user.read(["technology"])[0]["technology"], "ChatGPT")
        with self.assertRaises(AccessError):
            as_user.write({"note": "nope"})
        with self.assertRaises(AccessError):
            self.Reference.with_user(self.user_iris).create({
                "technology": "MCP",
                "ga_date": date(2024, 11, 25),
            })
        with self.assertRaises(AccessError):
            as_user.unlink()

    def test_manager_can_maintain_the_table(self):
        Manager = self.Reference.with_user(self.user_manager)
        row = Manager.create({
            "technology": "vLLM",
            "ga_date": date(2023, 6, 20),
        })
        row.write({"note": "initial release"})
        self.assertEqual(row.note, "initial release")

    # ------------------------------------------------------------------
    # Quarterly review cron
    # ------------------------------------------------------------------
    def _review_activities(self):
        return self.env["mail.activity"].search([
            ("res_model", "=", "iris.tech.date.reference"),
        ])

    def test_cron_no_ops_on_empty_table(self):
        result = self.Reference._cron_schedule_quarterly_review()
        self.assertFalse(result)
        self.assertFalse(self._review_activities())

    def test_cron_schedules_todo_on_latest_row(self):
        self._make_row("ChatGPT")
        latest = self._make_row("MCP", date(2024, 11, 25))
        result = self.Reference._cron_schedule_quarterly_review()
        self.assertTrue(result)

        activities = self._review_activities()
        self.assertEqual(len(activities), 1)
        activity = activities[0]
        self.assertEqual(activity.res_id, latest.id)
        self.assertEqual(
            activity.activity_type_id,
            self.env.ref("mail.mail_activity_data_todo"),
        )
        self.assertIn("Quarterly review", activity.summary)
        self.assertTrue(activity.user_id.active)

    def test_cron_skips_archived_only_table(self):
        row = self._make_row("ChatGPT")
        row.action_archive()
        self.assertFalse(self.Reference._cron_schedule_quarterly_review())
        self.assertFalse(self._review_activities())
