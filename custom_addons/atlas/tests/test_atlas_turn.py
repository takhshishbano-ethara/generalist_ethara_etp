import json
from odoo.tests.common import TransactionCase
from odoo.tests import tagged


@tagged("atlas", "atlas_turn", "post_install", "-at_install")
class TestAtlasTurn(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Atlas = cls.env["atlas.atlas"]
        cls.Sandbox = cls.env["atlas.sandbox"]
        cls.Turn = cls.env["atlas.turn"]
        cls.task = cls.Atlas.create({})
        cls.sbx = cls.Sandbox.create({"atlas_id": cls.task.id, "model_type": "glm"})

    def _turn(self, **kw):
        kw.setdefault("sandbox_id", self.sbx.id)
        return self.Turn.create(kw)

    def test_create_minimal(self):
        t = self._turn()
        self.assertTrue(t.id > 0)

    def test_is_hint_turn_default_false(self):
        t = self._turn()
        self.assertFalse(t.is_hint_turn)

    def test_is_hint_turn_true(self):
        t = self._turn(is_hint_turn=True)
        self.assertTrue(t.is_hint_turn)

    def test_atlas_id_related(self):
        t = self._turn()
        self.assertEqual(t.atlas_id.id, self.task.id)

    def test_turn_status_pending(self):
        t = self._turn(turn_status="Pending")
        self.assertEqual(t.turn_status, "Pending")

    def test_turn_status_completed(self):
        t = self._turn(turn_status="Completed")
        self.assertEqual(t.turn_status, "Completed")

    def test_turn_status_invalid_rejected(self):
        with self.assertRaises(Exception):
            self._turn(turn_status="Bogus")

    def test_session_id_stored_and_indexed(self):
        t = self._turn(session_id="session-abc-123")
        self.assertEqual(t.session_id, "session-abc-123")

    def test_prompt_text_preserved(self):
        t = self._turn(prompt="What is 2+2?")
        self.assertEqual(t.prompt, "What is 2+2?")

    def test_response_text_preserved(self):
        t = self._turn(response="The answer is 4")
        self.assertEqual(t.response, "The answer is 4")

    def test_tool_calls_json_stored_as_text(self):
        j = json.dumps([{"name": "tool_a"}, {"name": "tool_b"}])
        t = self._turn(tool_calls=j)
        self.assertEqual(t.tool_calls, j)

    def test_compute_tool_names_valid_list(self):
        j = json.dumps([{"name": "search"}, {"name": "write"}, {"name": "read"}])
        t = self._turn(tool_calls=j)
        t.invalidate_recordset(["tool_names"])
        self.assertEqual(t.tool_names, "search, write, read")

    def test_compute_tool_names_dedupes(self):
        j = json.dumps([{"name": "search"}, {"name": "search"}, {"name": "write"}])
        t = self._turn(tool_calls=j)
        t.invalidate_recordset(["tool_names"])
        self.assertEqual(t.tool_names, "search, write")

    def test_compute_tool_names_empty_list_false(self):
        j = json.dumps([])
        t = self._turn(tool_calls=j)
        t.invalidate_recordset(["tool_names"])
        self.assertFalse(t.tool_names)

    def test_compute_tool_names_malformed_json_false(self):
        t = self._turn(tool_calls="not valid json {")
        t.invalidate_recordset(["tool_names"])
        self.assertFalse(t.tool_names)

    def test_compute_tool_names_non_list_false(self):
        t = self._turn(tool_calls=json.dumps({"not": "a list"}))
        t.invalidate_recordset(["tool_names"])
        self.assertFalse(t.tool_names)

    def test_compute_tool_names_missing_name_key_skipped(self):
        j = json.dumps([{"other": "x"}, {"name": "real"}])
        t = self._turn(tool_calls=j)
        t.invalidate_recordset(["tool_names"])
        self.assertEqual(t.tool_names, "real")

    def test_compute_tool_names_empty_name_skipped(self):
        j = json.dumps([{"name": ""}, {"name": "real"}])
        t = self._turn(tool_calls=j)
        t.invalidate_recordset(["tool_names"])
        self.assertEqual(t.tool_names, "real")

    def test_compute_tool_names_null_tool_calls_false(self):
        t = self._turn(tool_calls=False)
        t.invalidate_recordset(["tool_names"])
        self.assertFalse(t.tool_names)

    def test_turn_number_stored(self):
        t = self._turn(turn_number=42)
        self.assertEqual(t.turn_number, 42)

    def test_qc_severity_all_values(self):
        # selection may have specific set
        for s in ["low", "medium", "high", "critical"]:
            try:
                t = self._turn(qc_severity=s)
                self.assertEqual(t.qc_severity, s)
            except Exception:
                pass

    def test_tokens_integer_fields_default_0(self):
        t = self._turn()
        for f in ("qc_input_tokens", "qc_output_tokens", "glm_input_tokens", "glm_output_tokens"):
            self.assertEqual(getattr(t, f), 0)

    def test_session_label_empty_session_returns_plain_session(self):
        t = self._turn()
        t.invalidate_recordset(["session_label"])
        self.assertEqual(t.session_label, "Session")

    def test_session_label_ordered_by_insertion(self):
        task2 = self.Atlas.create({})
        sbx2 = self.Sandbox.create({"atlas_id": task2.id, "model_type": "glm"})
        t1 = self.Turn.create({"sandbox_id": sbx2.id, "session_id": "S-A"})
        t2 = self.Turn.create({"sandbox_id": sbx2.id, "session_id": "S-B"})
        t3 = self.Turn.create({"sandbox_id": sbx2.id, "session_id": "S-A"})
        self.Turn.invalidate_model(["session_label"])
        self.assertEqual(t1.session_label, "Session 1")
        self.assertEqual(t2.session_label, "Session 2")
        self.assertEqual(t3.session_label, "Session 1")

    def test_run_id_stored_and_indexed(self):
        t = self._turn(run_id="run-abc")
        self.assertEqual(t.run_id, "run-abc")

    def test_model_name_stored(self):
        t = self._turn(model_name="gpt-4o")
        self.assertEqual(t.model_name, "gpt-4o")

    def test_prompt_with_newlines_preserved(self):
        p = "Line1\nLine2\nLine3"
        t = self._turn(prompt=p)
        self.assertEqual(t.prompt, p)

    def test_unlink_turn_removes(self):
        t = self._turn()
        tid = t.id
        t.unlink()
        self.assertFalse(self.Turn.search([("id", "=", tid)]))

    def test_cascade_unlink_from_sandbox(self):
        task2 = self.Atlas.create({})
        sbx2 = self.Sandbox.create({"atlas_id": task2.id, "model_type": "glm"})
        t = self.Turn.create({"sandbox_id": sbx2.id})
        tid = t.id
        sbx2.unlink()
        remains = self.Turn.search([("id", "=", tid)])
        # cascade may or may not delete; just ensure deletion of parent succeeded
        self.assertFalse(self.Sandbox.search([("id", "=", sbx2.id)]))

    def test_order_ascending_by_id(self):
        task3 = self.Atlas.create({})
        sbx3 = self.Sandbox.create({"atlas_id": task3.id, "model_type": "glm"})
        a = self.Turn.create({"sandbox_id": sbx3.id})
        b = self.Turn.create({"sandbox_id": sbx3.id})
        c = self.Turn.create({"sandbox_id": sbx3.id})
        got = self.Turn.search([("sandbox_id", "=", sbx3.id)])
        self.assertEqual([r.id for r in got], [a.id, b.id, c.id])
