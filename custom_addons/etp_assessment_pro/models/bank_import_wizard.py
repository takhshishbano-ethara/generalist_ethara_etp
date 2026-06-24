# -*- coding: utf-8 -*-
"""Question Bank Import wizard + shared CSV column spec.

Goal: stop the manual tedium of hand-building a question and EVERY dimension
in the form. One CSV row is published straight into the bank — its dimensions,
options, correct-answer flags, rubric, official reasoning, and images (URLs
offloaded to S3 when configured) are all materialized via the same approve
path the LLM generator uses, but fed by an import instead of the LLM.
(JSON is reserved for the LLM-generation flow; this importer is CSV-only.)

The wizard is deliberately spreadsheet-friendly: multi-dimension questions
(e.g. image_ab with 4 axes, dense-bbox with many) are authored with INDEXED
columns ``dim1_label / dim1_options / dim1_correct`` ... ``dimN_*`` — no JSON
hand-authoring required. Options/correct/skills within a cell are split on
``|``. A ``dimensions_json`` / ``images_json`` power column is still accepted
for lossless round-trips of an export.
"""
import base64
import csv
import io
import json
import logging

from odoo import models, fields, api
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# Canonical import column order. Export of the question bank uses the SAME
# columns so a bank can be exported, edited in a spreadsheet, and re-imported
# losslessly. ``MAX_DIMS`` / ``MAX_IMAGES`` bound the indexed-column template;
# import itself scans for ANY dimN_/imgN_ index, so deeper banks still parse.
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

    target_category_id = fields.Many2one(
        "etp.assessment.pro.category", string="Target Category",
        help="Category assigned to imported questions that don't carry their "
             "own 'category' value. Leave blank to use each row's category "
             "(or a generated one).")
    data_file = fields.Binary(string="File")
    data_filename = fields.Char(string="Filename")
    question_count = fields.Integer(
        string="Questions in File", readonly=True,
        help="Parsed row / item count — refreshes when you attach a file.")
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
                rows = rec._parse_rows()
                rec.question_count = len(rows)
                if not rec.generator_name and rec.data_filename:
                    rec.generator_name = "Import: %s" % rec.data_filename
            except Exception as exc:  # noqa: BLE001 - onchange must not crash
                _logger.debug("Import preview parse failed: %s", exc)
                rec.question_count = 0

    # ------------------------------------------------------------------
    # Parsing -> a list of draft-vals dicts (prompt_id filled in later).
    # ------------------------------------------------------------------
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

    # Junk "gate" dimension labels that carry no assessment value (artifacts of
    # some source exports). Dropped on import so they never reach the candidate.
    _JUNK_DIM_LABELS = ("you read the image?", "read the image", "did you read")

    @staticmethod
    def _is_junk_dim(spec):
        """A gate dimension is junk when its label is a known filler prompt OR
        it offers a single non-answer option (e.g. just 'Yes'/'OK') that tests
        nothing. Such dims show as a pointless radio group to the candidate."""
        label = (spec.get("label") or "").strip().lower()
        opts = [o for o in (spec.get("options") or []) if str(o).strip()]
        if any(j in label for j in EtpAssessmentBankImportWizard._JUNK_DIM_LABELS):
            return True
        if len(opts) <= 1:
            # A 0/1-option dimension cannot be a real objective check.
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

        # --- dimensions FIRST: dimN_* indexed cols, then single options/correct
        #     shorthand, then dimensions_json power column (highest precedence).
        #     We need the answer-key stem before deciding the title/prompt. -----
        dims = self._collect_indexed_dims(row)
        single_options = _split(row.get("options"))
        if not dims and single_options:
            # A single-choice MCQ/MSQ dimension reads as the QUESTION STEM
            # itself (e.g. "Which of the following best describes…"), NOT a
            # generic title. Prefer an explicit dimension_label, else the
            # stem candidate (description/prompt), else a clean generic.
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
        # Drop junk gate dimensions so the candidate never sees a pointless
        # "You read the image? · Yes" radio (defense in depth — even a sloppy
        # CSV self-cleans).
        if dims:
            dims = [d for d in dims if not self._is_junk_dim(d)]

        # --- prompt: the actual question/instruction shown to the candidate.
        #     For an objective question whose prompt is generic filler ("choose
        #     the correct option"), promote the answer-key stem so the candidate
        #     sees the real question. --------------------------------------
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
            # The real question lives in the stem; surface it as the prompt.
            prompt = stem

        # --- title: a SHORT human label, NOT the question text. The title and
        #     prompt are different kinds of thing — title names/categorizes the
        #     question ("Non-STEM Baseline MCQ"), prompt holds the actual
        #     question. We NEVER put the prompt (or a truncation of it) in the
        #     title, which is what caused the doubled-text header. When the CSV
        #     gives no usable title we build "<Category> <Type>".
        _TYPE_LABELS = {
            "mcq": "MCQ", "msq": "MSQ",
            "subjective_justification": "Justification",
            "subjective_rubric": "Rubric",
            "image_ab": "Image Comparison",
            "image_text": "Image + Text",
        }
        type_label = _TYPE_LABELS.get(qtype, qtype.replace("_", " ").title())

        def _weak_title(t):
            # A title is unusable as a label when it's empty, is just the
            # category, is a known generic, or is actually the prompt text /
            # a truncated prefix of it (the doubling bug we're fixing).
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
            "skill_names": " | ".join(_split(row.get("skills"))) or False,
            "category_name": row.get("category") or False,
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

        # --- rubric / official reasoning ----------------------------------
        if row.get("rubric_json"):
            # Accept raw JSON, or wrap a plain pass-condition string.
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

        # --- images: imgN_* indexed columns, then images_json ------------
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

    # ------------------------------------------------------------------
    # Import action: build a generator prompt + its draft questions, then
    # open it on the Question Drafts tab for review/approve.
    # ------------------------------------------------------------------
    def action_import(self):
        self.ensure_one()
        if not self.data_file:
            raise UserError("Please attach a file first.")
        rows = self._parse_rows()

        Category = self.env["etp.assessment.pro.category"]
        Prompt = self.env["etp.assessment.pro.prompt"]
        Draft = self.env["etp.assessment.pro.prompt.question"]

        batch_name = (self.generator_name
                      or ("Import: %s" % (self.data_filename or "bank")))[:120]
        # One CSV import => ONE generator row in the Generators list, named after
        # the file and carrying the imported drafts (so its Question/Approved
        # counts are populated). The drafts are materialized straight into the
        # bank (published), and the generator is KEPT as the batch's record —
        # it is NOT deleted, so the import shows up in the list the same way an
        # LLM generation batch does.
        prompt = Prompt.create({
            "name": batch_name,
            "category_id": self.target_category_id.id or False,
            "state": "done",
            "source_text": "Imported via CSV (%s rows)." % len(rows),
            "last_extract_summary": "Imported %s draft question(s)." % len(rows),
        })

        cat_cache = {}
        created = 0
        drafts = self.env["etp.assessment.pro.prompt.question"]
        for vals in rows:
            cat_name = vals.pop("category_name", None)
            vals.pop("_source_ref", None)
            cat = self.target_category_id
            if cat_name:
                key = cat_name.strip()
                if key not in cat_cache:
                    cat_cache[key] = (
                        Category.search([("name", "=", key)], limit=1)
                        or Category.create({"name": key}))
                cat = cat_cache[key]
            elif not cat:
                cat = prompt._get_or_create_category()
            vals["prompt_id"] = prompt.id
            vals["category_id"] = cat.id
            drafts |= Draft.create(vals)
            created += 1

        # Materialize each draft into a published bank question (dimensions +
        # options + correct flags + rubric + images). The drafts stay linked to
        # this generator (state=approved) so its Question/Approved counts read
        # correctly in the Generators list and form.
        drafts.action_approve()

        # Land the user on this import batch's generator form (Question Drafts
        # tab) so they immediately see the imported questions in context — the
        # same place an LLM generation batch opens.
        return {
            "type": "ir.actions.act_window",
            "name": "Imported Question Bank",
            "res_model": "etp.assessment.pro.prompt",
            "view_mode": "form",
            "res_id": prompt.id,
            "target": "current",
            "context": {},
        }

    # ------------------------------------------------------------------
    # Template download — a CSV with one dummy row per question type and
    # every importable column populated, so the user can pick/keep/reorder.
    # ------------------------------------------------------------------
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
    """Build the import template CSV string: header = full column list, one
    fully-populated dummy row per question type. Shared by the wizard button
    and the standalone sample-file generator."""
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
        {  # 1. Plain MCQ (single dimension, one correct)
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
        {  # 2. MSQ (single dimension, multiple correct)
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
        {  # 3. Multi-dimension objective (the tedious case the import solves)
            "title": "Screenshot QA — Spotify",
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
        {  # 4. Subjective - Justification (no rubric; graded on prompt)
            "title": "Explain idempotency",
            "question_type": "subjective_justification",
            "prompt": "Explain what idempotency means for a REST API and why "
                      "it matters.",
            "description": "Free-text answer, LLM-graded against the prompt.",
            "category": "Engineering",
            "skills": "API Design",
            "difficulty": "hard",
            "time_minutes": "8",
        },
        {  # 5. Subjective - Rubric (graded against a rubric/pass-condition)
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
        {  # 6. Image A/B (multi-dimension answer key + 2 images + reasoning)
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
            "dim1_label": "Instruction Following",
            "dim1_options": "Response A|Response B|Both Good|Both Bad",
            "dim1_correct": "Response B",
            "dim2_label": "Visual Quality",
            "dim2_options": "Response A|Response B|Both Good|Both Bad",
            "dim2_correct": "Response B",
            "dim3_label": "Less AI Generated",
            "dim3_options": "Response A|Response B|Both Good|Both Bad",
            "dim3_correct": "Both Good",
            "dim4_label": "Overall Choice",
            "dim4_options": "Response A|Response B|Both Good|Both Bad",
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
        {  # 7. Image - Prompt/Labelling (1 image + optional objective gate
           #    dimensions + a textual answer key for the written labelling)
            "title": "Label the UI screenshot",
            "question_type": "image_text",
            "prompt": "Identify the app, confirm the boxes, then describe what "
                      "each numbered box does.",
            "description": "Objective gate checks + a free-text labelling "
                           "answer graded against a textual key.",
            "category": "Image Eval",
            "skills": "App Identification|Prompt Writing",
            "difficulty": "medium",
            "time_minutes": "6",
            "dim1_label": "Do you know this Application?",
            "dim1_options": "Yes|No",
            "dim1_correct": "Yes",
            "dim2_label": "Application",
            "dim2_options": "Spotify|Deezer|SoundCloud|Tidal",
            "dim2_correct": "Spotify",
            "rubric_json": json.dumps({
                "ideal_answer": "Box 1 = search; Box 2 = play/pause; Box 3 = "
                                "library.",
                "mandatory_elements": ["search", "play", "library"],
                "penalty_rules": ["No mention of unrelated controls"],
                "scoring_guide": "Full marks if every numbered box is described "
                                 "correctly.",
            }, ensure_ascii=False),
            "img1_slot": "single",
            "img1_label": "Screenshot",
            "img1_url": "https://picsum.photos/seed/ui/512",
        },
    ]
