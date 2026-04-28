# -*- coding: utf-8 -*-
import json
import re
from unittest.mock import patch, MagicMock

from odoo.tests import tagged

from .common import TalosTestCase

_QC_MOD = "odoo.addons.talos.controllers.llm_assisst_qc"


@tagged("post_install", "-at_install")
class TestCallBedrockConverse(TalosTestCase):

    @patch(_QC_MOD + ".httpx")
    def test_call_bedrock_success(self, mock_httpx):
        from odoo.addons.talos.controllers.llm_assisst_qc import _call_bedrock_converse

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "output": {
                "message": {"content": [{"text": "Hello response"}]}
            },
            "usage": {"inputTokens": 10, "outputTokens": 5},
        }
        mock_client = MagicMock()
        mock_client.post.return_value = mock_resp
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_httpx.Client.return_value = mock_client

        text, usage = _call_bedrock_converse(
            "key", "arn:model", "us-east-1", "system", "user msg",
        )
        self.assertEqual(text, "Hello response")
        self.assertEqual(usage["input_tokens"], 10)
        self.assertEqual(usage["output_tokens"], 5)

    @patch(_QC_MOD + ".httpx")
    def test_call_bedrock_http_error(self, mock_httpx):
        from odoo.addons.talos.controllers.llm_assisst_qc import _call_bedrock_converse

        mock_resp = MagicMock()
        mock_resp.status_code = 429
        mock_resp.text = "Throttled"
        mock_client = MagicMock()
        mock_client.post.return_value = mock_resp
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_httpx.Client.return_value = mock_client

        with self.assertRaises(RuntimeError):
            _call_bedrock_converse("key", "arn:m", "us-east-1", "", "msg")

    @patch(_QC_MOD + ".httpx")
    def test_call_bedrock_service_error(self, mock_httpx):
        from odoo.addons.talos.controllers.llm_assisst_qc import _call_bedrock_converse

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "output": {"__type": "ValidationException"},
        }
        mock_client = MagicMock()
        mock_client.post.return_value = mock_resp
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_httpx.Client.return_value = mock_client

        with self.assertRaises(RuntimeError):
            _call_bedrock_converse("key", "arn:m", "us-east-1", "", "msg")


@tagged("post_install", "-at_install")
class TestParseHelpers(TalosTestCase):

    def test_parse_json_response_code_block(self):
        from odoo.addons.talos.controllers.llm_assisst_qc import _parse_json_response

        text = 'Here is the result:\n```json\n{"severity": "low"}\n```\nDone.'
        result = _parse_json_response(text)
        self.assertIsNotNone(result)
        self.assertEqual(result["severity"], "low")

    def test_parse_json_response_bare_json(self):
        from odoo.addons.talos.controllers.llm_assisst_qc import _parse_json_response

        text = 'Some preamble {"key": "val"} more text'
        result = _parse_json_response(text)
        self.assertIsNotNone(result)
        self.assertEqual(result["key"], "val")

    def test_parse_json_response_no_json(self):
        from odoo.addons.talos.controllers.llm_assisst_qc import _parse_json_response

        result = _parse_json_response("No JSON here at all!")
        self.assertIsNone(result)

    def test_parse_qc_verdict_valid(self):
        from odoo.addons.talos.controllers.llm_assisst_qc import _parse_qc_verdict

        text = '```json\n{"severity": "high", "summary": "Bad", "total_fails": 3, "total_warns": 1, "total_passes": 2, "checks": []}\n```'
        v = _parse_qc_verdict(text)
        self.assertIsNotNone(v)
        self.assertEqual(v["severity"], "high")
        self.assertEqual(v["summary"], "Bad")
        self.assertEqual(v["total_fails"], 3)
        self.assertEqual(v["total_warns"], 1)
        self.assertEqual(v["total_passes"], 2)

    def test_parse_qc_verdict_fallback_severity(self):
        from odoo.addons.talos.controllers.llm_assisst_qc import _parse_qc_verdict

        text = '{"summary": "ok"}\nOVERALL VERDICT: pass'
        v = _parse_qc_verdict(text)
        self.assertIsNotNone(v)
        self.assertEqual(v["severity"], "low")

    def test_is_degenerate_normal_text(self):
        from odoo.addons.talos.controllers.llm_assisst_qc import _is_degenerate

        self.assertFalse(_is_degenerate("This is a normal response with varied text."))

    def test_is_degenerate_repeated_chars(self):
        from odoo.addons.talos.controllers.llm_assisst_qc import _is_degenerate

        self.assertTrue(_is_degenerate("!" * 100))


