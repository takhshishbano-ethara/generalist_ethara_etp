# Generation & Scoring Flow - `etp_assessment_pro`

**Module version:** 19.0.1.119.0 (`__manifest__.py:3`)
**Regenerated:** 2026-07-17 (rewritten line-for-line against current source; supersedes the v72 revision)
**Purpose:** End-to-end reference for the three LLM pipelines - question **generation** (text + image + video), and subjective **scoring** - naming exactly which prompt/`.md` file and which inline directive feeds the model at each step, and which Python code owns the deterministic parts.

> All paths and `file:line` citations are relative to the module root `custom_addons/etp_assessment_pro/`. Vertex AI is the only model backend (**Gemini** for text/image, **Veo** for video); `services/vertex.py` is the shared API transport.

## What changed since the v72 revision (delta)

- **`video_prompt` is a full generation + scoring pipeline now.** The generator emits a `video_prompt` draft whose `image_specs` carries per-clip briefs; async **Veo** ops render the clips; scoring grades the candidate's written prompt through the **same lane as `image_prompt`** (against `ideal_prompt`). See §2.5 and §3.
- **`subjective_justification` was removed** (migration `19.0.1.89.0`); `subjective_rubric` (with an optionally empty rubric) is the only subjective text type.
- **The forced-type mechanism became an allow-list.** `generate_questions_from_sop(..., allowed_types=())` (`vertex.py:2108`) takes a tuple of permitted types; the directive is built by `_allowed_types_directive` (`vertex.py:2076`). The legacy scalar `force_question_type` survives only as a fallback that collapses the allow-list to one type.
- **Scoring is v10, not v6.** The grader still returns one JSON object per submission, but the score is **re-derived deterministically** by `_recompute_v10` (`scoring.py:764`) from structured verdicts (`golden_claims` / `elements` / `clarity`) - the judge's own arithmetic is not trusted. Weights: `key_closeness = 0.70·deciding + 0.30·supporting`; `score = 0.60·key_closeness + 0.25·sop_coverage + 0.15·clarity`.
- **Tag extraction is inline (no cron).** `action_extract_tags` → `_run_tag_extract_inline` runs on the web request; migration 98.0 removed `ir_cron_extract_tags`.

## Generation self-heal + authoring-harness import (v122.0)

Two harness-aligned additions layer on top of the base generation run in `generate_questions_from_sop` (`services/vertex.py`). Both are opt-out via `ir.config_parameter` and never change the persisted-draft schema.

