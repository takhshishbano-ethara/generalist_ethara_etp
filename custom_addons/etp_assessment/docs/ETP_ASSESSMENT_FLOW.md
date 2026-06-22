# ETP Assessment — Module Knowledge Graph & Working Flow

> Standalone reference for the `custom_addons/etp_assessment` Odoo 19 module.
> Every field / method / route / menu name below was verified against the
> source. Where this doc differs from earlier informal notes, the code wins —
> the **"Corrections vs. prior notes"** section at the end lists the deltas.

---

## 1. Overview & Purpose

`etp_assessment` is an **LLM-driven assessment platform** built on **Vertex AI
Gemini**. It does two things:

1. **Phase 1 — Question Bank Generation.** An admin uploads source documents
   (SOP / Vendor / Client), the LLM **extracts skills**, then **generates
   question drafts** per skill. Drafts are reviewed and **approved** into a
   reusable Question Bank.
2. **Phase 2/3 — Assessment Lifecycle.** An admin builds a **multi-day plan**,
   assigns **candidates** (`hr.applicant` + portal users), launches/invites,
   and candidates take proctored exams in the **website portal**. Objective
   questions auto-score in pure Python; subjective answers are scored by a
   batched Vertex call. Results roll up per day and per candidate.

| Manifest fact | Value |
|---|---|
| `name` | ETP Assessment |
| `version` | **19.0.3.0.0** |
| `application` | `True` |
| `license` | LGPL-3 |
| `depends` | `base`, `mail`, `hr`, `hr_recruitment`, `employee_extension`, `website` |
| `external_dependencies` (python) | `PyJWT`, `httpx`, `boto3`, `cryptography` |
| frontend asset | `static/src/scss/portal.scss` |

Source: `__manifest__.py:1`.

**Candidate identity is `hr.applicant`** (NOT `hr.employee`). Each candidate is
linked to a **portal `res.users`** via `hr.applicant.candidate_user_id` (a field
defined in the `employee_extension` module, not in this module — see
`employee_extension/models/employee_candidate.py:35`).

---

## 2. Data Model Map

### 2.1 Models at a glance

| Model | File | Purpose |
|---|---|---|
| `etp.assessment` | `models/assessment.py:36` | Assessment container + lifecycle, proctoring rules, candidate assignment, CSV import, results export, LLM-score trigger |
| `etp.assessment.evaluator` | `models/assessment.py:614` | Per-candidate assignment row (the materialized candidate); rolls up score & result |
| `etp.assessment.response` | `models/assessment.py:906` | Per-question answer; objective `_compute_score` + subjective LLM fields |
| `etp.assessment.response.line` | `models/assessment.py:1196` | One picked option per dimension (`selected_option_id` → master option) |
| `etp.assessment.day` | `models/assessment_day.py:28` | Plan row: one day, bound to a Skill or Category |
| `etp.assessment.day.session` | `models/assessment_day.py:164` | Per-candidate × per-day execution unit; state machine + token |
| `etp.assessment.skill` | `models/skill.py:4` | First-class Skill Bank (UNIQUE name) |
| `etp.assessment.category` | `models/category.py:4` | Question category (+ "Add from Bank" picker) |
| `etp.assessment.dimension` (+ `.option`) | `models/dimension.py:4,34` | Master scoring dimension + master options |
| `etp.assessment.question` | `models/question.py:4` | Question Bank entry |
| `etp.assessment.question.dimension` (+ `.option`) | `models/question_dimension.py:5,79` | Per-question dimension link + per-question options carrying `is_correct` |
| `etp.assessment.prompt` | `models/prompt.py:9` | One LLM generation session (resources + drafts) |
| `etp.assessment.prompt.resource` | `models/prompt.py:377` | Uploaded source file (text extracted on create/write) |
| `etp.assessment.prompt.skill` | `models/prompt.py:215` | Transient view of what THIS run extracted |
| `etp.assessment.prompt.question` | `models/prompt.py:251` | Draft question awaiting approve/deny |
| `etp.assessment.bank.import` (AbstractModel) | `models/bank_import.py:19` | JSON question-bank importer |

### 2.2 Relationship diagram

```
                         etp.assessment.prompt  (LLM generation session)
                          | resource_ids   | skill_bank_ids (M2M)   | question_ids (drafts)
                          v                 v                        v
            prompt.resource         etp.assessment.skill <----+   prompt.question (draft)
            (SOP/Vendor/Client)      ^  (UNIQUE name)         |        | approve
                                     | skill_ids (M2M)        |        v
                                     |                        |   etp.assessment.question  (BANK)
                                     +--- question_ids (M2M) -+        | category_id (M2O, single)
                                                                       | question_dimension_ids (O2M)
                                                                       v
                                                      question.dimension --+-- option_line_ids
                                                       | dimension_id      |     (is_correct, master_option_id)
                                                       v                   v
                                              etp.assessment.dimension  dimension.option (MASTER)

  ASSESSMENT SIDE
  etp.assessment ──┬── evaluator_ids (M2M hr.applicant)   "assigned" candidates (pre-launch)
                   ├── day_ids (O2M)         → etp.assessment.day  (pool_by skill|category)
                   ├── assessment_evaluator_ids (O2M) → etp.assessment.evaluator (materialized candidate)
                   ├── day_session_ids (O2M) → etp.assessment.day.session
                   └── response_ids (O2M)    → etp.assessment.response

  etp.assessment.evaluator ──┬── applicant_id (M2O hr.applicant)  ── candidate_user_id (res.users portal)
                             ├── day_session_ids (O2M)
                             └── response_ids (O2M)

  etp.assessment.day.session ──┬── day_id / evaluator_id / assessment_id
                               └── response_ids (O2M)  → response.line_ids → selected_option_id
```