@tagged("post_install", "-at_install")
class TestQCEndpoint(TalosTestCase):

    @patch(_QC_MOD + "._call_bedrock_converse")
    @patch(_QC_MOD + "._load_dotenv", return_value={"AWS_BEARER_TOKEN_BEDROCK": "tok"})
    @patch(_QC_MOD + "._get_system_prompt", return_value="sys prompt")
    def test_qc_success(self, _sys, _dotenv, mock_bedrock):
        mock_bedrock.return_value = ('{"severity":"low","summary":"ok","total_fails":0,"total_warns":0,"total_passes":5,"checks":[]}', {"input_tokens": 10, "output_tokens": 5})
        self._set_param("talos.bedrock_inference_arn", "arn:test")
        self._set_param("talos.bedrock_region", "us-east-1")

        ctrl = self.env["ir.http"]
        from odoo.addons.talos.controllers.llm_assisst_qc import (
            _call_bedrock_converse,
            _parse_json_response,
            _parse_qc_verdict,
            _is_degenerate,
            _load_dotenv,
        )

        env = _load_dotenv()
        api_key = env.get("AWS_BEARER_TOKEN_BEDROCK", "").strip()
        self.assertTrue(api_key)
        text, usage = _call_bedrock_converse(
            api_key, "arn:test", "us-east-1", "sys prompt", "test prompt",
        )
        self.assertIn("severity", text)
        self.assertEqual(usage["input_tokens"], 10)

    def test_qc_missing_prompt(self):
        from odoo.addons.talos.controllers.llm_assisst_qc import LlmAssistQc

        ctrl = LlmAssistQc()
        with patch("odoo.http.request") as mock_req:
            mock_req.env = self.env
            result = ctrl.qc_prompt(prompt="")
        self.assertIn("error", result)

    @patch(_QC_MOD + "._load_dotenv", return_value={})
    def test_qc_missing_credentials(self, _dotenv):
        from odoo.addons.talos.controllers.llm_assisst_qc import LlmAssistQc

        ctrl = LlmAssistQc()
        with patch("odoo.http.request") as mock_req:
            mock_req.env = self.env
            result = ctrl.qc_prompt(prompt="test")
        self.assertIn("error", result)
        self.assertIn("AWS_BEARER_TOKEN", result["error"])

    @patch(_QC_MOD + "._call_bedrock_converse")
    @patch(_QC_MOD + "._load_dotenv", return_value={"AWS_BEARER_TOKEN_BEDROCK": "tok"})
    @patch(_QC_MOD + "._get_system_prompt", return_value="sys")
    def test_qc_degenerate_retries(self, _sys, _dotenv, mock_bedrock):
        degenerate = "!" * 100
        normal = '{"severity":"low","summary":"ok","total_fails":0,"total_warns":0,"total_passes":1,"checks":[]}'
        mock_bedrock.side_effect = [
            (degenerate, {"input_tokens": 5, "output_tokens": 5}),
            (normal, {"input_tokens": 5, "output_tokens": 5}),
        ]
        self._set_param("talos.bedrock_inference_arn", "arn:test")
        self._set_param("talos.bedrock_region", "us-east-1")

        from odoo.addons.talos.controllers.llm_assisst_qc import LlmAssistQc

        ctrl = LlmAssistQc()
        with patch("odoo.http.request") as mock_req:
            mock_req.env = self.env
            result = ctrl.qc_prompt(prompt="test prompt", temperature=0.3)
        self.assertTrue(result.get("success"))
        self.assertEqual(mock_bedrock.call_count, 2)


