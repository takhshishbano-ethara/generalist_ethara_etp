# Generation & Scoring Flow — `etp_assessment_pro`

**Module version:** 19.0.1.72.0 (`__manifest__.py:3`)
**Generated:** 2026-07-13
**Purpose:** End-to-end reference for the two LLM pipelines (question **generation** and subjective **scoring**), naming exactly which prompt/`.md` file and which inline directive feeds the model at each step, and which Python code owns the deterministic parts.

> All paths and `file:line` citations are relative to the module root `custom_addons/etp_assessment_pro/`. Vertex AI Gemini is the only model backend; `services/vertex.py` is the shared API transport.

---

## 1. Overview

### The two pipelines

| Pipeline | Trigger | Entry point | Model call | Output |
|---|---|---|---|---|
| **Generation** | Admin uploads a SOP on a Prompt, clicks *Generate* | `generate_questions_from_sop` (`services/vertex.py:1106`) via cron `_cron_generate_from_sop` (`models/prompt.py:249`) | ONE multimodal `generateContent` per run | draft questions (`etp.assessment.pro.prompt.question`) |
| **Scoring** | Candidate submits → admin runs *Subjective Evaluation* | `score_evaluator` / `_score_submission` (`services/scoring.py:808`, `:844`) via cron `_cron_llm_auto_score` (`models/assessment.py:545`) | ONE `generateContent` per candidate submission (sub-batched) | immutable `llm_raw_100` per response |

Two smaller, independent generation sub-flows share the transport:
- **Tag extraction** — `extract_tags_from_sop` (`services/vertex.py:1243`), its own system prompt `_TAG_SYSTEM_PROMPT` (`:1220`), cron `_cron_extract_tags` (`models/prompt.py:329`). Independent of question generation.
- **Image detection** — `detect_image_elements` (`services/vertex.py:636`) for `image_label`, prompts `_DETECT_PROMPT`/`_UI_PROMPT` (`:586`,`:599`), cron `_cron_detect_image_labels`.

### Every prompt / inline directive at a glance

| Artifact | Kind | Location | Governs |
|---|---|---|---|
| `prompts/question.md` | bundled system prompt | file | Generation: 7 question types, self-contained HARD RULE, JSON-array output contract, image-first steering, swap-test, `image_ab` cross-set verdict distribution |
| `prompts/scoring.md` | bundled system prompt | file | Scoring: `subjective-judge-v6` rubric contract, weights, ceilings, gates, per-type grading, platform-side enforcement notes |
| `INLINE_QUESTION_PROMPT` | inline fallback | `services/vertex.py:30` | Generation system prompt **only if** `question.md` is missing/empty |
| `DEFAULT_SCORING_PROMPT` | inline fallback | `services/scoring.py:27` | Scoring system prompt **only if** `scoring.md` is missing/empty |
| generic SOP directive | inline user-part directive | `services/vertex.py:1127-1144` | Appended in a non-forced generation run: "follow the format in the SOP", question-type policy (image majority for visual SOPs), JSON-only |
| `_SELF_CONTAINED_RULE` | inline directive | `services/vertex.py:46` | Appended to every generation directive: anti-leak (never cite the SOP/source) |
| `_forced_type_directive` + `_FORCED_TYPE_SPEC` | inline directive | `services/vertex.py:1095`, `:1079` | When admin sets **Force Question Type**: pins every item to one type + that type's required answer-key shape |
| `_SOURCE_LEAK_CORRECTION` | inline directive | `services/vertex.py:56` | Regenerate-on-leak correction text (see §2 note) |
| `_TAG_SYSTEM_PROMPT` | inline system prompt | `services/vertex.py:1220` | Tag-extraction sub-flow |
| `_DETECT_SYSTEM_PROMPT` / `_DETECT_PROMPT` / `_UI_PROMPT` | inline prompts | `services/vertex.py:581`,`586`,`599` | `image_label` box detection (objects vs UI elements) |
| `_image_type_contract` / `_image_question_directive` / `_image_contracts_note` | inline directive | `services/vertex.py:698` | image_specs OUTPUT CONTRACTS. `_image_question_directive` pins a forced image run to one image type; `_image_contracts_note` appends the contracts to the generic multi-type directive. Both carry image_ab's `flaw_plan`/`construction_keys` contract so image_ab items are well-formed (Phase-3 flaw injection active) |