### 2.3 Key fields per model (selected)

**`etp.assessment`** — `name`, `state` (`draft|in_progress|done|cancelled`),
`assessment_mode` (`single|multi_day`, default **multi_day**; single is dormant),
`num_days` (default 1), `sequential_days` (default True), `results_release`
(`manual|eod|immediate`, default manual), `start_date`, `end_date`,
`evaluator_ids` (M2M `hr.applicant`), proctoring booleans
(`rule_block_tab_switch`, `rule_block_screenshot`, `rule_block_copy_paste`,
`rule_block_right_click`, `rule_block_devtools`, `rule_watermark`,
`rule_fullscreen`, `rule_webcam`), `max_violations`, `violation_action`
(`auto_submit|log_only`), `llm_auto_score`, `candidate_csv_file`.

**`etp.assessment.evaluator`** — `applicant_id`, `access_token` (uuid),
`question_order` (JSON, single-mode), `started_at`, `deadline_datetime`,
`state` (`pending|in_progress|submitted`), `is_locked`, violation fields,
`llm_state` (`pending|scoring|scored|partial|failed`), score roll-ups
(`total_score`, `max_possible_score`, `llm_total_score`, `llm_max_score`,
`subjective_pending`, `score_percent`, `pass_threshold`, `result`),
`results_released`, day-progress (`days_total`, `days_done`,
`day_progress_label`).

**`etp.assessment.day`** — `sequence`, `pool_by` (`skill|category`), `skill_id`,
`category_id`, `question_count`, `available_question_count` (computed via
`_pool_questions`), `duration_minutes`, `scheduled_start`, `question_source`
(`skill_pool|manual`), `question_ids` (manual pick).

**`etp.assessment.day.session`** — `access_token`, `question_order` (JSON),
`total_questions`, `started_at`, `deadline_datetime`, `state`
(`locked|available|in_progress|submitted|scored|missed`), `score`, `max_score`,
`bucket` (`available|upcoming|past`), `score_display`.

**`etp.assessment.skill`** — `name` (UNIQUE), `tags`, `question_type`
(`mcq|msq|subjective_justification|subjective_rubric`), `question_count`
(default 5, labelled **"Questions to Generate"**; hidden from the Skill list
view), `time_minutes` (default 10), `difficulty` (`easy|medium|hard`),
`question_ids` (M2M to bank), `bank_question_count` (computed `len(question_ids)`),
`extracted_from_prompt_id` (M2O back to the source generator; drives
`action_generate_questions`).

**`etp.assessment.question`** — `name`, `question_type`, `prompt`, `description`,
`category_id` (M2O — **single** category), `skill_ids` (**M2M**),
`question_dimension_ids`, `subjective_rubric_json`, `difficulty`, `time_minutes`,
`has_subjective` (computed).

---

## 3. Phase 1 — Question Bank Generation

### 3.1 Flow

```
 etp.assessment.prompt (Generator)
   │  upload SOP / Vendor / Client / Other docs  → prompt.resource (text auto-extracted)
   │      (.docx via zip+XML, .txt/.md/.csv/.html/.json/.xml decoded; .pdf NOT supported)
   ▼
 [Extract Skills]  action_extract_skills → vertex.extract_skills
   │  system prompt = skill_gen.md ; user = compiled resource text
   │  parse JSON array; UPSERT each skill by UNIQUE name into etp.assessment.skill
   │  (existing → skipped; new → created)   + a prompt.skill transient row per item
   ▼
 [Generate Questions]  action_generate_questions → vertex.generate_questions (one call PER selected skill)
   │  system prompt = question.md ; user = source text + skill artifacts
   │  (ALT trigger: from the Skill Bank — skill.action_generate_questions
   │   re-drives the source generator for the picked skills, see §3.2)
   │  → etp.assessment.prompt.question  (state=draft)
   ▼
 [Review]  Pending Approvals screen (all drafts across generators) OR the
   │  generator's own "Question Drafts" tab. Reachable via the "Pending
   │  Approvals" button on the Question Bank control panel.
   ▼
 [Approve]  prompt.question.action_approve →  etp.assessment.question (BANK)
   │  MCQ/MSQ: _materialize_dimension() builds dimension + master options + is_correct flags
   │  description set to False (never leak options/answer to candidate)
   ▼
 Question Bank ready  → Export CSV / JSON (question.action_export_csv / _json)
```

