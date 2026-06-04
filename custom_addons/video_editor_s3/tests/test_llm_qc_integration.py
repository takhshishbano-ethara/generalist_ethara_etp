# -*- coding: utf-8 -*-
import base64
from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged

from ..models.video_editor_job import _verdict_to_field


@tagged("post_install", "-at_install", "video_editor_s3")
class TestLLMQCProjectFields(TransactionCase):

    def setUp(self):
        super().setUp()
        self.project = self.env["video.editor.project"].create({
            "name": "Test Project",
        })

    def test_llm_fields_default_empty(self):
        self.assertFalse(self.project.llm_qc_result)
        self.assertFalse(self.project.llm_failure_reason)
        self.assertFalse(self.project.llm_fixed_prompt)
        self.assertFalse(self.project.llm_evaluated_at)
        self.assertFalse(self.project.llm_qc_force_passed)
        self.assertFalse(self.project.llm_qc_force_passed_by)
        self.assertFalse(self.project.llm_qc_force_passed_at)
        self.assertFalse(self.project.llm_qc_force_pass_reason)


@tagged("post_install", "-at_install", "video_editor_s3")
class TestActionRunLLMQC(TransactionCase):

    def _make_project(self, **overrides):
        vals = {
            "name": "QC Project",
            "output_s3_url": "https://example.com/trimmed.mp4",
            "prompt": "make a clip of a curling stone",
            "category": "high_motion_action",
            "style": "casual",
        }
        vals.update(overrides)
        return self.env["video.editor.project"].create(vals)

    def test_requires_output_s3_url(self):
        project = self._make_project(output_s3_url=False)
        with self.assertRaises(UserError):
            project.action_run_llm_qc()

    def test_requires_prompt(self):
        project = self._make_project(prompt=False)
        with self.assertRaises(UserError):
            project.action_run_llm_qc()

    def test_requires_category(self):
        project = self._make_project(category=False)
        with self.assertRaises(UserError):
            project.action_run_llm_qc()

    def test_requires_style(self):
        project = self._make_project(style=False)
        with self.assertRaises(UserError):
            project.action_run_llm_qc()

    def test_kicks_job_when_all_set(self):
        project = self._make_project()
        with patch.object(type(project), "_kick_job", return_value=self.env["video.editor.job"].new({"id": 99})) as kick:
            result = project.action_run_llm_qc()
        kick.assert_called_once_with("llm_qc")
        self.assertEqual(result.get("type"), "ir.actions.client")
        self.assertEqual(result.get("tag"), "display_notification")


@tagged("post_install", "-at_install", "video_editor_s3")
class TestActionForcePass(TransactionCase):

    def _evaluated_project(self):
        return self.env["video.editor.project"].create({
            "name": "Eval Project",
            "llm_qc_result": "fail",
            "llm_failure_reason": "Reviewer flagged GV-IDENTITY-DRIFT",
            "llm_evaluated_at": "2026-06-01 10:00:00",
        })

    def test_without_reason_opens_wizard(self):
        project = self._evaluated_project()
        result = project.action_force_pass_llm_qc()
        self.assertEqual(result.get("type"), "ir.actions.act_window")
        self.assertEqual(result.get("res_model"), "video.editor.llm.qc.force.pass.wizard")
        self.assertEqual(result.get("target"), "new")
        self.assertEqual(result["context"]["default_project_id"], project.id)
        self.assertFalse(project.llm_qc_force_passed)

    def test_with_reason_in_context_writes_fields(self):
        project = self._evaluated_project()
        before = self.env["mail.message"].search_count([
            ("model", "=", "video.editor.project"), ("res_id", "=", project.id),
        ])
        project.with_context(
            default_llm_qc_force_pass_reason="Manual visual inspection ok"
        ).action_force_pass_llm_qc()
        self.assertTrue(project.llm_qc_force_passed)
        self.assertEqual(project.llm_qc_force_passed_by, self.env.user)
        self.assertTrue(project.llm_qc_force_passed_at)
        self.assertEqual(project.llm_qc_force_pass_reason, "Manual visual inspection ok")
        self.assertEqual(project.llm_qc_result, "fail")
        after = self.env["mail.message"].search_count([
            ("model", "=", "video.editor.project"), ("res_id", "=", project.id),
        ])
        self.assertGreater(after, before)

    def test_refuses_when_not_evaluated(self):
        project = self.env["video.editor.project"].create({"name": "Unrun"})
        with self.assertRaises(UserError):
            project.with_context(
                default_llm_qc_force_pass_reason="anything"
            ).action_force_pass_llm_qc()

    def test_refuses_double_force_pass(self):
        project = self._evaluated_project()
        project.with_context(
            default_llm_qc_force_pass_reason="first"
        ).action_force_pass_llm_qc()
        with self.assertRaises(UserError):
            project.with_context(
                default_llm_qc_force_pass_reason="second"
            ).action_force_pass_llm_qc()


