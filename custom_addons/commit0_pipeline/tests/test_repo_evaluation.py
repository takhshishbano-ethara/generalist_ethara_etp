# -*- coding: utf-8 -*-
from odoo.exceptions import UserError
from odoo.tests import tagged
from odoo.tests.common import TransactionCase


@tagged("post_install", "-at_install")
class TestRepoEvaluation(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Eval = cls.env["commit0.repo.evaluation"]
        cls.eval_record = cls.Eval.create(
            {
                "repo_name": "test-repo",
                "repo_url": "https://github.com/test/test-repo",
            }
        )

    def test_create_assigns_sequence(self):
        self.assertTrue(self.eval_record.name)
        rec2 = self.Eval.create(
            {
                "repo_name": "another-repo",
                "repo_url": "https://github.com/test/another-repo",
            }
        )
        self.assertTrue(rec2.name)
        self.assertIn(
            rec2.name[:4],
            ("REPO", "New"),
        )

    def test_default_stage_is_stage1(self):
        self.assertEqual(self.eval_record.current_stage, "stage1")

    def test_default_terminal_state_is_none(self):
        self.assertEqual(self.eval_record.terminal_state, "none")

    def test_stage1_requires_clone_path(self):
        self.eval_record.write({"repo_understood": True, "clone_path": False})
        with self.assertRaises(UserError):
            self.eval_record.action_confirm_understanding()

    def test_stage1_requires_understanding(self):
        self.eval_record.write(
            {
                "clone_path": "/tmp/test-repo",
                "repo_understood": False,
            }
        )
        with self.assertRaises(UserError):
            self.eval_record.action_confirm_understanding()

    def test_stage1_to_stage2(self):
        self.eval_record.write(
            {
                "clone_path": "/tmp/test-repo",
                "repo_understood": True,
            }
        )
        self.eval_record.action_confirm_understanding()
        self.assertEqual(self.eval_record.current_stage, "stage2")

    def test_stage2_pass_requires_status(self):
        self.eval_record.write(
            {
                "current_stage": "stage2",
                "repo_status": "pending",
            }
        )
        with self.assertRaises(UserError):
            self.eval_record.action_stage2_pass()

    def test_stage2_pass_advances(self):
        self.eval_record.write(
            {
                "current_stage": "stage2",
                "repo_status": "pass",
            }
        )
        self.eval_record.action_stage2_pass()
        self.assertEqual(self.eval_record.current_stage, "stage3")

    def test_stage2_fail_requires_reason(self):
        self.eval_record.write(
            {
                "current_stage": "stage2",
                "repo_status": "fail",
                "failure_reason": False,
            }
        )
        with self.assertRaises(UserError):
            self.eval_record.action_stage2_fail()

    def test_stage2_fail_terminal(self):
        self.eval_record.write(
            {
                "current_stage": "stage2",
                "repo_status": "fail",
                "failure_reason": "Too many C extensions.",
            }
        )
        self.eval_record.action_stage2_fail()
        self.assertEqual(self.eval_record.terminal_state, "repo_not_suitable")
        self.assertEqual(self.eval_record.current_stage, "failed")

    def test_nine_checklist_fields_exist(self):
        checklist_fields = [
            "check_language",
            "check_tests",
            "check_documentation",
            "check_github_metrics",
            "check_project_structure",
            "check_build",
            "check_code_quality",
            "check_reliability",
            "check_complexity",
        ]
        for field_name in checklist_fields:
            value = getattr(self.eval_record, field_name)
            self.assertFalse(
                value,
                f"{field_name} should default to False on a new record.",
            )

    def test_stage3_complete_computed_false(self):
        self.eval_record.write(
            {
                "fork_status": "done",
                "reference_commit_status": "running",
                "document_create_status": "pending",
            }
        )
        self.assertFalse(self.eval_record.stage3_complete)

    def test_stage3_complete_computed_true(self):
        self.eval_record.write(
            {
                "fork_status": "done",
                "reference_commit_status": "done",
                "document_create_status": "done",
            }
        )
        self.assertTrue(self.eval_record.stage3_complete)

    def test_advance_to_stage4_requires_complete(self):
        self.eval_record.write(
            {
                "current_stage": "stage3",
                "fork_status": "done",
                "reference_commit_status": "done",
                "document_create_status": "pending",
            }
        )
        with self.assertRaises(UserError):
            self.eval_record.action_advance_to_stage4()

    def test_doc_valid_computed(self):
        self.eval_record.write(
            {
                "doc_check_related": True,
                "doc_check_not_blank": True,
                "doc_check_meaningful": False,
            }
        )
        self.assertFalse(self.eval_record.doc_valid)

        self.eval_record.write({"doc_check_meaningful": True})
        self.assertTrue(self.eval_record.doc_valid)

    def test_reject_spec_terminal(self):
        self.eval_record.write({"current_stage": "stage4"})
        self.eval_record.action_reject_spec()
        self.assertEqual(self.eval_record.terminal_state, "rejected")
        self.assertEqual(self.eval_record.current_stage, "failed")

    def test_trigger_stubbing_requires_valid_doc(self):
        self.eval_record.write(
            {
                "current_stage": "stage4",
                "doc_check_related": True,
                "doc_check_not_blank": False,
                "doc_check_meaningful": True,
            }
        )
        with self.assertRaises(UserError):
            self.eval_record.action_trigger_stubbing()

    def test_stub_approve_requires_yes(self):
        self.eval_record.write(
            {
                "current_stage": "stage5",
                "stub_proper": "pending",
            }
        )
        with self.assertRaises(UserError):
            self.eval_record.action_stub_approve()

    def test_stub_reject_requires_reason(self):
        self.eval_record.write(
            {
                "current_stage": "stage5",
                "stub_proper": "no",
                "stub_failure_reason": False,
            }
        )
        with self.assertRaises(UserError):
            self.eval_record.action_stub_reject()

    def test_stub_reject_terminal(self):
        self.eval_record.write(
            {
                "current_stage": "stage5",
                "stub_proper": "no",
                "stub_failure_reason": "Stubs are incomplete and missing docstrings.",
            }
        )
        self.eval_record.action_stub_reject()
        self.assertEqual(self.eval_record.terminal_state, "not_stubbed")
        self.assertEqual(self.eval_record.current_stage, "failed")