@tagged("post_install", "-at_install")
class TestTrajectoryQC(TalosTestCase):

    def test_trajectory_qc_sets_pending(self):
        traj = json.dumps([self._make_session_entry()])
        self.task.write({"claude_trajectory": traj})

        from odoo.addons.talos.controllers.llm_assisst_qc import LlmAssistQc

        ctrl = LlmAssistQc()
        with patch("odoo.http.request") as mock_req:
            mock_req.env = self.env
            mock_req.env.cr = self.env.cr
            mock_req.env.cr.dbname = self.env.cr.dbname
            mock_req.env.cr.postcommit = MagicMock()
            result = ctrl.trajectory_qc(
                record_id=self.task.id,
                field_name="claude_trajectory",
                entry_index=0,
            )
        self.assertTrue(result.get("success"))
        self.task.invalidate_recordset()
        data = json.loads(self.task.claude_trajectory)
        entries = data if isinstance(data, list) else [data]
        self.assertEqual(entries[0].get("qc_status"), "pending")

    def test_trajectory_qc_invalid_params(self):
        from odoo.addons.talos.controllers.llm_assisst_qc import LlmAssistQc

        ctrl = LlmAssistQc()
        with patch("odoo.http.request") as mock_req:
            mock_req.env = self.env
            result = ctrl.trajectory_qc(record_id=0, field_name="")
        self.assertIn("error", result)


@tagged("post_install", "-at_install")
class TestQCToggle(TalosTestCase):

    def test_qc_prompt_skipped_when_disabled(self):
        from odoo.addons.talos.controllers.llm_assisst_qc import LlmAssistQc

        self._set_param("talos.disable_prompt_qc", "True")
        ctrl = LlmAssistQc()
        with patch("odoo.http.request") as mock_req:
            mock_req.env = self.env
            result = ctrl.qc_prompt(prompt="test")
        self.assertTrue(result.get("skipped"))

    @patch(_QC_MOD + "._call_bedrock_converse")
    @patch(_QC_MOD + "._load_dotenv", return_value={"AWS_BEARER_TOKEN_BEDROCK": "tok"})
    @patch(_QC_MOD + "._get_system_prompt", return_value="sys")
    def test_qc_prompt_runs_when_not_disabled(self, _sys, _dotenv, mock_bedrock):
        from odoo.addons.talos.controllers.llm_assisst_qc import LlmAssistQc

        mock_bedrock.return_value = (
            '{"severity":"low","summary":"ok","total_fails":0,"total_warns":0,"total_passes":1,"checks":[]}',
            {"input_tokens": 5, "output_tokens": 5},
        )
        self._set_param("talos.disable_prompt_qc", "False")
        self._set_param("talos.bedrock_inference_arn", "arn:test")
        self._set_param("talos.bedrock_region", "us-east-1")
        ctrl = LlmAssistQc()
        with patch("odoo.http.request") as mock_req:
            mock_req.env = self.env
            result = ctrl.qc_prompt(prompt="test")
        self.assertNotIn("skipped", result)

    @patch(_QC_MOD + "._call_bedrock_converse")
    @patch(_QC_MOD + "._load_dotenv", return_value={"AWS_BEARER_TOKEN_BEDROCK": "tok"})
    @patch(_QC_MOD + "._get_system_prompt", return_value="sys")
    def test_qc_prompt_runs_when_param_missing(self, _sys, _dotenv, mock_bedrock):
        from odoo.addons.talos.controllers.llm_assisst_qc import LlmAssistQc

        mock_bedrock.return_value = (
            '{"severity":"low","summary":"ok","total_fails":0,"total_warns":0,"total_passes":1,"checks":[]}',
            {"input_tokens": 5, "output_tokens": 5},
        )
        self._set_param("talos.bedrock_inference_arn", "arn:test")
        self._set_param("talos.bedrock_region", "us-east-1")
        ctrl = LlmAssistQc()
        with patch("odoo.http.request") as mock_req:
            mock_req.env = self.env
            result = ctrl.qc_prompt(prompt="test")
        self.assertNotIn("skipped", result)

    def test_trajectory_qc_skipped_when_disabled(self):
        from odoo.addons.talos.controllers.llm_assisst_qc import LlmAssistQc

        self._set_param("talos.disable_trajectory_qc", "True")
        ctrl = LlmAssistQc()
        with patch("odoo.http.request") as mock_req:
            mock_req.env = self.env
            result = ctrl.trajectory_qc(
                record_id=self.task.id,
                field_name="claude_trajectory",
                entry_index=0,
            )
        self.assertTrue(result.get("skipped"))

    def test_trajectory_qc_runs_when_not_disabled(self):
        from odoo.addons.talos.controllers.llm_assisst_qc import LlmAssistQc

        traj = json.dumps([self._make_session_entry()])
        self.task.write({"claude_trajectory": traj})
        self._set_param("talos.disable_trajectory_qc", "False")
        ctrl = LlmAssistQc()
        with patch("odoo.http.request") as mock_req:
            mock_req.env = self.env
            mock_req.env.cr = self.env.cr
            mock_req.env.cr.dbname = self.env.cr.dbname
            mock_req.env.cr.postcommit = MagicMock()
            result = ctrl.trajectory_qc(
                record_id=self.task.id,
                field_name="claude_trajectory",
                entry_index=0,
            )
        self.assertNotIn("skipped", result)