@tagged("post_install", "-at_install", "video_editor_s3")
class TestForcePassWizard(TransactionCase):

    def setUp(self):
        super().setUp()
        self.project = self.env["video.editor.project"].create({
            "name": "Wiz Project",
            "llm_qc_result": "fail",
            "llm_failure_reason": "GV-HAND-MORPHOLOGY",
            "llm_evaluated_at": "2026-06-01 10:00:00",
        })

    def test_empty_reason_rejected(self):
        wiz = self.env["video.editor.llm.qc.force.pass.wizard"].create({
            "project_id": self.project.id, "reason": "   ",
        })
        with self.assertRaises(UserError):
            wiz.action_confirm()
        self.assertFalse(self.project.llm_qc_force_passed)

    def test_confirm_force_passes_project(self):
        wiz = self.env["video.editor.llm.qc.force.pass.wizard"].create({
            "project_id": self.project.id, "reason": "approved by editor",
        })
        wiz.action_confirm()
        self.assertTrue(self.project.llm_qc_force_passed)
        self.assertEqual(self.project.llm_qc_force_pass_reason, "approved by editor")

    def test_related_fields(self):
        wiz = self.env["video.editor.llm.qc.force.pass.wizard"].create({
            "project_id": self.project.id, "reason": "x",
        })
        self.assertEqual(wiz.original_verdict, "fail")
        self.assertEqual(wiz.original_failure_reason, "GV-HAND-MORPHOLOGY")


@tagged("post_install", "-at_install", "video_editor_s3")
class TestActionApplyFixedPrompt(TransactionCase):

    def test_copies_fixed_prompt_to_prompt(self):
        project = self.env["video.editor.project"].create({
            "name": "Fix Project",
            "prompt": "original prompt",
            "llm_fixed_prompt": "corrected prompt with audio cues",
        })
        project.action_apply_fixed_prompt()
        self.assertEqual(project.prompt, "corrected prompt with audio cues")

    def test_refuses_when_no_fixed_prompt(self):
        project = self.env["video.editor.project"].create({
            "name": "No Fix", "prompt": "p",
        })
        with self.assertRaises(UserError):
            project.action_apply_fixed_prompt()


@tagged("post_install", "-at_install", "video_editor_s3")
class TestVerdictToField(TransactionCase):

    def test_pass_mapping(self):
        self.assertEqual(_verdict_to_field("PASS"), "pass")
        self.assertEqual(_verdict_to_field("pass"), "pass")

    def test_fail_mapping(self):
        self.assertEqual(_verdict_to_field("FAIL"), "fail")

    def test_flag_mapping(self):
        self.assertEqual(_verdict_to_field("FLAG"), "flag")

    def test_unknown_returns_false(self):
        self.assertFalse(_verdict_to_field(""))
        self.assertFalse(_verdict_to_field(None))
        self.assertFalse(_verdict_to_field("review"))


@tagged("post_install", "-at_install", "video_editor_s3")
class TestLLMQCSeedResolver(TransactionCase):

    def setUp(self):
        super().setUp()
        self.ICP = self.env["ir.config_parameter"].sudo()
        self.settings = self.env["video.editor.s3.settings"]
        self.ICP.set_param("video_editor_s3.llm_qc_seed_file", "")
        self.ICP.set_param("video_editor_s3.llm_qc_seed_filename", "")

    def test_falls_back_to_bundled_when_empty(self):
        result = self.settings.get_llm_qc_seed_prompt()
        self.assertTrue(result)
        self.assertGreater(len(result), 500)

    def test_uploaded_override_decoded(self):
        b64 = base64.b64encode(b"CUSTOM SEED CONTENT").decode("ascii")
        self.ICP.set_param("video_editor_s3.llm_qc_seed_file", b64)
        self.assertEqual(self.settings.get_llm_qc_seed_prompt(), "CUSTOM SEED CONTENT")

    def test_invalid_base64_falls_back(self):
        self.ICP.set_param("video_editor_s3.llm_qc_seed_file", "!!not-base64!!")
        result = self.settings.get_llm_qc_seed_prompt()
        self.assertTrue(result)
        self.assertGreater(len(result), 500)


