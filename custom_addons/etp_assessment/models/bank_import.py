# -*- coding: utf-8 -*-
"""Question-bank JSON importer.

Consumes the research-team / generator question-bank JSON
(knowledge/*/output-schema.json) and materializes it into the assessment
question bank: category, questions, dimensions + options, objective answer
keys (is_correct), subjective rubrics, skillset, and (optionally) generated
+ S3-hosted images.

This ONE parser serves both flows:
  - IMPORT: research team hands a JSON bank      -> import_bank(json)
  - GENERATE: seed prompt emits the SAME schema  -> import_bank(json)

Schema (output-schema.json, abridged):
  project { name, task_type, sop_title, settings{...} }
  question_bank[ {
     id, inputs{ prompt|instruction, media[{label,type,placeholder}] },
     fields[{ key, label, type(single_choice|yes_no|free_text), options[] }],
     grading{ <field_key>: objective{answer,reason} | subjective{checklist,
              constraints,pass_condition} },
     meta{ scenario_type, answer_pattern, difficulty, trap }
  } ]
  skillset[{name, description}]
  worker_grading_rubric{...}
"""
import json
import logging

from odoo import models, api
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)

OBJECTIVE_FIELD_TYPES = ("single_choice", "yes_no")
SUBJECTIVE_FIELD_TYPES = ("free_text",)


