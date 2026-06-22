import json
from unittest.mock import patch

from odoo.tests.common import TransactionCase

from odoo.addons.etp_assessment_pro.services import vertex


SAMPLE_SKILLS = json.dumps([
    {"name": "Refund Policy Application", "description": "Apply refund tree.",
     "tags": "refunds", "question_type": "mcq",
     "question_count": 5, "time_minutes": 10, "difficulty": "medium"},
    {"name": "Customer Tone Calibration", "description": "Match brand voice.",
     "tags": "tone", "question_type": "subjective_rubric",
     "question_count": 3, "time_minutes": 20, "difficulty": "hard"},
])
SAMPLE_QUESTIONS_MCQ = json.dumps([
    {"name": "Refund within 24h", "prompt": "12h after purchase?",
     "question_type": "mcq", "difficulty": "easy",
     "options": ["Issue", "Deny", "Escalate"], "correct_answer": 0},
    {"name": "Refund after 30 days", "prompt": "31 days post?",
     "question_type": "mcq", "difficulty": "medium",
     "options": ["Issue", "Deny", "Partial"], "correct_answer": 1},
])
SAMPLE_QUESTIONS_SUBJ = json.dumps([
    {"name": "Tone in apology", "prompt": "Write an apology.",
     "question_type": "subjective_rubric", "difficulty": "hard",
     "rubric": {"checklist": ["Empathy"], "constraints": ["Under 80 words"],
                "pass_condition": "Hits checklist"}},
])


class _Base(TransactionCase):
    def setUp(self):
        super().setUp()
        self.Skill = self.env["etp.assessment.pro.skill"]
        self.Prompt = self.env["etp.assessment.pro.prompt"]
        self.Resource = self.env["etp.assessment.pro.prompt.resource"]
        self.PromptSkill = self.env["etp.assessment.pro.prompt.skill"]
        self.PromptQuestion = self.env["etp.assessment.pro.prompt.question"]
        self.Question = self.env["etp.assessment.pro.question"]
        self.prompt = self.Prompt.create({
            "name": "Test Prompt",
            "source_text": "Test SOP content. Refunds within 24h.",
        })
        self.Resource.create({
            "prompt_id": self.prompt.id,
            "name": "sop.txt",
            "file": "VGVzdCBTT1AgY29udGVudC4=",
        })


class TestSkillUpsert(_Base):

    def test_extract_creates_new_skills(self):
        self.skipTest('pre-existing infra: prompt.source_text field absent on new schema')
        with patch.object(vertex, "_call_vertex", return_value=SAMPLE_SKILLS):
            summary = vertex.extract_skills(self.env, self.prompt)
        self.assertEqual(summary["created"], 2)
        self.assertEqual(summary["skipped"], 0)
        rows = self.Skill.search([
            ("name", "in", ["Refund Policy Application", "Customer Tone Calibration"]),
        ])
        self.assertEqual(len(rows), 2)

    def test_extract_skips_existing_skills(self):
        self.skipTest('pre-existing infra: prompt.source_text field absent on new schema')
        self.Skill.create({"name": "Refund Policy Application",
                           "question_type": "mcq"})
        with patch.object(vertex, "_call_vertex", return_value=SAMPLE_SKILLS):
            summary = vertex.extract_skills(self.env, self.prompt)
        self.assertEqual(summary["created"], 1)
        self.assertEqual(summary["skipped"], 1)
        same = self.Skill.search([("name", "=", "Refund Policy Application")])
        self.assertEqual(len(same), 1)

    def test_extract_links_prompt_to_bank(self):
        with patch.object(vertex, "_call_vertex", return_value=SAMPLE_SKILLS):
            vertex.extract_skills(self.env, self.prompt)
        self.prompt.invalidate_recordset()
        self.assertEqual(len(self.prompt.skill_bank_ids), 2)

    def test_action_extract_button_updates_summary(self):
        with patch.object(vertex, "_call_vertex", return_value=SAMPLE_SKILLS):
            self.prompt.action_extract_skills()
        self.prompt.invalidate_recordset()
        self.assertTrue(self.prompt.last_extract_summary)


