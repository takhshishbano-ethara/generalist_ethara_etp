from odoo import models, fields, api

from odoo.exceptions import UserError

from ..constants import (
    QUESTION_TYPE_SELECTION, DIFFICULTY_SELECTION, IMAGE_QUESTION_TYPES,
    VIDEO_QUESTION_TYPES, DETECTION_MODE_SELECTION,
    option_name_reveals_reasoning, text_has_source_reference,
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
    generator_id = fields.Many2one(
        "etp.assessment.pro.prompt", string="Generator",
        ondelete="set null", index=True,
    )
    question_dimension_ids = fields.One2many(
        "etp.assessment.pro.question.dimension",
        "question_id",
        string="Dimensions",
    )
    grading_json = fields.Text(string="Grading (raw)")
    subjective_rubric_json = fields.Text(string="Subjective Rubric (JSON)")
    meta_json = fields.Text(string="Meta (JSON)")
    covers_elements_json = fields.Text(
        string="Covers Elements (JSON)",
        help="Required-element ids this question exercises, carried from the "
             "draft on approve. Threaded to scoring so the judge grades SOP "
             "coverage against the project's real required_elements.")
    solution_json = fields.Text(
        string="Solution / Golden Answer (JSON)", copy=False,
        help="The golden answer (research solutions.answers), carried from the "
             "draft on approve. Fed to the subjective judge as the answer key "
             "it decomposes into golden claims before reading the worker.")
    solution_rationale = fields.Text(
        string="Solution Rationale", copy=False,
        help="How the golden answer is known (construction ground truth, the "
             "SOP's own rule, or derivation).")
    verification_json = fields.Text(
        string="Verification Record (JSON)", copy=False,
        help="image_ab construction verification carried from the draft on "
             "approve: which planted flaws were confirmed visible in the "
             "rendered assets. Fed to the judge in scoring Step 0 as the "
             "confirmed-flaw ground-truth record.")
    image_ids = fields.One2many(
        "etp.assessment.pro.question.image",
        "question_id",
        string="Images",
    )
    video_ids = fields.One2many(
        "etp.assessment.pro.question.video",
        "question_id",
        string="Videos",
    )
    upload_video = fields.Binary(
        string="Upload Clip", attachment=True,
        help="video_prompt: attach an mp4/webm, pick the slot, then use "
             "'Apply Uploaded Video' to store it as a question.video row.")
    upload_video_filename = fields.Char(string="Upload Clip Filename")
    upload_video_slot = fields.Selection(
        [("reference", "Reference"), ("output", "Output"), ("single", "Single")],
        string="Upload Slot", default="reference",
        help="Which clip the uploaded file becomes: Reference + Output for a "
             "video_prompt pair, Single for a lone clip.")
    official_reasoning = fields.Text(
        string="Official Reasoning",
        help="image_ab: the official rationale the LLM grades the candidate's "
             "justification against.")
    flaw_plan_json = fields.Text(
        string="Flaw Plan (JSON)",
        help="image_ab flaw-injection plan copied from the draft on approve: the "
             "worker/render prompts, planted flaws, and the construction_keys the "
             "answer key was derived from. NULL for pre-Phase-3 questions (guards "
             "no-op). The approve- and score-time key-drift guards hard-fail if "
             "the stored answer key ever diverges from these keys.")
    difficulty = fields.Selection(
        DIFFICULTY_SELECTION,
        string="Difficulty",
    )
    detection_mode = fields.Selection(
        DETECTION_MODE_SELECTION,
        string="Detection Mode",
        default="object",
        help="image_label: whether the source picture is a photo (detect "
             "objects) or a UI screenshot (detect clickable UI elements). "
             "Selects which detection prompt the annotation cron uses.")
    time_minutes = fields.Integer(string="Time (minutes)", default=0)
    has_subjective = fields.Boolean(
        compute="_compute_has_subjective", store=True
    )
    source_ref = fields.Char(string="Source Ref")

    answer_key_preview = fields.Html(
        string="Answer Key", compute="_compute_answer_key_preview",
        sanitize=False)
    has_answer_key = fields.Boolean(compute="_compute_answer_key_preview")
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
                 "question_dimension_ids.name")
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
                    style = ("margin:3px;font-size:0.95rem;padding:0.45em 0.7em;"
                             "font-weight:%s") % ("600" if is_c else "400")
                    chips.append(
                        f'<span class="{cls}" style="{style}">'
                        f'{_html.escape(ol.name or "")}{mark}</span>')
                if not chips:
                    continue
                blocks.append(
                    '<div class="mb-2"><strong style="font-size:0.95rem">'
                    f'{_html.escape(qd.name or "")}</strong><br/>'
                    f'{"".join(chips)}</div>')
            rec.answer_key_preview = "".join(blocks) if blocks else False
            rec.has_answer_key = bool(blocks)

    @api.depends("subjective_rubric_json", "question_type")
    def _compute_has_subjective(self):
        for rec in self:
            text = (rec.subjective_rubric_json or "").strip()
            rec.has_subjective = bool(text and text not in ("[]", "{}")) or \
                rec.question_type == "subjective_rubric"

    def _has_required_images(self):
        """False when this is an image question missing its picture(s): an
        image_ab needs both the a and b slots, image_prompt/image_label need at
        least one.
        The exam-selection/serving guards drop such questions so a candidate is
        never shown an image question with no image."""
        self.ensure_one()
        if self.question_type not in IMAGE_QUESTION_TYPES:
            return True
        slots = set(self.image_ids.mapped("slot"))
        if self.question_type == "image_ab":
            return {"a", "b"}.issubset(slots)
        return bool(self.image_ids)

    def _has_required_videos(self):
        """False when a video question has no clip. video_prompt is the video
        twin of image_prompt: it needs at least one video, mirroring
        _has_required_images. The exam-selection guard drops such questions so a
        candidate is never shown a video question with no clip."""
        self.ensure_one()
        if self.question_type not in VIDEO_QUESTION_TYPES:
            return True
        return bool(self.video_ids)

    def action_apply_uploaded_video(self):
        """Store the uploaded clip as a question.video row for upload_video_slot,
        replacing any existing clip in that slot. Mirrors the draft image
        uploader (action_apply_uploaded_image): the binary is ingested to S3 when
        configured (video_url), else kept on the record as a dev-only fallback."""
        self.ensure_one()
        if not self.upload_video:
            return self._bank_notify(
                "No File", "Attach a clip in 'Upload Clip' first.", "warning")
        from ..services import image_ingest
        slot = (self.upload_video_slot or "reference").strip().lower()
        raw_b64 = self.upload_video
        if isinstance(raw_b64, bytes):
            raw_b64 = raw_b64.decode("ascii", errors="ignore")
        filename = self.upload_video_filename or ""
        content_type = "video/webm" if filename.lower().endswith(".webm") \
            else "video/mp4"
        url, stored_b64 = image_ingest.ingest(
            self.env, None, raw_b64, content_type=content_type,
            key_hint="qvideo-%s-%s" % (self.id, slot))
        existing = self.video_ids.filtered(lambda v: v.slot == slot)
        existing.unlink()
        Video = self.env["etp.assessment.pro.question.video"]
        Video.create({
            "question_id": self.id,
            "slot": slot,
            "label": slot.title(),
            "video_url": url or False,
            "video": stored_b64 or False,
            "video_filename": filename or False,
            "sequence": {"reference": 10, "output": 20}.get(slot, 30),
        })
        self.write({"upload_video": False, "upload_video_filename": False})
        return self._bank_notify(
            "Video Uploaded", "Your clip now fills the %r slot." % slot)

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

    def action_detect_now(self):
        """Run image_label detection immediately on the Single source image
        (respecting detection_mode) instead of waiting for the 1-min cron.
        Delegates to the image row, which reuses the cron's attempt cap and
        surfaces Vertex failures as a friendly UserError."""
        self.ensure_one()
        if self.question_type != "image_label":
            raise UserError(
                "Detect Now is only available for Image - Labelling questions.")
        images = self.image_ids.filtered(
            lambda im: im.slot == "single" and (im.image or im.image_url))
        if not images:
            raise UserError(
                "Add a source image to the Single slot before detecting.")
        for img in images:
            img.action_detect_now()
        return self._bank_notify(
            "Detection Complete",
            "Detected elements and drew the numbered overlay.")

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
                "generator": q.generator_id.name or "",
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
                "name": qd.name or "",
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
                "generator": q.generator_id.name or "",
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
        w.writerow(["id","name","generator","question_type","difficulty",
                    "time_minutes","prompt","description","options",
                    "correct_answer","rubric_pass_condition","source_ref"])
        for row in recs._export_payload():
            rubric = row["rubric"]
            pass_cond = ""
            if isinstance(rubric, list) and rubric:
                pass_cond = (rubric[0] or {}).get("pass_condition", "") if isinstance(rubric[0], dict) else ""
            elif isinstance(rubric, dict):
                pass_cond = rubric.get("pass_condition", "")
            w.writerow([row["id"], row["name"], row["generator"],
                        row["question_type"],
                        row["difficulty"], row["time_minutes"], row["prompt"],
                        row["description"], " | ".join(row["options"]),
                        row["correct_answer"], pass_cond, row["source_ref"]])
        return recs._export_download_action(
            "question_bank.csv", "text/csv", buf.getvalue().encode("utf-8"))