### 3.2 Details

- **Resource buckets.** Uploads land via onchange helpers into `prompt.resource`
  with a `category` of `sop|vendor|client|other` (`models/prompt.py:85-107`).
  Text extraction runs on create/write (`_run_extraction`, `prompt.py:455`).
- **Extract Skills** (`prompt.py:159` → `services/vertex.py:248`). Idempotent
  upsert keyed on `Skill.name` (UNIQUE). Returns `{created, skipped, total}`;
  the prompt stores `last_extract_summary` and links `skill_bank_ids`.
- **Generate Questions** (`prompt.py:187` → `vertex.py:323`). Requires
  `selected_skill_ids`; one Vertex call per skill; clears prior **draft** rows.
- **Approve** (`prompt.py:290`). Reuses/creates the prompt's target category
  (`_get_or_create_category`, `prompt.py:147`, default name `Gen: <prompt name>`).
  For `mcq/msq`, `_materialize_dimension` (`prompt.py:315`) creates a
  `etp.assessment.dimension` + master options, then a `question.dimension` whose
  `option_line_ids` carry `is_correct` for the correct indices. `action_deny` /
  `action_approve_all` also exist.
- **Pending Approvals (consolidated review).** Draft questions
  (`etp.assessment.prompt.question`, `state=draft`) from **every** generator can
  be reviewed in one place via `action_etp_assessment_prompt_question_pending`
  (`views/prompt_views.xml`): a list/form/search on the draft model with per-row
  ✓/✗ buttons, **bulk** "Approve Selected" / "Deny Selected" header buttons, and
  Pending/Approved/Denied + group-by-generator/skill filters (defaults to
  Pending). It is **not** a menu — it is surfaced as a `display="always"`
  control-panel button on the **Question Bank** list, injected by the inherited
  view `etp_assessment_question_tree_pending_button`. (The per-generator
  "Question Drafts" tab approval still works identically.)
- **Generate from the Skill Bank.** `skill.action_generate_questions`
  (`skill.py:55`) lets you generate drafts straight from one or more **Skill**
  rows — a form-header **Generate Questions** button (visible when the skill has
  an `extracted_from_prompt_id` source generator) and a list bulk action
  (`action_generate_skill_questions`). It groups the selected skills by their
  source generator, temporarily sets that generator's `selected_skill_ids`,
  calls the generator's `action_generate_questions`, then restores the prior
  selection in a `finally`. Output is still **drafts** (review/approve as above).
  The skill's `question_count` field is labelled **"Questions to Generate"** and
  `bank_question_count` (`skill.py:44`, computed `len(question_ids)`) shows how
  many approved bank questions the skill already has.
- **Categories vs Skills on a question.** `category_id` is a single M2O;
  `skill_ids` is M2M (`models/question.py:25-34`).
- **"Add from Question Bank"** picker on a category: `add_question_ids` (M2M,
  domain active) + `action_add_questions_from_bank` (`models/category.py:37`)
  — it **reparents** picked questions by writing their `category_id` (a question
  belongs to a single category, so adding MOVES it), then clears the picker.
- **Export.** `question.action_export_csv` / `action_export_json`
  (`models/question.py:108,115`) build an `ir.attachment` and return a download
  `act_url`.
- **JSON importer.** `etp.assessment.bank.import.import_bank` (AbstractModel,
  `models/bank_import.py:23`) ingests a `{project, skillset, question_bank}`
  JSON, infers `question_type` from field types, upserts skills, materializes
  objective dimensions/options and subjective rubric JSON.

### 3.3 JSON-RPC generation API (`controllers/prompt_controller.py`)

| Route | Method | Action |
|---|---|---|
| `/etp/skill_gen/extract` | jsonrpc, auth=user | run extract on a prompt |
| `/etp/skill_gen/skills` | jsonrpc, auth=user | list/search Skill Bank |
| `/etp/question_gen/generate` | jsonrpc, auth=user | generate drafts for skill_ids |
| `/etp/question_gen/drafts/<id>/approve` | jsonrpc, auth=user | approve draft |
| `/etp/question_gen/drafts/<id>/deny` | jsonrpc, auth=user | deny draft |

---

## 4. Candidate Identity & Provisioning

- **Candidate = `hr.applicant`** (`email_from`, `partner_name`).
- A **portal `res.users`** (`base.group_portal`) is linked through
  `hr.applicant.candidate_user_id` (field from `employee_extension`).
- **`_ensure_candidate_user(applicant)`** (`models/assessment.py:346`):
  idempotent; returns `'exists' | 'linked' | 'created' | 'skipped'`.
  - If already linked → `exists`.
  - No email → `skipped`.
  - Finds an existing `res.users` by login=email → `linked`; else **creates** a
    portal user and calls `user.action_reset_password()` (set-password invite) →
    `created`.
  - Sets `applicant.candidate_user_id` and back-fills `applicant.partner_id`.
