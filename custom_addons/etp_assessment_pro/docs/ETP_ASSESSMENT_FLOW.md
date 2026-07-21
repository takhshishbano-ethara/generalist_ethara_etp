# ETP Assessment Pro - Complete Feature & Flow Inventory

| | |
|---|---|
| **Module path** | `custom_addons/etp_assessment_pro/` (in the `ethara-etp` repo) |
| **Manifest version** | `19.0.1.121.0` (`__manifest__.py:3`) |
| **Generated** | 2026-07-13 · **last updated** 2026-07-17 |
| **Status** | Rewritten against current source (line-for-line). Every `file:line` below is relative to the module root `custom_addons/etp_assessment_pro/`. |

> **Single source of truth** for this module's features and flows. The module was heavily refactored; the prior doc described a removed architecture (skills, categories, master dimensions, multi-day plans). This revision documents only the CURRENT code. See the delta block immediately below for what changed away from the old model.

> **What changed since v64 (this update, v120):** a 4th media type - **`video_prompt`** (async Veo generation) - was added; the legacy **`subjective_justification`** type was removed (migration `19.0.1.89.0`); the subjective judge prompt advanced and **the rendered media is now attached to the scoring call** (`scoring.py:1130` `user_parts`) so image_ab/image_label are no longer graded blind; tags gained a growth-readable **`display`** alias + a manual **Tags** admin UI with a **Merge** action; the admin dashboard gained a **global candidate Leaderboard**; the candidate portal gained a **"My Performance"** analytics block; the **LLM Budget** dashboard now splits **input / output / thinking** tokens and tokens-by-operation; candidate submit can **auto-queue** subjective grading (per-assessment `llm_auto_score`, default OFF); proctoring violations post via `navigator.sendBeacon` and "Review & Submit" is a real POST (both fix silent answer loss). Migrations now run through `19.0.1.118.0` (18 total), and an 8th cron `ir_cron_poll_video_ops` drains async video jobs.

## Architecture (current, v19.0.1.121.0)

The module is now a **single-sitting, SOP-direct, tag-aware** assessment platform. What changed away from the prior doc's architecture:

- **Generation is SOP-direct - the "skill" subsystem is gone.** There is no `etp.assessment.pro.skill`, no `prompt.skill`, no two-stage extract-skills-then-generate flow. An admin uploads a SOP document to a **generator** (`etp.assessment.pro.prompt`) and the document is sent **natively (base64 multimodal)** to Gemini, which authors draft questions directly. (`models/prompt.py:224`, `services/vertex.py:1040`.)
- **Semantic SOP tags + similarity ranking are new.** `etp.assessment.pro.tag` (`models/tag.py:19`) + weighted-Jaccard "Similar Projects" ranking replace any skill taxonomy.
- **Single-sitting only - the multi-day / day-plan subsystem is removed.** There is no `etp.assessment.pro.day`, no `.day.session`. An assessment is one timed sitting per candidate. (Flattened in migration `19.0.1.11.0`.)
- **Questions link to an assessment by GENERATOR, not by category.** `etp.assessment.pro.category` is deleted (migration `19.0.1.54.0`); `assessment.generator_id` + `question.generator_id` (`models/assessment.py:42`, `models/question.py:28`) replace it.
- **Per-question dimensions are self-contained - the master `etp.assessment.pro.dimension` library is removed** (migration `19.0.1.12.0`). Every question/draft carries its OWN `question.dimension(.option)` rows; `response.line` references the per-question option via `selected_option_id` (`models/assessment.py:1521`), not a shared master option.
- **The `image_text` question type is removed** (migration `19.0.1.8.0`); it split into `image_prompt` and `image_label`.
- **Pass/fail is a live derivation from an immutable raw score.** The LLM writes only `llm_raw_100`; pass/fail derives live against the per-assessment `subjective_threshold` (`models/assessment.py:81`).

Removed since the prior doc (do not look for these - they no longer exist): skill flow / `skill` model, `category` model, master `dimension(.option)`, `day` / `day.session`, `image_text` type, the global Settings pass thresholds, `assessment_mode` single/multi switch.

---

## Table of contents

