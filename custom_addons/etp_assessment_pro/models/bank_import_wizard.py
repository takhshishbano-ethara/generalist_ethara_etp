# -*- coding: utf-8 -*-
"""Question Bank Import wizard + shared CSV column spec (CSV-only; JSON is the LLM flow).

Multi-dimension questions use INDEXED columns dim1_label/dim1_options/dim1_correct
... dimN_*; values within a cell split on ``|``. dimensions_json / images_json power
columns are accepted for lossless round-trips of an export.
"""
import base64
import csv
import io
import json
import logging

from odoo import models, fields, api
from odoo.exceptions import UserError

from ..constants import QUESTION_TYPE_SHORT_LABELS, AB_DIMENSION_NAMES, AB_CHOICES

_logger = logging.getLogger(__name__)

MAX_DIMS = 4
MAX_IMAGES = 3

CORE_COLUMNS = [
    "title", "question_type", "prompt", "description",
    "category", "skills", "difficulty", "time_minutes",
    "options", "correct_answer",
]
SUBJECTIVE_COLUMNS = ["rubric_json", "official_reasoning"]


def dimension_columns(n=MAX_DIMS):
    cols = []
    for i in range(1, n + 1):
        cols += [f"dim{i}_label", f"dim{i}_options", f"dim{i}_correct"]
    return cols


def image_columns(n=MAX_IMAGES):
    cols = []
    for i in range(1, n + 1):
        cols += [f"img{i}_slot", f"img{i}_label", f"img{i}_url"]
    return cols


def import_columns():
    """Full ordered import/round-trip column list."""
    return (CORE_COLUMNS + dimension_columns() + SUBJECTIVE_COLUMNS
            + image_columns())


def _split(cell, sep="|"):
    if not cell:
        return []
    return [p.strip() for p in str(cell).split(sep) if p.strip()]


