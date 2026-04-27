from odoo.tests.common import TransactionCase
from odoo.tests import tagged


@tagged("atlas", "atlas_aam", "post_install", "-at_install")
class TestAtlasAtlasModel(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Atlas = cls.env["atlas.atlas"]

    def test_bare_create_succeeds(self):
        a = self.Atlas.create({})
        self.assertTrue(a.id > 0)

    def test_default_task_status_none(self):
        a = self.Atlas.create({})
        self.assertFalse(a.task_status)

    def test_default_goal_generation_status_idle(self):
        a = self.Atlas.create({})
        self.assertEqual(a.goal_generation_status, "idle")

    def test_default_rubric_generation_status_idle(self):
        a = self.Atlas.create({})
        self.assertEqual(a.rubric_generation_status, "idle")

    def test_default_tokens_zero(self):
        a = self.Atlas.create({})
        for f in ("glm_input_tokens", "glm_output_tokens", "qc_input_tokens", "qc_output_tokens",
                  "goal_input_tokens", "goal_output_tokens", "rubric_input_tokens",
                  "rubric_output_tokens", "rubric_qc_input_tokens", "rubric_qc_output_tokens"):
            self.assertEqual(getattr(a, f), 0, "field %s not 0" % f)

    def test_task_status_submitted(self):
        a = self.Atlas.create({"task_status": "Submitted"})
        self.assertEqual(a.task_status, "Submitted")

    def test_task_status_notsubmitted(self):
        a = self.Atlas.create({"task_status": "NotSubmitted"})
        self.assertEqual(a.task_status, "NotSubmitted")

    def test_task_status_invalid_rejected(self):
        with self.assertRaises(Exception):
            self.Atlas.create({"task_status": "Bogus"})

    def test_goal_gen_status_all_values(self):
        for s in ["idle", "running", "done", "error"]:
            a = self.Atlas.create({"goal_generation_status": s})
            self.assertEqual(a.goal_generation_status, s)

    def test_rubric_gen_status_all_values(self):
        for s in ["idle", "running", "done", "error"]:
            a = self.Atlas.create({"rubric_generation_status": s})
            self.assertEqual(a.rubric_generation_status, s)

    def test_email_stored(self):
        a = self.Atlas.create({"email": "u@example.com"})
        self.assertEqual(a.email, "u@example.com")

    def test_password_stored(self):
        a = self.Atlas.create({"password": "s3cr3t"})
        self.assertEqual(a.password, "s3cr3t")

    def test_goal_description_multiline(self):
        g = "line1\nline2\nline3"
        a = self.Atlas.create({"goal_description": g})
        self.assertEqual(a.goal_description, g)

    def test_has_turns_false_when_no_turns(self):
        a = self.Atlas.create({})
        self.assertFalse(a.has_turns)

    def test_has_turns_computed_from_turn_ids(self):
        a = self.Atlas.create({})
        sbx = self.env["atlas.sandbox"].create(
            {"atlas_id": a.id, "model_type": "glm"})
        self.env["atlas.turn"].create({"sandbox_id": sbx.id})
        a.invalidate_recordset(["has_turns"])
        self.assertTrue(a.has_turns)

    def test_write_task_status_transition(self):
        a = self.Atlas.create({"task_status": "NotSubmitted"})
        a.task_status = "Submitted"
        self.assertEqual(a.task_status, "Submitted")

    def test_write_all_tokens(self):
        a = self.Atlas.create({})
        a.write({"glm_input_tokens": 100, "glm_output_tokens": 200})
        self.assertEqual(a.glm_input_tokens, 100)
        self.assertEqual(a.glm_output_tokens, 200)

    def test_unlink_removes_record(self):
        a = self.Atlas.create({})
        aid = a.id
        a.unlink()
        self.assertFalse(self.Atlas.search([("id", "=", aid)]))

    def test_create_multi_batch(self):
        r = self.Atlas.create([{}, {}, {}])
        self.assertEqual(len(r), 3)

    def test_employee_id_defaults_to_current_user(self):
        a = self.Atlas.create({})
        if self.env.user.employee_id:
            self.assertEqual(a.employee_id.id, self.env.user.employee_id.id)

    def test_user_id_related_follows_employee(self):
        a = self.Atlas.create({})
        if a.employee_id:
            self.assertEqual(a.user_id.id, a.employee_id.user_id.id)

    def test_gog_auth_json_text_stored(self):
        v = '{"token": "abc", "refresh": "xyz"}'
        a = self.Atlas.create({"gog_auth": v})
        self.assertEqual(a.gog_auth, v)

    def test_gog_auth_token_stored(self):
        a = self.Atlas.create({"gog_auth_token": "tok"})
        self.assertEqual(a.gog_auth_token, "tok")

    def test_goal_description_long_5000_chars(self):
        g = "g" * 5000
        a = self.Atlas.create({"goal_description": g})
        self.assertEqual(len(a.goal_description), 5000)

    def test_is_atlas_admin_computed(self):
        a = self.Atlas.create({})
        self.assertIn(a.is_atlas_admin, (True, False))

    def test_rubric_criterion_ids_initially_empty(self):
        a = self.Atlas.create({})
        self.assertEqual(len(a.rubric_criterion_ids), 0)

    def test_adding_criterion_populates_o2m(self):
        a = self.Atlas.create({})
        self.env["atlas.rubric.criterion"].create(
            {"atlas_id": a.id, "name": "one"})
        self.assertEqual(len(a.rubric_criterion_ids), 1)

    def test_sandbox_ids_initially_empty(self):
        a = self.Atlas.create({})
        self.assertEqual(len(a.sandbox_ids), 0)

    def test_write_email_and_password_together(self):
        a = self.Atlas.create({})
        a.write({"email": "a@b.c", "password": "pw"})
        self.assertEqual(a.email, "a@b.c")
        self.assertEqual(a.password, "pw")

    def test_token_field_negative_accepted(self):
        a = self.Atlas.create({"glm_input_tokens": -5})
        self.assertEqual(a.glm_input_tokens, -5)