@tagged("post_install", "-at_install")
class TestGenerateTaskDescription(TalosTestCase):

    def test_generate_taskdesc_success(self):
        from odoo.addons.talos.controllers.llm_assisst_qc import LlmAssistQc

        traj = json.dumps([self._make_session_entry()])
        self.task.write({"claude_trajectory": traj})
        ctrl = LlmAssistQc()
        with patch("odoo.http.request") as mock_req:
            mock_req.env = self.env
            mock_req.env.cr = self.env.cr
            mock_req.env.cr.dbname = self.env.cr.dbname
            mock_req.env.cr.postcommit = MagicMock()
            result = ctrl.generate_task_description(
                record_id=self.task.id,
                field_name="claude_trajectory",
                entry_index=0,
            )
        self.assertTrue(result.get("success"))
        self.task.invalidate_recordset()
        data = json.loads(self.task.claude_trajectory)
        entries = data if isinstance(data, list) else [data]
        self.assertEqual(entries[0].get("task_description_status"), "pending")

    def test_generate_taskdesc_missing_params(self):
        from odoo.addons.talos.controllers.llm_assisst_qc import LlmAssistQc

        ctrl = LlmAssistQc()
        with patch("odoo.http.request") as mock_req:
            mock_req.env = self.env
            result = ctrl.generate_task_description(record_id=0, field_name="")
        self.assertIn("error", result)

    def test_generate_taskdesc_nonexistent_task(self):
        from odoo.addons.talos.controllers.llm_assisst_qc import LlmAssistQc

        ctrl = LlmAssistQc()
        with patch("odoo.http.request") as mock_req:
            mock_req.env = self.env
            result = ctrl.generate_task_description(
                record_id=999999,
                field_name="claude_trajectory",
                entry_index=0,
            )
        self.assertIn("error", result)
        self.assertIn("Task not found", result["error"])

    def test_generate_taskdesc_empty_trajectory(self):
        from odoo.addons.talos.controllers.llm_assisst_qc import LlmAssistQc

        self.task.write({"claude_trajectory": ""})
        ctrl = LlmAssistQc()
        with patch("odoo.http.request") as mock_req:
            mock_req.env = self.env
            result = ctrl.generate_task_description(
                record_id=self.task.id,
                field_name="claude_trajectory",
                entry_index=0,
            )
        self.assertIn("error", result)
        self.assertIn("No trajectory data", result["error"])

    def test_generate_taskdesc_unparseable_json(self):
        from odoo.addons.talos.controllers.llm_assisst_qc import LlmAssistQc

        self.task.write({"claude_trajectory": "NOT JSON"})
        ctrl = LlmAssistQc()
        with patch("odoo.http.request") as mock_req:
            mock_req.env = self.env
            result = ctrl.generate_task_description(
                record_id=self.task.id,
                field_name="claude_trajectory",
                entry_index=0,
            )
        self.assertIn("error", result)
        self.assertIn("Could not parse", result["error"])

    def test_generate_taskdesc_invalid_entry_index(self):
        from odoo.addons.talos.controllers.llm_assisst_qc import LlmAssistQc

        traj = json.dumps([self._make_session_entry()])
        self.task.write({"claude_trajectory": traj})
        ctrl = LlmAssistQc()
        with patch("odoo.http.request") as mock_req:
            mock_req.env = self.env
            result = ctrl.generate_task_description(
                record_id=self.task.id,
                field_name="claude_trajectory",
                entry_index=99,
            )
        self.assertIn("error", result)
        self.assertIn("Invalid entry index", result["error"])

    def test_generate_taskdesc_negative_entry_index(self):
        from odoo.addons.talos.controllers.llm_assisst_qc import LlmAssistQc

        traj = json.dumps([self._make_session_entry()])
        self.task.write({"claude_trajectory": traj})
        ctrl = LlmAssistQc()
        with patch("odoo.http.request") as mock_req:
            mock_req.env = self.env
            result = ctrl.generate_task_description(
                record_id=self.task.id,
                field_name="claude_trajectory",
                entry_index=-1,
            )
        self.assertIn("error", result)
        self.assertIn("Invalid entry index", result["error"])