class EtpAssessmentBankImportWizard(models.TransientModel):
    _name = "etp.assessment.pro.bank.import.wizard"
    _description = "Import Question Bank (CSV / JSON) into review drafts"

    data_file = fields.Binary(string="File")
    data_filename = fields.Char(string="Filename")
    question_count = fields.Integer(
        string="Questions in File", readonly=True,
        help="Parsed row / item count - refreshes when you attach a file.")
    generator_name = fields.Char(
        string="Batch Name",
        help="Name for the import batch (a generator record). Defaults to the "
             "filename.")

    @api.onchange("data_file", "data_filename")
    def _onchange_count(self):
        for rec in self:
            rec.question_count = 0
            if not rec.data_file:
                continue
            try:
                native = rec._try_native_payload()
                if native is not None:
                    rec.question_count = len(native.get("questions") or [])
                else:
                    rec.question_count = len(rec._parse_rows())
                if not rec.generator_name and rec.data_filename:
                    rec.generator_name = "Import: %s" % rec.data_filename
            except Exception as exc:  # noqa: BLE001 - onchange must not crash
                _logger.debug("Import preview parse failed: %s", exc)
                rec.question_count = 0

    def _decode(self):
        self.ensure_one()
        try:
            return base64.b64decode(self.data_file)
        except Exception:
            raise UserError("Could not decode the uploaded file.")

    def _parse_rows(self):
        self.ensure_one()
        raw = self._decode()
        return self._parse_csv(raw)

    def _parse_csv(self, raw):
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("latin-1")
        reader = csv.DictReader(io.StringIO(text))
        if not reader.fieldnames:
            raise UserError("CSV has no header row.")
        headers = {(h or "").strip().lower() for h in reader.fieldnames}
        if "title" not in headers and "prompt" not in headers:
            raise UserError(
                "CSV must have at least a 'title' or 'prompt' column. "
                "Use Download Template for the expected columns.")
        out = []
        for n, row in enumerate(reader, start=2):
            row = {(k or "").strip().lower(): (v or "").strip()
                   for k, v in row.items() if k}
            if not any(row.values()):
                continue
            try:
                out.append(self._row_to_vals(row))
            except Exception as exc:  # noqa: BLE001
                raise UserError(f"Row {n}: {exc}")
        if not out:
            raise UserError("No data rows found in the CSV.")
        return out

    _JUNK_DIM_LABELS = ("you read the image?", "read the image", "did you read")

    @staticmethod
    def _is_junk_dim(spec):
        """A gate dimension is junk when its label is filler or it has <=1 option."""
        label = (spec.get("label") or "").strip().lower()
        opts = [o for o in (spec.get("options") or []) if str(o).strip()]
        if any(j in label for j in EtpAssessmentBankImportWizard._JUNK_DIM_LABELS):
            return True
        if len(opts) <= 1:
            return True
        return False

    def _row_to_vals(self, row):
        qtype = (row.get("question_type") or "mcq").strip()
        valid = dict(self.env["etp.assessment.pro.prompt.question"]
                     ._fields["question_type"].selection)
        if qtype not in valid:
            raise UserError(
                f"unknown question_type '{qtype}'. "
                f"Allowed: {', '.join(valid)}")

        dims = self._collect_indexed_dims(row)
        single_options = _split(row.get("options"))
        if not dims and single_options:
            stem = (row.get("dimension_label")
                    or row.get("description")
                    or row.get("prompt")
                    or "Answer")
            dims = [{
                "label": stem[:200],
                "options": single_options,
                "correct": _split(row.get("correct_answer")),
            }]
        if row.get("dimensions_json"):
            try:
                dims = json.loads(row["dimensions_json"])
            except (ValueError, TypeError):
                raise UserError("dimensions_json is not valid JSON.")
        if dims:
            dims = [d for d in dims if not self._is_junk_dim(d)]

        raw_title = (row.get("title") or "").strip()
        raw_prompt = (row.get("prompt") or "").strip()
        category = (row.get("category") or "").strip()
        stem = ""
        if qtype in ("mcq", "msq") and dims:
            stem = (dims[0].get("label") or "").strip()

        _GENERIC_PROMPTS = ("choose the correct option",
                            "choose the correct answer",
                            "select the correct option", "")
        prompt = raw_prompt
        if qtype in ("mcq", "msq") and stem and \
                raw_prompt.lower() in _GENERIC_PROMPTS:
            prompt = stem

        type_label = QUESTION_TYPE_SHORT_LABELS.get(
            qtype, qtype.replace("_", " ").title())

        def _weak_title(t):
            if not t or t == category:
                return True
            if t in ("Multiple Choice Question", "Image comparison",
                     "Untitled Question"):
                return True
            base = prompt or raw_prompt
            if base and (t == base or base.startswith(t) or len(t) > 80):
                return True
            return False

        if _weak_title(raw_title):
            title = ("%s %s" % (category, type_label)).strip() \
                if category else type_label
        else:
            title = raw_title

        vals = {
            "name": title[:200],
            "question_prompt": prompt or title,
            "description": row.get("description") or False,
            "question_type": qtype,
        }
        diff = (row.get("difficulty") or "").lower()
        if diff in ("easy", "medium", "hard"):
            vals["difficulty"] = diff
        try:
            vals["time_minutes"] = int(float(row.get("time_minutes") or 0))
        except (TypeError, ValueError):
            vals["time_minutes"] = 0

        if dims:
            vals["dimensions_json"] = json.dumps(dims, ensure_ascii=False)

        if row.get("rubric_json"):
            rubric_raw = row["rubric_json"].strip()
            try:
                json.loads(rubric_raw)
                vals["rubric_json"] = rubric_raw
            except (ValueError, TypeError):
                vals["rubric_json"] = json.dumps(
                    [{"label": title[:80], "pass_condition": rubric_raw}],
                    ensure_ascii=False)
        if row.get("official_reasoning"):
            vals["official_reasoning"] = row["official_reasoning"]

        images = self._collect_indexed_images(row)
        if row.get("images_json"):
            try:
                images = json.loads(row["images_json"])
            except (ValueError, TypeError):
                raise UserError("images_json is not valid JSON.")
        if images:
            vals["images_json"] = json.dumps(images, ensure_ascii=False)
        return vals

    @staticmethod
    def _collect_indexed_dims(row):
        idxs = set()
        for k in row:
            if k.startswith("dim") and ("_label" in k or "_options" in k
                                        or "_correct" in k):
                num = k[3:].split("_", 1)[0]
                if num.isdigit():
                    idxs.add(int(num))
        dims = []
        for i in sorted(idxs):
            options = _split(row.get(f"dim{i}_options"))
            if not options and not row.get(f"dim{i}_label"):
                continue
            dims.append({
                "label": row.get(f"dim{i}_label") or f"Dimension {i}",
                "options": options,
                "correct": _split(row.get(f"dim{i}_correct")),
            })
        return dims

    @staticmethod
    def _collect_indexed_images(row):
        idxs = set()
        for k in row:
            if k.startswith("img") and ("_url" in k or "_slot" in k
                                        or "_label" in k):
                num = k[3:].split("_", 1)[0]
                if num.isdigit():
                    idxs.add(int(num))
        images = []
        for i in sorted(idxs):
            url = row.get(f"img{i}_url")
            if not url:
                continue
            images.append({
                "slot": row.get(f"img{i}_slot") or False,
                "label": row.get(f"img{i}_label") or False,
                "url": url,
            })
        return images

    def _try_harness_payload(self):
        """Return the parsed authoring-harness run dict/list if the upload is one,
        else None. The harness emits questions.json (a list of {id, instruction,
        fields, assets}) or output.json ({questions:[...], solutions:{...}}). We
        fingerprint on those shapes so it routes to import_bank_harness and not the
        CSV or native round-trip paths."""
        try:
            data = json.loads(self._decode().decode("utf-8-sig"))
        except Exception as exc:  # noqa: BLE001
            _logger.debug("Harness-payload parse failed, trying next path: %s", exc)
            return None

        def _looks_harness(q):
            return isinstance(q, dict) and (
                "fields" in q or ("instruction" in q and "id" in q))

        if isinstance(data, list) and data and _looks_harness(data[0]):
            return data
        if isinstance(data, dict) and isinstance(data.get("questions"), list) \
                and not data.get("etp_assessment_pro_bank"):
            qs = data["questions"]
            if qs and _looks_harness(qs[0]):
                return data
        return None

    def _try_native_payload(self):
        """Return the parsed native round-trip export dict if the upload is one,
        else None (so the CSV path runs)."""
        try:
            data = json.loads(self._decode().decode("utf-8-sig"))
        except Exception as exc:  # noqa: BLE001
            # Not parseable as JSON → fall through to the CSV path. Log at debug
            # so a corrupt native-export upload is diagnosable (otherwise the
            # admin sees a confusing "no CSV rows" error instead).
            _logger.debug("Native-payload parse failed, trying CSV path: %s", exc)
            return None
        if isinstance(data, dict) and data.get("etp_assessment_pro_bank") \
                and isinstance(data.get("questions"), list):
            return data
        return None

    def action_import(self):
        self.ensure_one()
        if not self.data_file:
            raise UserError("Please attach a file first.")
        harness = self._try_harness_payload()
        if harness is not None:
            res = self.env["etp.assessment.pro.bank.import"].import_bank_harness(
                harness,
                generator_name=(self.generator_name
                                or ("Harness: %s" % (self.data_filename
                                                     or "run"))))
            warns = res.get("warnings") or []
            msg = "Imported %s draft question(s) from the authoring harness." \
                % res.get("questions_created", 0)
            if warns:
                msg += " %d note(s) - see logs for asset follow-ups." % len(warns)
                for w in warns[:20]:
                    _logger.info("harness import: %s", w)
            return {
                "type": "ir.actions.act_window",
                "name": "Imported Question Bank",
                "res_model": "etp.assessment.pro.prompt",
                "view_mode": "form",
                "res_id": res.get("generator_id"),
                "target": "current",
                "context": {},
            }
        native = self._try_native_payload()
        if native is not None:
            res = self.env["etp.assessment.pro.bank.import"].import_bank_native(
                native)
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": "Question Bank Imported",
                    "message": "Rebuilt %s question(s) from the round-trip "
                               "export." % res.get("questions_created", 0),
                    "type": "success",
                    "sticky": False,
                },
            }
        rows = self._parse_rows()

        Prompt = self.env["etp.assessment.pro.prompt"]
        Draft = self.env["etp.assessment.pro.prompt.question"]

        batch_name = (self.generator_name
                      or ("Import: %s" % (self.data_filename or "bank")))[:120]
        prompt = Prompt.create({
            "name": batch_name,
            "state": "done",
            "source_text": "Imported via CSV (%s rows)." % len(rows),
            "last_extract_summary": "Imported %s draft question(s)." % len(rows),
        })

        created = 0
        drafts = self.env["etp.assessment.pro.prompt.question"]
        for vals in rows:
            vals.pop("_source_ref", None)
            vals["prompt_id"] = prompt.id
            drafts |= Draft.create(vals)
            created += 1

        drafts.with_context(skip_image_ready_guard=True).action_approve()

        return {
            "type": "ir.actions.act_window",
            "name": "Imported Question Bank",
            "res_model": "etp.assessment.pro.prompt",
            "view_mode": "form",
            "res_id": prompt.id,
            "target": "current",
            "context": {},
        }

    def action_download_template(self):
        content = build_template_csv()
        att = self.env["ir.attachment"].create({
            "name": "question_bank_import_template.csv",
            "type": "binary",
            "datas": base64.b64encode(content.encode("utf-8")).decode(),
            "mimetype": "text/csv",
            "res_model": self._name,
        })
        return {
            "type": "ir.actions.act_url",
            "url": f"/web/content/{att.id}?download=true",
            "target": "self",
        }