- **Self-heal robustness layer** (`_selfheal_generation`, runs after the base batch + terse-retry, before the create loop). Three best-effort stages, each wrapped so a failure keeps the base batch intact:
  - **Top-up** (`_topup_items`): when a run comes up short of the requested `count` (early truncation), request only the shortfall - passing the already-authored titles so the model does not duplicate, and targeting `required_elements` no item covers yet (`_uncovered_elements`, read from the generator's `required_elements_json`). Loops up to 3 times.
  - **Backfill** (`_backfill_solutions`): solutions are emitted LAST in the envelope, so a big-metadata SOP that truncates drops them first. This regenerates answer keys from the finalized questions on a dedicated bounded call, so every item ends with a key.
  - **Critique** (`_critique_revise`): a strict second-opinion pass on the assembled answer keys against the SOP; only a fully 1:1-aligned correction set is auto-applied (never a partial remap that could mis-key), and item/prompt issues are logged. This is the discipline that catches a wrong/ambiguous or keyless objective question (H-11 class) *before* candidates sit it.
  - Config: `etp_assessment_pro.gen_selfheal` (default `1`; `0` disables top-up + backfill) and `etp_assessment_pro.gen_critique` (default `1`; `0` disables the critique pass). Each stage logs to `usageMetadata` under `operation=generate_questions` with a `note` of `topup`/`backfill_solutions`/`critique`.

- **Authoring-harness import** (`import_bank_harness`, `models/bank_import.py`). The standalone harness (`/Users/cj/Developer/harness`) is an offline authoring twin using the SAME Vertex models. It emits a run folder in the Opus seed-prompt schema (`questions.json` + `solutions.json`, or a combined `output.json`). `import_bank_harness` maps that into review **drafts** on a generator (never live questions), attaching answer keys from the harness solutions and surfacing image/video assets by URL when fetchable (local run-folder files are reported in warnings for a manual upload). The **Import Question Bank** wizard fingerprints a harness upload (`_try_harness_payload`) and routes to it ahead of the CSV / native round-trip paths.

---

## 1. Overview

### The three pipelines

| Pipeline | Trigger | Entry point | Model call | Output |
|---|---|---|---|---|
| **Generation** | Admin uploads a SOP on a generator, clicks *Generate from SOP* | `generate_questions_from_sop` (`services/vertex.py:2108`) via cron `_cron_generate_from_sop` (`models/prompt.py:432`) | ONE multimodal `generateContent` per run (+ one terse-retry if it parses to nothing) | draft questions (`etp.assessment.pro.prompt.question`) |
| **Media render (async)** | A draft needs images or video | image: `render_draft_images` (`vertex.py:1439`) via `_cron_render_pending_images` (`prompt.py:2153`); video: `submit_video_op`/`fetch_video_op` (`vertex.py:596`/`:671`) via `_cron_poll_video_ops` (`prompt.py:2418`) | image: one `generateContent` per brief; video: async Veo long-running op per clip | rendered images / `video_files_json` |
| **Scoring** | Candidate submits → admin runs *Subjective Evaluation* | `score_evaluator` / `_score_submission` (`services/scoring.py:947`, `:976`) via cron `_cron_llm_auto_score` (`models/assessment.py:536`) | ONE `generateContent` per candidate submission (sub-batched) | immutable `llm_raw_100` per response |

Two smaller generation sub-flows share the transport:
- **Tag extraction** - `extract_tags_from_sop` (`vertex.py:2311`), its own system prompt `_TAG_SYSTEM_PROMPT`, run INLINE from `action_extract_tags` (`prompt.py:583`). No cron. Independent of question generation.
- **Image box detection** - `detect_image_elements` (`vertex.py:815`) for `image_label`, cron `_cron_detect_image_labels` (`question_image.py`).

### Every prompt / inline directive at a glance

| Artifact | Kind | Location | Governs |
|---|---|---|---|
| `prompts/question.md` | bundled system prompt | file | Generation: the 7 question types, self-contained HARD RULE, JSON-array output contract, image/video-first steering, `image_ab` flaw-injection (3-prompt/per-dimension `flaw_plan`), `video_prompt` clip briefs |
| `prompts/scoring.md` | bundled system prompt | file | Scoring: the **v10** judge contract - golden-claim decomposition, `key_closeness`/`sop_coverage`/`clarity` weights, self-applied ceilings, per-type grading |
| `INLINE_QUESTION_PROMPT` | inline fallback | `vertex.py:37` | Generation system prompt **only if** `question.md` is missing/empty |
| generic SOP directive | inline user-part directive | `vertex.py:2134-2154` | Non-allow-listed run: "follow the format in the SOP", image-majority policy for visual SOPs, envelope reminder, JSON-only |
| `_allowed_types_directive` | inline directive | `vertex.py:2076` | Allow-listed run: pins every item to the permitted `allowed_types` + each type's required shape |
| `_forced_type_directive` | inline directive | `vertex.py:1912` | Legacy single-type fallback (allow-list collapsed to one type) |
| `_SELF_CONTAINED_RULE` | inline directive | `vertex.py` | Anti-leak: never cite the SOP/source. Appended to every generation directive |
| `_image_type_contract` / `_image_question_directive` / `_image_contracts_note` | inline directive | `vertex.py:873`,`:1056`,`:1065` | image_specs OUTPUT CONTRACTS incl. `image_ab` `flaw_plan`/`construction_keys` and `video_prompt` clip briefs |
| `_TAG_SYSTEM_PROMPT` | inline system prompt | `vertex.py` | Tag-extraction sub-flow |
| `_DETECT_*` prompts | inline prompts | `vertex.py:815`+ | `image_label` box detection (objects vs UI elements) |

### The prompt-override mechanism

Both bundled prompts can be overridden by an `ir.config_parameter` value, resolved at call time:

| Bundled file | Config-parameter key (override) | Resolver | Precedence |
|---|---|---|---|
| `prompts/question.md` | `etp_assessment_pro.question_prompt` | `_get_question_prompt` (`vertex.py:260`) | override → bundled `question.md` → `INLINE_QUESTION_PROMPT` |
| `prompts/scoring.md` | `etp_assessment_pro.scoring_system_prompt` | `_get_scoring_prompt` (`scoring.py:35`) | override → bundled `scoring.md` → `DEFAULT_SCORING_PROMPT` |

The override is uploaded as a `.md` in **Settings → System Prompts**; clearing the parameter reverts to the bundled file.

**Why prompts and code must stay in sync:** each system prompt defines the model's **output contract** (the JSON shape). The Python layer parses that exact shape - generation via `_extract_json_array` + `_validate_question_item` (`vertex.py:732`,`:1665`); scoring via `_parse_results` keyed on `item_id`/`id` and the **v10** field names (`scoring.py:635`). Editing a prompt changes what the model emits; if the shape drifts, items are silently dropped (generation) or fall back to error handling (scoring). `scoring._build_item` (`:562`) is shaped 1:1 with `prompts/scoring.md` GRADING BY TYPE so prompt and code never drift, and `_recompute_v10` (`:764`) re-derives the score from the structured verdicts rather than trusting the judge's emitted number.

### Model selection

| Task | Selector | Default | Never uses |
|---|---|---|---|
| Question generation (reads the SOP document) | `_generation_model` (`vertex.py:1778`) → `etp_assessment_pro.generation_model` | `GENERATION_DEFAULT_MODEL = "gemini-3.1-pro-preview"` (`constants.py:94`) | the image model (a PDF is opaque binary to it) |
| Scoring | `_scoring_model` (`vertex.py:1784`) → `etp_assessment_pro.vertex_model` | `VERTEX_DEFAULT_MODEL = "gemini-3-pro-image"` (`constants.py:90`) | - |
| Image render | `_vertex_image_model` (`vertex.py:106`) = configured `vertex_model` | `gemini-3-pro-image` | - |
| **Video render** | `_video_model` (`vertex.py:552`) → `etp_assessment_pro.video_model`, on `_video_location` (`:557`) | `VIDEO_DEFAULT_MODEL = "veo-3.1-generate-001"` @ `us-central1` (`constants.py:98-99`) | the `global` location (Veo 404s there) |
| Box detection | `_detection_model` (`vertex.py:810`) → `etp_assessment_pro.detection_model`, else generation model | generation model | - |

---

## 2. Generation flow (step-by-step)

### Steps

1. **SOP upload.** Admin creates a generator (`etp.assessment.pro.prompt`), uploads SOP/vendor/client files as `resource_ids`. Optional: **Additional Notes** (`source_text`), a **Sample Questions** file (`sample_questions_file`, sent natively), a target count (`sop_question_count`), an **allow-list** (`allowed_question_type_ids`), and the legacy **Force Question Type** fallback (`force_question_type`).
2. **Queue.** `action_generate_from_sop` (`prompt.py:412`) sets `sop_gen_state="queued"`, `state="generating"`, returns a notification. No LLM call on the web request (avoids "cursor already closed").
3. **Cron drain.** `_cron_generate_from_sop` (`prompt.py:432`) takes a **session advisory lock** `ADVISORY_LOCK_QUESTION_GEN` (827201), picks ≤2 queued generators, `commit`s before the slow call, then calls `generate_questions_from_sop(env, prompt, count=…, allowed_types=prompt._allowed_question_types())` (`prompt.py:453`). A 429 re-queues (transient); other errors set `sop_gen_state="failed"`.
4. **Assemble the request** - `generate_questions_from_sop` (`vertex.py:2108`):
   - **System prompt** = `_get_question_prompt` → `question.md` (or the `question_prompt` override) (`:2117`).
   - **Model** = `_generation_model` (`:2118`) - always the document-capable model, never the image model.
   - **Allow-list validation** (`:2119-2126`): unknown types in `allowed_types` raise `ValueError` (fails the run loudly rather than authoring garbage).
   - **Directive** (`:2129-2154`):
     - If `allowed_types` is set → `_allowed_types_directive(allowed, count, ab_dims)` + facet-vocabulary note.
     - Else → the **generic SOP directive**: follow the SOP's format (and the sample if attached), approximate count, allowed types = `QUESTION_TYPE_PROMPT_LIST`, the **image-majority policy** for visual SOPs, `_ENVELOPE_REMINDER`, `_image_contracts_note(ab_dims)` (image_specs contracts incl. `image_ab` `flaw_plan` and `video_prompt` clip briefs), `_SELF_CONTAINED_RULE`, and the facet-vocabulary note.
   - **User parts** (`:2155-2166`): the SOP files sent **natively** as base64 `inlineData` (`_sop_doc_parts` `:1825`, mime map + `%PDF` guard `_inline_doc_part` `:1800`); then `ADDITIONAL NOTES`; then the native Sample-Questions doc (`_sample_doc_parts` `:1848`); then the **directive** as the last part.
5. **Model call.** `_call_vertex` (`vertex.py:399`) with `response_json=True`, `_GEN_MAX_OUTPUT_TOKENS=64000`, `temperature=0.5`. Built-in MAX_TOKENS / unparseable-JSON retry with doubled budget up to `_MAX_OUTPUT_TOKENS_CEILING=64000` (`:461-483`).
6. **Empty-result retry.** If the run parses to zero items, ONE retry appends `_TERSE_RETRY_DIRECTIVE` (`:2185-2193`) - a terser-output nudge - before giving up.
7. **Parse.** `_extract_json_array` (`:732`) - accepts a bare array or an object wrapping it under `skills/items/questions/data/results` (`_unwrap_json_list` `:701`), with a truncation-salvage path (`_salvage_json_objects` `:711`). Solutions are pulled out by `_extract_solutions` (`:1927`) and re-attached (`_attach_solutions` `:1949`).
8. **Per-item pipeline** (`:2197-2248`+), for each dict item:
   - **Resolve type:** `_resolve_item_type` (`:2093`) - an item outside the allow-list is **dropped as out-of-scope** (counted in `dropped_out_of_scope`).
   - **Anti-leak drop:** `_item_cites_source` (`:73`) - items citing the SOP/section/guidelines are dropped.
   - **Validate:** `_validate_question_item` (`:1665`) returns contract violations per type; violating items are skipped.
   - **Build draft values:** image/video types → `_build_image_draft_fields` (`:1381`) sets answer-key + `image_brief_json` (and for `video_prompt`, `video_brief_json` via `_video_prompt_briefs` `:1221`, which reuses the `image_prompt` slot logic), and `image_state`/`video_state="pending"` when briefs exist; text types → options/correct/rubric/official_reasoning JSON.
   - Create `etp.assessment.pro.prompt.question`.
9. **Image render (async).** Cron `_cron_render_pending_images` (`prompt.py:2153`) renders `image_brief_json` → images via `render_draft_images` (`vertex.py:1439`) → `generate_image` (`:506`). Under `ADVISORY_LOCK_IMAGE_RENDER` (827194), all-or-nothing.
10. **Video render (async).** Cron `_cron_poll_video_ops` (`prompt.py:2418`, 2-min, `ADVISORY_LOCK_VIDEO_POLL` 827196): `_submit_video_ops` (`prompt.py:2234`) submits one Veo long-running op per clip slot via `submit_video_op` (`vertex.py:596`); **the stored `op_name` is the idempotency handle** - a slot already carrying an `op_name` is never re-submitted (double-bill guard). `fetch_video_op` (`vertex.py:671`) polls; a completed clip lands in `video_files_json` and `video_state` flips `rendered`. Costed via `_estimate_video_cost` (`vertex.py:297`).
11. **Box detection (async, image_label).** Cron `_cron_detect_image_labels` runs `detect_image_elements` (`vertex.py:815`) → stores `detections_json` (+ behavioural key) on the source image.
12. **Review + approve.** `action_approve` (`prompt.py:1371`): guards that image drafts have a rendered image, creates the bank `etp.assessment.pro.question`, copies `flaw_plan_json`, materializes dimensions/images/**videos** (`_materialize_videos` `:2349`), and for a flaw-injected `image_ab` runs the **approve-time key-drift guard** - refuses approval if the materialized answer key diverges from `construction_keys`.

### `image_ab` flaw-injection - where it lives

The **consumption / guard** side is fully live: `_build_flaw_injected_ab_fields` (`vertex.py:1110`) normalizes the plan (`normalize_flaw_plan` `constants.py:218`, which supports both the new 3-prompt `render_prompts`/`planted`/`faithful_side` shape and the legacy `clean_prompt`/`flawed_prompt`/`injected_flaws` shape), randomly assigns slots to defeat position bias, derives the per-dimension key from `construction_keys` (`ab_specs_from_construction_keys` `constants.py:318`), and persists `flaw_plan_json`. `validate_flaw_plan` (`constants.py:267`) enforces the invariant (flawed side never wins any dimension; OC names the clean side; `Both Bad` requires both sides flawed). Approve-time and score-time drift guards (`prompt.py`, `scoring.py:393`) hard-fail on divergence. An optional render-time verification loop exists (`verify_planted_flaws` `:1501`, `verify_and_regenerate_ab_images` `:1549`) to re-render images whose planted flaws the model can't actually see.

### Tag-extraction sub-flow (independent, inline)

`action_extract_tags` (`prompt.py:583`) → `_run_tag_extract_inline` → `extract_tags_from_sop` (`vertex.py:2311`): its own `_TAG_SYSTEM_PROMPT`, SOP sent natively, existing tag vocabulary injected so the model reuses tags verbatim. Cheap call. Returns prefixed kebab-case tags (`domain:/task:/skill:/modality:/output-format:`). Purely for the "Similar Projects" ranking; NO effect on question generation or scoring. **Runs synchronously on the button click - there is no tag cron** (removed in migration 98.0).

---

## 3. Scoring flow (step-by-step)

### Objective vs subjective split

- **Objective (`mcq`/`msq`) - graded inline, no LLM, deterministic.** `_compute_score` (`assessment.py:1454`): all-correct-dimensions → `score=1` else `0`, `max_score=1`.
- **Subjective (`subjective_rubric`, `image_ab`, `image_prompt`, `image_label`, `video_prompt`) - never inline.** On submit, `action_submit` (`assessment.py:1483`) calls `_enqueue_subjective_scoring` (`:1514`), which only sets `llm_state` and (optionally) flags the evaluator; the actual Vertex call is admin-triggered.

### Steps

1. **Submit.** `action_submit` → `_enqueue_subjective_scoring` (`assessment.py:1514`):
   - A **verdict-only `image_ab`** (`require_justification_image_comparison` off, or blank justification) is settled immediately and deterministically (no Vertex).
   - Everything else needing the LLM → `llm_state="pending"`.
2. **Admin trigger.** `action_llm_score_all` (`assessment.py:496`) flags evaluators `scoring_requested=True`. (Auto-queue when the assessment has `llm_auto_score` on.)
3. **Cron drain.** `_cron_llm_auto_score` (`assessment.py:536`, session lock `ADVISORY_LOCK_AUTOSCORE` 827193 with `pg_advisory_unlock_all()` at entry) picks ≤20 requested evaluators, calls `ev.action_llm_score` → `scoring.score_evaluator` (`scoring.py:947`).
4. **Per candidate** - `score_evaluator` (`:947`) collects the candidate's `needs_llm` responses and sends them to `_score_submission` (`:976`) in sub-batches of `scoring_batch_size` (default 8, `:54`).
5. **Integrity gates FIRST (pre-LLM)** - inside `_score_submission` (`:979-999`), per response, before any Vertex call:
   - `image_ab` verdict-only → `_store_ab_verdict_only` (`:451`).
   - `image_ab` with a flaw plan → score-time drift guard `_ab_key_drift` (`:393`); on drift → `_store_ab_key_drift` (raw 0 + `key_drift` flag, `:405`).
   - `evaluate_gates` (`services/gates.py`): `empty_answer` (blank) or `injection_attempt` (matches `INTEGRITY_GATE_PATTERNS` `constants.py:140`) → `_store_gated` (`:900`) writes raw 0 and **skips the LLM** for that answer.
6. **Build items.** For the remaining gradable responses, `_build_item` (`:562`) builds one submission item per response, shaped 1:1 with `scoring.md` GRADING BY TYPE. **`image_prompt` and `video_prompt` share one lane** (`:609`): rubric from mandatory elements/penalties vs `ideal_prompt`, candidate answer read from `resp.justification`.
7. **One Vertex call.** `_call_vertex` (`vertex.py:399`) with system prompt `_get_scoring_prompt` (`scoring.py:35`, → `scoring.md` or override), `temperature=0.2`, `response_json=True`. ONE call per candidate submission (per sub-batch).
8. **Parse.** `_parse_results` (`:635`) - accepts the v10 wrapper object (`results[]`) or a bare array, with a truncation-salvage path (`_salvage_truncated_results` `:662`); results keyed back to responses by `id`/`item_id` (`_result_id` `:710`).
9. **Compose the immutable raw score.** Per response, before the single write (`_store_scored` `:833`):
   - Per-type lane adjustment mutates `it["score"]` (image_ab blend, image_label coverage).
   - **`_recompute_v10` (`:764`) re-derives the score** from `golden_claims` (deciding/supporting), `elements` (shown/not_shown → `sop_coverage`), and `clarity`; if the judge's emitted score differs by > 1.5 it flags `needs_review` + `score_recomputed` and records a `recompute_note`.
   - `_coerce_100` (`:700`, 0–1 → 0–100) → `_apply_ceilings` (`:740`) → compose `llm_raw_100` and write it **exactly once**, with the full v10 audit in `llm_result_json` (+ `llm_key_closeness`, `llm_sop_coverage`, `llm_clarity`, `llm_verdict_consistency`, `llm_golden_claims_json`). Pass/fail and the earned mark are **not** written here.
10. **Pass/fail derives LIVE.** `_compute_subjective_marks` (`assessment.py:1399`) compares the immutable `llm_raw_100` against the **live** per-assessment `subjective_threshold` (default 70). Changing the threshold re-decides pass/fail with no re-scoring; `_cron_recompute_subjective_results` (`:213`) updates large assessments within a minute.

### Per-type scoring lanes

| Type | Lane | Code | Rule |
|---|---|---|---|
| `subjective_rubric` | supplied rubric loaded unchanged, else generated | `_build_item` `:598` | graded by `scoring.md` v10 |
| `image_ab` (verdict-only) | **deterministic verdict, NO LLM** | `_score_ab_verdicts` `:426`, `_store_ab_verdict_only` `:451` | mean exact-match of per-dimension picks vs keyed verdicts → `raw = verdict*100` |
| `image_ab` (with justification) | **two-lane blend** | `_blend_ab_justification` `:477` | `raw = 0.75*verdict + 0.25*justification` (`AB_VERDICT_WEIGHT`/`AB_JUSTIFICATION_WEIGHT` `constants.py:8-9`); grader scores justification only |
| `image_prompt` | rubric from mandatory elements/penalties vs `ideal_prompt` | `_image_prompt_rubric` `:126`, `_build_item` `:609` | graded against `ideal_prompt` |
| **`video_prompt`** | **same lane as `image_prompt`** | `_build_item` `:609` | graded against `ideal_prompt` (candidate wrote the generating prompt for the clip) |
| `image_label` | **coverage × correctness** | `_apply_image_label_coverage` `:503`, `_apply_coverage_gate` `:263` | correctness = grader 0–1; coverage = attempted/total boxes; low coverage caps the score. Rubric prefers the DOM **behavioural key** (`_image_label_behavioural_rubric` `:209`), else detections (`_image_label_rubric` `:160`), else `ideal_labels` |
| flaw-injected `image_ab` | **key-drift guard** | `_ab_key_drift` `:393` | score 0 + `key_drift` flag when stored key ≠ `construction_keys` |

Sub-scores live in `llm_result_json` **audit only** (`_ab_scores_audit` `:440`) - never in a stored mutable mark field.

### The v10 recompute (why the judge's number isn't trusted)

`_recompute_v10` (`scoring.py:764`) rebuilds the score from the judge's *structured verdicts*, not its emitted `score`:

```
deciding_credit  = avg over deciding claims  (hit=100, partial=50, miss=0)
supporting_credit= avg over supporting claims (same scale)
key_closeness    = 0.70*deciding + 0.30*supporting   (=deciding if no supporting)
sop_coverage     = 100 * shown / (shown + not_shown)  (from `elements`, if any)
clarity          = {clear:100, mixed:50, unclear:0}
score            = weighted mean of [key_closeness (0.60), sop_coverage (0.25), clarity (0.15)]
                   over whichever components are present
```

If this differs from the judge's own number by more than 1.5 points, the response is flagged `needs_review` + `score_recomputed` with a `recompute_note` for audit. This is the platform-side defence against a judge that reasons correctly but does the final arithmetic wrong.

### Ceilings and integrity signals (`_apply_ceilings` `:740`, `SCORE_CEILINGS` `constants.py:154`)

A ceiling only ever **lowers** the raw score, only when its trigger is present in the v10 result:
- `verdict_consistency == "contradiction"` → cap **25**.
- `checklist_zero_count >= 2` → cap **55**.
- `fabrication_count >= 1` OR a fabricated/hallucinated flag → cap **25**.

`_store_scored` also raises `integrity_alert` in the audit flags for a `wrong_item` or `injection_attempt` gate (`:855`); an honest `empty_answer` stays clean. The response-level `integrity_alert` computed field surfaces `integrity_alert`/`key_drift` for the UI without touching the score.

---

## 4. What instruction lives where / how to change behaviour

| To change… | Edit | Notes |
|---|---|---|
| The 7 question types, self-contained rule, output JSON shape, image/video-first steering, `image_ab` flaw-injection, `video_prompt` clip briefs | `prompts/question.md` (or the `etp_assessment_pro.question_prompt` override) | Changes the model's **output contract**; keep in sync with `_validate_question_item` / `_build_image_draft_fields` |
| Which types a run may author | `allowed_question_type_ids` on the generator (M2M into the seeded `question.type` vocabulary) | Enforced by `_allowed_types_directive` + `_resolve_item_type`; the legacy `force_question_type` collapses it to one type |
| The generic "follow the SOP format" + image-majority policy | generic SOP directive (`vertex.py:2134`) | Inline, non-allow-listed runs |
| The image_specs OUTPUT CONTRACTS (esp. `image_ab` `flaw_plan`/`construction_keys`, `video_prompt` briefs) | `_image_type_contract` + `_image_question_directive` + `_image_contracts_note` (`vertex.py:873`+) | Keep in sync with `validate_flaw_plan` / `prompts/question.md` |
| Which model reads the SOP / renders images / renders video / detects | `etp_assessment_pro.generation_model` / `vertex_model` / `video_model` / `detection_model`; defaults `constants.py:90-99` | Generation must stay document-capable; Veo must stay on its regional location |
| The v10 grading contract, weights, ceilings, gates, per-type grading | `prompts/scoring.md` (or `etp_assessment_pro.scoring_system_prompt` override) | Keep in sync with `_build_item`, `_parse_results`, AND `_recompute_v10` weights |
| Deterministic scoring lanes (AB verdict blend, image_label coverage cap, key drift, v10 recompute) | `services/scoring.py` | Platform-side; the prompt cannot override these |
| Pre-LLM injection/empty gates | `constants.INTEGRITY_GATE_PATTERNS` (`:140`) + `services/gates.py` | Deterministic, runs before the grader |
| Post-LLM ceilings | `constants.SCORE_CEILINGS` (`:154`) + `_apply_ceilings` | Only lower the score |
| The pass bar | `subjective_threshold` on the assessment (`assessment.py:80`) | Live; re-decides pass/fail without re-scoring |

**Invariant to preserve:** prompt files control the **output shape**; Python controls **parsing, the deterministic lanes, gates, ceilings, the v10 recompute, and the immutable-raw / live-threshold** design. `llm_raw_100` is written exactly once by `_store_scored`; pass/fail is always a live computation.

---

## 5. Glossary

- **`llm_raw_100`** - the immutable 0–100 grader score for one subjective response, written once by `_store_scored` (`scoring.py:833`). All pass/fail and mark derivation reads it live; never mutated by a threshold change.
- **v10 recompute** - the platform re-derives the score from the judge's structured verdicts (`_recompute_v10` `scoring.py:764`) rather than trusting its emitted number; a > 1.5-point divergence flags `needs_review`/`score_recomputed`.
- **`subjective_threshold`** - per-assessment pass bar 0–100 (default 70). Compared live against `llm_raw_100` in `_compute_subjective_marks` (`assessment.py:1399`).
- **`construction_keys` / `flaw_plan_json`** - the `image_ab` flaw-injection plan. The per-dimension answer key is **derived** from `construction_keys` (ground-truth by construction). Validated by `validate_flaw_plan` (`constants.py:267`), guarded at approve and score time.
- **Veo op / `op_name`** - an async Vertex video-generation long-running operation; its `op_name` is persisted as the idempotency handle so a `video_prompt` clip slot is never re-submitted (double-bill guard). Submitted/polled by `_cron_poll_video_ops`.
- **Allow-list** - `allowed_question_type_ids`, the generator's Many2many restricting which of the 7 types a generation run may author; enforced both in the directive and by `_resolve_item_type` (out-of-scope items are dropped).
- **Gates** - pre-LLM deterministic screens (`services/gates.py`): `empty_answer` and `injection_attempt` resolve to raw 0 and skip the Vertex call.
- **Ceilings** - post-LLM defensive caps (`SCORE_CEILINGS`, `_apply_ceilings`) that can only lower `llm_raw_100` when a v10 trigger signal is present.