@tagged("post_install", "-at_install")
class TestAbortTrajectoryAction(TalosTestCase):

    def _write_entry_with_status(self, status_key, status_val):
        entry = self._make_session_entry()
        entry[status_key] = status_val
        traj = json.dumps([entry])
        self.task.write({"claude_trajectory": traj})

    def test_abort_qc_success(self):
        from odoo.addons.talos.controllers.llm_assisst_qc import LlmAssistQc

        self._write_entry_with_status("qc_status", "pending")
        ctrl = LlmAssistQc()
        with patch("odoo.http.request") as mock_req:
            mock_req.env = self.env
            result = ctrl.abort_trajectory_action(
                record_id=self.task.id,
                field_name="claude_trajectory",
                entry_index=0,
                action_type="qc",
            )
        self.assertTrue(result.get("success"))
        self.task.invalidate_recordset()
        data = json.loads(self.task.claude_trajectory)
        entries = data if isinstance(data, list) else [data]
        self.assertEqual(entries[0].get("qc_status"), "aborted")

    def test_abort_taskdesc_success(self):
        from odoo.addons.talos.controllers.llm_assisst_qc import LlmAssistQc

        self._write_entry_with_status("task_description_status", "pending")
        ctrl = LlmAssistQc()
        with patch("odoo.http.request") as mock_req:
            mock_req.env = self.env
            result = ctrl.abort_trajectory_action(
                record_id=self.task.id,
                field_name="claude_trajectory",
                entry_index=0,
                action_type="task_description",
            )
        self.assertTrue(result.get("success"))
        self.task.invalidate_recordset()
        data = json.loads(self.task.claude_trajectory)
        entries = data if isinstance(data, list) else [data]
        self.assertEqual(entries[0].get("task_description_status"), "aborted")

    def test_abort_missing_params(self):
        from odoo.addons.talos.controllers.llm_assisst_qc import LlmAssistQc

        ctrl = LlmAssistQc()
        with patch("odoo.http.request") as mock_req:
            mock_req.env = self.env
            result = ctrl.abort_trajectory_action(record_id=0)
        self.assertIn("error", result)

    def test_abort_invalid_action_type(self):
        from odoo.addons.talos.controllers.llm_assisst_qc import LlmAssistQc

        self._write_entry_with_status("qc_status", "pending")
        ctrl = LlmAssistQc()
        with patch("odoo.http.request") as mock_req:
            mock_req.env = self.env
            result = ctrl.abort_trajectory_action(
                record_id=self.task.id,
                field_name="claude_trajectory",
                entry_index=0,
                action_type="invalid",
            )
        self.assertIn("error", result)
        self.assertIn("must be 'qc' or 'task_description'", result["error"])

    def test_abort_nonexistent_task(self):
        from odoo.addons.talos.controllers.llm_assisst_qc import LlmAssistQc

        ctrl = LlmAssistQc()
        with patch("odoo.http.request") as mock_req:
            mock_req.env = self.env
            result = ctrl.abort_trajectory_action(
                record_id=999999,
                field_name="claude_trajectory",
                entry_index=0,
                action_type="qc",
            )
        self.assertIn("error", result)
        self.assertIn("Task not found", result["error"])

    def test_abort_empty_trajectory(self):
        from odoo.addons.talos.controllers.llm_assisst_qc import LlmAssistQc

        self.task.write({"claude_trajectory": ""})
        ctrl = LlmAssistQc()
        with patch("odoo.http.request") as mock_req:
            mock_req.env = self.env
            result = ctrl.abort_trajectory_action(
                record_id=self.task.id,
                field_name="claude_trajectory",
                entry_index=0,
                action_type="qc",
            )
        self.assertIn("error", result)
        self.assertIn("No trajectory data", result["error"])

    def test_abort_invalid_json(self):
        from odoo.addons.talos.controllers.llm_assisst_qc import LlmAssistQc

        self.task.write({"claude_trajectory": "NOT JSON"})
        ctrl = LlmAssistQc()
        with patch("odoo.http.request") as mock_req:
            mock_req.env = self.env
            result = ctrl.abort_trajectory_action(
                record_id=self.task.id,
                field_name="claude_trajectory",
                entry_index=0,
                action_type="qc",
            )
        self.assertIn("error", result)
        self.assertIn("Could not parse", result["error"])

    def test_abort_out_of_bounds_index(self):
        from odoo.addons.talos.controllers.llm_assisst_qc import LlmAssistQc

        traj = json.dumps([self._make_session_entry()])
        self.task.write({"claude_trajectory": traj})
        ctrl = LlmAssistQc()
        with patch("odoo.http.request") as mock_req:
            mock_req.env = self.env
            result = ctrl.abort_trajectory_action(
                record_id=self.task.id,
                field_name="claude_trajectory",
                entry_index=99,
                action_type="qc",
            )
        self.assertIn("error", result)
        self.assertIn("Invalid entry index", result["error"])

    def test_abort_not_pending(self):
        from odoo.addons.talos.controllers.llm_assisst_qc import LlmAssistQc

        self._write_entry_with_status("qc_status", "done")
        ctrl = LlmAssistQc()
        with patch("odoo.http.request") as mock_req:
            mock_req.env = self.env
            result = ctrl.abort_trajectory_action(
                record_id=self.task.id,
                field_name="claude_trajectory",
                entry_index=0,
                action_type="qc",
            )
        self.assertIn("error", result)
        self.assertIn("not currently running", result["error"])