- **No `hr.employee` is ever created** — candidate email lives only on the
  `hr.applicant`, so the company `@ethara.ai` work-email constraint imposed by
  `task_forge_bridge` is never triggered.
- **CSV import** `action_import_candidates_csv` (`assessment.py:397`): requires
  `name,email` columns; upserts `hr.applicant` by `email_from`, adds to
  `evaluator_ids`, and provisions portal users — reporting counts of
  created/linked/exists/skipped. `action_download_candidate_template`
  (`assessment.py:478`) returns a sample CSV.
- Self-registered candidates already carry `candidate_user_id` (set by
  `employee_extension`'s self-registration controller).

---

## 5. Phase 2/3 — Assessment Lifecycle

`assessment_mode` is forced to **`multi_day`** in the UI (`single` is dormant;
`num_days=1` covers the old single-test case). Single-mode buttons/pages are
hidden (`views/assessment_views.xml:31`).

```
 create assessment (state=draft)
   │  set Schedule (start_date/end_date), num_days, sequential_days, results_release,
   │  proctoring rules, violation policy, llm_auto_score
   ▼
 Assign Candidates           evaluator_ids ← pick hr.applicant OR Import CSV
   ▼
 [Build Day Plan]  action_scaffold_days (assessment_day.py:505)
   │  idempotent; creates missing day rows 1..num_days
   │  scheduled_start = (start_date or now) + (seq-1) days  (SAME time-of-day preserved)
   ▼
 configure each day:  Pool By = Skill | Category, skill_id/category_id,
                      question_count, duration_minutes; available_question_count via _pool_questions
   ▼
 [Launch & Invite]  action_generate_plan (assessment_day.py:537)
   │  validates: every day has a binding + non-empty pool; ≥1 candidate
   │  per candidate: _ensure_candidate_user, get/create evaluator
   │  per candidate × day: create day.session with
   │      access_token (uuid), question_order (shuffled _resolve_question_ids),
   │      total_questions, state
   │  state: day1 (or all in parallel) = available; rest = locked;
   │         future scheduled_start ⇒ locked regardless
   │  _send_day_invitation()  (force_send=True)
   │  assessment.state draft → in_progress
   ▼
 sequential unlock during the run + crons (see §13)
```

- **Date constraints** (`assessment.py:333` `_check_schedule_dates`): in `draft`,
  `start_date` cannot be in the past; `end_date ≥ start_date`. A second
  `_check_dates` (`assessment.py:246`) also enforces `start_date < end_date`.
- **Pool resolution** `etp.assessment.day._pool_questions` (`assessment_day.py:92`):
  Skill mode → `skill.question_ids` filtered active; Category mode → all active
  questions with that `category_id` (may span skills).
- **`_resolve_question_ids`** (`assessment_day.py:143`): shuffles the pool (or the
  manual `question_ids`), truncates to `question_count` (0 = unlimited).
- **Manual mode** validation (`_check_manual_questions`, `assessment_day.py:123`):
  every manual question must belong to the day's pool.

---

## 6. Candidate Exam Flow (Portal)

Routes in `controllers/portal.py`. Multi-day token → `etp.assessment.day.session`;
single token → `etp.assessment.evaluator`.

```
 email "Start this assessment"  → /assessment/day/<token>   (day_landing)
   │
   │ if public user → redirect /web/login?redirect=/assessment/day/<token>   (login gate)
   │ _candidate_guard: logged-in user must == evaluator's candidate_user_id
   │    (mismatch → portal_wrong_candidate; managers w/o link may PREVIEW read-only)
   ▼
 state routing:
   submitted/scored/missed → _render_day_result (score gated by results_release)
   assessment not in_progress → portal_assessment_closed
   locked   → portal_day_locked
   available→ portal_instructions  (confirm-before-start)
   in_progress (deadline passed) → _auto_submit_day_on_expiry → result
   ▼
 POST /assessment/day/<token>/begin   (day_begin)
   │  _is_real_candidate guard (managers can't start the real timer)
   │  action_start_day → state=in_progress, started_at=now, deadline = started_at + duration
   ▼
 per-question page  portal_question_page  (free navigation, ?q=<index>)
   │  MCQ  = radio (one master option id per dimension)
   │  MSQ  = checkboxes → serialized CSV "o1,o2" in dimension_<id>
   │  subjective = textarea (justification)
   │  proctoring JS reads rules_json: tab-switch / copy-paste / right-click /
   │     devtools / screenshot / fullscreen / webcam / watermark
   │     + countdown to deadline_iso → auto-submit on expiry
   │  POST /submit per question (_record_response → response + response.line; action_submit)
   ▼
 /review (portal_review_page: answered/unanswered)  →  POST /finish
   │  day_finish → action_submit_day:
   │     _ensure_unanswered_placeholders (zero-score rows for skipped Qs)
   │     state=submitted → _rollup_day_score → _unlock_next_day → evaluator._rollup_overall_from_days
   ▼
 result page  portal_day_result
   per question: candidate answer (answer_summary) vs correct (correct_summary),
   objective score, + subjective llm_feedback when scored
```

- **Violations** POST to `/assessment/day/<token>/violation` →
  `_record_violation_day` (`portal.py:641`): increments `violation_count`;
  if `violation_action=auto_submit` AND `max_violations` reached → auto-submit.
  Enforcement is **server-side** (client JS is untrusted).
- **Single-mode** mirror routes exist: `/assessment/<token>` (+ `/begin`,
  `/submit`, `/review`, `/finish`, `/violation`) on the evaluator.
- **`/my/assessments`** (`controllers/candidate_portal.py:128`): lists the
  candidate's own day-sessions + single evaluators bucketed into **Available /
  In Progress / Upcoming / Completed**. All reads via `sudo()` scoped to the
  logged-in user's applicant (`_resolve_applicant`).
- **`/my` redirect**: `EtpPortalHome.home` (`candidate_portal.py:218`) sends a
  candidate with ≥1 assessment straight to `/my/assessments`.

---

## 7. Scoring

### 7.1 Objective (mcq / msq) — pure Python, ALL-OR-NOTHING

`response._compute_score` (`assessment.py:1031`):

- Only `mcq`/`msq` score; everything else → `score=0, max_score=0`.
- `max_score` = number of objective dimensions (those with ≥1 `is_correct`
  option). 1 point per dimension.
- For **every** objective dimension the candidate's chosen master-option set
  must **exactly equal** the correct set; any mismatch ⇒ whole question = 0.
- Instant, no LLM. `has_objective` true for `mcq/msq` (`assessment.py:1014`).

### 7.2 Subjective (subjective_justification / subjective_rubric) — Vertex

- `needs_llm` (`assessment.py:1014`): true when type is `subjective_*` AND the
  candidate left a **non-placeholder** justification (not starting with
  `"[Auto-submitted"`).
- `services/scoring.py:score_evaluator` — **ONE batched Vertex call per
  candidate**: bundles all `needs_llm` responses, expects a JSON array of
  `{id, score (0..1), feedback}`.
  - `subjective_justification` → judged against an **empty rubric** (question
    prompt only).
  - `subjective_rubric` → includes the question's rubric (checklist /
    constraints / pass_condition) via `_rubric_text` (`scoring.py:65`).
  - Threshold `subjective_pass_threshold` (default **0.7**; values >1 divided by
    100) decides PASS → `subjective_points` (default **10**) else 0.
  - Writes `llm_state=scored`, `llm_raw_score`, `llm_passed`, `llm_score`,
    `llm_max_score`, `llm_feedback`.