### The prompt-override mechanism

Both bundled prompts can be overridden by an `ir.config_parameter` value, resolved at call time:

| Bundled file | Config-parameter key (override) | Resolver | Precedence |
|---|---|---|---|
| `prompts/question.md` | `etp_assessment_pro.question_prompt` | `_get_question_prompt` (`services/vertex.py:259`) | override → bundled `question.md` → `INLINE_QUESTION_PROMPT` |
| `prompts/scoring.md` | `etp_assessment_pro.scoring_system_prompt` | `_get_scoring_prompt` (`services/scoring.py:46`) | override → bundled `scoring.md` → `DEFAULT_SCORING_PROMPT` |

The override is uploaded as a `.md` in **Settings → System Prompts** (`views/res_config_settings_views.xml:127-163`); the setting stores the file text into the config parameter (the scoring `res.config.settings` field is `etp_assessment_pro_scoring_prompt`, persisting to the key `etp_assessment_pro.scoring_system_prompt`). Clearing the parameter reverts to the bundled file.

**Why prompts and code must stay in sync:** each system prompt defines the model's **output contract** (the JSON shape). The Python layer parses that exact shape — generation via `_extract_json_array` + `_validate_question_item` (`services/vertex.py:559`,`:942`); scoring via `_parse_results` keyed on `item_id`/`id` and the v6 field names (`services/scoring.py:622`). Editing a prompt changes what the model emits; if the shape drifts from what the parser expects, items are silently dropped (generation) or fall back to error handling (scoring). `services/scoring.py:_build_item` is explicitly "aligned 1:1 with prompts/scoring.md GRADING BY TYPE so prompt and code never drift" (`:567`).

### Model selection

| Task | Selector | Default | Never uses |
|---|---|---|---|
| Question generation (reads the SOP document) | `_generation_model` (`services/vertex.py:1016`) → `etp_assessment_pro.generation_model` | `GENERATION_DEFAULT_MODEL = "gemini-3.1-pro-preview"` (`constants.py:102`) | the image model (a PDF is opaque binary to it) |
| Scoring | default configured model via `_call_vertex` (`services/vertex.py:380`) → `etp_assessment_pro.vertex_model` | `VERTEX_DEFAULT_MODEL = "gemini-3-pro-image"` (`constants.py:97`) | — |
| Image render | `_vertex_image_model` (`services/vertex.py:103`) = configured `vertex_model` | `gemini-3-pro-image` | — |
| Box detection | `_detection_model` (`services/vertex.py:631`) → `etp_assessment_pro.detection_model`, else generation model | generation model | — |

> Per `memory/vertex-model-availability.md`: in project `spots-437321`, `gemini-3.1-pro` returns 404 — use `gemini-3.1-pro-preview` (the shipped generation default).

---

## 2. Generation flow (step-by-step)

### Steps