@tagged("post_install", "-at_install")
class TestLegacyQCEndpoint(TalosTestCase):

    @patch(_QC_MOD + "._call_bedrock_converse")
    @patch(_QC_MOD + "._load_dotenv", return_value={"AWS_BEARER_TOKEN_BEDROCK": "tok"})
    @patch(_QC_MOD + "._get_system_prompt", return_value="sys")
    def test_legacy_qc_success(self, _sys, _dotenv, mock_bedrock):
        from odoo.addons.talos.controllers.llm_assisst_qc import LlmAssistQc

        mock_bedrock.return_value = ("plain response", {"input_tokens": 5, "output_tokens": 3})
        self._set_param("talos.bedrock_inference_arn", "arn:test")
        self._set_param("talos.bedrock_region", "us-east-1")
        ctrl = LlmAssistQc()
        with patch("odoo.http.request") as mock_req:
            mock_req.env = self.env
            mock_req.get_json_data.return_value = {"prompt": "test prompt"}
            mock_req.make_json_response = MagicMock(side_effect=lambda data, **kw: data)
            result = ctrl.llm_assist_qc_legacy()
        self.assertTrue(result.get("success"))

    def test_legacy_qc_missing_prompt(self):
        from odoo.addons.talos.controllers.llm_assisst_qc import LlmAssistQc

        ctrl = LlmAssistQc()
        with patch("odoo.http.request") as mock_req:
            mock_req.get_json_data.return_value = {"prompt": ""}
            mock_req.make_json_response = MagicMock(side_effect=lambda data, **kw: data)
            result = ctrl.llm_assist_qc_legacy()
        self.assertEqual(result.get("status"), 400)

    @patch(_QC_MOD + "._load_dotenv", return_value={})
    def test_legacy_qc_missing_credentials(self, _dotenv):
        from odoo.addons.talos.controllers.llm_assisst_qc import LlmAssistQc

        ctrl = LlmAssistQc()
        with patch("odoo.http.request") as mock_req:
            mock_req.env = self.env
            mock_req.get_json_data.return_value = {"prompt": "test"}
            mock_req.make_json_response = MagicMock(side_effect=lambda data, **kw: data)
            result = ctrl.llm_assist_qc_legacy()
        self.assertEqual(result.get("status"), 500)

    @patch(_QC_MOD + "._load_dotenv", return_value={"AWS_BEARER_TOKEN_BEDROCK": "tok"})
    def test_legacy_qc_missing_inference_arn(self, _dotenv):
        from odoo.addons.talos.controllers.llm_assisst_qc import LlmAssistQc

        self._set_param("talos.bedrock_inference_arn", "")
        ctrl = LlmAssistQc()
        with patch("odoo.http.request") as mock_req:
            mock_req.env = self.env
            mock_req.get_json_data.return_value = {"prompt": "test"}
            mock_req.make_json_response = MagicMock(side_effect=lambda data, **kw: data)
            result = ctrl.llm_assist_qc_legacy()
        self.assertEqual(result.get("status"), 500)

    @patch(_QC_MOD + "._call_bedrock_converse", side_effect=RuntimeError("Bedrock down"))
    @patch(_QC_MOD + "._load_dotenv", return_value={"AWS_BEARER_TOKEN_BEDROCK": "tok"})
    @patch(_QC_MOD + "._get_system_prompt", return_value="sys")
    def test_legacy_qc_bedrock_exception(self, _sys, _dotenv, _bedrock):
        from odoo.addons.talos.controllers.llm_assisst_qc import LlmAssistQc

        self._set_param("talos.bedrock_inference_arn", "arn:test")
        self._set_param("talos.bedrock_region", "us-east-1")
        ctrl = LlmAssistQc()
        with patch("odoo.http.request") as mock_req:
            mock_req.env = self.env
            mock_req.get_json_data.return_value = {"prompt": "test"}
            mock_req.make_json_response = MagicMock(side_effect=lambda data, **kw: data)
            result = ctrl.llm_assist_qc_legacy()
        self.assertEqual(result.get("status"), 500)

    @patch(_QC_MOD + "._call_bedrock_converse")
    @patch(_QC_MOD + "._load_dotenv", return_value={"AWS_BEARER_TOKEN_BEDROCK": "tok"})
    def test_legacy_qc_custom_system_prompt(self, _dotenv, mock_bedrock):
        from odoo.addons.talos.controllers.llm_assisst_qc import LlmAssistQc

        mock_bedrock.return_value = ("response", {"input_tokens": 5, "output_tokens": 3})
        self._set_param("talos.bedrock_inference_arn", "arn:test")
        self._set_param("talos.bedrock_region", "us-east-1")
        ctrl = LlmAssistQc()
        with patch("odoo.http.request") as mock_req:
            mock_req.env = self.env
            mock_req.get_json_data.return_value = {
                "prompt": "test",
                "system_prompt": "custom sys prompt",
            }
            mock_req.make_json_response = MagicMock(side_effect=lambda data, **kw: data)
            ctrl.llm_assist_qc_legacy()
        call_args = mock_bedrock.call_args
        self.assertEqual(call_args.kwargs.get("system_prompt") or call_args[0][3], "custom sys prompt")

    @patch(_QC_MOD + "._call_bedrock_converse")
    @patch(_QC_MOD + "._load_dotenv", return_value={"AWS_BEARER_TOKEN_BEDROCK": "tok"})
    @patch(_QC_MOD + "._get_system_prompt", return_value="sys")
    def test_legacy_qc_parsed_json_in_response(self, _sys, _dotenv, mock_bedrock):
        from odoo.addons.talos.controllers.llm_assisst_qc import LlmAssistQc

        mock_bedrock.return_value = (
            '{"severity": "low", "summary": "all good"}',
            {"input_tokens": 5, "output_tokens": 5},
        )
        self._set_param("talos.bedrock_inference_arn", "arn:test")
        self._set_param("talos.bedrock_region", "us-east-1")
        ctrl = LlmAssistQc()
        with patch("odoo.http.request") as mock_req:
            mock_req.env = self.env
            mock_req.get_json_data.return_value = {"prompt": "test"}
            mock_req.make_json_response = MagicMock(side_effect=lambda data, **kw: data)
            result = ctrl.llm_assist_qc_legacy()
        self.assertTrue(result.get("success"))
        self.assertIn("parsed_json", result)
        self.assertEqual(result["parsed_json"]["severity"], "low")
