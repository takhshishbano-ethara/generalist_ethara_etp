from odoo import models, fields, api

from ..constants import (
    QUESTION_TYPE_SELECTION, DIFFICULTY_SELECTION, option_name_reveals_reasoning,
    text_has_source_reference,
)


class EtpAssessmentQuestion(models.Model):
    _name = "etp.assessment.pro.question"
    _description = "Assessment Question"
    _order = "sequence, id"

    name = fields.Char(string="Title", required=True)
    sequence = fields.Integer(default=10)
    question_type = fields.Selection(
        QUESTION_TYPE_SELECTION,
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
        DIFFICULTY_SELECTION,
        string="Difficulty",
    )
    time_minutes = fields.Integer(string="Time (minutes)", default=0)
    has_subjective = fields.Boolean(
        compute="_compute_has_subjective", store=True
    )
    source_ref = fields.Char(string="Source Ref")

    # Reviewer-facing answer-key preview so a BANK question (whether imported or
    # approved from an LLM draft) READS like the draft preview — every option as
    # a chip, the correct one(s) highlighted green with a ✓ — instead of forcing
    # the reviewer to open the Dimensions editor. Mirrors the draft model's
    # dimensions_preview, but sourced from the materialized question.dimension /
    # option lines (is_correct) that actually drive scoring.
    answer_key_preview = fields.Html(
        string="Answer Key", compute="_compute_answer_key_preview",
        sanitize=False)
    has_answer_key = fields.Boolean(compute="_compute_answer_key_preview")
    # Mis-keyed objective questions (mcq/msq with no correct option anywhere)
    # are unscoreable and must NOT silently vanish from the grade. This flag
    # surfaces them in the bank list/form so a reviewer can fix the key.
    has_valid_key = fields.Boolean(
        string="Answer Key OK", compute="_compute_has_valid_key", store=True,
        help="False when an objective (MCQ/MSQ) question has no correct option "
             "marked — it cannot be scored and needs a fix.")

    has_revealing_option = fields.Boolean(
        compute="_compute_has_revealing_option",
        help="True when an objective option name embeds its rationale "
             "(\"Image B, because ...\"). Option names are shown to the "
             "candidate, so the rationale must live in the hidden answer key.")
    has_source_reference = fields.Boolean(
        compute="_compute_has_source_reference",
        help="True when the question or its answer key cites the source "
             "material the candidate never sees (\"according to the SOP\", "
             "\"Section 2.1\"). Items must be self-contained.")

    @api.depends("question_type",
                 "question_dimension_ids.option_line_ids.is_correct")
    def _compute_has_valid_key(self):
        for rec in self:
            if rec.question_type not in ("mcq", "msq"):
                rec.has_valid_key = True
                continue
            has_correct = any(
                qd.option_line_ids.filtered("is_correct")
                for qd in rec.question_dimension_ids)
            rec.has_valid_key = bool(has_correct)

    @api.depends("question_type", "question_dimension_ids.option_line_ids.name")
    def _compute_has_revealing_option(self):
        for rec in self:
            flag = False
            if rec.question_type in ("mcq", "msq"):
                names = rec.question_dimension_ids.option_line_ids.mapped("name")
                flag = any(option_name_reveals_reasoning(n) for n in names)
            rec.has_revealing_option = flag

    @api.depends("prompt", "official_reasoning", "subjective_rubric_json",
                 "description", "question_dimension_ids.option_line_ids.name")
    def _compute_has_source_reference(self):
        for rec in self:
            opt_names = " ".join(
                rec.question_dimension_ids.option_line_ids.mapped("name"))
            rec.has_source_reference = text_has_source_reference(
                rec.prompt, rec.official_reasoning, rec.subjective_rubric_json,
                rec.description, opt_names)

    @api.depends("question_dimension_ids.option_line_ids.is_correct",
                 "question_dimension_ids.option_line_ids.name",
                 "question_dimension_ids.dimension_id.name")
    def _compute_answer_key_preview(self):
        import html as _html
        for rec in self:
            blocks = []
            for qd in rec.question_dimension_ids.sorted("sequence"):
                chips = []
                for ol in qd.option_line_ids.sorted("sequence"):
                    is_c = ol.is_correct
                    cls = ("badge text-bg-success" if is_c
                           else "badge text-bg-light border")
                    mark = " \u2713" if is_c else ""
                    # Bigger, more legible chips than the Bootstrap default.
                    style = ("margin:3px;font-size:0.95rem;padding:0.45em 0.7em;"
                             "font-weight:%s") % ("600" if is_c else "400")
                    chips.append(
                        f'<span class="{cls}" style="{style}">'
                        f'{_html.escape(ol.name or "")}{mark}</span>')
                if not chips:
                    continue
                blocks.append(
                    '<div class="mb-2"><strong style="font-size:0.95rem">'
                    f'{_html.escape(qd.dimension_id.name or "")}</strong><br/>'
                    f'{"".join(chips)}</div>')
            rec.answer_key_preview = "".join(blocks) if blocks else False
            rec.has_answer_key = bool(blocks)

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

    def _image_brief_json(self):
        """Best-effort source of render briefs for a bank question: prefer the
        originating draft's image_brief_json (kept after approval), else build
        a brief per existing image slot from its label."""
        self.ensure_one()
        Draft = self.env["etp.assessment.pro.prompt.question"]
        draft = Draft.search(
            [("approved_question_id", "=", self.id)], limit=1)
        if draft and draft.image_brief_json:
            return draft.image_brief_json
        return False

    def _bank_notify(self, title, message, kind="success"):
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {"title": title, "message": message, "type": kind,
                       "sticky": kind in ("danger", "warning")},
        }

    def action_upload_question_image(self):
        """Open a small wizard-less prompt is overkill; instead this action is
        a no-op placeholder. Upload is done directly on the question.image row
        (Binary field) in the form. Kept for symmetry/help text."""
        return self._bank_notify(
            "Upload Image",
            "Edit an image row below and use its Image field to upload your "
            "own picture, or add a new image line.", "info")

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

    def _export_native(self):
        """Lossless, round-trippable export: every field needed to rebuild the
        EXACT question (answer key with is_correct, official_reasoning, rubric,
        images) so ``import_bank_native`` reproduces it identically. Distinct from
        _export_payload, which is the lossy human/CSV view."""
        out = []
        for q in self:
            dims = [{
                "name": qd.dimension_id.name or "",
                "options": [
                    {"name": ol.name or "", "is_correct": bool(ol.is_correct)}
                    for ol in qd.option_line_ids.sorted("sequence")],
            } for qd in q.question_dimension_ids.sorted("sequence")]
            images = [{
                "slot": im.slot or "single",
                "label": im.label or "",
                "image_url": im.image_url or "",
                "image_b64": im.image.decode() if im.image else "",
            } for im in q.image_ids.sorted("sequence")]
            out.append({
                "name": q.name or "",
                "prompt": q.prompt or "",
                "description": q.description or "",
                "question_type": q.question_type or "mcq",
                "difficulty": q.difficulty or "",
                "time_minutes": q.time_minutes or 0,
                "sequence": q.sequence or 10,
                "source_ref": q.source_ref or "",
                "official_reasoning": q.official_reasoning or "",
                "subjective_rubric_json": q.subjective_rubric_json or "",
                "grading_json": q.grading_json or "",
                "meta_json": q.meta_json or "",
                "category": q.category_id.name or "",
                "skills": q.skill_ids.mapped("name"),
                "dimensions": dims,
                "images": images,
            })
        return out

    def action_export_native_json(self):
        """One-click LOSSLESS export for round-trip import (rebuilds identically)."""
        import json
        recs = self or self.search([])
        payload = json.dumps(
            {"etp_assessment_pro_bank": "1", "questions": recs._export_native()},
            indent=2, ensure_ascii=False)
        return recs._export_download_action(
            "question_bank_native.json", "application/json",
            payload.encode("utf-8"))

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
