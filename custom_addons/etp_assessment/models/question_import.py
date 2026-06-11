# -*- coding: utf-8 -*-
"""Question Bank CSV import wizard.

Imports questions of ALL types in one CSV, including their dimensions and
the correct option per dimension — the two things Odoo's generic import
cannot express on this schema (the question.dimension link auto-populates
its option lines on create; correct options must be flagged afterwards).

CSV columns (header row required, order free):
  name*            question title
  question_type*   image_comparison | text | coding | image_text | video
  prompt*          full question text
  description      extra context (text / image_text)
  category         category NAME (created if missing; falls back to wizard default)
  image_a_url      image_comparison / image_text
  image_b_url      image_comparison
  code_snippet     coding
  code_language    python|javascript|java|csharp|cpp|go|rust|other
  video_url        video
  sequence         integer (default 10)
  active           true/false (default true)
  dimensions       pipe-separated dimension NAMES, e.g. "Accuracy|Clarity"
                   (must exist as master dimensions; options auto-populate)
  correct_options  per-dimension correct option, e.g.
                   "Accuracy:Yes|Clarity:Excellent"
"""
import base64
import csv
import io
import logging

from odoo import models, fields, api
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

TRUE_VALUES = {"1", "true", "yes", "y", "t"}


class EtpAssessmentQuestionImport(models.TransientModel):
    _name = "etp.assessment.question.import"
    _description = "Question Bank CSV Import Wizard"

    csv_file = fields.Binary(string="CSV File", required=True)
    csv_filename = fields.Char(string="Filename")
    default_category_id = fields.Many2one(
        "etp.assessment.category",
        string="Default Category",
        help="Used for rows that leave the 'category' column empty.",
    )
    create_missing_dimensions = fields.Boolean(
        string="Create Missing Dimensions",
        default=False,
        help="If a row references a dimension that does not exist, create it "
             "(without options) instead of failing the row. Off = strict.",
    )
    result_summary = fields.Text(string="Result", readonly=True)
    state = fields.Selection(
        [("draft", "Upload"), ("done", "Done")], default="draft"
    )

    # ------------------------------------------------------------------
    def _parse_csv(self):
        try:
            data = base64.b64decode(self.csv_file)
            text = data.decode("utf-8-sig")  # tolerate Excel BOM
            reader = csv.DictReader(io.StringIO(text))
        except Exception:
            raise UserError("Invalid CSV file. Please upload a valid UTF-8 CSV.")
        if not reader.fieldnames:
            raise UserError("CSV file appears to be empty.")
        # normalize headers (strip + lowercase)
        reader.fieldnames = [(f or "").strip().lower() for f in reader.fieldnames]
        required = {"name", "question_type", "prompt"}
        missing = required - set(reader.fieldnames)
        if missing:
            raise UserError(
                "CSV is missing required column(s): %s. Found: %s"
                % (", ".join(sorted(missing)), ", ".join(reader.fieldnames))
            )
        return list(reader)

    def _resolve_category(self, row_value, cache):
        name = (row_value or "").strip()
        if not name:
            return self.default_category_id
        if name in cache:
            return cache[name]
        Category = self.env["etp.assessment.category"]
        category = Category.search([("name", "=", name)], limit=1)
        if not category:
            category = Category.create({"name": name})
        cache[name] = category
        return category

    def _resolve_dimensions(self, dim_spec, row_num, errors):
        """'Accuracy|Clarity' -> etp.assessment.dimension recordset."""
        Dimension = self.env["etp.assessment.dimension"]
        dims = Dimension.browse()
        for dname in (d.strip() for d in (dim_spec or "").split("|")):
            if not dname:
                continue
            dim = Dimension.search([("name", "=", dname)], limit=1)
            if not dim:
                if self.create_missing_dimensions:
                    dim = Dimension.create({"name": dname})
                else:
                    errors.append(
                        "Row %d: dimension '%s' not found (enable 'Create "
                        "Missing Dimensions' or create it first)." % (row_num, dname)
                    )
                    continue
            dims |= dim
        return dims

    @staticmethod
    def _parse_correct_options(spec):
        """'Accuracy:Yes|Clarity:Excellent' -> {'accuracy': 'yes', ...}"""
        out = {}
        for pair in (p.strip() for p in (spec or "").split("|")):
            if not pair or ":" not in pair:
                continue
            dim, opt = pair.split(":", 1)
            out[dim.strip().lower()] = opt.strip().lower()
        return out

    # ------------------------------------------------------------------
    def action_import(self):
        self.ensure_one()
        rows = self._parse_csv()
        Question = self.env["etp.assessment.question"]
        QDim = self.env["etp.assessment.question.dimension"]
        valid_types = dict(
            Question._fields["question_type"].selection
        )

        created = []
        errors = []
        cat_cache = {}

        for row_num, row in enumerate(rows, start=2):
            get = lambda key: (row.get(key) or "").strip()  # noqa: E731

            name = get("name")
            qtype = get("question_type").lower()
            prompt = get("prompt")
            if not name or not prompt:
                errors.append("Row %d: 'name' and 'prompt' are required." % row_num)
                continue
            if qtype not in valid_types:
                errors.append(
                    "Row %d: invalid question_type '%s'. Allowed: %s"
                    % (row_num, qtype, ", ".join(valid_types))
                )
                continue

            category = self._resolve_category(get("category"), cat_cache)
            if not category:
                errors.append(
                    "Row %d: no category given and no Default Category set "
                    "on the wizard." % row_num
                )
                continue

            dims = self._resolve_dimensions(get("dimensions"), row_num, errors)

            vals = {
                "name": name,
                "question_type": qtype,
                "prompt": prompt,
                "description": get("description") or False,
                "category_id": category.id,
                "image_a_url": get("image_a_url") or False,
                "image_b_url": get("image_b_url") or False,
                "code_snippet": get("code_snippet") or False,
                "video_url": get("video_url") or False,
                "active": (get("active").lower() in TRUE_VALUES) if get("active") else True,
            }
            lang = get("code_language").lower()
            if lang:
                allowed_langs = dict(Question._fields["code_language"].selection)
                vals["code_language"] = lang if lang in allowed_langs else "other"
            seq = get("sequence")
            if seq.isdigit():
                vals["sequence"] = int(seq)

            question = Question.create(vals)

            # dimensions: create() auto-populates option lines from the master
            correct_map = self._parse_correct_options(get("correct_options"))
            dim_seq = 10
            for dim in dims:
                qd = QDim.create({
                    "question_id": question.id,
                    "dimension_id": dim.id,
                    "sequence": dim_seq,
                })
                dim_seq += 10
                wanted = correct_map.get((dim.name or "").lower())
                if wanted:
                    line = qd.option_line_ids.filtered(
                        lambda l: (l.name or "").lower() == wanted
                    )[:1]
                    if line:
                        line.is_correct = True
                    else:
                        errors.append(
                            "Row %d: correct option '%s' not found among "
                            "options of dimension '%s' (question imported "
                            "without the correct flag)."
                            % (row_num, wanted, dim.name)
                        )
            created.append(question.id)

        summary = "%d question(s) imported." % len(created)
        if errors:
            summary += "\n\n%d issue(s):\n%s" % (len(errors), "\n".join(errors))
        _logger.info(
            "Question CSV import: %d created, %d errors", len(created), len(errors)
        )
        self.write({"result_summary": summary, "state": "done"})
        return {
            "type": "ir.actions.act_window",
            "res_model": self._name,
            "res_id": self.id,
            "view_mode": "form",
            "target": "new",
        }

    def action_download_template(self):
        self.ensure_one()
        header = (
            "name,question_type,prompt,description,category,image_a_url,"
            "image_b_url,code_snippet,code_language,video_url,sequence,active,"
            "dimensions,correct_options\n"
        )
        sample = (
            '"Sunset render comparison","image_comparison","Which render '
            'better follows the brief?","","Image Eval",'
            '"https://example.com/a.png","https://example.com/b.png",'
            '"","","",10,true,"Accuracy|Composition",'
            '"Accuracy:Response A|Composition:Response A"\n'
            '"Binary search review","coding","Evaluate this implementation.",'
            '"","Coding","","","def bsearch(arr, t): ...","python","",20,true,'
            '"Correctness","Correctness:Yes"\n'
        )
        attachment = self.env["ir.attachment"].create({
            "name": "question_import_template.csv",
            "type": "binary",
            "datas": base64.b64encode((header + sample).encode("utf-8")),
            "mimetype": "text/csv",
        })
        return {
            "type": "ir.actions.act_url",
            "url": f"/web/content/{attachment.id}?download=true",
            "target": "new",
        }