class EtpAssessmentBankImport(models.AbstractModel):
    _name = "etp.assessment.bank.import"
    _description = "Question Bank JSON Importer"

    # ------------------------------------------------------------------
    @api.model
    def import_bank(self, bank, category_id=False, category_name=None,
                    generate_images=False, source_tag="json"):
        """Import a question-bank dict (already json.loads'd) into the bank.

        Returns a summary dict:
          {category_id, questions_created, objective_fields, subjective_fields,
           skills_created, images_generated, warnings[]}
        """
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

        # ---- category ----
        Cat = self.env["etp.assessment.category"].sudo()
        if category_id:
            category = Cat.browse(category_id)
            if not category.exists():
                raise UserError("Target category not found.")
        else:
            name = (category_name or project.get("name")
                    or "Imported Bank").strip()
            category = Cat.search([("name", "=", name)], limit=1) or Cat.create(
                {"name": name})

        # ---- skillset (idempotent: store on category description note) ----
        skills_created = 0
        skillset = bank.get("skillset") or []
        if skillset and "etp.assessment.prompt.skill" in self.env:
            # skills are advisory; we just log them on the category for now
            note = "\n".join(
                f"- {s.get('name')}: {s.get('description','')}"
                for s in skillset if isinstance(s, dict))
            if note and hasattr(category, "description"):
                category.sudo().write({"description": (
                    (category.description or "") + "\n[Skillset]\n" + note)[:8000]})
            skills_created = len(skillset)

        # ---- questions ----
        Question = self.env["etp.assessment.question"].sudo()
        Dimension = self.env["etp.assessment.dimension"].sudo()
        QDim = self.env["etp.assessment.question.dimension"].sudo()
        QOpt = self.env["etp.assessment.question.dimension.option"].sudo()

        created = 0
        obj_fields = 0
        subj_fields = 0
        images_generated = 0
        proj_name = project.get("name") or "bank"

        for idx, item in enumerate(items, start=1):
            if not isinstance(item, dict):
                warnings.append(f"item {idx}: not an object, skipped")
                continue
            inputs = item.get("inputs") or {}
            fields_def = item.get("fields") or []
            grading = item.get("grading") or {}
            meta = item.get("meta") or {}

            prompt_text = (inputs.get("instruction")
                           or inputs.get("prompt") or "").strip()
            title = (prompt_text[:60] or f"Question {item.get('id', idx)}")
            media = inputs.get("media") or []

            # task type: image media => image_comparison, else text
            qtype = "image_comparison" if any(
                (m.get("type") in ("image", "screenshot"))
                for m in media if isinstance(m, dict)) else "text"

            # split fields into objective (single_choice/yes_no) and
            # subjective (free_text)
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

            question = Question.create({
                "name": title,
                "sequence": (item.get("id") or idx) * 10,
                "question_type": qtype,
                "prompt": prompt_text or title,
                "category_id": category.id,
                "image_a_url": self._media_url(media, 0),
                "image_b_url": self._media_url(media, 1),
                "grading_json": json.dumps(grading, ensure_ascii=False),
                "subjective_rubric_json": json.dumps(
                    subjective_rubric, ensure_ascii=False),
                "meta_json": json.dumps(meta, ensure_ascii=False),
                "difficulty": meta.get("difficulty") if meta.get(
                    "difficulty") in ("easy", "medium", "hard") else False,
                "source_ref": f"{source_tag}:{proj_name}#{item.get('id', idx)}",
            })
            created += 1

            # ---- objective fields -> dimensions + correct option ----
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
                    Dimension, QDim, QOpt, question, f, grade, warnings)

            # ---- image generation -> S3 (optional) ----
            if generate_images and qtype == "image_comparison":
                n = self._maybe_generate_images(question, media, warnings)
                images_generated += n

        return {
            "category_id": category.id,
            "category_name": category.name,
            "questions_created": created,
            "objective_fields": obj_fields,
            "subjective_fields": subj_fields,
            "skills_created": skills_created,
            "images_generated": images_generated,
            "warnings": warnings,
        }

    # ------------------------------------------------------------------
    @staticmethod
    def _media_url(media, index):
        """Return an existing URL for media[index] if present, else ''.

        Media placeholders are text descriptions; a real URL only exists if
        the bank already carries one (key 'url' or 'src'). Generated images
        get their URL written later by _maybe_generate_images.
        """
        if index < len(media) and isinstance(media[index], dict):
            return media[index].get("url") or media[index].get("src") or ""
        return ""

    def _materialize_objective_field(self, Dimension, QDim, QOpt, question,
                                     field_def, grade, warnings):
        """Create/reuse a dimension for an objective field + mark correct.

        Robust to real-world banks: yes_no fields get synthesized Yes/No
        options when none are given; answer matching is normalized
        (strip + casefold) and tolerates true/false/yes/no spellings; a
        no-match is a LOUD warning (the dimension would otherwise count in
        the max but never be earnable).
        """
        label = field_def.get("label") or field_def.get("key") or "Field"
        options = list(field_def.get("options") or [])
        ftype = field_def.get("type")
        if not options and ftype == "yes_no":
            options = ["Yes", "No"]
        if not options:
            warnings.append(
                f"BLOCKING {question.name}: objective field '{label}' has no "
                f"options — not scorable, dimension skipped")
            return

        # reuse a master dimension by name, else create with these options
        dim = Dimension.search([("name", "=", label)], limit=1)
        if not dim:
            dim = Dimension.create({
                "name": label,
                "option_ids": [(0, 0, {"name": o, "sequence": (i + 1) * 10})
                               for i, o in enumerate(options)],
            })
        else:
            existing = set(dim.option_ids.mapped("name"))
            missing = [o for o in options if o not in existing]
            if missing:
                dim.write({"option_ids": [
                    (0, 0, {"name": o}) for o in missing]})

        qd = QDim.create({"question_id": question.id, "dimension_id": dim.id})
        # qd auto-populates option lines from the master dimension on create

        correct_answer = (grade or {}).get("answer")
        if correct_answer is None or correct_answer == "":
            return

        match = self._match_option(qd.option_line_ids, correct_answer)
        if match:
            match.write({"is_correct": True})
        else:
            warnings.append(
                f"BLOCKING {question.name}: correct answer "
                f"'{correct_answer}' not among options for '{label}' "
                f"({[l.name for l in qd.option_line_ids]}) — dimension "
                f"counts toward max but can never be earned")

    @staticmethod
    def _match_option(option_lines, answer):
        """Find the option line matching `answer`, normalized + alias-aware."""
        def norm(s):
            return str(s).strip().casefold()
        target = norm(answer)
        # alias common boolean spellings
        aliases = {
            "true": ("yes", "true"), "false": ("no", "false"),
            "yes": ("yes", "true"), "no": ("no", "false"),
        }
        targets = {target} | set(aliases.get(target, ()))
        for line in option_lines:
            name = norm(line.name)
            if name in targets or name == target:
                return line
            # also alias the option side (option "Yes" vs answer "true")
            if name in aliases and target in (aliases.get(name) or ()):
                return line
        return option_lines.browse()

    def _maybe_generate_images(self, question, media, warnings):
        """Generate A/B images from placeholders, upload to S3, set URLs."""
        from ..services import bedrock_images, s3_service
        env = self.env
        if not bedrock_images.is_configured(env):
            warnings.append(f"{question.name}: image gen skipped (not configured)")
            return 0
        if not s3_service.is_configured(env):
            warnings.append(f"{question.name}: S3 not configured, image kept inline")
        count = 0
        for idx, url_field, bin_field in (
                (0, "image_a_url", "image_a"), (1, "image_b_url", "image_b")):
            if idx >= len(media):
                continue
            m = media[idx]
            if not isinstance(m, dict):
                continue
            placeholder = m.get("placeholder") or m.get("label") or ""
            if not placeholder:
                continue
            try:
                b64 = bedrock_images.generate_image_b64(env, placeholder)
            except Exception as exc:
                warnings.append(f"{question.name}: image gen failed ({exc})")
                continue
            # upload to S3 if possible (retried inside), else keep binary
            try:
                url, _key = s3_service.upload_image_b64(
                    env, b64, key_hint=f"q{question.id}-{idx}")
                question.write({url_field: url})
            except Exception as exc:
                warnings.append(
                    f"{question.name}: S3 upload failed ({exc}), kept inline")
                question.write({bin_field: b64})
            count += 1
        return count
