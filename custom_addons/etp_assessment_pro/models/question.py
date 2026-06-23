from odoo import models, fields, api


class EtpAssessmentQuestion(models.Model):
    _name = "etp.assessment.pro.question"
    _description = "Assessment Question"
    _order = "sequence, id"

    name = fields.Char(string="Title", required=True)
    sequence = fields.Integer(default=10)
    question_type = fields.Selection(
        [
            ("mcq", "Objective - MCQ"),
            ("msq", "Objective - MSQ"),
            ("subjective_justification", "Subjective - Justification"),
            ("subjective_rubric", "Subjective - Rubric"),
            ("image_ab", "Image - A/B Evaluation"),
            ("image_text", "Image - Prompt/Labelling"),
        ],
        string="Question Type",
        required=True,
        default="mcq",
    )
    prompt = fields.Text(string="Prompt", required=True)
    description = fields.Text()
    active = fields.Boolean(default=True)
    category_id = fields.Many2one(
        "etp.assessment.pro.category", string="Category", ondelete="restrict"
    )
    skill_ids = fields.Many2many(
        "etp.assessment.pro.skill",
        relation="etp_assessment_pro_question_skill_rel",
        column1="question_id",
        column2="skill_id",
        string="Skills",
    )
    question_dimension_ids = fields.One2many(
        "etp.assessment.pro.question.dimension",
        "question_id",
        string="Dimensions",
    )
    grading_json = fields.Text(string="Grading (raw)")
    subjective_rubric_json = fields.Text(string="Subjective Rubric (JSON)")
    meta_json = fields.Text(string="Meta (JSON)")
    image_ids = fields.One2many(
        "etp.assessment.pro.question.image",
        "question_id",
        string="Images",
    )
    official_reasoning = fields.Text(
        string="Official Reasoning",
        help="image_ab: the official rationale the LLM grades the candidate's "
             "justification against.")
    difficulty = fields.Selection(
        [("easy", "Easy"), ("medium", "Medium"), ("hard", "Hard")],
        string="Difficulty",
    )
    time_minutes = fields.Integer(string="Time (minutes)", default=0)
    has_subjective = fields.Boolean(
        compute="_compute_has_subjective", store=True
    )
    source_ref = fields.Char(string="Source Ref")

    @api.depends("subjective_rubric_json", "question_type")
    def _compute_has_subjective(self):
        for rec in self:
            text = (rec.subjective_rubric_json or "").strip()
            rec.has_subjective = bool(text and text not in ("[]", "{}")) or \
                rec.question_type in ("subjective_justification", "subjective_rubric")

    def action_offload_images_s3(self):
        from ..services import s3_service
        moved = 0
        for q in self:
            for img in q.image_ids:
                if img.image_url or not img.image:
                    continue
                url, _key = s3_service.upload_b64(
                    self.env, img.image,
                    key_hint="qimg-%s" % img.id, content_type="image/png")
                img.write({"image_url": url})
                moved += 1
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Images Offloaded",
                "message": "Pushed %s image(s) to S3." % moved,
                "type": "success" if moved else "warning",
                "sticky": False,
            },
        }

    def _export_payload(self):
        import json
        out = []
        for q in self:
            options = []
            correct_idx = []
            for qd in q.question_dimension_ids:
                for i, ol in enumerate(qd.option_line_ids.sorted("sequence")):
                    options.append(ol.name)
                    if ol.is_correct:
                        correct_idx.append(i)
            rubric = None
            if q.subjective_rubric_json:
                try:
                    rubric = json.loads(q.subjective_rubric_json)
                except (ValueError, TypeError):
                    rubric = q.subjective_rubric_json
            out.append({
                "id": q.id,
                "name": q.name,
                "category": q.category_id.name or "",
                "skills": q.skill_ids.mapped("name"),
                "question_type": q.question_type,
                "difficulty": q.difficulty or "",
                "time_minutes": q.time_minutes,
                "prompt": q.prompt or "",
                "description": q.description or "",
                "options": options,
                "correct_answer": correct_idx[0] if len(correct_idx) == 1 else correct_idx,
                "rubric": rubric,
                "source_ref": q.source_ref or "",
            })
        return out

    def _export_download_action(self, filename, mimetype, content_bytes):
        att = self.env["ir.attachment"].create({
            "name": filename,
            "type": "binary",
            "datas": __import__("base64").b64encode(content_bytes).decode(),
            "mimetype": mimetype,
            "res_model": self._name,
        })
        return {
            "type": "ir.actions.act_url",
            "url": f"/web/content/{att.id}?download=true",
            "target": "self",
        }

    def action_export_json(self):
        import json
        recs = self or self.search([])
        payload = json.dumps(recs._export_payload(), indent=2, ensure_ascii=False)
        return recs._export_download_action(
            "question_bank.json", "application/json", payload.encode("utf-8"))

    def action_export_csv(self):
        import csv, io
        recs = self or self.search([])
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["id","name","category","skills","question_type","difficulty",
                    "time_minutes","prompt","description","options",
                    "correct_answer","rubric_pass_condition","source_ref"])
        for row in recs._export_payload():
            rubric = row["rubric"]
            pass_cond = ""
            if isinstance(rubric, list) and rubric:
                pass_cond = (rubric[0] or {}).get("pass_condition", "") if isinstance(rubric[0], dict) else ""
            elif isinstance(rubric, dict):
                pass_cond = rubric.get("pass_condition", "")
            w.writerow([row["id"], row["name"], row["category"],
                        " | ".join(row["skills"]), row["question_type"],
                        row["difficulty"], row["time_minutes"], row["prompt"],
                        row["description"], " | ".join(row["options"]),
                        row["correct_answer"], pass_cond, row["source_ref"]])
        return recs._export_download_action(
            "question_bank.csv", "text/csv", buf.getvalue().encode("utf-8"))
