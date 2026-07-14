from odoo.tests.common import TransactionCase


class TestTagSimilarity(TransactionCase):
    """Weighted-Jaccard tag ranking (Chunk B). Exercises the real M2M self-join
    SQL, so a wrong table/column name crashes here rather than silently at
    runtime."""

    def setUp(self):
        super().setUp()
        self.Prompt = self.env["etp.assessment.pro.prompt"]
        Tag = self.env["etp.assessment.pro.tag"]
        self.t_task = Tag.create({"name": "task:pairwise-comparison"})
        self.t_domain = Tag.create({"name": "domain:image-evaluation"})
        self.t_skill = Tag.create({"name": "skill:labelling"})
        self.t_mod = Tag.create({"name": "modality:text"})

    def _prompt(self, name, tags):
        return self.Prompt.create({
            "name": name, "tag_ids": [(6, 0, tags.ids)]})

    def test_ranking_and_count(self):
        # weights: task=3, domain=2, skill=2, modality=1
        p = self._prompt("P", self.t_task + self.t_domain + self.t_skill)
        q = self._prompt("Q", self.t_task + self.t_domain + self.t_mod)
        r = self._prompt("R", self.t_skill + self.t_mod)
        s = self._prompt("S", self.t_mod)

        sims = p._similar_prompts(limit=None)
        ranked = [d["prompt"].id for d in sims]

        # Q (shares task+domain, weight 5) outranks R (shares skill, weight 2).
        self.assertEqual(ranked[0], q.id)
        self.assertIn(r.id, ranked)
        # S shares no tag with P -> excluded; self excluded.
        self.assertNotIn(s.id, ranked)
        self.assertNotIn(p.id, ranked)

        by_id = {d["prompt"].id: d for d in sims}
        self.assertEqual(by_id[q.id]["shared_weight"], 5.0)
        self.assertEqual(by_id[q.id]["shared"], self.t_task + self.t_domain)
        # score = shared / union = 5 / (7 + 6 - 5) = 0.625
        self.assertAlmostEqual(by_id[q.id]["score"], 0.625)
        self.assertEqual(by_id[r.id]["shared_weight"], 2.0)

        # similar_count gates on shared_weight >= 2.0 (default): Q(5) and R(2).
        self.assertEqual(p.similar_count, 2)

        ids = p.action_view_similar()["domain"][0][2]
        self.assertEqual(set(ids), {q.id, r.id})

    def test_no_tags(self):
        empty = self.Prompt.create({"name": "E"})
        self.assertEqual(empty._similar_prompts(), [])
        self.assertEqual(empty.similar_count, 0)

    def test_prefix_weight_override(self):
        self.env["ir.config_parameter"].sudo().set_param(
            "etp_assessment_pro.tag_weight_task", "10")
        self.assertEqual(self.Prompt._tag_prefix_weight("task"), 10.0)
        self.assertEqual(self.Prompt._tag_prefix_weight("domain"), 2.0)
