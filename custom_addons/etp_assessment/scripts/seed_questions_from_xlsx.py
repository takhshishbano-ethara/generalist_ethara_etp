#!/usr/bin/env python3
"""Seed etp.assessment.question records from an XLSX.

Runs in the Odoo shell (`env` is provided by the shell context). Reads a
spreadsheet whose columns are:
    Sequence | Title | Question Type | Category | Prompt | Description |
    Dimensions | Response A URL | Response B URL |
    Response Image A | Response Image B

For each row it:
  - finds-or-creates the Category by name
  - creates the Question (auto-mapping the question_type label)
  - splits the "Dimensions" cell on newlines/commas, finds-or-creates each
    master Dimension, ensures it has Yes/No options, links it to the
    question (option_line_ids are auto-populated by the model), and marks
    "Yes" as the correct option (a sensible default for "did the edit
    succeed?" style image-comparison questions; tweak per-row in the UI
    afterwards).

Usage:
    src/odoo-bin shell -c src/odoo.conf -d <DB> --log-level=error \
        < custom_addons/etp_assessment/scripts/seed_questions_from_xlsx.py

Re-running is safe: existing categories/dimensions are reused, but
questions are always inserted as new rows (titles aren't unique in the
model, so we don't de-dupe). To re-seed cleanly, manually delete prior
runs from the Question Bank UI first.
"""
import openpyxl

XLSX_PATH = (
    "/Users/apple/Desktop/clowley/ethara-etp/"
    "Assessment Question (etp.assessment.question) (2).xlsx"
)

TYPE_MAP = {
    "image comparison": "image_comparison",
    "text": "text",
    "coding": "coding",
    "image + text": "image_text",
    "image text": "image_text",
    "video": "video",
}

Category = env["etp.assessment.category"]
Dimension = env["etp.assessment.dimension"]
DimensionOption = env["etp.assessment.dimension.option"]
Question = env["etp.assessment.question"]
QuestionDimension = env["etp.assessment.question.dimension"]


def _get_or_create_category(name):
    name = (name or "").strip() or "Uncategorized"
    cat = Category.search([("name", "=", name)], limit=1)
    return cat or Category.create({"name": name})


def _get_or_create_dimension(name):
    """Find / create a master Dimension by name with default Yes/No options."""
    name = (name or "").strip()
    if not name:
        return None
    dim = Dimension.search([("name", "=", name)], limit=1)
    if not dim:
        dim = Dimension.create({"name": name})
    if not dim.option_ids:
        DimensionOption.create([
            {"name": "Yes", "dimension_id": dim.id, "sequence": 10},
            {"name": "No", "dimension_id": dim.id, "sequence": 20},
        ])
    return dim


def _split_dimensions(cell):
    if not cell:
        return []
    s = str(cell)
    parts = s.split("\n") if "\n" in s else s.split(",")
    return [p.strip() for p in parts if p.strip()]


wb = openpyxl.load_workbook(XLSX_PATH, data_only=True)
ws = wb.active
rows = list(ws.iter_rows(values_only=True))
headers = [str(h or "").strip() for h in rows[0]]
print(f"loaded {len(rows) - 1} rows from {XLSX_PATH}")

created = 0
skipped = 0
errors = []
for i, row in enumerate(rows[1:], start=2):
    data = dict(zip(headers, row))
    title = (data.get("Title") or "").strip()
    if not title:
        skipped += 1
        continue
    try:
        qtype = TYPE_MAP.get(
            (data.get("Question Type") or "").strip().lower(), "text")
        cat = _get_or_create_category(data.get("Category"))
        vals = {
            "name": title,
            "sequence": int(data.get("Sequence") or 10),
            "question_type": qtype,
            "prompt": (data.get("Prompt") or "").strip(),
            "description": (data.get("Description") or "").strip(),
            "category_id": cat.id,
            "image_a_url": (data.get("Response A URL") or "").strip() or False,
            "image_b_url": (data.get("Response B URL") or "").strip() or False,
        }
        question = Question.create(vals)
        seq = 10
        for dim_name in _split_dimensions(data.get("Dimensions")):
            dim = _get_or_create_dimension(dim_name)
            if not dim:
                continue
            qd = QuestionDimension.create({
                "question_id": question.id,
                "dimension_id": dim.id,
                "sequence": seq,
            })
            seq += 10
            yes_opt = qd.option_line_ids.filtered(
                lambda l: (l.name or "").strip().lower() == "yes")[:1]
            if yes_opt:
                yes_opt.is_correct = True
        created += 1
    except Exception as exc:
        errors.append(f"row {i} '{title[:40]}': {exc}")

env.cr.commit()

print(f"created={created}  skipped(no title)={skipped}  errors={len(errors)}")
for e in errors[:20]:
    print("  -", e)
print()
print("=== sample created (most recent 5) ===")
for q in Question.search([], order="id desc", limit=5):
    dims = ", ".join(q.question_dimension_ids.mapped("dimension_id.name"))
    print(f"  #{q.id:5d}  {q.question_type:18s}  cat={q.category_id.name!r}"
          f"  dims=[{dims}]  title={q.name!r}")
print("done.")