1. **SOP upload.** Admin creates a Prompt (`etp.assessment.pro.prompt`), uploads SOP/vendor/client files as resources (`resource_ids`, `models/prompt.py:30`, onchange helpers `:149-179`). Optional: **Additional Notes** (`source_text`), a **Sample Questions** file (`sample_questions_file`, `:112`), a target count (`sop_question_count`, `:118`), and **Force Question Type** (`force_question_type`, `:121`).
2. **Queue.** `action_generate_from_sop` (`models/prompt.py:225`) sets `sop_gen_state="queued"`, `state="generating"`, and returns a notification. No LLM call happens on the web request (avoids the "cursor already closed" crash).
3. **Cron drain.** `_cron_generate_from_sop` (`models/prompt.py:249`) takes a **session advisory lock** `ADVISORY_LOCK_QUESTION_GEN` (`constants.py:37`), picks ≤2 queued prompts, `commit`s before the slow call, then calls `generate_questions_from_sop`. A 429 re-queues (transient); other errors set `sop_gen_state="failed"`.
4. **Assemble the request** — `generate_questions_from_sop` (`services/vertex.py:1106`):
   - **System prompt** = `_get_question_prompt` → `question.md` (or the `question_prompt` override) (`:1121`).
   - **Model** = `_generation_model` (`:1122`) — always the document-capable model, never the image model.
   - **User parts** (`:1145-1156`): the SOP files sent **natively** as base64 `inlineData` (`_sop_doc_parts` `:1056`, mime map + `%PDF` guard `_inline_doc_part` `:1032`); then `ADDITIONAL NOTES` if any; then the native Sample-Questions doc if any; then the **directive** (last part).
   - **Directive** (`:1123-1144`):
      - If **Force Question Type** is a valid type → `_forced_type_directive(forced, count, ab_dims)`: "Generate EXACTLY N … EVERY item's question_type MUST be `<type>`" + that type's required shape (text types from `_FORCED_TYPE_SPEC`; an **image** type instead carries its full image_specs contract from `_image_question_directive`, so a forced `image_ab` run is instructed to emit a `flaw_plan`) + `_SELF_CONTAINED_RULE`.
      - Else → the **generic SOP directive**: follow the format in the SOP (and sample if attached), approximate count, allowed types = `QUESTION_TYPE_PROMPT_LIST`, the **image-majority policy** for visual SOPs, JSON-only, + `_image_contracts_note(ab_dims)` (the image_specs OUTPUT CONTRACTS, incl. image_ab's `flaw_plan`) + `_SELF_CONTAINED_RULE`.
5. **Model call.** `_call_vertex` (`services/vertex.py:380`) with `response_json=True`, `_GEN_MAX_OUTPUT_TOKENS=32000`, `temperature=0.5`. Built-in MAX_TOKENS / unparseable-JSON single retry with doubled budget (`:449-477`).
6. **Parse.** `_extract_json_array` (`:559`) — accepts a bare array or an object wrapping it under `skills/items/questions/data/results` (`_unwrap_json_list` `:541`).
7. **Per-item pipeline** (`:1167-1213`), for each dict item:
   - Resolve `qtype` (forced type wins; else the item's `question_type` if valid, else `mcq`).
   - **Anti-leak drop:** `_item_cites_source` (`:67`, uses `text_has_source_reference` `constants.py:134`) — items citing the SOP/section/guidelines are dropped.
   - **Validate:** `_validate_question_item` (`:942`) returns contract violations per type (mcq/msq option+answer resolution, subjective rubric keys, image specs); violating items are skipped.
   - **Build draft values:** image types → `_build_image_draft_fields` (`:826`) sets answer-key + `image_brief_json`, and `image_state="pending"` when briefs exist; text types → options/correct/rubric/official_reasoning JSON.
   - Create `etp.assessment.pro.prompt.question`.
8. **Image render (async).** Cron `_cron_render_pending_images` (`data/cron.xml:35`) renders `image_brief_json` → images via `render_draft_images` (`:881`) → `generate_image` (`:493`, image model, `_image_brief` wrapper `:765`). Under `ADVISORY_LOCK_IMAGE_RENDER`.
9. **Box detection (async, image_label).** Cron `_cron_detect_image_labels` (`data/cron.xml:45`) runs `detect_image_elements` (`:636`) → stores `detections_json` (and behavioural key) on the source image.
10. **Review + approve.** `action_approve` (`models/prompt.py:954`): guards that image drafts have a rendered image, creates the bank `etp.assessment.pro.question`, copies `flaw_plan_json` (`:980`), materializes dimensions/images, and for a flaw-injected `image_ab` runs the **approve-time key-drift guard** `_assert_no_key_drift` (`:994`) — refuses approval if the materialized answer key diverges from `construction_keys`.

### `image_ab` flaw-injection — where it actually lives

The **consumption / guard** side is fully live: `_build_image_draft_fields` accepts a `flaw_plan` (`services/vertex.py:832-835`) → `_build_flaw_injected_ab_fields` (`:787`) randomly assigns the flawed image to slot a/b (defeats position bias), derives the per-dimension key from `construction_keys` (`ab_specs_from_construction_keys` `constants.py:328`), and persists `flaw_plan_json`. `validate_flaw_plan` (`constants.py:278`) enforces the invariant (flawed side never wins any dimension; OC names the clean side). Approve-time and score-time drift guards (`models/prompt.py:994`, `services/scoring.py:392`) hard-fail on divergence.

### Findings / deviations from the outline (verify)

- **The image_ab `flaw_plan` contract is now wired into generation (v19.0.1.73.0).** `_image_type_contract` holds the per-type image_specs contract; `_image_question_directive` (forced single image type) and `_image_contracts_note` (generic multi-type run) both append the image_ab `flaw_plan`/`construction_keys` contract to the directive, and `prompts/question.md` documents it too. The model is therefore instructed to emit a `flaw_plan`, image_ab items pass `_validate_question_item`, and `_build_image_draft_fields` takes the flaw branch → `flaw_plan_json` persisted (Phase-3 active). The legacy `image_a_prompt`/`image_b_prompt` non-flaw branch remains as a fallback for any item without a `flaw_plan`.
- `INLINE_QUESTION_PROMPT` (`:30`) and the `_SOURCE_LEAK_CORRECTION` regenerate loop (`:56`) exist but are fallback/legacy: the SOP flow uses the bundled/override system prompt and does a single generation call (the multi-attempt anti-leak regeneration is not invoked in `generate_questions_from_sop`; per-item leak filtering via `_item_cites_source` is what runs). Marked here as **verify** if you intend to rely on them.

### Tag-extraction sub-flow (independent)

`action_extract_tags` (`models/prompt.py:308`) → `_cron_extract_tags` (`:329`, lock `ADVISORY_LOCK_TAG_EXTRACT`) → `extract_tags_from_sop` (`services/vertex.py:1243`): system prompt `_TAG_SYSTEM_PROMPT` (`:1220`), SOP sent natively, existing tag vocabulary injected so the model reuses tags verbatim. Cheap call (`max_tokens=4096`, `temperature=0.2`). Returns 4–8 prefixed kebab-case tags (`domain:/task:/skill:/modality:/output-format:`). Purely for the "similar generators" ranking; has no effect on question generation or scoring.

---

## 3. Scoring flow (step-by-step)

### Objective vs subjective split

- **Objective (`mcq`/`msq`) — graded inline, no LLM, deterministic.** `_compute_score` (`models/assessment.py:1492`): all-correct-dimensions → `score=1` else `0`, `max_score=1`.
- **Subjective (`subjective_justification`, `subjective_rubric`, `image_ab`, `image_prompt`, `image_label`) — never inline.** `_compute_scoring_kind` (`:1471`) sets `needs_llm`. On submit, `action_submit` (`:1521`) calls `_enqueue_subjective_scoring` (`:1552`), which only sets `llm_state` and (optionally) flags the evaluator; the actual Vertex call is admin-triggered.

### Steps

1. **Submit.** `action_submit` (`models/assessment.py:1521`) → `_enqueue_subjective_scoring` (`:1552`):
   - A **verdict-only `image_ab`** (`require_justification_image_comparison` off, or blank justification — `_image_ab_uses_llm` `:1398`) is settled immediately and deterministically via `_store_ab_verdict_only` (no Vertex).
   - Everything else needing the LLM → `llm_state="pending"`.
2. **Admin trigger.** `action_llm_score_all` (`:502`) flags evaluators `scoring_requested=True, llm_state="pending"`. (Auto-queue when the assessment has `llm_auto_score` on, `:161`.)
3. **Cron drain.** `_cron_llm_auto_score` (`:545`, lock `ADVISORY_LOCK_AUTOSCORE`) picks ≤20 requested evaluators, calls `ev.action_llm_score` (`:1002`) → `scoring_svc.score_evaluator` (`services/scoring.py:808`).
4. **Per candidate** — `score_evaluator` (`:808`) collects the candidate's `needs_llm` responses, splits verdict-only `image_ab` out (`:821-826`), and sends the rest to `_score_submission` (`:844`) in sub-batches of `scoring_batch_size` (default 8, `:69`).
5. **Integrity gates FIRST (pre-LLM)** — inside `_score_submission` (`:851-874`), per response, before any Vertex call:
   - `image_ab` verdict-only → `_store_ab_verdict_only` (`:464`).
   - `image_ab` with a flaw plan → score-time drift guard `_ab_key_drift` (`:392`); on drift → `_store_ab_key_drift` (raw 0 + `key_drift` flag, `:406`).
   - `evaluate_gates` (`services/gates.py:31`): `empty_answer` (blank) or `injection_attempt` (matches `INTEGRITY_GATE_PATTERNS` `constants.py:156`) → `_store_gated` (`:756`) writes raw 0 and **skips the LLM** for that answer.
6. **Build items.** For the remaining gradable responses, `_build_item` (`:567`) builds one submission item per response, shaped 1:1 with `scoring.md` GRADING BY TYPE (rubric block / answer key / behavioural rubric per type).
7. **One Vertex call.** `_call_vertex` (`services/vertex.py:380`) with system prompt `_get_scoring_prompt` (`scoring.py:46`, → `scoring.md` or override), `temperature=0.2`, `response_json=True`, budget `1200 + 800*len(items)` (`:891`). ONE call per candidate submission (per sub-batch).
8. **Parse.** `_parse_results` (`:622`) — accepts the v6 wrapper object (`results[]`) or a bare array; results keyed back to responses by `id`/`item_id` (`_result_id` `:656`).
9. **Compose the immutable raw score.** Per response, before the single write:
   - Per-type lane adjustment (see below) mutates `it["score"]`.
   - `_store_scored` (`:707`): `_coerce_100` (`:645`, 0–1 → 0–100) → `_apply_ceilings` (`:675`) → compose `llm_raw_100` and write it **exactly once**, with the full v6 audit in `llm_result_json`. Pass/fail and the earned mark are **not** written here.
10. **Pass/fail derives LIVE.** `_compute_subjective_marks` (`models/assessment.py:1429`) compares the immutable `llm_raw_100` against the **live** per-assessment `subjective_threshold` (`:81`, default 70). Changing the threshold re-decides pass/fail with no re-scoring; `_cron_recompute_subjective_results` (`:219`) updates large assessments within a minute.

### Per-type scoring lanes

| Type | Lane | Code | Rule |
|---|---|---|---|
| `subjective_justification` | rubric generated from prompt+skill | `_build_item` `:583` | graded by `scoring.md` v6 |
| `subjective_rubric` | supplied rubric loaded unchanged | `_build_item` `:587` | graded by `scoring.md` v6 |
| `image_ab` (verdict-only) | **deterministic verdict, NO LLM** | `_score_ab_verdicts` `:430`, `_store_ab_verdict_only` `:464` | mean exact-match of per-dimension picks vs keyed verdicts → `raw = verdict*100` |
| `image_ab` (with justification) | **two-lane blend** | `_blend_ab_justification` `:494` | `raw = 0.75*verdict + 0.25*justification` (`AB_VERDICT_WEIGHT`/`AB_JUSTIFICATION_WEIGHT` `constants.py:9`); grader scores justification only |
| `image_prompt` | rubric from mandatory elements/penalties vs `ideal_prompt` | `_image_prompt_rubric` `:156`, `_build_item` `:597` | graded against `ideal_prompt` |
| `image_label` | **coverage × correctness** | `_apply_image_label_coverage` `:527` | correctness = grader 0–1; coverage = attempted/total boxes; **coverage < 0.5 → cap 40**. Rubric prefers the DOM **behavioural key** (`_image_label_behavioural_rubric` `:246`), else detections (`_image_label_rubric` `:196`), else `ideal_labels` |
| flaw-injected `image_ab` | **key-drift guard** | `_ab_key_drift` `:392` | score 0 + `key_drift` flag when stored key ≠ `construction_keys` |

Sub-scores (`ab_scores`, `label_scores`) live in `llm_result_json` **audit only** (`_ab_scores_audit` `:451`; `label_scores` set in `:549`) — never in a stored mutable mark field.

### Ceilings and integrity signals (`_apply_ceilings` `:675`, `SCORE_CEILINGS` `constants.py:183`)

A ceiling only ever **lowers** the raw score, only when its trigger is present in the v6 result:
- `verdict_consistency == "contradiction"` → cap **25**.
- `checklist_zero_count >= 2` → cap **55**.
- `fabrication_count >= 1` OR a fabricated/hallucinated flag → cap **25**.

`_store_scored` also raises `integrity_alert` in the audit flags for a `wrong_item` or `injection_attempt` gate (`:718`); an honest `empty_answer` stays clean. The response-level `integrity_alert` computed field (`models/assessment.py:1299`) surfaces `integrity_alert`/`key_drift` for the UI without touching the score.

---

## 4. What instruction lives where / how to change behaviour

| To change… | Edit | Notes |
|---|---|---|
| The 7 question types, self-contained rule, output JSON shape, image-first steering, swap-test, cross-set verdict spread | `prompts/question.md` (or the `etp_assessment_pro.question_prompt` override) | Changes the model's **output contract**; keep in sync with `_validate_question_item` / `_build_image_draft_fields` |
| Forced-type behaviour / per-type required answer shape | `_FORCED_TYPE_SPEC` + `_forced_type_directive` (`services/vertex.py:1079`,`:1095`) | Only active when **Force Question Type** is set |
| The generic "follow the SOP format" + image-majority policy | generic SOP directive (`services/vertex.py`) | Inline, non-forced runs |
| The image_specs OUTPUT CONTRACTS (esp. image_ab `flaw_plan`/`construction_keys`) | `_image_type_contract` + `_image_question_directive` (forced) + `_image_contracts_note` (generic) (`services/vertex.py:698`) | Wired into both directive paths; keep in sync with `validate_flaw_plan` / `prompts/question.md` |
| Which model reads the SOP / renders / detects | `etp_assessment_pro.generation_model` / `vertex_model` / `detection_model`; defaults in `constants.py:97-102` | Generation must stay document-capable |
| The v6 grading rubric, weights, ceilings, gates, per-type grading | `prompts/scoring.md` (or `etp_assessment_pro.scoring_system_prompt` override) | Keep in sync with `_build_item` and `_parse_results` |
| Deterministic scoring lanes (AB verdict blend, image_label coverage cap, key drift) | `services/scoring.py` (`_score_ab_verdicts`, `_blend_ab_justification`, `_apply_image_label_coverage`, `_ab_key_drift`) | Platform-side; the prompt cannot override these |
| Pre-LLM injection/empty gates | `constants.INTEGRITY_GATE_PATTERNS` (`:156`) + `services/gates.py` | Deterministic, runs before the grader |
| Post-LLM ceilings | `constants.SCORE_CEILINGS` (`:183`) + `_apply_ceilings` | Only lower the score |
| The pass bar | `subjective_threshold` on the assessment (`models/assessment.py:81`) | Live; re-decides pass/fail without re-scoring |

**Invariant to preserve:** prompt files control the **output shape**; Python controls **parsing, the deterministic lanes, gates, ceilings, and the immutable-raw / live-threshold** design. `llm_raw_100` is written exactly once by `_store_scored`; pass/fail is always a live computation.

---

## 5. Glossary

- **`llm_raw_100`** — the immutable 0–100 grader score for one subjective response, written once by `_store_scored` (`services/scoring.py:707`). All pass/fail and mark derivation reads it live; it is never mutated by a threshold change.
- **`subjective_threshold`** — per-assessment pass bar 0–100 (default 70, `models/assessment.py:81`). Compared live against `llm_raw_100` in `_compute_subjective_marks` (`:1429`).
- **`construction_keys` / `flaw_plan_json`** — the `image_ab` flaw-injection plan (`flawed_side`, `clean_prompt`, `flawed_prompt`, `injected_flaws`, `construction_keys`). The per-dimension answer key is **derived** from `construction_keys` (ground-truth by construction). Validated by `validate_flaw_plan` (`constants.py:278`), guarded at approve (`models/prompt.py:994`) and score time (`services/scoring.py:392`).
- **`detections_json` / `behavioural_key_json`** — per-box data on an `image_label` source image: detected boxes (`detect_image_elements`) and the DOM behavioural key (element + functionality). Drive the `image_label` rubric (`services/scoring.py:196`,`:246`).
- **`integrity_alert`** — advisory flag (not a score) raised on `injection_attempt`/`wrong_item` gates and `key_drift`. Response-level computed field `models/assessment.py:1299`.
- **Gates** — pre-LLM deterministic screens (`services/gates.py`): `empty_answer` and `injection_attempt` resolve to raw 0 and skip the Vertex call.
- **Ceilings** — post-LLM defensive caps (`SCORE_CEILINGS`, `_apply_ceilings`) that can only lower `llm_raw_100` when a v6 trigger signal is present.