- **Trigger paths:**
  - On submit, `_enqueue_subjective_scoring` (`assessment.py:1099`) — inline
    score now if `llm_auto_score` ON, else mark `pending`.
  - Manual: `action_llm_score_all` (button "Score Subjective (All)") /
    per-evaluator `action_llm_score`.
  - Cron `_cron_llm_auto_score` (`assessment.py:524`) — advisory-locked
    (`pg_try_advisory_xact_lock(827193)`), drains up to 20 evaluators where the
    assessment has `llm_auto_score` ON.
- **Wrong-objective "hints"** on the result page = `correct_summary` (static,
  from option `is_correct`) + `question.description` (static). **Not** LLM.

### 7.3 Roll-ups

| Level | Fields | Source |
|---|---|---|
| `response` | `score`/`max_score` (objective), `llm_score`/`llm_max_score` (subjective) | computed |
| `day.session` | `total_questions`, `answered_count`, `score`, `max_score` (objective+subjective) | `assessment_day.py:283,289` |
| `evaluator` | `total_questions` (dual: sum of day sessions, else `len(question_order)`), `answered_count`, `total_score`, `max_possible_score`, `llm_total_score`, `llm_max_score`, `subjective_pending`, `score_percent`, `result` | `assessment.py:738-775,851` |

- **Result** (`_compute_result`, `assessment.py:749`): `score_percent =
  (objective+subjective earned) / (objective+subjective possible) * 100`.
  `result` stays `pending` unless `state=submitted` AND no subjective pending
  AND possible>0; then `pass` if `score_percent >= pass_threshold`
  (default **70**), else `fail`.
- **Subjective Max scale** = `subjective_points × (# subjective questions)`
  (e.g. 3 × 10 = 30).

---

## 8. Vertex AI / LLM Configuration (`services/vertex.py`)

### 8.1 Auth router (`_gemini_request`, `vertex.py:118`)

Resolution order (bearer **wins** over api_key):

1. **Bearer present** (`_vertex_bearer`, `vertex.py:111`): a direct
   `vertex_access_token`, else a **minted** service-account bearer
   (`_minted_bearer`). → host `https://{location}-aiplatform.googleapis.com`
   (or `https://aiplatform.googleapis.com` when `location == "global"`), path
   `/v1/projects/{project}/locations/{location}/publishers/google/models/{model}:{suffix}`,
   header `Authorization: Bearer …`.
