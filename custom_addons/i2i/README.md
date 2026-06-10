# I2I - Image-to-Image QC

Odoo 19 module for the Generalist Image-to-Image (I2I) QC project.

## What it does

Captures image-pair QC submissions (Original Image URL, Edited Image URL,
Instruction) and three independent quality ratings. The tasker fills the
ratings; an LLM layer (OpenRouter + `google/gemini-3.5-flash`) runs in
parallel and surfaces its own verdicts to the Manager during review.
The Manager has final approve / reject authority.

### Form fields captured

1. **Project Type** (dropdown — 5 FLUX2 values)
2. **Instruction** (text)
3. **Original Image URL** (text)
4. **Edited Image URL** (text)
5. **Does the edit make only the instructed change?** (`Instruction Aligned` / `No`)
6. **Are the two images aligned?** (`Images Aligned` / `No`)
7. **Are both images free of AI slop?** (`Slop Free` / `No`)

### Operational features

- Recommended Flow: tasker fills ratings **independently**, then clicks
  **Send to Human QC** which transitions state to `human_qc` and queues
  the LLM call. The cron picks up pending items every 2 minutes.
- QC view: dedicated Pending QC list, Manager Approve / Reject with
  mandatory remark on reject.
- LLM & Comparison tab (Manager-only): tasker-vs-LLM side-by-side per
  question, `has_disagreement` highlight, LLM reasoning + cost / tokens.
- Bulk import: CSV / xlsx upload (one row per item) under
  **I2I → Configuration → Import Items**.
- Image Preview: click-to-expand lightbox on each image, plus a
  **Flip Compare** button that overlays Original ↔ Edited (press `F` to
  toggle).
- Tasker guidance baked into the Human Ratings tab: Golden Rule
  ("If unsure, choose No"), 12-area inspection checklist, and 6 AI slop
  categories with concrete examples.

### State machine

`draft` → (tasker fills ratings) → **Send to Human QC** → `human_qc`
→ (Manager) → `approved` | `rejected`

LLM runs in parallel after `Send to Human QC` and writes its verdicts
into the `llm_*` mirror fields without touching `state`.

## Dependencies

**Odoo:** `base`, `web`, `mail`

**Python:** `cryptography`, `requests`, `openpyxl`

## Environment

| Variable | Purpose |
|---|---|
| `I2I_ENCRYPTION_KEY` | Fernet key encrypting the OpenRouter API key. Generate via `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`. Dev auto-generates and persists. |
| `I2I_ENCRYPTION_KEY_PREVIOUS` | Optional, for key-rotation overlap. |

## Install

```bash
python src/odoo-bin -c odoo.conf -i i2i --stop-after-init
```

Then **Settings → I2I**: paste the OpenRouter API key. Default model is
`google/gemini-3.5-flash`. A green check (`✓ API key configured`) appears
once a key is stored.

## Usage

1. **Tasker** opens **I2I → Projects → New** and fills Project Type,
   Instruction, Original / Edited Image URLs.
2. Tasker fills the three Human Ratings **independently** (following the
   in-form Golden Rule + 12-area checklist).
3. Tasker clicks **Send to Human QC**. State moves to `human_qc` and the
   LLM is queued (`llm_status = pending`).
4. The cron (`I2I: Run pending LLM QC reviews`, every 2 min) calls
   OpenRouter, parses the JSON contract from `qc_system_prompt.md`, and
   populates `llm_edit_only_instructed`, `llm_images_aligned`,
   `llm_free_of_ai_slop`, `llm_reasoning`, `llm_cost_usd`, etc.
5. **Manager** opens **I2I → QC Queue**, reviews:
   - Tasker ratings vs LLM ratings side-by-side (Manager-only tab),
   - `has_disagreement` alert when tasker and LLM differ,
   - Flip-compare lightbox on the Image Preview,
   - LLM reasoning + auto-fail codes + per-axis findings.
6. Manager clicks **Approve** or **Reject** (remark mandatory on reject).
7. Bulk import: **I2I → Configuration → Import Items** for CSV / xlsx
   uploads.

## LLM System Prompt

The LLM prompt is held in `qc_system_prompt.md` at the module root
(loaded once at runtime by `services/llm_qc_client.py`, cached for the
process lifetime). It defines:

- 12 inspection surfaces (face, hair, hands_fingers, text, shadows,
  reflections, etc.)
- 3 ground-truth verdicts (`q1`, `q2`, `q3`) with `auto_fail_code` per
  question (e.g. `Q2-SHIFT`, `Q3-ENV`)
- Strict JSON output contract consumed by `llm_qc_client.review_image_pair`
- LABEL mode (no anchoring — tasker's submission is never shown to the
  LLM)

To change the LLM behaviour, edit `qc_system_prompt.md` and restart Odoo
to clear the module-level cache. The file is **not** declared in
`__manifest__.py` data (it's a Python runtime resource).

## Security

Two groups only:

- **I2I User** — tasker. Sees items where `user_id = uid`,
  `assigned_user_id = uid`, or member of the item's project. No access
  to LLM tab, Configuration menu, or Approve / Reject.
- **I2I Manager** — implies User. Sees company-wide, can approve /
  reject, edit Settings, run Import, and view the LLM & Comparison tab.

`qc_reviewer_id` is a field that records who clicked Approve / Reject —
**not** a security group.
