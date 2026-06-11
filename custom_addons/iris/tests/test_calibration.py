"""Tests for the calibration session models (P2-7, lean).

``action_start`` builds the participant×resume task matrix and queues one
LLM reference screening per task on the SHARED queue cron
(``iris.calibration.task`` is registered in ``_LLM_QUEUE_MODELS``); the
hardened verdict parser fills ``llm_verdict`` (parse failure →
``needs_review``); screeners record their own verdicts independently; the
session computes divergence per reference resume and ``action_close``
posts the summary to chatter. Plain iris users are read-only on
sessions/resumes and may only WRITE tasks (their verdict), never create.
"""

import base64

from odoo.exceptions import AccessError, UserError
from odoo.tests.common import tagged

from .common import IrisCase, make_pdf_bytes, mock_llm


@tagged("post_install", "-at_install", "iris")
class TestCalibration(IrisCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Session = cls.env["iris.calibration.session"]
        cls.Resume = cls.env["iris.calibration.resume"]
        cls.Task = cls.env["iris.calibration.task"]

    # ------------------------------------------------------------------
    # Factories
    # ------------------------------------------------------------------
    def _make_ref_resume(self, session, name="Reference A",
                         text="Synthetic resume — Staff Engineer at Initech"):
        pdf_b64 = base64.b64encode(make_pdf_bytes(text)).decode("ascii")
        return self.Resume.create({
            "session_id": session.id,
            "name": name,
            "file": pdf_b64,
            "filename": "reference.pdf",
            "target_role": "Head of Engineering",
        })

    def _make_session(self, n_resumes=2, participants=None):
        session = self.Session.create({"name": "Q3 Calibration"})
        for index in range(n_resumes):
            self._make_ref_resume(session, name=f"Reference {chr(65 + index)}")
        if participants is None:
            participants = self.user_iris | self.user_second
        session.write({"participant_ids": [(6, 0, participants.ids)]})
        return session

    def _started_session(self, **kwargs):
        session = self._make_session(**kwargs)
        session.action_start()
        return session

    def _task_for(self, session, user, resume=None):
        tasks = session.task_ids.filtered(lambda t: t.screener_id == user)
        if resume is not None:
            tasks = tasks.filtered(lambda t: t.resume_ref_id == resume)
        return tasks[:1]

    # ------------------------------------------------------------------
    # Reference resumes — extraction, no candidate linkage
    # ------------------------------------------------------------------
    def test_reference_resume_extracts_text_on_create(self):
        session = self.Session.create({"name": "Extract"})
        resume = self._make_ref_resume(
            session, text="Reference text for extraction",
        )
        self.assertIn("Reference text for extraction", resume.resume_text)
        # No candidate / S3 side effects — it is a session-local artifact.
        self.assertNotIn("resume_s3_key", resume._fields)

    # ------------------------------------------------------------------
    # action_start guards
    # ------------------------------------------------------------------
    def test_start_requires_two_resumes(self):
        session = self._make_session(n_resumes=1)
        with self.assertRaises(UserError):
            session.action_start()
        self.assertEqual(session.state, "draft")
        self.assertFalse(session.task_ids)

    def test_start_requires_two_participants(self):
        session = self._make_session(participants=self.user_iris)
        with self.assertRaises(UserError):
            session.action_start()

    def test_start_requires_extracted_text(self):
        session = self._make_session()
        blank = session.resume_ids[0]
        blank.sudo().write({"resume_text": False})
        with self.assertRaises(UserError) as ctx:
            session.action_start()
        self.assertIn(blank.name, str(ctx.exception))

    def test_start_only_from_draft(self):
        session = self._started_session()
        with self.assertRaises(UserError):
            session.action_start()

    # ------------------------------------------------------------------
    # Matrix creation + queueing
    # ------------------------------------------------------------------
    def test_start_creates_participant_resume_matrix_and_queues(self):
        session = self._started_session()
        self.assertEqual(session.state, "in_progress")
        self.assertEqual(len(session.task_ids), 4)  # 2 resumes × 2 users
        pairs = {
            (task.resume_ref_id.id, task.screener_id.id)
            for task in session.task_ids
        }
        self.assertEqual(len(pairs), 4)
        for task in session.task_ids:
            self.assertEqual(task.llm_status, "queued")
        bodies = "\n".join(self._chatter_bodies(session))
        self.assertIn("Calibration started", bodies)

    def test_start_without_api_key_raises_cleanly(self):
        session = self._make_session()
        self._clear_api_key()
        with self.assertRaises(UserError):
            session.action_start()

    # ------------------------------------------------------------------
    # LLM runs through the shared queue cron
    # ------------------------------------------------------------------
    def test_queue_cron_processes_tasks_and_parses_verdict(self):
        session = self._started_session()
        with mock_llm(self.VALID_SHIP_RECORD):
            self._run_llm_queue()
        for task in session.task_ids:
            self.assertEqual(task.llm_status, "done")
            self.assertEqual(task.llm_verdict, "ship")
            self.assertEqual(task.markdown_record, self.VALID_SHIP_RECORD)

    def test_task_prompt_uses_shared_fenced_inputs_builder(self):
        session = self._started_session()
        with mock_llm(self.VALID_SHIP_RECORD):
            self._run_llm_queue()
        task = session.task_ids[0]
        prompt = task.llm_prompt_input
        self.assertIn("INPUTS:", prompt)
        self.assertIn("TARGET ROLE / LEVEL:   Head of Engineering", prompt)
        self.assertIn("BEGIN RESUME>>>", prompt)
        self.assertIn("END RESUME>>>", prompt)

    def test_parse_failure_routes_task_to_needs_review(self):
        session = self._started_session()
        with mock_llm(self.UNPARSEABLE_RECORD):
            self._run_llm_queue()
        for task in session.task_ids:
            self.assertEqual(task.llm_status, "needs_review")
            self.assertFalse(task.llm_verdict)
            # The record stays readable for manual comparison.
            self.assertEqual(task.markdown_record, self.UNPARSEABLE_RECORD)

    def test_llm_failure_keeps_session_alive(self):
        session = self._started_session()
        with mock_llm(side_effect=RuntimeError("provider down")):
            self._run_llm_queue()
        for task in session.task_ids:
            self.assertEqual(task.llm_status, "failed")
        self.assertEqual(session.state, "in_progress")

    # ------------------------------------------------------------------
    # Screener verdict submission
    # ------------------------------------------------------------------
    def test_submit_requires_a_selected_verdict(self):
        session = self._started_session()
        task = self._task_for(session, self.user_iris)
        with self.assertRaises(UserError):
            task.action_submit_verdict()

    def test_screener_submits_own_task(self):
        session = self._started_session()
        task = self._task_for(session, self.user_iris)
        as_owner = task.with_user(self.user_iris)
        as_owner.write({"screener_verdict": "ship"})
        as_owner.action_submit_verdict()
        bodies = "\n".join(self._chatter_bodies(session))
        self.assertIn("SHIP", bodies)

    def test_other_user_cannot_submit_someone_elses_task(self):
        session = self._started_session()
        task = self._task_for(session, self.user_iris)
        task.write({"screener_verdict": "ship"})
        with self.assertRaises(UserError):
            task.with_user(self.user_second).action_submit_verdict()

    def test_manager_may_submit_on_behalf(self):
        session = self._started_session()
        task = self._task_for(session, self.user_iris)
        task.write({"screener_verdict": "hold"})
        self.assertTrue(
            task.with_user(self.user_manager).action_submit_verdict(),
        )

    # ------------------------------------------------------------------
    # Divergence compute
    # ------------------------------------------------------------------
    def test_divergent_when_screeners_disagree_on_a_resume(self):
        session = self._started_session()
        resume = session.resume_ids[0]
        self._task_for(session, self.user_iris, resume).write({
            "screener_verdict": "ship",
        })
        self._task_for(session, self.user_second, resume).write({
            "screener_verdict": "block",
        })
        self.assertTrue(session.divergent)
        self.assertIn("DIVERGENT", session.divergence_summary)
        self.assertIn(resume.name, session.divergence_summary)

    def test_aligned_verdicts_are_not_divergent(self):
        session = self._started_session()
        for task in session.task_ids:
            task.write({"screener_verdict": "ship"})
        self.assertFalse(session.divergent)
        self.assertIn("aligned", session.divergence_summary)

    def test_summary_includes_llm_verdict_note(self):
        session = self._started_session()
        with mock_llm(self.VALID_HOLD_RECORD):
            self._run_llm_queue()
        for task in session.task_ids:
            task.write({"screener_verdict": "ship"})
        self.assertIn("LLM: HOLD", session.divergence_summary)

    # ------------------------------------------------------------------
    # action_close
    # ------------------------------------------------------------------
    def test_close_requires_all_screener_verdicts(self):
        session = self._started_session()
        session.task_ids[0].write({"screener_verdict": "ship"})
        with self.assertRaises(UserError):
            session.action_close()
        self.assertEqual(session.state, "in_progress")

    def test_close_only_from_in_progress(self):
        session = self._make_session()
        with self.assertRaises(UserError):
            session.action_close()

    def test_close_posts_divergence_summary(self):
        session = self._started_session()
        resume_b = session.resume_ids[1]
        for task in session.task_ids:
            task.write({"screener_verdict": "ship"})
        self._task_for(session, self.user_second, resume_b).write({
            "screener_verdict": "hold",
        })
        session.action_close()
        self.assertEqual(session.state, "done")
        bodies = "\n".join(self._chatter_bodies(session))
        self.assertIn("divergence summary", bodies)
        self.assertIn("DIVERGENT", bodies)

    # ------------------------------------------------------------------
    # ACL — users read-only on sessions/resumes; tasks write-only
    # ------------------------------------------------------------------
    def test_plain_user_cannot_create_sessions_or_resumes(self):
        with self.assertRaises(AccessError):
            self.Session.with_user(self.user_iris).create({"name": "Rogue"})
        session = self.Session.create({"name": "Host"})
        with self.assertRaises(AccessError):
            self.Resume.with_user(self.user_iris).create({
                "session_id": session.id,
                "name": "Rogue Ref",
                "file": base64.b64encode(make_pdf_bytes()).decode("ascii"),
                "target_role": "Head of Engineering",
            })

    def test_plain_user_cannot_create_tasks_but_may_write_own_verdict(self):
        session = self._started_session()
        task = self._task_for(session, self.user_iris)
        with self.assertRaises(AccessError):
            self.Task.with_user(self.user_iris).create({
                "session_id": session.id,
                "resume_ref_id": session.resume_ids[0].id,
                "screener_id": self.user_iris.id,
            })
        as_owner = task.with_user(self.user_iris)
        as_owner.write({"screener_verdict": "ship"})
        self.assertEqual(task.screener_verdict, "ship")

    def test_manager_can_create_sessions(self):
        session = self.Session.with_user(self.user_manager).create({
            "name": "Manager Session",
        })
        self.assertEqual(session.state, "draft")