2. **Else api_key**: if it starts with `AQ.` → `aiplatform.googleapis.com`
   publishers path; otherwise (e.g. `AIza…`) →
   `generativelanguage.googleapis.com/v1beta`. Header `x-goog-api-key`.
3. Else raise "Vertex/Gemini not configured".

**Service-account minting** (`_minted_bearer`, `vertex.py:46`): signs a JWT with
**PyJWT** (`RS256`) using the SA `private_key`, exchanges it at `token_uri` for a
1h OAuth bearer via `httpx`; caches token + expiry in `ir.config_parameter`
(`vertex_minted_token`, `vertex_minted_token_expires`) with a 300s safety
margin. Raises a clear error if the wrong `jwt` PyPI package is installed.

### 8.2 Prompt resolution (per generator)

| Prompt | Tiers | Source |
|---|---|---|
| Skill extraction | System Param `etp_assessment.skill_gen_prompt` → bundled `prompts/skill_gen.md` → inline `INLINE_SKILL_GEN_PROMPT` | `vertex.py:174` |
| Question generation | System Param `etp_assessment.question_prompt` → bundled `prompts/question.md` → inline `INLINE_QUESTION_PROMPT` | `vertex.py:183` |
| Subjective scoring | System Param `etp_assessment.scoring_system_prompt` → inline `DEFAULT_SCORING_PROMPT` (**no bundled file**) | `scoring.py:35` |

Bundled prompt files present: **`prompts/skill_gen.md`** and
**`prompts/question.md`** only.

### 8.3 Call mechanics

