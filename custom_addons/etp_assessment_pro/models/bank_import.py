import json
import logging

from odoo import models, api
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

OBJECTIVE_FIELD_TYPES = ("single_choice", "yes_no", "multi_choice")
SUBJECTIVE_FIELD_TYPES = ("free_text",)

_OBJECTIVE_TYPE_MAP = {
    "single_choice": "mcq",
    "yes_no": "mcq",
    "multi_choice": "msq",
}


class EtpAssessmentBankImport(models.AbstractModel):
    _name = "etp.assessment.pro.bank.import"
    _description = "Question Bank JSON Importer"

    @api.model
    def import_bank_harness(self, payload, generator_id=False,
                            generator_name=None):
        """Import an authoring-harness run folder (the Opus/seed-prompt schema:
        questions.json + solutions.json, optionally combined in output.json) into
        review drafts on a generator.

        The harness is an offline authoring twin that emits, per question:
          {id, instruction, covers_elements, fields:{question_type, difficulty,
           options, correct_answer, rubric, image_specs, official_reasoning},
           assets:{role: {type, file}}}
        and solutions keyed by the same id: {qid: {answers, rationale}}.

        We map that onto prompt.question drafts (NOT live questions) so the admin
        reviews and approves them through the normal pipeline. Image/video assets
        are surfaced as pending briefs by URL when the harness provided a fetchable
        location; local run-folder files cannot be read from inside Odoo, so those
        are reported in warnings for a follow-up upload."""
        payload = self._coerce_json(payload, "harness run")
        questions, solutions = self._harness_questions_solutions(payload)
        if not questions:
            raise UserError(
                "No questions found - expected a harness questions.json array or "
                "an output.json with a 'questions' list.")

        Prompt = self.env["etp.assessment.pro.prompt"].sudo()
        if generator_id:
            generator = Prompt.browse(generator_id)
            if not generator.exists():
                raise UserError("Target generator not found.")
        else:
            name = (generator_name or "Harness Import").strip()
            generator = Prompt.search([("name", "=", name)], limit=1) \
                or Prompt.create({"name": name})

        Draft = self.env["etp.assessment.pro.prompt.question"].sudo()
        valid_types = dict(
            Draft._fields["question_type"].selection)
        created, warnings = 0, []
        for idx, q in enumerate(questions, start=1):
            if not isinstance(q, dict):
                warnings.append("item %d: not an object, skipped" % idx)
                continue
            qid = q.get("id") or ("q%02d" % idx)
            fields_def = q.get("fields") or {}
            qtype = fields_def.get("question_type") or q.get("question_type") \
                or "subjective_rubric"
            if qtype not in valid_types:
                warnings.append(
                    "item %s: unknown question_type %r, imported as "
                    "subjective_rubric" % (qid, qtype))
                qtype = "subjective_rubric"
            prompt_text = (q.get("instruction") or q.get("prompt") or "").strip()
            name = (prompt_text[:60] or qid)
            diff = fields_def.get("difficulty") or q.get("difficulty")
            vals = {
                "prompt_id": generator.id,
                "name": name[:200],
                "question_prompt": prompt_text or name,
                "question_type": qtype,
                "difficulty": diff if diff in ("easy", "medium", "hard")
                else "medium",
                "gen_batch": "harness:%s" % qid,
            }
            ce = q.get("covers_elements")
            if isinstance(ce, list) and ce:
                vals["covers_elements_json"] = json.dumps(ce, ensure_ascii=False)
            if fields_def.get("official_reasoning"):
                vals["official_reasoning"] = fields_def["official_reasoning"]

            if qtype in ("mcq", "msq"):
                options = fields_def.get("options") or []
                if options:
                    vals["options_json"] = json.dumps(
                        options, ensure_ascii=False)
                ca = fields_def.get("correct_answer")
                if ca is not None:
                    vals["correct_answer_json"] = json.dumps(
                        ca, ensure_ascii=False)
            elif qtype == "subjective_rubric":
                rubric = fields_def.get("rubric")
                if rubric:
                    vals["rubric_json"] = json.dumps(rubric, ensure_ascii=False)

            self._apply_harness_solution(vals, solutions.get(qid))
            self._apply_harness_assets(vals, q.get("assets"), qid, qtype,
                                       warnings)
            Draft.create(vals)
            created += 1

        return {
            "generator_id": generator.id,
            "generator_name": generator.name,
            "questions_created": created,
            "warnings": warnings,
        }

    @staticmethod
    def _coerce_json(payload, label):
        if isinstance(payload, str):
            if len(payload) > 50 * 1024 * 1024:
                raise UserError(
                    "Import payload too large (%d bytes; max 50MB)."
                    % len(payload))
            try:
                return json.loads(payload)
            except Exception as exc:
                raise UserError("Invalid JSON (%s): %s" % (label, exc))
        return payload

    @staticmethod
    def _harness_questions_solutions(payload):
        """Accept either a bare questions.json array, or a combined object with
        'questions' (+ optional 'solutions'). Returns (questions, solutions_dict)
        with solutions keyed by question id."""
        questions, solutions = [], {}
        if isinstance(payload, list):
            questions = payload
        elif isinstance(payload, dict):
            questions = payload.get("questions") or []
            sols = payload.get("solutions")
            if isinstance(sols, dict):
                solutions = sols
            elif isinstance(sols, list):
                for i, s in enumerate(sols, start=1):
                    if isinstance(s, dict):
                        solutions[s.get("id") or ("q%02d" % i)] = s
        return ([q for q in questions if isinstance(q, dict)], solutions)

    @staticmethod
    def _apply_harness_solution(vals, sol):
        if not isinstance(sol, dict):
            return
        ans = sol.get("answers")
        if ans is not None:
            vals["solution_json"] = json.dumps(ans, ensure_ascii=False)
        if sol.get("rationale"):
            vals["solution_rationale"] = str(sol["rationale"])

    @staticmethod
    def _apply_harness_assets(vals, assets, qid, qtype, warnings):
        """The harness writes rendered media as local run-folder files
        (assets/qNN_*.png). Odoo cannot read the harness's filesystem, so we only
        auto-attach when an asset carries a fetchable http(s) URL; otherwise we
        record a warning so the admin knows to upload the rendered asset."""
        if not isinstance(assets, dict) or not assets:
            return
        local = []
        for role, a in assets.items():
            if not isinstance(a, dict):
                continue
            src = a.get("url") or a.get("file") or ""
            if isinstance(src, str) and src.startswith(("http://", "https://")):
                imgs = json.loads(vals.get("images_json") or "[]")
                imgs.append({"slot": role, "label": role, "url": src})
                vals["images_json"] = json.dumps(imgs, ensure_ascii=False)
            elif src:
                local.append("%s=%s" % (role, src))
        if local:
            warnings.append(
                "%s: %d local asset file(s) not auto-attached (upload manually): "
                "%s" % (qid, len(local), ", ".join(local)))

    @api.model
    def import_bank_native(self, payload):
        """Lossless round-trip partner of ``question._export_native`` — must stay in sync with it."""
        if isinstance(payload, str):
            # M-7: bound the parse. A multi-hundred-MB JSON string would pin the
            # worker parsing it. 50MB covers any realistic native bank export.
            if len(payload) > 50 * 1024 * 1024:
                raise UserError(
                    "Import payload too large (%d bytes; max 50MB)."
                    % len(payload))
            try:
                payload = json.loads(payload)
            except Exception as exc:
                raise UserError(f"Invalid JSON: {exc}")
        questions = (payload or {}).get("questions") or []
        Question = self.env["etp.assessment.pro.question"]
        Generator = self.env["etp.assessment.pro.prompt"]
        QDim = self.env["etp.assessment.pro.question.dimension"]
        Image = self.env["etp.assessment.pro.question.image"]
        created = 0
        for item in questions:
            if not isinstance(item, dict):
                continue
            gen = False
            gname = (item.get("generator") or "").strip()
            if gname:
                gen = Generator.search([("name", "=", gname)], limit=1) \
                    or Generator.create({"name": gname})
            diff = item.get("difficulty")
            q = Question.create({
                "name": (item.get("name") or "Imported Question")[:200],
                "prompt": item.get("prompt") or "",
                "description": item.get("description") or False,
                "question_type": item.get("question_type") or "mcq",
                "difficulty": diff if diff in ("easy", "medium", "hard") else False,
                "time_minutes": item.get("time_minutes") or 0,
                "sequence": item.get("sequence") or 10,
                "source_ref": item.get("source_ref") or False,
                "official_reasoning": item.get("official_reasoning") or False,
                "subjective_rubric_json": item.get("subjective_rubric_json") or False,
                "grading_json": item.get("grading_json") or False,
                "meta_json": item.get("meta_json") or False,
                "generator_id": gen.id if gen else False,
            })
            for dim in item.get("dimensions") or []:
                opts = dim.get("options") or []
                if not opts:
                    continue
                QDim.create({
                    "question_id": q.id,
                    "name": (dim.get("name") or "Answer")[:200],
                    "option_line_ids": [
                        (0, 0, {"name": o.get("name") or "",
                                "sequence": (i + 1) * 10,
                                "is_correct": bool(o.get("is_correct"))})
                        for i, o in enumerate(opts)],
                })
            for im in item.get("images") or []:
                vals = {
                    "question_id": q.id,
                    "slot": im.get("slot") or "single",
                    "label": im.get("label") or "",
                    "sequence": im.get("sequence") or 10,
                }
                if im.get("image_url"):
                    vals["image_url"] = im["image_url"]
                if im.get("image_b64"):
                    vals["image"] = im["image_b64"]
                Image.create(vals)
            created += 1
        return {"questions_created": created}

    @api.model
    def import_bank(self, bank, generator_id=False, generator_name=None,
                    source_tag="json"):
        if isinstance(bank, str):
            try:
                bank = json.loads(bank)
            except Exception as exc:
                raise UserError(f"Invalid JSON: {exc}")
        if not isinstance(bank, dict):
            raise UserError("Question bank must be a JSON object.")

        project = bank.get("project") or {}
        items = bank.get("question_bank") or []
        if not items:
            raise UserError("question_bank is empty.")

        warnings = []
        Gen = self.env["etp.assessment.pro.prompt"].sudo()
        if generator_id:
            generator = Gen.browse(generator_id)
            if not generator.exists():
                raise UserError("Target generator not found.")
        else:
            name = (generator_name or project.get("name") or "Imported Bank").strip()
            generator = Gen.search([("name", "=", name)], limit=1) or Gen.create(
                {"name": name}
            )

        Question = self.env["etp.assessment.pro.question"].sudo()
        QDim = self.env["etp.assessment.pro.question.dimension"].sudo()

        created = 0
        obj_fields = 0
        subj_fields = 0
        proj_name = project.get("name") or "bank"

        for idx, item in enumerate(items, start=1):
            if not isinstance(item, dict):
                warnings.append(f"item {idx}: not an object, skipped")
                continue
            inputs = item.get("inputs") or {}
            fields_def = item.get("fields") or []
            grading = item.get("grading") or {}
            meta = item.get("meta") or {}

            prompt_text = (inputs.get("instruction") or inputs.get("prompt") or "").strip()
            title = prompt_text[:60] or f"Question {item.get('id', idx)}"

            qtype = self._infer_question_type(fields_def, grading)

            subjective_rubric = []
            for f in fields_def:
                if not isinstance(f, dict):
                    continue
                fkey = f.get("key")
                ftype = f.get("type")
                grade = grading.get(fkey) or {}
                gtype = grade.get("type")
                if ftype in SUBJECTIVE_FIELD_TYPES or gtype == "subjective":
                    subjective_rubric.append({
                        "key": fkey,
                        "label": f.get("label", ""),
                        "checklist": grade.get("checklist", []),
                        "constraints": grade.get("constraints", []),
                        "pass_condition": grade.get("pass_condition", ""),
                    })
                    subj_fields += 1

            diff = meta.get("difficulty")
            question = Question.create({
                "name": title,
                "sequence": (item.get("id") or idx) * 10,
                "question_type": qtype,
                "prompt": prompt_text or title,
                "generator_id": generator.id,
                "grading_json": json.dumps(grading, ensure_ascii=False),
                "subjective_rubric_json": json.dumps(
                    subjective_rubric, ensure_ascii=False
                ),
                "meta_json": json.dumps(meta, ensure_ascii=False),
                "difficulty": diff if diff in ("easy", "medium", "hard") else False,
                "source_ref": f"{source_tag}:{proj_name}#{item.get('id', idx)}",
            })
            created += 1

            for f in fields_def:
                if not isinstance(f, dict):
                    continue
                if f.get("type") not in OBJECTIVE_FIELD_TYPES:
                    continue
                grade = grading.get(f.get("key")) or {}
                if grade.get("type") and grade.get("type") != "objective":
                    continue
                obj_fields += 1
                self._materialize_objective_field(
                    QDim, question, f, grade, warnings
                )

        return {
            "generator_id": generator.id,
            "generator_name": generator.name,
            "questions_created": created,
            "objective_fields": obj_fields,
            "subjective_fields": subj_fields,
            "warnings": warnings,
        }

    @staticmethod
    def _infer_question_type(fields_def, grading):
        types_seen = set()
        for f in fields_def:
            if not isinstance(f, dict):
                continue
            t = f.get("type")
            if t in _OBJECTIVE_TYPE_MAP:
                types_seen.add(_OBJECTIVE_TYPE_MAP[t])
            elif t in SUBJECTIVE_FIELD_TYPES:
                types_seen.add("subjective_rubric")
        if not types_seen:
            return "mcq"
        if "msq" in types_seen:
            return "msq"
        if "subjective_rubric" in types_seen and "mcq" not in types_seen:
            return "subjective_rubric"
        return "mcq"

    def _materialize_objective_field(self, QDim, question,
                                     field_def, grade, warnings):
        label = field_def.get("label") or field_def.get("key") or "Field"
        options = list(field_def.get("options") or [])
        ftype = field_def.get("type")
        if not options and ftype == "yes_no":
            options = ["Yes", "No"]
        if not options:
            warnings.append(
                f"BLOCKING {question.name}: objective field '{label}' has no "
                f"options - not scorable, dimension skipped"
            )
            return

        qd = QDim.create({
            "question_id": question.id,
            "name": label,
            "option_line_ids": [
                (0, 0, {"name": o, "sequence": (i + 1) * 10})
                for i, o in enumerate(options)
            ],
        })

        correct_answer = (grade or {}).get("answer")
        if correct_answer is None or correct_answer == "":
            return

        match = self._match_option(qd.option_line_ids, correct_answer)
        if match:
            match.write({"is_correct": True})
        else:
            warnings.append(
                f"BLOCKING {question.name}: correct answer '{correct_answer}' "
                f"not among options for '{label}' "
                f"({[line.name for line in qd.option_line_ids]})"
            )

    @staticmethod
    def _match_option(option_lines, answer):
        def norm(s):
            return str(s).strip().casefold()
        target = norm(answer)
        aliases = {
            "true": ("yes", "true"), "false": ("no", "false"),
            "yes": ("yes", "true"), "no": ("no", "false"),
        }
        targets = {target} | set(aliases.get(target, ()))
        for line in option_lines:
            name = norm(line.name)
            if name in targets:
                return line
            if name in aliases and target in (aliases.get(name) or ()):
                return line
        return option_lines.browse()