1. [How to use this document](#1-how-to-use-this-document)
2. [Scope & boundaries](#2-scope--boundaries)
3. [System architecture at a glance](#3-system-architecture-at-a-glance)
4. [Data model map](#4-data-model-map)
5. [End-to-end flows](#5-end-to-end-flows)
   - [5.1 Question Bank generation](#51-question-bank-generation-admin)
   - [5.2 Assessment setup, assignment & launch](#52-assessment-setup-candidate-assignment--launch-admin)
   - [5.3 Candidate exam-taking](#53-candidate-exam-taking-portal)
   - [5.4 Scoring & results](#54-scoring--results)
   - [5.5 Automated & scheduled flows](#55-automated--scheduled-flows)
6. [Feature inventory by area](#6-feature-inventory-by-area)
7. [Master feature checklist (test matrix)](#7-master-feature-checklist-test-matrix)
8. [Coverage & open questions](#8-coverage--open-questions)
9. [Glossary](#9-glossary)

---

## 1. How to use this document

This is the master reference for the test campaign (unit, integration, portal, and load/concurrency for 200–300 simultaneous candidates).

- **Every feature is a testable unit.** §6 (Feature inventory) is the ground-truth catalogue of models, fields, methods, routes, crons, security, and services, each with a `file:line` reference.
- **§5 (End-to-end flows)** stitches those features into the five journeys the campaign must exercise, citing the concrete routes/methods so a tester can trace a flow to its implementation.
- **§7 (Master checklist)** collapses features into GitHub-style checkboxes usable directly as the test matrix.
- **§8 (Coverage & open questions)** records residual gaps and unresolved design questions.

Recommended order for a tester: skim §2–§4 for orientation, read the relevant flow in §5, drill into §6 for the exact `file:line` contract, then check boxes in §7.

---

## 2. Scope & boundaries

**In scope - what `etp_assessment_pro` is:** a self-contained Odoo 19 module implementing the full **single-sitting** assessment lifecycle: an AI-assisted question-bank generation workspace (SOP upload → native multimodal generation of MCQ/MSQ/subjective/image/**video** questions → draft review → approve), semantic SOP tagging + similarity, per-assessment candidate provisioning, a website-portal exam runner with client-side proctoring, objective (code) scoring + subjective (media-aware LLM) scoring with an immutable-raw / live-threshold model, results release, CSV/JSON export, and analytics surfaces (admin dashboard + candidate "My Performance" + LLM Budget).

**Candidate channel - the Odoo WEBSITE PORTAL.** Candidates take the exam through this module's own QWeb portal (`/my/pro_assessments`, `/pro_assessment/<token>`), authenticated as `base.group_portal` users (or email-matched internal users) linked to `hr.applicant` via `candidate_user_id`. This module is **not** the Flutter app and **not** any REST extension - those are separate channels out of scope here.

**External dependencies (runtime):**

- **Vertex AI (Gemini + Veo)** - serves generation, tag extraction, image render, image detection, subjective scoring, and **async video generation**. Configured models: a document/generation model (`GENERATION_DEFAULT_MODEL = "gemini-3.1-pro-preview"`, `constants.py:101`) used for SOP reading, an image model (`VERTEX_DEFAULT_MODEL = "gemini-3-pro-image"`, `constants.py:96`), and a **video model** (`VIDEO_DEFAULT_MODEL = "veo-3.1-generate-001"`, `constants.py:98`) used for `video_prompt` reference clips via a long-running submit/poll operation. Generation **never** falls back to the image model (a PDF is opaque to it → "document has no pages"; `services/vertex.py:950`). All heavy calls run off the web request via cron drainers.
- **S3** (boto3) - optional server-side storage/proxy for question images; empty bucket config disables it and the portal serves stored binaries.
- **PostgreSQL** - Odoo's datastore; the module relies on `pg_advisory_lock` / `pg_advisory_xact_lock` for cron mutual exclusion and commits before slow calls (managed-Postgres idle-in-transaction reaper safety).

**Manifest module dependencies (`__manifest__.py:9-10`):** `base`, `mail`, `hr`, `hr_recruitment`, `employee_extension`, `website`, `auth_signup`.

**Manifest Python `external_dependencies` (`__manifest__.py:32-34`):** `PyJWT` (service-account bearer minting), `httpx` (Vertex + image transport), `boto3` (S3), `cryptography` (RS256 JWT signing).

---

## 3. System architecture at a glance

**Actors**

- **Admin / Manager** (`group_assessment_manager`, internal) - builds the question bank, configures LLM/S3, creates assessments, assigns/launches candidates, runs subjective evaluation, releases results, exports. Full CRUD on every model.
- **Evaluator** (`group_assessment_evaluator`, internal) - read-only on bank/tags/dashboards; read+write on the working candidate rows (evaluator, response, response.line) narrowed by candidate-isolation record rules.
- **Candidate** (`base.group_portal`, or an email-matched internal user) - takes the exam through the website portal; the only portal ACL is read-only on question images (`ir.model.access.csv:7`); all portal writes go through controller `sudo()` gated by the per-record `access_token`.

**Component sketch**

```
 Admin/Evaluator ─► Backend UI (views/*.xml, dashboards, generators, question bank, settings)
                      │  buttons → model methods
                      ▼
 models/*.py ── assessment · evaluator · response · response.line
             ── question · question.dimension(.option) · question.image
             ── prompt (generator) · prompt.question(.dimension.option) · prompt.resource · tag
             ── llm.usage · dashboard · llm.dashboard · bank.import(.wizard)
        │  service calls                         │  11 cron records (8 functions; auto-score x4 shards) in data/cron.xml
        ▼                                        ▼
 services/*.py (stateless)              auto-score · recompute-threshold · send-invites
  vertex · scoring · imaging ·          render-images · detect-image-labels ·
  export · image_ingest · s3_service    generate-from-sop · extract-tags
        │            │
        ▼            ▼
   Vertex AI       S3 (boto3)     Candidate (portal) ─► controllers/portal.py + candidate_portal.py
   (Gemini)                                              QWeb portal_templates.xml + inline JS
   PostgreSQL ◄──── all models
```

---

## 4. Data model map

21 own models (`_name`) + 2 inherits. All defining `file:line` verified. Registration order: `models/__init__.py`.

| Model (`_name`) | File:line | Kind | Purpose |
|---|---|---|---|
| `etp.assessment.pro` | `models/assessment.py:26` | Model | Assessment container / single-sitting state machine (`state` at `:31`) |
| `etp.assessment.pro.evaluator` | `models/assessment.py:600` | Model | Per-candidate assignment (`_rec_name=applicant_id`); scoring rollup, invite state, tokens |
| `etp.assessment.pro.response` | `models/assessment.py:1122` | Model | Per-question answer; objective + subjective scoring fields |
| `etp.assessment.pro.response.line` | `models/assessment.py:1513` | Model | Per-dimension answer (`question_dimension_id`, `selected_option_id`) |
| `etp.assessment.pro.question` | `models/question.py:13` | Model | Published bank question (`generator_id` at `:28`) |
| `etp.assessment.pro.question.dimension` | `models/question_dimension.py:6` | Model | Per-question dimension/axis (self-contained) |
| `etp.assessment.pro.question.dimension.option` | `models/question_dimension.py:38` | Model | Per-question option line (`is_correct` = answer key) |
| `etp.assessment.pro.question.image` | `models/question_image.py:21` | Model | Image for image questions (Binary/S3; annotation + detections) |
| `etp.assessment.pro.question.video` | `models/question_video.py` | Model | Clip for `video_prompt` questions (slot a/b/single; Binary/URL; async Veo render) |
| `etp.assessment.pro.question.type` | `models/question_type.py` | Model | Controlled **question-type vocabulary** (M2M allow-list per generator; replaced the old stored allow-list string in migration `19.0.1.103.0`) |
| `etp.assessment.pro.prompt` | `models/prompt.py:23` | Model | **Generator** - SOP resources → drafts + tags |
| `etp.assessment.pro.prompt.question` | `models/prompt.py:606` | Model | Draft question (reviewer-facing, → bank on approve) |
| `etp.assessment.pro.prompt.question.dimension` | `models/prompt_question_dimension.py:8` | Model | Draft answer axis (editable) |
| `etp.assessment.pro.prompt.question.dimension.option` | `models/prompt_question_dimension.py:26` | Model | Draft answer option (`is_correct`) |
| `etp.assessment.pro.prompt.resource` | `models/prompt.py:1401` | Model | Uploaded source file + extracted text |
| `etp.assessment.pro.tag` | `models/tag.py:19` | Model | SOP semantic tag (prefixed, canonicalized, unique) |
| `etp.assessment.pro.llm.usage` | `models/llm_usage.py:7` | Model | Token/cost ledger (one row per LLM/image call) |
| `etp.assessment.pro.dashboard` | `models/dashboard.py:8` | Transient | Analytics dashboard (`display_name` "Assessment Analytics") |
| `etp.assessment.pro.llm.dashboard` | `models/llm_dashboard.py:14` | Transient | LLM Budget dashboard (`display_name` "LLM Budget") |
| `etp.assessment.pro.bank.import` | `models/bank_import.py:20` | Abstract | Native/structured bank importer (`import_bank_native` at `:24`) |
| `etp.assessment.pro.bank.import.wizard` | `models/bank_import_wizard.py` | Transient | CSV/JSON import wizard |
| `hr.applicant` (**_inherit**) | `models/hr_applicant.py:7` | Inherit | Adds `candidate_user_id` + per-candidate analytics/charts |
| `res.config.settings` (**_inherit**) | `models/res_config_settings.py` | Inherit | Vertex/S3/scoring/generation config parameters |

**Entity-relationship summary**

```
Bank / generation                              Runtime
──────────────────                             ────────
prompt (generator) ─1─* prompt.resource        assessment.pro ─1─* evaluator (per candidate)
       ─1─* prompt.question ─► question                │  generator_id ─► prompt
       ─*─* tag  (SOP semantic tags)                   │  question_ids (M2M, resolved on launch)
prompt.question ─1─* prompt.question.dimension ─1─* option
                                               evaluator ─1─* response ─1─* response.line
question ─1─* question.dimension ─1─* option           │                        │
question ─1─* question.image                    response.line.question_dimension_id ─► question.dimension
                                                response.line.selected_option_id  ─► question.dimension.option

Identity / security
───────────────────
hr.applicant.candidate_user_id ─► res.users (portal, or email-matched internal)
evaluator.applicant_id ─► hr.applicant ;  response.evaluator_id ─► hr.applicant (candidate)
record rules key off *.candidate_user_id == user.id  (candidate isolation)
```

Key facts: two candidate collections coexist on the container - `evaluator_ids` (admin-edited `hr.applicant` M2M, pre-launch; `assessment.py:98`) vs `assessment_evaluator_ids` (materialized `evaluator` rows at launch; `assessment.py:105`). A DB `UNIQUE(assessment_id, applicant_id)` on evaluator (`assessment.py:617`) and `UNIQUE(assessment_evaluator_id, question_id)` on response (`assessment.py:1249`) backstop concurrent races. There are **no** `_sql_constraints`; uniqueness is via `@api.constrains` (tag, question.dimension) or `init()` indexes.

---

## 5. End-to-end flows

### 5.1 Question Bank generation (admin)

1. **Create a generator** - *Generators* menu (`action_etp_assessment_pro_prompt`) → new `etp.assessment.pro.prompt` (`prompt.py:23`).
2. **Upload the SOP** - files land as `etp.assessment.pro.prompt.resource` (`prompt.py:1401`) via the upload onchanges (`_onchange_upload_sop` etc., `prompt.py:156-178`). Text is extracted on create for docx/txt/md/csv/html/json/xml (`_extract_text`, `prompt.py:1452`); **PDF/PNG/JPG/WEBP/GIF are NOT text-extracted** - they are sent natively to the model (`prompt.py:1470-1474`). `has_sop_resource` gates the mandatory-SOP indicator.
3. **(Optional) Sample Questions file** - `sample_questions_file` Binary (`prompt.py:111`) is a FILE UPLOAD sent natively (multimodal) so the model matches the format; leave empty to follow the SOP's own format. `force_question_type` (`prompt.py:120`) coerces EVERY generated item to one type; `sop_question_count` (`prompt.py:117`) sets a target (0 = model decides).
4. **Generate (async, SOP-direct)** - *Generate from SOP* (`action_generate_from_sop`, `prompt.py:224`) sets `sop_gen_state='queued'` + `state='generating'` and returns immediately. Cron `ir_cron_generate_from_sop` → `_cron_generate_from_sop` (`prompt.py:248`, limit 2, session lock `827201`) commits, then calls `vertex.generate_questions_from_sop(env, prompt, count=…, force_type=…)` (`vertex.py:1040`). A Vertex **429** raises `VertexQuotaError` (`vertex.py:484`) and re-queues; other errors set `sop_gen_state='failed'` + `sop_gen_error`.
5. **Extract tags (async)** - *Extract Tags* (`action_extract_tags`, `prompt.py:307`) sets `tag_extract_state='queued'`; cron `ir_cron_extract_tags` → `_cron_extract_tags` (`prompt.py:328`, limit 2, session lock `827202`) calls `vertex.extract_tags_from_sop` (`vertex.py:1177`, a separate cheap call), canonicalizes via `tag._get_or_create` (`tag.py:71`), stores `tag_ids` + raw `tags_json`. `action_backfill_all_tags` (`prompt.py:580`) queues every un-tagged SOP generator.
6. **Similarity** - `_similar_prompts` (`prompt.py:405`) ranks OTHER generators by **weighted-Jaccard** tag overlap (prefix weights `constants.py:15`: task 3, domain 2, skill 2, modality 1, output-format 1; default 1). `similar_count` (`prompt.py:471`) and *View Similar* (`action_view_similar`, `prompt.py:551`) gate on shared-weight ≥ `TAG_SIMILAR_MIN_SCORE_DEFAULT` (2.0, `constants.py:27`). The form shows tag pills + a "Similar Projects" alignment-% panel (`similar_html`, `prompt.py:495`).
7. **Review drafts** - each `prompt.question` (`prompt.py:606`, `state` draft/approved/denied) shows leak guards `has_revealing_option` (`prompt.py:757`) and `has_source_reference` (`prompt.py:772`), an answer-key preview, and editable friendly rubric fields (`ak_ideal_answer`, `ak_checklist`, `ak_constraints`, `ak_pass_condition`, …) that inverse into `rubric_json` (`prompt.py:910`). Draft dimensions are self-contained (`answer_dimension_ids` → `prompt.question.dimension(.option)`).
8. **Image drafts render (async)** - image drafts carry `image_brief_json` + `image_state='pending'`; cron `ir_cron_render_pending_images` → `_cron_render_pending_images` (`prompt.py:1329`, limit 2, session lock `827194`) renders **all-or-nothing** via `vertex.render_draft_images` (`vertex.py:819`). `action_apply_uploaded_image` (`prompt.py:1374`) swaps in an admin-supplied picture.
8b. **Video drafts render (async, Veo)** - `video_prompt` drafts carry `video_brief_json` + `video_state` pending→generating→ready/failed (`prompt.py:1089-1114`). Cron `ir_cron_poll_video_ops` → `_cron_poll_video_ops` (`prompt.py:2487`) first **submits** the brief to Veo as a long-running operation (`_submit_video_ops` → `vertex.submit_video_op`, `prompt.py:2303`, `vertex.py:606`), stores the operation name, then on later ticks **polls** it (`_poll_video_ops`, `prompt.py:2339`) until the clip is ready and persists it as an `etp.assessment.pro.question.video` row. Video is a two-phase async job (submit then poll) precisely because Veo generation runs far longer than a single request; a paid clip is persisted so a re-poll is free. `action_apply_uploaded_video` swaps in an admin-supplied clip (`question.py:70-79`).
9. **Approve → bank** - *Approve* (`action_approve`, `prompt.py:944`; guards image drafts have a picture) creates the published `etp.assessment.pro.question` with `generator_id = prompt.id`, materializes a **private per-question** dimension set (`_materialize_dimensions`, `prompt.py:1137`) and image rows (`_materialize_images` → `image_ingest.ingest` → S3, `prompt.py:1184`). *Deny* (`action_deny`) drops the draft.
10. **(Alt) Bulk import/export** - native round-trip via `bank.import.import_bank_native` (`bank_import.py:24`) and the question model's `action_export_native_json` / `action_export_json` / `action_export_csv` (`question.py:309/334/341`).

### 5.2 Assessment setup, candidate assignment & launch (admin)

1. **Create assessment** - *Assessments* (`action_etp_assessment`) → new `etp.assessment.pro` (`assessment.py:26`), `state='draft'`. Set `generator_id`, `question_limit` (0 = all), `duration_minutes` (0 = no limit), `start_date`/`end_date`, `results_release` (manual/immediate), `subjective_threshold` (default 70), proctoring rule booleans (`assessment.py:117-137`), `max_violations`, `violation_action`, `require_objective_justification`, `require_justification_image_comparison`, `llm_auto_score`.
2. **Assign candidates** - manually via `evaluator_ids` (M2M of `hr.applicant`), or *Import Candidates CSV* (`action_import_candidates_csv`, `assessment.py:403`) which upserts `hr.applicant` by email and calls `_ensure_candidate_user` (`assessment.py:356`) to bind a portal `res.users` (`candidate_user_id`). *Download Template* (`action_download_candidate_template`, `assessment.py:484`) provides the CSV shape. An email matching an INTERNAL user is **not** bound (`_is_internal()` skip, `assessment.py:366`); a DEACTIVATED portal match is linked but not reactivated.
3. **Launch & invite** - *Start* (`action_start`, `assessment.py:279`): validates candidates + generator, selects the generator's `active` questions filtered by `_has_required_images` (`question.py:152`), enforces `question_limit`, flips `state='in_progress'`, then per candidate provisions the portal user, `random.sample`+`shuffle`s a per-candidate `question_order`, and creates an `evaluator` row with a fresh `access_token` and `invite_state='queued'`. **Launch only QUEUES** - the background cron sends emails.
4. **Invitations (async)** - cron `ir_cron_send_pending_invitations` → `_cron_send_pending_invitations` (`assessment.py:957`, batch 25, per-candidate savepoint+commit) calls `evaluator._deliver_invitation` (`assessment.py:943`): a one-time set-password link (`action_reset_password` for a never-logged-in, non-internal user) + the exam invite `mail_template_single_invitation` (`data/mail_template.xml:3`, `force_send=False`). A failed send flags `invite_state='failed'` + `invite_error`; `invite_summary` (`assessment.py:235`) shows "N queued · M sent · K failed"; *Resend* = `action_requeue_all_invitations` (`assessment.py:250`).
5. **Lifecycle buttons** - `action_done` (`assessment.py:320`), `action_cancel` (`:326`), `action_reset_draft` (`:333`). The assessment auto-flips to `done` once every evaluator is `submitted` (`_check_assessment_complete`, `assessment.py:1496`).

### 5.3 Candidate exam-taking (portal)

1. **Hub / home** - the portal `home` override (`candidate_portal.py:177`) redirects a candidate to *My Assessments* (`/my/pro_assessments`, `candidate_portal.py:106`), which buckets their evaluators into available / in-progress / upcoming / completed, each card linking to the tokenized exam URL. Applicant resolution falls back candidate-link → partner → `login == email_from` so an internal-user candidate resolves (`_resolve_applicant`, `candidate_portal.py:15`). A backend "My Assessments" menu (`ir.actions.act_url`, `groups="base.group_user"`) gives internal users an entry point. **A "My Performance" analytics block** (`_candidate_kpis`, `candidate_portal.py:148`) sits above the buckets - pass-rate + average-score rings, best/passed/completed/awaiting KPI cards, and a per-assessment score breakdown - computed purely from the candidate's already-loaded cards and gated on `results_released` (so nothing shows before an admin releases). Mirrors the admin dashboard's look, scoped to the one candidate.
2. **Landing & guards** - `/pro_assessment/<token>` (`portal.py:109`): token→evaluator (`_get_evaluator_from_token`), public→login redirect, `_candidate_guard` (`portal.py:80`; blocks link-sharing, managers may preview), then routing: locked/submitted → complete page; `state != in_progress` → `portal_assessment_closed`; not started → `portal_instructions`; time expired → auto-submit + complete; else serve the current question.
3. **Begin** - `/pro_assessment/<token>/begin` POST (`portal.py:147`, `csrf=True`) requires the real candidate (`_is_real_candidate`), stamps `started_at`, flips `state='in_progress'` (which drives the stored `deadline_datetime`, `assessment.py:1094`).
4. **Answer** - `_serve_question` (`portal.py:360`) renders `portal_question_page` with per-type inputs: mcq radios, msq native `name`d checkboxes (read via `getlist()`), subjective textarea, image_ab verdict radios + optional justification, image_prompt textarea, and **image_label per-box inputs** (`label_<n>` fields). Box count comes from the image's `detections_json`, else the answer-key `ideal_labels` length (`_image_label_context` + `_ideal_labels_count`, `portal.py:420/34`). Images stream via the token-scoped proxy `/pro_assessment/qimage/<token>/<image_id>` (`portal.py:257`).
5. **Save & advance** - each *Save & Next* posts `/submit` (`portal.py:167`, `csrf=True`) → `_record_response` (`portal.py:511`): rejects a `question_id` not in `question_order` (score-inflation guard), validates option ids against the question's own options (drops tampered/stale), collects image_label answers as JSON into `justification`, **upserts** one response per (question, evaluator) idempotently (savepoint + `IntegrityError` re-fetch), calls `action_submit()` (or `_enqueue_subjective_scoring` when editing a submitted answer). `_next_index` (`portal.py:345`) computes the next `?q=`.
6. **Navigate back** - Prev posts the same `/submit` with `nav=prev`; revisited questions pre-fill via `_existing_response` and overwrite in place.
7. **Review & submit** - *Review & Submit* is now a **form POST** with `nav=review` (`_next_index` returns `None` → review page **after** saving the current answer, `portal.py:517`), fixing a prior data-loss bug where it was an `<a href>` GET inside the form that silently discarded the just-typed answer on navigation. `/finish` POST (`portal.py:224`, `csrf=True`) bounces to `/review?incomplete=1` when questions are unanswered and the deadline has not passed (warning banner), else `_auto_submit_remaining_single` (`portal.py:652`) fills `[Auto-submitted]` placeholders, locks the evaluator (`state='submitted'`, `is_locked=True`), and flips the assessment to `done` when all are submitted.
8. **Proctoring** - inline JS reads the 8 rule booleans (`_rules_json`, `portal.py:16`), enforces tab-switch/copy-paste/right-click/devtools/screenshot/fullscreen/webcam/watermark client-side, and reports a violation via **`navigator.sendBeacon`** to `/violation` (`portal.py:241`, `csrf=True`) - a synchronous form POST from inside `visibilitychange`/unload was routinely dropped by the browser when the tab backgrounded, so violations never reached the server; the beacon survives backgrounding (a hidden-form POST remains as fallback). No blind page reload is fired (it destroyed in-progress answers); a non-destructive notice shows instead, and enforcement is entirely server-side. `_record_violation_single` (`portal.py:631`) atomically increments `violation_count`; at `max_violations` with `violation_action='auto_submit'` the attempt auto-submits and every later POST is `is_locked`-guarded. The devtools heuristic is normalized by `devicePixelRatio` so a low-vision candidate who zooms is not auto-submitted for cheating.
9. **Deadline / resilience** - `is_time_expired` (`assessment.py:1104`) is checked on landing/submit + a client timer; cron `ir_cron_mark_missed` does **not** exist any more (single-sitting) - a closed-tab session past deadline is rescued on the candidate's next hit or left for the admin. A live timer + real-time progress bar render from `deadline_iso` + answered/total.

### 5.4 Scoring & results

1. **Objective (inline, code)** - on `action_submit` (`assessment.py:1415`), `_compute_score` (`assessment.py:1386`) grades mcq/msq **all-or-nothing, equal marks** (1 only when the chosen option set equals the correct set for every objective dimension).
2. **Subjective enqueue (never inline)** - `_enqueue_subjective_scoring` (`assessment.py:1446`) sets each needs-LLM response to `pending` (or `not_needed`/`scored` for verdict-only image_ab), and - only when `llm_auto_score` is on - flags the evaluator `scoring_requested`. Nothing calls Vertex on the candidate's request path.
3. **Batched LLM scoring** - admin *Run Subjective Evaluation* (`action_llm_score_all`, `assessment.py:502`) or per-candidate queue (`action_queue_llm_score`, `assessment.py:1012`) sets `scoring_requested`; cron `ir_cron_llm_auto_score` → `_cron_llm_auto_score` (`assessment.py:545`, ≤20 evaluators/tick, session advisory lock `827193` with `pg_advisory_unlock_all()` at entry) calls `scoring.score_evaluator` (`scoring.py`): all of a candidate's needs-LLM answers go in **ONE Vertex call** (sub-batched only past `scoring_batch_size`; a retried item is re-sent alone). Verdict-only image_ab short-circuits with no call. **The rendered media the candidate saw is attached to that call** - `_score_submission` passes `user_parts=[text]+media_parts` (`scoring.py:1130`), where `_media_parts_for` (`scoring.py:596`) inlines the A/B images (or the annotated image_label overlay) as base64 parts and the prompt indexes where each item's images sit; items with no renderable media are named so the judge stamps `media_unseen` instead of hallucinating a view. Before this fix every image_ab / image_label justification was graded blind on its text alone.
   - **Candidate-driven auto-queue:** when an assessment has `llm_auto_score` ON (per-assessment toggle, **default OFF** while the Vertex testing budget is frozen), a candidate's submit auto-sets `scoring_requested` in the evaluator `write()` override (`assessment.py:763`) so the cron picks it up with no admin click - the Vertex call still happens in the cron, never on the request path.
4. **Immutable raw + live threshold** - `_store_scored` (`scoring.py:395`) writes ONLY the immutable `llm_raw_100` plus the v6 audit trail (`llm_gate`, `llm_reasoning`, `llm_reference_answer`, `llm_result_json`, …). It does **not** write pass/fail: the grader's own v6 `passed` verdict is retained inside `llm_result_json` for audit but is **ignored** for the decision. `_compute_subjective_marks` (`assessment.py:1325`) derives the earned mark + `llm_passed` LIVE from `llm_raw_100` vs the assessment's `subjective_threshold`, so changing the threshold re-decides pass/fail with no re-scoring. `_store_error` (`scoring.py:418`) retries under the attempt cap (`failed`) then surfaces a terminal `error` (never a silent 0).
5. **image_ab blend** - `_compute_ab_scores` (`assessment.py:1306`): verdict% is objective (`_image_ab_mcq_pct`, `assessment.py:1274`); final% is verdict-only when justification is off, else `ceil(0.75·verdict% + 0.25·justification%)` (`AB_VERDICT_WEIGHT`/`AB_JUSTIFICATION_WEIGHT`, `constants.py:8-9`). A Vertex call is used only when justification is required AND written (`_image_ab_uses_llm`, `assessment.py:1291`).
6. **Roll-up** - response → evaluator `_compute_progress` (objective) + `_compute_llm_progress` (subjective) → `_compute_result` (`assessment.py:879`): equal-marks %, denominator = assigned `total_questions`, pass/fail vs `pass_threshold` (= clamped `subjective_threshold`). `_compute_subjective_rollup` (`assessment.py:896`) sets evaluator `llm_state` (incl. off-enum `error`); `scoring_error_flag` (`assessment.py:707`) renders a red "!" for terminally-errored candidates. *Reset & Re-score Errored* = `action_reset_errored_scoring` (`assessment.py:1031`).
7. **Threshold recompute** - writing `subjective_threshold` (`assessment.py:180`) recomputes small assessments inline (≤500 needs-LLM responses) or flags `threshold_recompute_pending` for cron `ir_cron_recompute_subjective_results` → `_cron_recompute_subjective_results` (`assessment.py:219`, batched, committed).
8. **Results release** - `results_release='immediate'` reveals on scoring via `_apply_results_disclosure` (`assessment.py:1110`, monotonic); `manual` requires *Release Results* (`action_release_all_results` / `action_release_results`, `assessment.py:526/1059`). The portal gates candidate-facing scores on `results_released` (`candidate_portal.py:55-65`).
9. **Export** - *Export Results* (`export.export_results`, one row/candidate) and *Export Responses* (`export.export_responses`, one row/answer), via `action_export_results` / `action_export_responses` (`assessment.py:579/585`), return `ir.actions.act_url` CSV downloads.

### 5.5 Automated & scheduled flows

**Eight `ir.cron` records** (`data/cron.xml`), all `state=code`, all active, all **1-minute** interval:

| Cron id | Method (`file:line`) | Model | Advisory lock | What it does |
|---|---|---|---|---|
| `ir_cron_llm_auto_score` (x4: shards 0-3) | `_cron_llm_auto_score(shard=k)` (`assessment.py:599`) | `etp.assessment.pro` | session `827193` (shard 0) / `827251-827253` (shards 1-3), each `+unlock_all` at entry | Grades ≤20 submitted + `scoring_requested` evaluators **per shard** (one Vertex call each). Shard k drains only `id %% scoring_shards == k`; `scoring_shards` (ir.config_parameter, 1-4, default 1) sets how many shard crons do work, so scoring parallelizes N-wide. Candidate submit auto-flags `scoring_requested` when `llm_auto_score` is ON (default OFF) |
| `ir_cron_recompute_subjective_results` | `_cron_recompute_subjective_results` (`assessment.py:219`) | `etp.assessment.pro` | - | Recomputes pass/fail for `threshold_recompute_pending` assessments in committed batches |
| `ir_cron_send_pending_invitations` | `_cron_send_pending_invitations` (`assessment.py`) | `etp.assessment.pro.evaluator` | - (per-candidate savepoint+commit) | Sends ≤25 `queued` invitations; flags failures |
| `ir_cron_render_pending_images` | `_cron_render_pending_images` (`prompt.py`) | `etp.assessment.pro.prompt.question` | session `827194` | Renders ≤2 `image_state=pending` drafts (all-or-nothing) |
| `ir_cron_detect_image_labels` | `_cron_detect_image_labels` (`question_image.py`) | `etp.assessment.pro.question.image` | session `827195` | Detects + numbered-box-annotates ≤2 image_label `single` images |
| `ir_cron_generate_from_sop` | `_cron_generate_from_sop` (`prompt.py`) | `etp.assessment.pro.prompt` | session `827201` | Drains ≤2 `queued/generating` SOP generators (native multimodal generation) |
| `ir_cron_expire_stale_attempts` | `_cron_expire_stale_attempts` (`assessment.py:1069`) | `etp.assessment.pro.evaluator` | session `827197` (+`unlock_all` at entry) | Auto-submits ≤100 abandoned `in_progress` attempts past `deadline_datetime` via the shared `_auto_submit_expired`; unblocks assessment completion (added `19.0.1.121.0`) |
| `ir_cron_poll_video_ops` | `_cron_poll_video_ops` (`prompt.py:2487`) | `etp.assessment.pro.prompt` | - | Submits + polls async **Veo** video jobs for `video_prompt` drafts (`_submit_video_ops`/`_poll_video_ops`, `prompt.py:2303/2339`); a long-running operation is submitted, then polled to completion and the clip persisted |

> **Not a cron:** tag extraction runs **inline** from the *Extract Tags* button (`_run_tag_extract_inline`) since migration `19.0.1.98.0` removed `ir_cron_extract_tags`; the `_cron_extract_tags` method still exists but no `ir.cron` record calls it. `ir_cron_mark_missed` was removed with the multi-day subsystem - `ir_cron_expire_stale_attempts` above is its single-sitting successor for deadline rescue.

**Email:** one template, `mail_template_single_invitation` (`data/mail_template.xml:3`), sent `force_send=False` (queued to the mail cron); no-email candidates get a durable cancelled `mail.mail` audit row with the link in `failure_reason` (`assessment.py:930`).

---

## 6. Feature inventory by area

### A. Assessment lifecycle (`models/assessment.py`, 1525 lines)

| Feature | file:line |
|---|---|
| `etp.assessment.pro` container; `state` draft/in_progress/done/cancelled | `assessment.py:26`, `:31` |
| `generator_id` (→ prompt), `question_limit`, `duration_minutes`, `question_ids` (M2M resolved on launch) | `:42`, `:47`, `:52`, `:57` |
| `subjective_threshold` Float (default 70, editable anytime) + `threshold_recompute_pending` | `:81`, `:90` |
| `results_release` manual/immediate; proctoring rule booleans (8) | `:68`, `:117-137` |
| `max_violations`, `violation_action`, `require_objective_justification`, `require_justification_image_comparison`, `llm_auto_score` | `:138-166` |
| `write` side-effects: re-pend image_ab on toggle; threshold recompute (inline ≤500 else flag) | `:171` |
| `action_start` (launch: resolve generator questions, shuffle per candidate, create evaluators, queue invites) | `:279` |
| `action_done` / `action_cancel` / `action_reset_draft` | `:320`/`:326`/`:333` |
| `_ensure_candidate_user` (portal provisioning; internal-user + deactivated guards) | `:356` |
| CSV candidate import + template | `:403`, `:484` |
| `action_llm_score_all` (queue subjective) / `action_release_all_results` | `:502`, `:526` |
| **evaluator** `etp.assessment.pro.evaluator` (`_rec_name=applicant_id`) | `:600` |
| `invite_state` none/queued/sent/failed; `UNIQUE(assessment_id, applicant_id)` | `:609`, `:617` |
| `_candidate_user` (candidate-link → partner → login==email) | `:623` |
| `access_token`, `question_order`, `started_at`, `submitted_at`, `deadline_datetime` | `:637-646` |
| `state` pending/in_progress/submitted; `write` stamps `submitted_at` | `:647`, `:719` |
| `llm_state` incl. off-enum `error`; `scoring_error_flag` "!" badge | `:679`, `:707` |
| `_compute_result` (equal-marks %, live pass/fail vs threshold) | `:879` |
| `_compute_subjective_rollup`, `_apply_results_disclosure` | `:896`, `:1110` |
| `action_llm_score` / `action_queue_llm_score` / `action_reset_errored_scoring` | `:988`/`:1012`/`:1031` |
| `_deliver_invitation`, `_cron_send_pending_invitations`, `action_requeue_invitation` | `:943`/`:957`/`:983` |
| **response** `etp.assessment.pro.response`; `state` draft/submitted; `UNIQUE(evaluator, question)` | `:1122`, `:1140`, `:1249` |
| `llm_raw_100` (immutable) + derived `llm_score`/`llm_passed`/`llm_raw_score` | `:1156`, `:1325` |
| `llm_state` not_needed/pending/queued/scored/failed/error; v6 audit fields | `:1205`, `:1180-1202` |
| `_compute_scoring_kind` (has_objective / needs_llm; placeholder guard) | `:1367` |
| `_compute_score` (mcq/msq all-or-nothing), `_compute_ab_scores` (0.75/0.25) | `:1386`, `:1306` |
| `action_submit`, `_enqueue_subjective_scoring`, `_check_all_submitted` | `:1415`/`:1446`/`:1483` |
| **response.line** `question_dimension_id`, `selected_option_id` (per-question option) | `:1513`, `:1518`, `:1521` |

### B. Question bank (`models/question*.py`)

| Feature | file:line |
|---|---|
| `etp.assessment.pro.question`; `generator_id`; 7-type `question_type` | `question.py:13`, `:28`, `:19` |
| `has_valid_key`, `has_revealing_option`, `has_source_reference`, answer-key preview | `:70`, `:75`, `:80`, `:66` |
| `detection_mode` object/ui; `_has_required_images`; `action_detect_now` | `:53`, `:152`, `:218` |
| Export payloads (lossy + native lossless) + `action_export_*` | `:238`, `:271`, `:309-341` |
| `question.dimension` (self-contained; `@api.constrains` unique-per-question) + `.option` (`is_correct`, `score`) | `question_dimension.py:6`, `:38` |
| `question.image`: slots a/b/single/reference/output; `image`/`image_url`; `annotated_image(_url)`; `detections_json` | `question_image.py:21`, `:39`, `:66-83` |
| `_detect_and_annotate` (Vertex detect → PIL numbered-box overlay → answer key) | `question_image.py:158` |
| `_cron_detect_image_labels` (session lock `827195`, attempt cap 3) | `question_image.py:214` |

### C. Generation engine (`models/prompt*.py`, `models/tag.py`)

| Feature | file:line |
|---|---|
| `etp.assessment.pro.prompt` (generator); `resource_ids`, `source_text` | `prompt.py:23`, `:29`, `:28` |
| `sop_gen_state`, `tag_extract_state`, `extract_state` (legacy idle field) | `:66`, `:77`, `:50` |
| `sample_questions_file` (native multimodal), `sop_question_count`, `force_question_type` | `:111`, `:117`, `:120` |
| `tag_ids`, `similar_count`, `similar_html` | `:76`, `:90`, `:100` |
| `action_generate_from_sop` + `_cron_generate_from_sop` (lock `827201`) | `:224`, `:248` |
| `action_extract_tags` + `_cron_extract_tags` (lock `827202`); `action_backfill_all_tags` | `:307`, `:328`, `:580` |
| `_similar_prompts` (weighted-Jaccard), `_tag_prefix_weight`, `action_view_similar` | `:405`, `:384`, `:551` |
| `prompt.question` draft (`state` draft/approved/denied); friendly `ak_*` rubric fields | `:606`, `:714-738` |
| `action_approve` (→ bank, `generator_id`, private dims + images), `action_deny` | `:944`, `:1242` |
| `_materialize_dimensions` (private per-question), `_materialize_images` (→ S3) | `:1137`, `:1184` |
| `_render_all_images` (all-or-nothing) + `_cron_render_pending_images` (lock `827194`) | `:1290`, `:1329` |
| `action_apply_uploaded_image` | `:1374` |
| `prompt.resource` extraction (docx/txt/…; PDF+images native, no extraction) | `:1400`, `:1452` |
| `prompt.question.dimension(.option)` draft answer axis/option | `prompt_question_dimension.py:8`, `:26` |
| `tag`: prefix/label compute, **`display` readable alias**, `usage_count`, `@api.constrains` case-insensitive unique, `_canonicalize`, `_get_or_create`, `_facet_vocabulary` (frequency-ranked, self-extending), `action_merge_tags` (manual merge → repoint + unlink) | `tag.py:27`, `:33`, `:53`, `:138`, `:158`, `:202` |

### D. Services (`services/*.py`, signatures only)

| Service / entry point | file:line |
|---|---|
| `vertex._call_vertex(env, system_prompt, user_text, max_tokens, temperature, response_json, usage_ctx)` | `vertex.py:376` |
| `vertex.generate_questions_from_sop(env, prompt_record, sample_text="", count=0, force_type="")` | `vertex.py:1040` |
| `vertex.extract_tags_from_sop(env, prompt_record)` | `vertex.py:1177` |
| `vertex.detect_image_elements(env, image_b64, ui=False, model=None)` | `vertex.py:632` |
| `vertex.render_draft_images(env, briefs, usage_ctx=None, only_slot=None)`; `generate_image(...)` | `vertex.py:819`, `:489` |
| `vertex.VertexQuotaError` (429), `LLMRefusalError`; bearer mint xact lock `827300` | `vertex.py:484`, `:312`, `:120` |
| `vertex._generation_model` (doc model, never image fallback) / `_vertex_image_model` | `vertex.py:950`, `:99` |
| `scoring.score_evaluator(env, evaluator)` (ONE call/candidate) | `scoring.py:440` |
| `scoring._score_submission`, `_build_item`, `_store_scored` (immutable raw), `_store_error` | `scoring.py:476`, `:301`, `:395`, `:418` |
| `scoring._image_prompt_key` / `_image_label_key` / `_image_ab_axes` | `scoring.py:124`, `:138`, `:273` |
| `imaging.annotate_image(image_bytes, detections)` (PIL numbered boxes) | `imaging.py:34` |
| `export.export_results` / `export.export_responses` | `services/export.py` |
| `image_ingest.ingest` / `_download` (15 MB streamed cap); `s3_service.upload_b64` / `download` / `object_key_from_url` | `services/image_ingest.py`, `services/s3_service.py` |

### E. Candidate portal & HTTP routes (`controllers/*.py`)

| Route | Method | file:line |
|---|---|---|
| `/my/pro_assessments` | GET, `auth=user` | `candidate_portal.py:106` |
| portal `home` override (redirect to hub) | - | `candidate_portal.py:177` |
| `/pro_assessment/<token>` (landing) | GET, `auth=public` | `portal.py:109` |
| `/pro_assessment/<token>/begin` | POST, `csrf=True` | `portal.py:147` |
| `/pro_assessment/<token>/submit` | POST, `csrf=True` | `portal.py:167` |
| `/pro_assessment/<token>/review` | GET | `portal.py:195` |
| `/pro_assessment/<token>/finish` | POST, `csrf=True` | `portal.py:224` |
| `/pro_assessment/<token>/violation` | POST, `csrf=True` | `portal.py:241` |
| `/pro_assessment/qimage/<token>/<image_id>` (token-scoped image proxy) | GET | `portal.py:257` |
| `/etp_assessment/admin_qimage/<image_id>` (ACL-checked admin proxy) | GET, `auth=user` | `portal.py:296` |

### F. Backend UI, dashboards & menus (`views/*.xml`, `models/dashboard*.py`)

| Feature | file:line |
|---|---|
| App root menu → Analytics dashboard (`action_etp_assessment_pro_home_dashboard`) | `menus.xml:4`, `dashboard_views.xml` |
| Menus: Assessments, Questions, Generators, All Responses, Configuration → (Settings, **Tags**, **LLM Budget**), My Assessments | `menus.xml` |
| `etp.assessment.pro.dashboard` (score rings, CSS charts, Performance breakdown, Recent Submissions, clickable KPI cards, **global candidate Leaderboard** `_build_leaderboard` `dashboard.py:355` - top-10 by avg score across all assessments, medals) | `dashboard.py:8` |
| Drilldowns: `action_open_assessments/candidates/submitted/passed/failed/pending` | `dashboard.py:136-173` |
| Candidate "My Performance" portal analytics (`_candidate_kpis`, rings + KPI cards + per-assessment breakdown, release-gated) | `candidate_portal.py:148`, `views/portal_templates.xml` |
| Per-candidate styled dashboard (`hr.applicant` form: score charts + result donut) | `hr_applicant.py:43`, `views/hr_applicant_views.xml:20` |
| `etp.assessment.pro.llm.dashboard` "LLM Budget" - cost by operation/model/**project**; **tokens split input/output/thinking** (`thoughts_total`, `chart_tokens_by_operation_html` `llm_dashboard.py:27/37`); video seconds; single consolidated menu | `llm_dashboard.py:14`, `llm_dashboard_views.xml` |
| `etp.assessment.pro.llm.usage` cost ledger (operations incl. `submit_video_op`, `consolidate_vocab`) | `llm_usage.py:7`, `:12` |
| **Tags admin UI** - editable list/form + **Merge** action (repoints `prompt.tag_ids` to the survivor, then unlinks losers), `usage_count` popularity, `display` readable-name column | `views/tag_views.xml`, `tag.py:53` |
| Analytics graph/pivot over evaluator (+ `applicant_id` group-by) | `analytics_views.xml` |

### G. Security, cron, mail, migrations

| Feature | file:line |
|---|---|
| Groups: `group_assessment_evaluator`, `group_assessment_manager` | `security/etp_assessment_security.xml` |
| ACLs (portal read-only on question.image only; LLM usage + dashboards Manager) | `security/ir.model.access.csv` |
| Candidate-isolation record rules (own-evaluator / all-manager, incl. response.line) | `security/etp_assessment_record_rules.xml` |
| 11 cron records (8 functions) | `data/cron.xml` |
| `mail_template_single_invitation` | `data/mail_template.xml:3` |
| **Advisory locks** (`constants.py`): AUTOSCORE 827193, IMAGE_RENDER 827194, IMAGE_DETECT 827195, VIDEO_POLL 827196, EXPIRE_ATTEMPTS 827197, AUTOSCORE_SHARD_BASE 827250 (+shard, 827251-827253), SKILL_EXTRACT 827200 (**legacy/unused** - skill flow removed), QUESTION_GEN 827201 (**active** - SOP generation cron), TAG_EXTRACT 827202, VERTEX_BEARER 827300 (xact) | `constants.py` |

**Migrations (18):**

| Version | file | Purpose |
|---|---|---|
| `19.0.1.3.0` | post-migrate | Remap Vertex location/model; drop dead `subjective_points`; backfill immutable `llm_raw_100` + forced recompute |
| `19.0.1.4.0` | pre + post | De-dup evaluators/sessions before new UNIQUE indexes; seed `subjective_threshold`; flag threshold recompute; backfill `invite_state='sent'` |
| `19.0.1.8.0` | post-migrate | Delete the legacy `image_text` question type + its data |
| `19.0.1.11.0` | pre-migrate | Flatten the multi-day subsystem into a single sitting; drop the day_session-based response unique index so `init()` rebuilds it on `(evaluator, question)` |
| `19.0.1.12.0` | pre-migrate | Decouple per-question dimensions from the removed master library; retarget `response.line`; drop master tables/XML-ids |
| `19.0.1.47.0` | post-migrate | Backfill `evaluator.submitted_at` |
| `19.0.1.53.0` | pre + post | Add `generator_id` (assessment + question) and backfill it |
| `19.0.1.54.0` | pre + post | Drop the `category` model + column |
| `19.0.1.67.0` | post-migrate | Phase 3 image_ab **flaw-injection** data backfill |
| `19.0.1.79.0` | post-migrate | Gap 2 **dense image_label** generation support |
| `19.0.1.83.0` | post-migrate | **`video_prompt`** Phase 3 - async Veo generation scaffolding |
| `19.0.1.89.0` | pre-migrate | **Remove the `subjective_justification` question type** (folded into `subjective_rubric`) |
| `19.0.1.93.0` | post-migrate | (data cleanup) |
| `19.0.1.98.0` | post-migrate | Tag extraction becomes **manual-only** (no auto-trigger on generate) |
| `19.0.1.101.0` | post-migrate | Drop orphaned `ir.cron` rows |
| `19.0.1.103.0` | post-migrate | Allow-list storage → **Many2many vocabulary** (question-type allow list) |
| `19.0.1.117.0` | post-migrate | Retire `data/llm_config_parameters.xml` |
| `19.0.1.118.0` | post-migrate | **Readable-tag rework** - backfill `display` + data-driven drift collapse (string-similarity merge of duplicate tags) |

### H. Constants & taxonomy (`constants.py`)

| Feature | file:line |
|---|---|
| `QUESTION_TYPE_SELECTION` (mcq, msq, subjective_rubric, image_ab, image_prompt, image_label, **video_prompt**) - 7 types; `subjective_justification` was **removed** (migration `19.0.1.89.0`) | `:34` |
| `OBJECTIVE_/SUBJECTIVE_/IMAGE_/VIDEO_QUESTION_TYPES` sets | `:47-50` |
| `DETECTION_MODE_SELECTION` object/ui | `:58` |
| `DEFAULT_SUBJECTIVE_THRESHOLD` 70; `AB_VERDICT_WEIGHT` 0.75 / `AB_JUSTIFICATION_WEIGHT` 0.25 | `:6`, `:8-9` |
| `TAG_PREFIX_WEIGHTS` + `TAG_SIMILAR_MIN_SCORE_DEFAULT` | `:15`, `:27` |
| `GENERATION_DEFAULT_MODEL` `gemini-3.1-pro-preview`; `VERTEX_DEFAULT_MODEL` `gemini-3-pro-image` | `:101`, `:96` |
| Leak guards `option_name_reveals_reasoning`, `text_has_source_reference` | `:109`, `:133` |

---

## 7. Master feature checklist (test matrix)

**Generation**
- [ ] Create generator; upload SOP resource; `has_sop_resource` gates the mandatory indicator (`prompt.py:180`)
- [ ] docx/txt/md text-extracted; PDF/image kept for native send, no extraction error (`prompt.py:1452`)
- [ ] `action_generate_from_sop` queues; cron drains ≤2; 429 re-queues, other errors → `failed` (`prompt.py:224`,`:248`)
- [ ] `force_question_type` coerces every item; `sop_question_count` honored (`prompt.py:120`,`:117`)
- [ ] Sample-questions file sent natively (`vertex.py:1001`)
- [ ] Tag extraction queues + cron drains; canonicalized, unique, prefixed (`tag.py`)
- [ ] Similar-projects ranking by weighted-Jaccard; threshold gate (`prompt.py:405`)
- [ ] Draft leak guards fire; `ak_*` rubric fields inverse into `rubric_json` (`prompt.py:910`)
- [ ] Image drafts render all-or-nothing; upload replacement works (`prompt.py:1290`,`:1374`)
- [ ] Approve creates bank question with `generator_id`, private dims + images (`prompt.py:944`)

**Assessment / launch**
- [ ] Draft config; `subjective_threshold` editable anytime; recompute inline vs cron (`assessment.py:180`)
- [ ] `action_start` resolves generator questions (image-complete only), shuffles per candidate, queues invites (`assessment.py:279`)
- [ ] CSV import upserts applicants + provisions portal users; internal/deactivated guards (`assessment.py:356`,`:403`)
- [ ] Invitation cron sends batches; `invite_state` badges + `invite_summary`; resend (`assessment.py:957`)

**Portal exam**
- [ ] Hub buckets evaluators; internal-user resolution (`candidate_portal.py:15`)
- [ ] Guards: invalid token, public→login, wrong-candidate, closed/locked (`portal.py:80`,`:109`)
- [ ] Begin stamps `started_at`; deadline computes; all POST routes `csrf=True`
- [ ] Per-type rendering incl. image_label per-box inputs (detections or ideal_labels count) (`portal.py:420`)
- [ ] `_record_response` score-inflation guard + option validation + idempotent upsert (`portal.py:511`)
- [ ] Back-nav prefill/overwrite; review incomplete banner bounces (`portal.py:224`)
- [ ] Proctoring increments `violation_count`; auto-submit at cap (`portal.py:631`)
- [ ] Image proxy token-scoped; admin proxy ACL-checked (`portal.py:257`,`:296`)

**Scoring / results**
- [ ] Objective all-or-nothing equal marks (`assessment.py:1386`)
- [ ] Subjective never inline; cron ≤20; ONE Vertex call/candidate; retry-then-error (`assessment.py:545`, `scoring.py:440`)
- [ ] `llm_raw_100` immutable; pass/fail derives live; threshold change re-decides without re-scoring (`assessment.py:1325`)
- [ ] image_ab verdict-only vs 0.75/0.25 blend (`assessment.py:1306`)
- [ ] `error` state surfaced (not silent 0); `scoring_error_flag`; reset & re-score (`assessment.py:1031`)
- [ ] Results release immediate vs manual gates portal scores (`assessment.py:1110`)
- [ ] Export results + responses CSV (`assessment.py:579`,`:585`)

**Automation / infra**
- [ ] All 11 cron records registered (8 functions; auto-score x4 shards), 1-min, correct model/method/lock (`data/cron.xml`)
- [ ] Advisory-lock keys unique; no aliasing; shard block 827250-827253 disjoint; VERTEX_BEARER xact-scoped (`constants.py`)
- [ ] All 8 migrations idempotent; unique indexes build on a de-duped table

---

## 8. Coverage & open questions

**Coverage.** This doc was rebuilt line-for-line against source for: `__manifest__.py`, `constants.py`, `models/{assessment,question,question_dimension,question_image,prompt,prompt_question_dimension,tag,dashboard,llm_dashboard,llm_usage,hr_applicant,bank_import}.py`, `controllers/{portal,candidate_portal}.py`, `data/cron.xml`, `data/mail_template.xml`, `security/ir.model.access.csv`, `views/{menus,dashboard,llm_dashboard,analytics,hr_applicant,llm_usage}.xml`, and the migration tree. Service files were read at signature granularity only.

**Corrections applied vs. the working brief (source is ground truth):**
- **`response.line` option field** is `selected_option_id` (→ `etp.assessment.pro.question.dimension.option`), not `selected_qd_option_id`. There is no `day_session_id` any more (single-sitting).
- **Advisory-lock roles:** `ADVISORY_LOCK_QUESTION_GEN` (827201) is **active** - it is the SOP-generation cron's lock (`prompt.py:256`), not legacy. `ADVISORY_LOCK_SKILL_EXTRACT` (827200) is the genuinely **legacy/unused** key (the skill flow is gone).
- **Scoring locks:** `_cron_llm_auto_score` uses a **session** advisory lock (`pg_try_advisory_lock` + `pg_advisory_unlock_all()` at entry), not an xact lock. Only `VERTEX_BEARER` (827300) is xact-scoped.
- **v6 `passed`** is not written to a stored field; it lives only inside `llm_result_json` and is ignored for the pass/fail decision (derived live from `llm_raw_100`).
- **`ir_cron_mark_missed` does not exist** - deadline handling is per-request/client-timer only (single-sitting removed the day-expiry cron). Only 7 crons exist.
- **One mail template** (`mail_template_single_invitation`); there is no day-invitation template.
- **`action_start`** IS the launch action (there is no separate multi-day `action_generate_plan`).

**Open questions for the load test:**
- A closed-tab session past its deadline is auto-submitted by `ir_cron_expire_stale_attempts` (≤100/tick, deadline-rescue since 121.0); unlimited-duration sittings (no `deadline_datetime`) are intentionally exempt and still need a candidate reopen or admin action.
- `_cron_llm_auto_score` at ≤20/tick/shard (1 min) implies ~1,200 candidates/hr per shard; with `scoring_shards` up to 4, ~4,800/hr max subjective throughput. Size the grading window accordingly (bounded by Vertex quota, not the lock).
- Generation/tag/render/detect crons drain only 2/tick - a large generation backlog clears slowly by design (quota safety); confirm timing expectations.
- `subjective_threshold` write recomputes inline at ≤500 needs-LLM responses; verify the cron path for larger assessments under concurrent edits.

---

## 9. Glossary

- **Generator** - an `etp.assessment.pro.prompt` record: the SOP + resources + tags + drafts workspace that authors questions. Assessments bind questions by `generator_id`.
- **SOP-direct generation** - sending the SOP document natively (base64 multimodal) to Gemini, which authors draft questions directly (no skill extraction step).
- **Semantic tag** - a prefixed, canonicalized `etp.assessment.pro.tag` (`task:` / `domain:` / `skill:` / `modality:` / `output-format:`) characterizing a generator's SOP; drives weighted-Jaccard similarity. The machine **key** (`name`) is what the ranker reads; a pinned growth-readable **`display`** alias is what non-technical teammates see (one key → one readable name, forever). A manual **Merge** action (repoint refs → unlink) is the human counterpart to the LLM `consolidate_vocabulary` drift-collapse pass.
- **Evaluator** (row) - `etp.assessment.pro.evaluator`: one candidate's assignment/attempt for one assessment (token, question order, scoring rollup). Distinct from the internal *evaluator group*.
- **Single sitting** - the only mode: one timed attempt per candidate; no days/day-sessions.
- **`llm_raw_100`** - the grader's immutable 0–100 quality score; the sole stored subjective truth. Mark and pass/fail are computed live from it vs `subjective_threshold`.
- **image_ab / image_prompt / image_label / video_prompt** - the four media question types: A/B comparison (verdict axes, optional justification blend), candidate writes the generating prompt (scored vs `ideal_prompt`), box-detection labelling (Gemini detects → numbered PIL overlay → candidate labels each box, scored per box), and **video_prompt** - the candidate writes a prompt that (would) generate a target clip, with the reference clip rendered async via Veo. The subjective judge now **sees the rendered image/annotated overlay** attached to the scoring call (`scoring.py:1130`), not just the candidate's text.
- **Advisory lock** - a Postgres `pg_advisory_lock` key serializing a cron across workers; keys are registered together in `constants.py:32-38` to prevent collisions.