@tagged("post_install", "-at_install", "video_editor_s3")
class TestLLMQCConfigResolver(TransactionCase):

    def test_returns_api_key_and_model(self):
        ICP = self.env["ir.config_parameter"].sudo()
        ICP.set_param("video_editor_s3.openrouter_api_key", "sk-or-test123")
        ICP.set_param("video_editor_s3.llm_qc_model_id", "openrouter/google/gemini-2.5-pro")
        cfg = self.env["video.editor.s3.settings"].get_llm_qc_config()
        self.assertEqual(cfg.get("api_key"), "sk-or-test123")
        self.assertEqual(cfg.get("model_id"), "openrouter/google/gemini-2.5-pro")

    def test_default_model_when_unset(self):
        ICP = self.env["ir.config_parameter"].sudo()
        ICP.set_param("video_editor_s3.llm_qc_model_id", "")
        cfg = self.env["video.editor.s3.settings"].get_llm_qc_config()
        self.assertIn("gemini", cfg.get("model_id", "").lower())


class _FakeRecord:

    def __init__(self, code=""):
        self.code = code

    def __bool__(self):
        return bool(self.code)


class _FakeProject:

    def __init__(self, **kwargs):
        self.category_id = _FakeRecord()
        self.sub_category_id = _FakeRecord()
        self.category = ""
        self.sub_category = ""
        self.topic_name = ""
        self.topic = ""
        self.style = ""
        self.source_metadata = {}
        self.output_resolution = ""
        self.output_duration_seconds = 0.0
        self.output_fps = 0.0
        self.resolution = ""
        self.duration_seconds = 0.0
        self.source_fps = 0.0
        for k, v in kwargs.items():
            setattr(self, k, v)


@tagged("post_install", "-at_install", "video_editor_s3")
class TestExtractQCMetadata(TransactionCase):

    def setUp(self):
        super().setUp()
        from odoo.addons.video_editor_s3.models.video_editor_job import _extract_qc_metadata
        self.extract = _extract_qc_metadata

    def test_category_id_preferred_over_selection(self):
        p = _FakeProject(category_id=_FakeRecord("test_category_code"), category="sports")
        self.assertEqual(self.extract(p)["category"], "test_category_code")

    def test_falls_back_to_category_selection_when_no_m2o(self):
        p = _FakeProject(category="sports")
        self.assertEqual(self.extract(p)["category"], "sports")

    def test_category_empty_when_neither_set(self):
        p = _FakeProject()
        self.assertEqual(self.extract(p)["category"], "")

    def test_sub_category_id_preferred_over_selection(self):
        p = _FakeProject(
            sub_category_id=_FakeRecord("test_sub_code"),
            sub_category="legacy_sub",
        )
        self.assertEqual(self.extract(p)["sub_category"], "test_sub_code")

    def test_topic_name_extracted(self):
        p = _FakeProject(topic_name="Snowboarding Trick")
        self.assertEqual(self.extract(p)["topic"], "Snowboarding Trick")

    def test_style_unchanged_path(self):
        p = _FakeProject(style="precise")
        self.assertEqual(self.extract(p)["style"], "precise")

    def test_output_resolution_preferred(self):
        p = _FakeProject(
            output_resolution="1920x1080",
            source_metadata={"resolution": "3840x2160"},
        )
        self.assertEqual(self.extract(p)["resolution"], "1920x1080")

    def test_resolution_falls_back_to_source_metadata(self):
        p = _FakeProject(source_metadata={"resolution": "1280x720"})
        self.assertEqual(self.extract(p)["resolution"], "1280x720")

    def test_output_duration_preferred(self):
        p = _FakeProject(
            output_duration_seconds=12.5,
            source_metadata={"duration": 300.0},
        )
        self.assertEqual(self.extract(p)["duration_seconds"], 12.5)

    def test_duration_falls_back_to_source_metadata(self):
        p = _FakeProject(source_metadata={"duration": 45.0})
        self.assertEqual(self.extract(p)["duration_seconds"], 45.0)

    def test_output_fps_preferred(self):
        p = _FakeProject(
            output_fps=24.0,
            source_metadata={"fps": 60.0},
        )
        self.assertEqual(self.extract(p)["fps"], 24.0)

    def test_fps_falls_back_to_source_metadata(self):
        p = _FakeProject(source_metadata={"fps": 30.0})
        self.assertEqual(self.extract(p)["fps"], 30.0)

    def test_invalid_duration_in_source_meta_returns_zero(self):
        p = _FakeProject(source_metadata={"duration": "not-a-number"})
        self.assertEqual(self.extract(p)["duration_seconds"], 0.0)

    def test_invalid_fps_in_source_meta_returns_zero(self):
        p = _FakeProject(source_metadata={"fps": "garbage"})
        self.assertEqual(self.extract(p)["fps"], 0.0)

    def test_full_priority_chain_for_resolution(self):
        p = _FakeProject(
            output_resolution="1920x1080",
            source_metadata={"resolution": "3840x2160"},
            resolution="640x480",
        )
        self.assertEqual(self.extract(p)["resolution"], "1920x1080")