def build_template_csv():
    """Build the import template CSV string: one dummy row per question type."""
    cols = import_columns()
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    for row in _template_rows():
        w.writerow(row)
    return buf.getvalue()


def _template_rows():
    """One dummy row per question type, exercising every relevant column."""
    return [
        {
            "title": "Capital of France",
            "question_type": "mcq",
            "prompt": "Which city is the capital of France?",
            "description": "Single correct answer.",
            "category": "Geography",
            "skills": "World Capitals",
            "difficulty": "easy",
            "time_minutes": "1",
            "options": "Paris|London|Berlin|Madrid",
            "correct_answer": "Paris",
        },
        {
            "title": "Prime numbers",
            "question_type": "msq",
            "prompt": "Select ALL prime numbers below.",
            "description": "One or more correct.",
            "category": "Mathematics",
            "skills": "Number Theory",
            "difficulty": "medium",
            "time_minutes": "2",
            "options": "2|3|4|9",
            "correct_answer": "2|3",
        },
        {
            "title": "Screenshot QA - Spotify",
            "question_type": "mcq",
            "prompt": "Review the screenshot and answer each check.",
            "description": "Several objective dimensions on one question.",
            "category": "Image Labelling",
            "skills": "App Identification|Box Coverage",
            "difficulty": "medium",
            "time_minutes": "3",
            "dim1_label": "Do you know this Application?",
            "dim1_options": "Yes|No",
            "dim1_correct": "Yes",
            "dim2_label": "Application",
            "dim2_options": "Spotify|Deezer|SoundCloud|Tidal",
            "dim2_correct": "Spotify",
            "dim3_label": "Are all interactive elements boxed?",
            "dim3_options": "Yes|No",
            "dim3_correct": "Yes",
            "dim4_label": "Next step",
            "dim4_options": "Proceed to labeling|Skip the image",
            "dim4_correct": "Proceed to labeling",
        },
        {
            "title": "Explain idempotency",
            "question_type": "subjective_rubric",
            "prompt": "Explain what idempotency means for a REST API and why "
                      "it matters.",
            "description": "Free-text answer, LLM-graded against the prompt.",
            "category": "Engineering",
            "skills": "API Design",
            "difficulty": "hard",
            "time_minutes": "8",
        },
        {
            "title": "Incident postmortem quality",
            "question_type": "subjective_rubric",
            "prompt": "Write a postmortem for the outage described above.",
            "description": "Free-text answer graded against a rubric.",
            "category": "Engineering",
            "skills": "Incident Response",
            "difficulty": "hard",
            "time_minutes": "15",
            "rubric_json": json.dumps([{
                "label": "Postmortem",
                "checklist": ["States root cause", "Lists impact",
                              "Has action items with owners"],
                "constraints": ["No blame of individuals"],
                "pass_condition": "Covers root cause, impact, and at least "
                                  "two concrete action items.",
            }], ensure_ascii=False),
        },
        {
            "title": "Compare two generated images",
            "question_type": "image_ab",
            "prompt": "Compare Response A and Response B against the prompt "
                      "across each axis.",
            "description": "Pick A / B / Both Good / Both Bad per axis; "
                           "justify your overall choice.",
            "category": "Image Eval",
            "skills": "Image Comparison",
            "difficulty": "hard",
            "time_minutes": "5",
            "dim1_label": AB_DIMENSION_NAMES["IF"],
            "dim1_options": "|".join(AB_CHOICES),
            "dim1_correct": "Response B",
            "dim2_label": AB_DIMENSION_NAMES["VQ"],
            "dim2_options": "|".join(AB_CHOICES),
            "dim2_correct": "Response B",
            "dim3_label": AB_DIMENSION_NAMES["LAI"],
            "dim3_options": "|".join(AB_CHOICES),
            "dim3_correct": "Both Good",
            "dim4_label": AB_DIMENSION_NAMES["OC"],
            "dim4_options": "|".join(AB_CHOICES),
            "dim4_correct": "Response B",
            "official_reasoning": "Response B shows the steam, stacked carpets "
                                  "and crowd the prompt asks for and is sharper.",
            "img1_slot": "a",
            "img1_label": "Response A",
            "img1_url": "https://picsum.photos/seed/respA/512",
            "img2_slot": "b",
            "img2_label": "Response B",
            "img2_url": "https://picsum.photos/seed/respB/512",
        },
        {
            "title": "Write the prompt for this image",
            "question_type": "image_prompt",
            "prompt": "Study the reference image, then write the text-to-image "
                      "prompt that would reproduce it.",
            "description": "Free-text prompt graded against an ideal prompt for "
                           "the required subject, style, and composition.",
            "category": "Image Eval",
            "skills": "Prompt Writing",
            "difficulty": "medium",
            "time_minutes": "6",
            "rubric_json": json.dumps({
                "ideal_prompt": "A photorealistic red vintage bicycle leaning "
                                "against a weathered blue door in golden-hour "
                                "light, shallow depth of field.",
                "mandatory_elements": ["red vintage bicycle", "blue door",
                                       "golden-hour light"],
                "penalty_rules": ["Penalise vague or generic prompts"],
                "scoring_guide": "Full marks when the prompt names the subject, "
                                 "style, lighting, and composition specifically.",
            }, ensure_ascii=False),
            "img1_slot": "reference",
            "img1_label": "Reference",
            "img1_url": "https://picsum.photos/seed/refbike/512",
        },
        {
            "title": "Label the UI screenshot",
            "question_type": "image_label",
            "prompt": "Identify the app, confirm the boxes, then label what "
                      "each numbered box does.",
            "description": "Objective gate checks + a free-text labelling "
                           "answer graded against a textual key.",
            "category": "Image Eval",
            "skills": "App Identification|Labelling",
            "difficulty": "medium",
            "time_minutes": "6",
            "dim1_label": "Do you know this Application?",
            "dim1_options": "Yes|No",
            "dim1_correct": "Yes",
            "dim2_label": "Application",
            "dim2_options": "Spotify|Deezer|SoundCloud|Tidal",
            "dim2_correct": "Spotify",
            "rubric_json": json.dumps({
                "ideal_labels": "Box 1 = search; Box 2 = play/pause; Box 3 = "
                                "library.",
                "mandatory_elements": ["search", "play", "library"],
                "penalty_rules": ["No mention of unrelated controls"],
                "scoring_guide": "Full marks if every numbered box is labelled "
                                 "correctly.",
            }, ensure_ascii=False),
            "img1_slot": "single",
            "img1_label": "Screenshot",
            "img1_url": "https://picsum.photos/seed/ui/512",
        },
    ]