`_call_vertex` (`vertex.py:192`) POSTs `generateContent` with `systemInstruction`
+ user `contents` + `generationConfig` (`maxOutputTokens`, `temperature`), via
`httpx` (read timeout 180s). `_extract_json_array` strips ``` fences and parses
the first JSON array. Model from `etp_assessment.vertex_model`
(default `gemini-2.5-pro`).

> Note: this module has **no image-generation path** (no `vertex_image_model` /
> Imagen code). The Settings UI exposes only the text/Vertex + S3 + prompt keys.

---

## 9. System Parameters Reference

All keys are `ir.config_parameter` under `etp_assessment.*`. Defaults marked
where the field/XML sets one.

| Key | Default | Purpose |
|---|---|---|
| `vertex_project_id` | — | GCP project |
| `vertex_location` | **`us-central1`** | region (`global` switches host) |
| `vertex_model` | `gemini-2.5-pro` | Gemini model id |
| `vertex_api_key` | — | `AIza…` (Gemini dev) or `AQ.` (Express) key |
| `vertex_access_token` | — | static OAuth bearer (takes precedence) |
| `vertex_service_account_json` | — | SA JSON text (module mints bearers) |
| `vertex_service_account_filename` | — | display name of uploaded SA |
| `vertex_minted_token` | — | cached minted bearer (managed) |
| `vertex_minted_token_expires` | — | epoch expiry of minted bearer (managed) |
| `subjective_points` | `10` | points awarded on subjective PASS |
| `subjective_pass_threshold` | `0.7` | 0..1 quality bar (>1 ⇒ /100) |
| `pass_threshold` | `70` | overall pass % |
| `scoring_system_prompt` | — | override subjective grader prompt |
| `skill_gen_prompt` (+ `_filename`) | — | override skill prompt |
| `question_prompt` (+ `_filename`) | — | override question prompt |
| `s3_bucket` | — | S3 bucket |
| `s3_region` | `us-east-1` | S3 region |
| `s3_access_key_id` / `s3_secret_key` | — | S3 creds |
| `s3_folder` | `etp_assessment` | key prefix |
| `s3_cdn_url` | — | optional CDN base |
| `s3_max_retries` | `3` | upload retry cap |

Plus (Odoo core, used for email): `mail.default.from` + `mail.catchall.domain`,
and `web.base.url` for portal links. Non-secret defaults shipped in
`data/llm_config_parameters.xml`; the Settings UI is
`models/res_config_settings.py` (with `.json`/`.md` upload onchanges).

> **S3 service** (`services/s3_service.py`) provides `upload_b64` /
> `delete_key` with exponential-backoff retries, but it is **not wired into**
> the current generation/scoring/exam flows — it's infrastructure available for
> future image-bearing questions.

---

## 10. Security & Access

- **Two groups** (`security/etp_assessment_security.xml`):
  `group_assessment_evaluator` (implies `base.group_user`) and
  `group_assessment_manager` (implies evaluator). Manager = full admin.
- **ACLs** (`security/ir.model.access.csv`): managers have full CRUD on all
  models; the evaluator group has read (and some write on
  evaluator/response/day.session) but **no access to question/dimension/option
  answer-key models**.
- **Candidates are PORTAL users** (`base.group_portal`), NOT in the evaluator
  group → they have **no ORM ACL** on the `etp_assessment` models at all.
  Isolation is enforced by the **portal controllers** (`candidate_portal.py`,
  `portal.py`) which `sudo()` and scope strictly by
  `applicant.candidate_user_id == request.env.user`.
- **Record rules** (`security/etp_assessment_record_rules.xml`) on
  evaluator / day.session / response use `…candidate_user_id == user.id` for
  own-records, but they target the **internal** `group_assessment_evaluator`
  group — so for portal candidates they are effectively **dormant** (portal
  users aren't in that group). Managers get an unrestricted `[(1,'=',1)]` rule.
- **Exam guard** (`portal.py:_candidate_guard`): the logged-in user must equal
  the candidate's linked user; otherwise `portal_wrong_candidate`. Managers may
  **preview** read-only (`_is_real_candidate` blocks them from starting timers
  or writing answers).
- The backend **"My Assessments"** menu was **removed** — candidates use the
  website portal page.

---

## 11. Admin UI / Menus (`views/menus.xml`)

Root app **ETP Assessment** (visible to evaluator group); all items below are
**manager-only**:

| Menu | Action | Model |
|---|---|---|
| Assessments | `action_etp_assessment` | etp.assessment |
| Plans ▸ Day Plans | `action_assessment_days_admin` | etp.assessment.day |
| Plans ▸ Day Sessions | `action_assessment_day_sessions_admin` | etp.assessment.day.session |
| Questions | `action_etp_assessment_question` | etp.assessment.question |
| Generators | `action_etp_assessment_prompt` | etp.assessment.prompt |
| All Responses | `action_etp_assessment_response` | etp.assessment.response |
| Configuration ▸ Skills | `action_etp_assessment_skill` | etp.assessment.skill |
| Configuration ▸ Categories | `action_etp_assessment_category` | etp.assessment.category |
| Configuration ▸ Dimensions | `action_etp_assessment_dimension` | etp.assessment.dimension |
| Configuration ▸ Settings | `action_etp_assessment_config` | res.config.settings |

- **Question Bank** carries a **Pending Approvals** control-panel button
  (`display="always"`, inherited view `etp_assessment_question_tree_pending_button`)
  that opens the consolidated draft-review screen
  (`action_etp_assessment_prompt_question_pending`). This replaced the
  short-lived top-level "Pending Approvals" menu, which was removed.
- **Skill Bank** rows expose a **Generate Questions** form-header button and a
  list bulk action (`action_generate_skill_questions` →
  `skill.action_generate_questions`); the obsolete "Generate for Skills Missing
  Questions" button was removed from the Generator.
- **All Responses** opens grouped by **Assessment ▸ Candidate**
  (`action_etp_assessment_response.context`), with sum columns on objective /
  subjective points.
- **Assessment form tabs** (`views/assessment_views.xml`): **Assign Candidates**
  (draft only), **Day Plan**, **Day Sessions**, **Candidates**, **Leaderboard**,
  **Selected Questions** (single-mode only), **Responses**.
- **Header buttons**: Build Day Plan, Launch & Invite, Mark Done,
  Score Subjective (All), Export Results, Cancel, Reset to Draft. (Start
  Assessment is `invisible="1"` — single-mode dormant.)
- **Evaluator form** has a **Score Details** tab (per-question candidate vs
  correct, objective + subjective columns) and a **Score Subjective** button.

---

## 12. Email / Invitations (`data/mail_template.xml`)

- **`mail_template_day_invitation`** (model `etp.assessment.day.session`) and
  **`mail_template_single_invitation`** (model `etp.assessment.evaluator`).
- `email_from` = `object.assessment_id.create_uid.email_formatted or
  user.email_formatted` — **the assessment creator needs an email** (or set
  `mail.default.from` + `mail.catchall.domain`).
- Sent with `force_send=True` at plan generation
  (`_send_day_invitation`, `assessment_day.py:310`).
- **No-email candidate**: a `mail.mail` is created in **`state='cancel'`** with
  the portal link stored in `failure_reason` (so the admin can copy it). See
  `assessment_day.py:321` / `assessment.py:804`.

---

## 13. Crons (`data/cron.xml`)

| Cron | Method | Interval | Purpose |
|---|---|---|---|
| Auto-score submitted candidates | `etp.assessment._cron_llm_auto_score` | 1 min | drain pending subjective scoring (advisory-locked, `llm_auto_score` ON only) |
| Open scheduled day sessions | `etp.assessment.day.session._cron_open_scheduled_days` | 5 min | flip `locked → available` once `scheduled_start` arrives (sequential: prior day finished) |
| Mark missed day sessions | `etp.assessment.day.session._cron_mark_missed` | 5 min | auto-submit `in_progress` sessions past their deadline (rescues closed tabs) |

---

## 14. Migrations & Deployment

- **`migrations/19.0.3.0.0/pre-migrate.py`** heals the `hr.employee →
  hr.applicant` candidate-identity change:
  1. **Clears stale attempt rows** (`etp_assessment_response_line`,
     `_response`, `_day_session`, `_evaluator`) that still hold old employee FKs
     (definition data — assessments/skills/questions/categories — is **kept**).
  2. Deletes the old `evaluator_ids` M2M link tables.
  3. Deletes the `noupdate` mail templates + record rules (and their
     `ir_model_data`) so the clean applicant-based XML **recreates** them in the
     same upgrade.
- **Deployment (EKS).** Because of the schema change, a deploy **must run
  `-u etp_assessment`** (ideally `-u all`) or you get
  `column/field does not exist` 500s. Rebuild the image from the latest commit
  and rollout-restart (beware same-tag image caching).
- **Flutter REST API** is provided by a **separate module**,
  **`etp_assessment_extension`** ("REST API for the ETP Assessment module"),
  under the prefix **`/api/v1/etp_assessment_ext/...`** (e.g.
  `/assessments`, `/assessments/<id>/candidates`, `/builder/generate`,
  `/candidate/me/current`, `/candidate/me/day/<i>/submit`, `/dashboard/kpis`,
  `/categories`, `/dimension_options`, …). `employee_extension` contributes the
  `hr.applicant.candidate_user_id` field + self-registration, **not** the
  assessment REST API.

---

## 15. Key Files Map

| File | Responsibility |
|---|---|
| `__manifest__.py` | deps, data load order, external py deps, frontend asset |
| `models/assessment.py` | assessment container + lifecycle, evaluator, response, response.line, CSV import, export, LLM-score trigger, cron |
| `models/assessment_day.py` | day plan row + per-candidate day session state machine, scaffold + generate-plan actions, day crons |
| `models/skill.py` | Skill Bank (UNIQUE name) + generate-from-skill action (`action_generate_questions` re-drives the source generator) |
| `models/question.py` | Question Bank entry + CSV/JSON export |
| `models/category.py` | category + "Add from Question Bank" reparent picker |
| `models/dimension.py` | master dimension + master option |
| `models/question_dimension.py` | per-question dimension link + options (`is_correct`, `master_option_id`) |
| `models/prompt.py` | generator session, resources (+text extraction), draft skills/questions, approve/deny + materialize |
| `models/bank_import.py` | abstract JSON question-bank importer |
| `models/res_config_settings.py` | Settings UI fields + SA/prompt upload onchanges |
| `services/vertex.py` | Vertex auth router, SA bearer minting, prompt resolution, generateContent calls, skill/question generation |
| `services/scoring.py` | batched subjective scoring (one call per candidate) |
| `services/s3_service.py` | S3 upload/delete with retries (infra, not yet wired) |
| `controllers/portal.py` | exam runner (single + multi-day): landing, begin, submit, review, finish, violation, auto-submit |
| `controllers/candidate_portal.py` | `/my/assessments` hub + `/my` redirect |
| `controllers/prompt_controller.py` | JSON-RPC generation API |
| `views/assessment_views.xml` | assessment/evaluator/response views, form tabs & buttons |
| `views/my_assessments_views.xml` | day-session admin views + Day Plans/Day Sessions actions |
| `views/menus.xml` | app menu tree (evaluator root, manager items) |
| `views/portal_templates.xml` | candidate-facing portal QWeb (instructions, question page, review, results, proctoring JS) |
| `data/cron.xml` | three crons |
| `data/mail_template.xml` | day + single invitation templates |
| `data/llm_config_parameters.xml` | non-secret default params |
| `security/*` | groups, ACL CSV, record rules |
| `migrations/19.0.3.0.0/pre-migrate.py` | employee→applicant heal |
| `prompts/skill_gen.md`, `prompts/question.md` | bundled LLM system prompts |

---

## Corrections vs. prior notes

The following seed assumptions were **corrected** against the actual code:

1. **No image generation.** There is no `vertex_image_model` / Imagen / per-model
   region routing in `services/vertex.py`; the module is text-only. Removed those
   from the params/Vertex sections.
2. **`vertex_location` default is `us-central1`**, not `global`. `"global"` is
   only a special case in host construction (`vertex.py:124`).
3. **Scoring prompt is 2-tier** (System Param `scoring_system_prompt` → inline
   `DEFAULT_SCORING_PROMPT`); there is **no bundled scoring `.md`**. Bundled
   files are `skill_gen.md` and `question.md` (not `*_master.md`).
4. **Auth precedence**: a bearer (`vertex_access_token` or minted SA) is checked
   **before** the api_key; bearer ⇒ aiplatform host. The api_key branch
   (`AQ.` → aiplatform; `AIza` → generativelanguage) only runs when no bearer.
5. **Flutter REST API** lives in the separate **`etp_assessment_extension`**
   module under `/api/v1/etp_assessment_ext/...`; `employee_extension` only
   provides `hr.applicant.candidate_user_id` + self-registration.
6. **Record rules** reference `applicant_id.candidate_user_id` paths but target
   the internal evaluator group, so they're dormant for portal candidates —
   isolation is enforced in the controllers, as the seed noted.
7. **S3 service** exists but is not currently wired into generation/scoring/exam
   flows.