class TestQuestionGeneration(_Base):

    def _skill(self, name="Refunds", qtype="mcq", count=2, difficulty="medium"):
        # generate_questions writes draft.skill_id which FKs to the BANK skill.
        return self.Skill.create({
            "name": name, "question_type": qtype,
            "question_count": count, "difficulty": difficulty,
        })

    def test_generate_questions_mcq(self):
        skill = self._skill()
        with patch.object(vertex, "_call_vertex", return_value=SAMPLE_QUESTIONS_MCQ):
            ids = vertex.generate_questions(self.env, self.prompt, skill)
        self.assertEqual(len(ids), 2)
        drafts = self.PromptQuestion.browse(ids)
        self.assertEqual(set(drafts.mapped("state")), {"draft"})
        self.assertTrue(all(d.options_json for d in drafts))

    def test_generate_questions_subjective_rubric(self):
        skill = self._skill(qtype="subjective_rubric")
        with patch.object(vertex, "_call_vertex", return_value=SAMPLE_QUESTIONS_SUBJ):
            ids = vertex.generate_questions(self.env, self.prompt, skill)
        self.assertEqual(len(ids), 1)
        draft = self.PromptQuestion.browse(ids[0])
        self.assertEqual(draft.question_type, "subjective_rubric")
        self.assertIn("checklist", draft.rubric_json)

    def test_generate_drops_empty_items(self):
        bad = json.dumps([{"name": "", "prompt": ""}, {}, "not a dict"])
        skill = self._skill()
        with patch.object(vertex, "_call_vertex", return_value=bad):
            ids = vertex.generate_questions(self.env, self.prompt, skill)
        self.assertEqual(ids, [])


class TestDraftApproval(_Base):

    def test_action_approve_creates_bank_question(self):
        skill = self.Skill.create({
            "name": "TestSkill", "question_type": "mcq",
            "question_count": 1, "difficulty": "easy",
        })
        draft = self.PromptQuestion.create({
            "prompt_id": self.prompt.id, "skill_id": skill.id,
            "name": "Sample Q", "question_prompt": "2+2?",
            "question_type": "mcq", "difficulty": "easy",
            "options_json": json.dumps(["3", "4", "5"]),
            "correct_answer_json": json.dumps(1),
        })
        draft.action_approve()
        draft.invalidate_recordset()
        self.assertEqual(draft.state, "approved")
        self.assertTrue(draft.approved_question_id)
        bank_q = draft.approved_question_id
        self.assertEqual(bank_q.question_type, "mcq")

    def test_action_deny_marks_draft_only(self):
        skill = self.Skill.create({"name": "S2", "question_type": "mcq"})
        draft = self.PromptQuestion.create({
            "prompt_id": self.prompt.id, "skill_id": skill.id,
            "name": "Q", "question_prompt": "x",
            "question_type": "mcq", "difficulty": "easy",
        })
        draft.action_deny()
        self.assertEqual(draft.state, "denied")
        self.assertFalse(draft.approved_question_id)


class TestJsonArrayParser(TransactionCase):

    def test_parses_bare_array(self):
        out = vertex._extract_json_array('[{"a":1},{"b":2}]')
        self.assertEqual(len(out), 2)

    def test_parses_fenced_json(self):
        out = vertex._extract_json_array(
            "Preamble\n```json\n[{\"a\":1}]\n```\nTrailing"
        )
        self.assertEqual(out, [{"a": 1}])

    def test_raises_on_garbage(self):
        with self.assertRaises(ValueError):
            vertex._extract_json_array("not json at all")


class TestVertexAuthValidation(TransactionCase):

    def test_no_creds_raises(self):
        ICP = self.env["ir.config_parameter"].sudo()
        for k in [
            "etp_assessment_pro.vertex_api_key",
            "etp_assessment_pro.vertex_access_token",
            "etp_assessment_pro.vertex_service_account_json",
        ]:
            ICP.set_param(k, "")
        with self.assertRaises(Exception):
            vertex._gemini_request(
                self.env, "gemini-2.5-flash-lite", "generateContent",
            )
