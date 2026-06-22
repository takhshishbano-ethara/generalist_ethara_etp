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
    def import_bank(self, bank, category_id=False, category_name=None,
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
        Cat = self.env["etp.assessment.pro.category"].sudo()
        if category_id:
            category = Cat.browse(category_id)
            if not category.exists():
                raise UserError("Target category not found.")
        else:
            name = (category_name or project.get("name") or "Imported Bank").strip()
            category = Cat.search([("name", "=", name)], limit=1) or Cat.create(
                {"name": name}
            )

        skillset = bank.get("skillset") or []
        skill_records, skills_created_count = self._upsert_skills(skillset)

        Question = self.env["etp.assessment.pro.question"].sudo()
        Dimension = self.env["etp.assessment.pro.dimension"].sudo()
        QDim = self.env["etp.assessment.pro.question.dimension"].sudo()
        QOpt = self.env["etp.assessment.pro.question.dimension.option"].sudo()

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

            question_skill_ids = []
            skill_name = item.get("skill") or meta.get("skill")
            if skill_name and skill_name in skill_records:
                question_skill_ids.append(skill_records[skill_name].id)

            diff = meta.get("difficulty")
            question = Question.create({
                "name": title,
                "sequence": (item.get("id") or idx) * 10,
                "question_type": qtype,
                "prompt": prompt_text or title,
                "category_id": category.id,
                "grading_json": json.dumps(grading, ensure_ascii=False),
                "subjective_rubric_json": json.dumps(
                    subjective_rubric, ensure_ascii=False
                ),
                "meta_json": json.dumps(meta, ensure_ascii=False),
                "difficulty": diff if diff in ("easy", "medium", "hard") else False,
                "source_ref": f"{source_tag}:{proj_name}#{item.get('id', idx)}",
                "skill_ids": [(6, 0, question_skill_ids)] if question_skill_ids else False,
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
                    Dimension, QDim, QOpt, question, f, grade, warnings
                )

        return {
            "category_id": category.id,
            "category_name": category.name,
            "questions_created": created,
            "objective_fields": obj_fields,
            "subjective_fields": subj_fields,
            "skills_created": skills_created_count,
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
                types_seen.add("subjective_justification")
        if not types_seen:
            return "mcq"
        if "msq" in types_seen:
            return "msq"
        if "subjective_justification" in types_seen and "mcq" not in types_seen:
            return "subjective_justification"
        return "mcq"

    def _upsert_skills(self, skillset):
        Skill = self.env["etp.assessment.pro.skill"].sudo()
        out = {}
        created_count = 0
        for s in skillset or []:
            if not isinstance(s, dict):
                continue
            name = (s.get("name") or "").strip()
            if not name:
                continue
            existing = Skill.search([("name", "=", name)], limit=1)
            if existing:
                out[name] = existing
                continue
            rec = Skill.create({
                "name": name,
                "description": s.get("description") or False,
                "tags": s.get("tags") or False,
            })
            out[name] = rec
            created_count += 1
        return out, created_count

    def _materialize_objective_field(self, Dimension, QDim, QOpt, question,
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

        dim = Dimension.search([("name", "=", label)], limit=1)
        if not dim:
            dim = Dimension.create({
                "name": label,
                "option_ids": [
                    (0, 0, {"name": o, "sequence": (i + 1) * 10})
                    for i, o in enumerate(options)
                ],
            })
        else:
            existing = set(dim.option_ids.mapped("name"))
            missing = [o for o in options if o not in existing]
            if missing:
                dim.write({"option_ids": [(0, 0, {"name": o}) for o in missing]})

        qd = QDim.create({"question_id": question.id, "dimension_id": dim.id})

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
