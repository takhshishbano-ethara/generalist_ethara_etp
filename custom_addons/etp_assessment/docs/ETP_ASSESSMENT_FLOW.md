# ETP Assessment — Complete Flow Reference

**Module:** `etp_assessment` v19.0.0.16 (Odoo 19)
**Audience:** engineers working in the module, ops running it, mobile/extension teams integrating with it
**Updated:** 2026-06-12

This is the engineering-level companion to the high-level `/ETP_ASSESSMENT.md` overview. It documents the data model, the three core flows (creation, generation, scoring), every external integration, every System Parameter, the deployment topology, and the local test commands.

---

## 1. Architecture at a glance

```
┌───────────────────────┐        ┌──────────────────────────┐
│   Manager (backend)   │◀──────▶│  Odoo (etp_assessment)   │
└──────────┬────────────┘        │  models, views, cron     │
           │                     │  controllers (HTTP)      │
           │                     └──┬──────────────────┬────┘
           │ assigns candidates     │                  │
           │                        │ publish          │ XML-RPC
           ▼                        ▼                  ▼
┌───────────────────────┐    ┌──────────────────┐  ┌─────────────────┐
│  Candidate (browser)  │    │   RabbitMQ       │  │ consumer.py     │
│  /assessment/<token>  │    │ etp_assessment_  │  │ (standalone)    │
│  no login, anti-cheat │    │      score       │◀─┤ ThreadPool, ack │
└──────────┬────────────┘    └──────────────────┘  └────────┬────────┘
           │ submit                                          │ scores
           ▼                                                 ▼
┌───────────────────────┐                          ┌─────────────────┐
│  Objective scoring    │                          │  Vertex AI      │
│  (instant, in-Odoo)   │                          │  Gemini + Imagen│
└───────────────────────┘                          │ generativelang. │
                                                   │ OR aiplatform   │
                                                   └────────┬────────┘
                                                            │ images
                                                            ▼
                                                   ┌─────────────────┐
                                                   │   AWS S3        │
                                                   │  A/B image pair │
                                                   └─────────────────┘
```

**Single LLM provider** since v19.0.0.16: Google Vertex AI. Gemini for text generation + multimodal scoring; Imagen for image generation. Prior `bedrock_*` references have been renamed throughout the codebase to `vertex_*` (see §12 for migration notes).

---

## 2. The two ways material enters the bank

### 2A. Import (research-team JSON)

Research team produces JSON with answer keys + rubrics. Loaded by `models/bank_import.py` into `etp.assessment.question` (+ `question.dimension` + `question.dimension.option` rows). `is_correct` flags drive objective scoring; `subjective_rubric_json` drives LLM scoring.

### 2B. Generate from SOP (LLM)

`models/prompt.py` houses the generator. Flow:

1. Manager uploads SOP documents (`.docx`/`.txt`/`.md`/`.csv`/`.html`/`.json`/`.xml`) → text extracted at upload via `EtpAssessmentPromptResource._extract_text()` (defusedxml-hardened for docx).
2. Manager picks **generation mode** on the prompt record:
   - **`seed`** (default, research-team design): paste one **golden example question**, set `max_questions` (0 = model decides). ONE Vertex Gemini call → flat question list.
   - **`skills`** (legacy two-stage): first extract skills (`action_extract_skills`), edit per-skill `max_questions`, then generate (`action_generate_questions`).
3. Draft questions land in `etp.assessment.prompt.question`. For `image_comparison` type, the LLM emits `image_prompt_a` / `image_prompt_b` text-to-image prompts.
4. (Optional) `action_generate_images()` calls `vertex_images.generate_image_b64()` for each draft → atomic A+B pair via `s3_service.upload_image_pair()` (with rollback on B-fail).
5. Manager approves drafts (`action_approve`) → real `etp.assessment.question` rows are created in the bank with the question's `category_id` (auto-created `Gen: <prompt name>` if unset).

Both paths produce the same `etp.assessment.question` shape, so everything downstream (assessment creation, candidate UI, scoring) is provider-agnostic.

---

## 3. Data model (10 + 4 entities)

### Master / configuration layer

| Model | Purpose | Key fields |
|---|---|---|
| `etp.assessment.category` | Group questions | `name`, `question_ids` |
| `etp.assessment.dimension` | Rating axis (e.g. "Visual Quality") | `name`, `option_ids` |
| `etp.assessment.dimension.option` | Selectable answer on a dimension | `name`, `dimension_id` |
| `etp.assessment.question` | Question bank entry | `question_type`, `prompt`, `category_id`, `subjective_rubric_json`, media fields |
| `etp.assessment.question.dimension` | Join (question ↔ dimension) | `option_line_ids` |
| `etp.assessment.question.dimension.option` | Per-question dimension option with answer key | `is_correct` (only ONE per dim) |

### Operational layer

| Model | Purpose | State |
|---|---|---|
| `etp.assessment` | The test instance | `draft → in_progress → done` (or `cancelled`) |
| `etp.assessment.evaluator` | Candidate assignment | `pending → in_progress → submitted` |
| `etp.assessment.response` | One candidate's answer to one question | `draft → submitted` (locked) |
| `etp.assessment.response.line` | Selected option for one dimension | — |

### Prompt (LLM-gen) layer

| Model | Purpose | State |
|---|---|---|
| `etp.assessment.prompt` | One generation session | `draft → skills_ready → generating → done` |
| `etp.assessment.prompt.skill` | Skill extracted in `skills` mode | — |
| `etp.assessment.prompt.question` | Draft question pre-approval | `draft → approved\|denied` |
| `etp.assessment.prompt.resource` | Uploaded SOP file | — |

### Cardinality

```
category 1─N question N─M assessment 1─N evaluator 1─N response 1─N response.line
                │                                        │
                │ question.dimension 1─N option_line       │
                │                                        │
              dimension 1─N option                       │
                                                         │
                                          evaluator → hr.employee (M2O)
                                          response → question (M2O)
```

---

## 4. End-to-end flow: candidate test

```
┌──────────────────────────────────────────────────────────────────────┐
│ STEP                       FILE / LINE                               │
├──────────────────────────────────────────────────────────────────────┤
│ 1. Manager creates assessment, picks category, sets duration,         │
│    proctoring rules, llm_auto_score                                   │
│                            views/assessment_views.xml                 │
│                                                                       │
│ 2. Manager assigns candidates (M2M tags OR CSV import)                │
│    action_import_candidates_csv() auto-creates hr.employee on miss   │
│                            models/assessment.py:174-252               │
│                                                                       │
│ 3. Manager clicks "Start Assessment" → action_start()                 │
│    - validates state/evaluators/category/questions                    │
│    - picks question_limit items from category, random.shuffle         │
│      per candidate, stores question_order JSON + UUID access_token   │
│      on etp.assessment.evaluator                                     │
│    - state → in_progress, start_date stamped                         │
│    - _send_assessment_emails() renders mail.template via web.base.url│
│                            models/assessment.py:400-442               │
│                                                                       │
│ 4. Candidate opens email link `/assessment/<access_token>`            │
│    - sees instructions page, anti-cheat rules summary                 │
│    - clicks Start → started_at stamped → evaluator.state in_progress │
│                            controllers/portal.py                      │
│                                                                       │
│ 5. Candidate answers questions one-by-one (countdown timer,           │
│    proctoring detectors armed per rule_block_* fields).               │
│    For each: select dimension options + write justification          │
│    → POST portal/<token>/submit → response.action_submit()           │
│                                                                       │
│ 6. action_submit() runs OBJECTIVE scoring instantly:                  │
│    counts is_correct matches across response.line_ids                 │
│    → response.score / max_score (per dim with answer key)            │
│                            models/assessment.py (response model)      │
│                                                                       │
│ 7. If response has a non-empty justification and the assessment       │
│    has llm_auto_score=True OR llm_auto_score=False (manual later):    │
│    _enqueue_subjective_scoring():                                     │
│    - publish to RabbitMQ etp_assessment_score queue → llm_state=queued│
│    - if broker unreachable → fall back to llm_state=pending           │
│                       services/rabbitmq_service.py                    │
│                                                                       │
│ 8. Candidate submits last question OR timer expires → portal          │
│    auto-submits any remaining as "[Auto-submitted: timeout]" (these   │
│    are skipped by LLM scoring via needs_llm filter)                   │
│    _check_all_submitted() → evaluator.is_locked=True                  │
│    _check_assessment_complete() → assessment.state=done when ALL      │
│    evaluators submitted                                               │
│                                                                       │
│ 9. RabbitMQ consumer.py picks up messages → XML-RPC into Odoo:        │
│    etp.assessment.response.rmq_score_subjective(id)                   │
│    → vertex_scoring.score_one_response(env, response):                │
│       - builds JSON item with q.prompt, candidate justification,      │
│         dim selections, correct options, rubric                       │
│       - for image_comparison/image_text: inlines A/B images (binary   │
│         or fetched URL, base64-encoded with mime sniff)               │
│       - _call_vertex() → Vertex AI Gemini generateContent             │
│         with inlineData parts (the model SEES the images)             │
│       - parses {"score": 0.0-1.0, "feedback": "..."}                  │
│    → response.llm_state=scored, llm_passed, llm_score/llm_max_score   │
│      derived against subjective_pass_threshold (default 0.7)          │
│                            services/vertex_scoring.py                 │
│                                                                       │
│ 10. _compute_subjective_rollup() updates evaluator.llm_state:         │
│     pending → scoring → scored (or partial / failed)                  │
│     evaluator.score_percent = (obj + llm) / (obj_max + llm_max) * 100│
│     evaluator.result = pass if score_percent >= pass_threshold (70)   │
│                                                                       │
│ 11. Backup path: cron _cron_llm_auto_score (1-min interval):          │
│     - auto-enqueue submitted candidates on llm_auto_score assessments │
│     - drain pending responses inline if broker down (calls            │
│       rmq_score_subjective directly)                                  │
│     - rescue stale queued responses older than 5 minutes              │
│     - bounded retry: skip responses with llm_attempts >= 3            │
│                            models/assessment.py:481-549               │
└──────────────────────────────────────────────────────────────────────┘
```

---

## 5. State machines

### Assessment
```
draft ──action_start──▶ in_progress ──action_done OR all-submitted──▶ done
  ▲                          │
  │                          ├──action_cancel──▶ cancelled
  │                          │                       │
  └──action_reset_draft──────┴───────────────────────┘
```

### Evaluator (one per candidate per assessment)
```
pending ──portal "Start"──▶ in_progress ──all responses submitted──▶ submitted
                                            (or timer expires + auto-submit)
```

### Response (one per candidate per question)
```
draft ──action_submit──▶ submitted (locked, cannot edit)
```

### Response.llm_state (subjective scoring sub-machine)
```
not_needed (no justification, or "[Auto-submitted" placeholder)

needed:
  pending ──enqueue OK──▶ queued ──consumer scores──▶ scored
     │                       │                          ▲
     │ broker down           │ stale > 5 min            │
     ▼                       ▼                          │
  pending (cron drains)   pending (cron re-enqueues) ───┘
     │
     └─ retry exhausted (attempts >= 3) ─▶ failed
```

---

## 6. LLM integration (Vertex AI)

`services/vertex_questions.py:_gemini_request()` chooses the endpoint by which credential is set. **Bearer wins when both are present.**

| Credential | Endpoint | Header | Use when |
|---|---|---|---|
| `vertex_api_key` (`AIza…`) | `https://generativelanguage.googleapis.com/v1beta/models/<model>:<suffix>` | `x-goog-api-key: <key>` | Text gen + scoring only |
| `vertex_access_token` (OAuth bearer, expires ~1h) | `https://<location>-aiplatform.googleapis.com/v1/projects/<project>/locations/<location>/publishers/google/models/<model>:<suffix>` | `Authorization: Bearer <token>` | Required for **Imagen** image gen |

**The 401 trap**: pasting an `AIza…` key into the `vertex_access_token` slot makes the code send `Authorization: Bearer AIza…` to `aiplatform.googleapis.com`, which rejects it with:
```
"Expected OAuth 2 access token, login cookie or other valid authentication credential."
```
This is `UNAUTHENTICATED` (HTTP 401). See §10 for the fix.

**Imagen image generation is Vertex-AI-only.** With api-key-only setup, image generation will fail. Clear `etp_assessment.vertex_image_model` to disable cleanly (drafts will keep their `image_prompt_a/b` text and `action_approve` records the prompts in the bank question's description for out-of-band generation).

### Service surface (callable from models/controllers)

```python
# services/vertex_questions.py
extract_skills(env, source_text, system_prompt=None)
    → [{"skill": ..., "reason": ..., "suggested_max": int}, ...]

generate_questions_from_seed(env, sop_text, golden_example,
                             system_prompt=None, max_questions=0, max_tokens=8000)
    → [{"skill": ..., "title": ..., "prompt": ..., "type": ...,
        "image_prompt_a": ..., "image_prompt_b": ...}, ...]

generate_questions(env, source_text, skills, system_prompt=None, max_tokens=8000)
    → same shape, grouped by skill, per-skill caps enforced

# services/vertex_scoring.py
score_one_response(env, response)
    → {"score01": 0..1, "feedback": "..."}

# services/vertex_images.py
is_configured(env) → bool
generate_image_b64(env, prompt, width=1024, height=1024) → str (base64 PNG)
```

### Tolerance built into the parsers

- LLM responses with stray prose around JSON: `_extract_json_array` and `_extract_json_object` strip ```json fences and regex-fallback to `{.*}` or `[.*]`.
- Score reply shape tolerance: `{score: 0..1}` used directly, `{score, max_score}` normalized, `{passed: bool}` mapped to 1.0/0.0, otherwise 0.0.

---

## 7. RabbitMQ + cron fallback

**Queue:** `etp_assessment_score` (env override: `ETP_SCORE_QUEUE`)
**Message shape:** `{"response_id": <int>, "action": "score"}`, persistent (delivery_mode=2)
**Connection:** module-level cached `_connection`/`_channel`, single-thread lock, heartbeat=600, blocked_connection_timeout=300.

`services/rabbitmq_service.py:publish_score_task(response_id)`:
- Reconnects automatically on `AMQPConnectionError`/`AMQPChannelError` (one retry, then re-raises).
- Caller handles the re-raise as a broker-down signal and falls back to `llm_state='pending'`.

`consumer.py` (standalone, **outside Odoo**):
- ThreadPoolExecutor, `CONSUMER_WORKERS=5` default
- Manual ack, retry-with-backoff (`CONSUMER_RETRY_BACKOFF=30`, exponential, capped 600s)
- `CONSUMER_MAX_RETRIES=5` default; permanent-failure heuristic drops messages whose error matches `"record does not exist" / "has been deleted" / "no justification" / "access denied" / "access error"`
- XML-RPC into `etp.assessment.response.rmq_score_subjective(id)` with `XMLRPC_TIMEOUT=600`

**Cron drainer** (`_cron_llm_auto_score`, 1-min interval, `data/cron.xml`):
1. Auto-enqueue submitted candidates on assessments with `llm_auto_score=True`
2. Drain `pending` responses inline if broker is down (`rmq_score_subjective` called directly in the cron's transaction)
3. Rescue stale `queued` responses older than 5 minutes (re-enqueue)
4. Bounded retry: skip responses where `llm_attempts >= MAX_ATTEMPTS=3`

The system stays functional with **either** the broker **or** the cron alone. Local dev doesn't need RabbitMQ — the cron drainer handles everything (slower, but correct).

---

## 8. S3 image hosting

`services/s3_service.py` is OFF until creds are set (anything containing `PLACEHOLDER` counts as unset). When OFF, generated images stay inline on the record as `Binary attachment=True`.

**Atomic pair upload** (`upload_image_pair`): if B fails after A succeeded, A's object is deleted so no orphan is left. Reuses one boto3 client. Retries transient errors (`RequestTimeout`, `Throttling`, `503`, `500`, `BotoCoreError`, `ConnectionError`) with exponential backoff (1s, 2s, 4s, capped at `s3_max_retries`, default 3).

**Public read trap** (smoke_live.py stage 4): the upload succeeding does NOT mean the candidate's browser can see the image. If your bucket objects are not publicly readable, the assessment portal silently renders broken images. Either set the bucket policy or set `etp_assessment.s3_cdn_url` to a front like CloudFront.

---

## 9. System Parameters reference

All under `etp_assessment.*`. Anything containing `PLACEHOLDER` is treated as unset. Seeded `noupdate=1` in `data/llm_config_parameters.xml` so module upgrades NEVER overwrite pasted values.

### LLM (Vertex AI)
| Key | Default | Purpose |
|---|---|---|
| `vertex_project_id` | `PLACEHOLDER_VERTEX_PROJECT_ID` | GCP project (required for OAuth bearer path) |
| `vertex_location` | `us-central1` | Vertex AI region |
| `vertex_model` | `gemini-3-pro` | Text + scoring model |
| `vertex_api_key` | `PLACEHOLDER_VERTEX_API_KEY` | Gemini Developer API key (`AIza…`) |
| `vertex_access_token` | `PLACEHOLDER_VERTEX_ACCESS_TOKEN` | Vertex AI OAuth bearer (~1h lifetime). **Either** api_key OR this, not both. |
| `vertex_image_model` | `imagen-4.0-generate-001` | Imagen model; clear to disable image gen |
| `seed_system_prompt` | embedded default | Live seed-mode prompt (research team edits in place) |
| `scoring_system_prompt` | embedded default | Live scoring prompt |
| `skills_system_prompt` | embedded default | Legacy two-stage mode only |
| `questions_system_prompt` | embedded default | Legacy two-stage mode only |

### Scoring thresholds
| Key | Default | Purpose |
|---|---|---|
| `pass_threshold` | `70` | Overall pass % cutoff (`evaluator.score_percent >= this → pass`) |
| `subjective_points` | `10` | Points awarded per subjective question on PASS |
| `subjective_pass_threshold` | `0.7` | LLM 0..1 score cutoff for subjective PASS |

### S3 image hosting
| Key | Default | Purpose |
|---|---|---|
| `s3_bucket` | `PLACEHOLDER_S3_BUCKET` | Target bucket |
| `s3_region` | `us-east-1` | AWS region |
| `s3_access_key_id` | `PLACEHOLDER_S3_ACCESS_KEY_ID` | AWS access key |
| `s3_secret_key` | `PLACEHOLDER_S3_SECRET_KEY` | AWS secret key |
| `s3_folder` | `etp_assessment` | Key prefix |
| `s3_cdn_url` | (empty) | Optional CDN base; replaces virtual-hosted URL |
| `s3_max_retries` | `3` | Per put_object before giving up |

---

## 10. Local test commands

### 10.1 Install / upgrade the module

```bash
# Fresh install
./odoo-bin -c odoo.conf -d ethara_dev -i etp_assessment --stop-after-init

# Upgrade after pulling these changes (vertex rename + version bump)
./odoo-bin -c odoo.conf -d ethara_dev -u etp_assessment --stop-after-init
```

### 10.2 Set the Vertex AI credential (api-key path, recommended for dev)

In Odoo: Settings → Technical → Parameters → System Parameters → filter `etp_assessment.`

1. **Clear** `etp_assessment.vertex_access_token` (delete the record or empty its value)
2. Set `etp_assessment.vertex_api_key` to your `AIza…` key
3. Confirm `etp_assessment.vertex_model` is a model your key serves (e.g. `gemini-2.5-flash` if `gemini-3-pro` is unavailable)
4. Optional: clear `etp_assessment.vertex_image_model` to cleanly disable image generation

### 10.3 Verify the API key out-of-band (before retrying through Odoo)

```bash
AIZA_KEY="AIza...YOURKEY..."
curl -sS -X POST \
  -H "x-goog-api-key: $AIZA_KEY" \
  -H "Content-Type: application/json" \
  "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent" \
  -d '{"contents":[{"role":"user","parts":[{"text":"ping"}]}]}'
```

- HTTP 200 with `candidates[].content.parts[].text` → key works
- HTTP 400 with `API_KEY_INVALID` → key is the problem
- HTTP 401 with `Expected OAuth 2 access token` → you accidentally hit the aiplatform endpoint (wrong header)

### 10.4 Run the live smoke test (no mocks, rolls back DB)

```bash
# tests Vertex text + Imagen + S3 upload + the 403 public-read trap
.venv/bin/python src/odoo-bin shell -c odoo.conf -d ethara_dev --log-level=error \
    < custom_addons/etp_assessment/scripts/smoke_live.py
```

Expected output:
```
PASS 0. Vertex configured
PASS 1. Vertex Gemini text call
PASS 2. Vertex Imagen image call -> base64    (FAIL with api-key only — Imagen needs bearer)
PASS 3. S3 upload -> URL                       (FAIL if S3 unconfigured)
PASS 4. Generated image is publicly fetchable (no 403)
PASS 5. Cleanup smoke object
```

### 10.5 Run the RabbitMQ consumer (optional for local; cron handles it without)

```bash
# Local rabbit (one-time):
brew install rabbitmq
brew services start rabbitmq

# Run consumer (uses defaults guest/guest@localhost:5672)
ODOO_URL=http://localhost:8069 ODOO_DB=ethara_dev \
ODOO_USERNAME=admin ODOO_PASSWORD=admin \
python custom_addons/etp_assessment/consumer.py
```

Without RabbitMQ, the in-Odoo cron `_cron_llm_auto_score` (1-min interval) drains pending responses directly — no message broker required for local dev.

### 10.6 Quick smoke of the prompt generation endpoint

```bash
# After logging in via /web/session/authenticate and capturing the session_id cookie:
curl -sS -X POST \
  -H "Content-Type: application/json" \
  -H "Cookie: session_id=$SESSION_ID" \
  http://localhost:8069/etp_assessment/prompt/config_status \
  -d '{"jsonrpc":"2.0","method":"call","params":{}}'
```

Expected when api_key is set:
```json
{"jsonrpc":"2.0","id":null,"result":{"configured":true,"region":"us-central1",
 "has_arn":false,"has_token":true}}
```

### 10.7 End-to-end candidate flow (manual)

1. Create a category + a few questions (some with dimensions + correct options, some with `subjective_rubric_json`).
2. Create an assessment, assign yourself (`hr.employee` with your work email).
3. Click Start Assessment.
4. Check your inbox/log for the invitation email containing `/assessment/<token>`.
5. Open the link in an incognito window (anti-cheat is real — tab-switch will auto-submit if `rule_block_tab_switch=True` + `violation_action=auto_submit`).
6. Answer + submit. Verify `etp.assessment.response.score` is populated instantly.
7. Verify `etp.assessment.response.llm_state` becomes `scored` within ~1 minute (cron) or seconds (broker).

### 10.8 Inspect a specific candidate's scoring

```python
# odoo-bin shell
ev = env['etp.assessment.evaluator'].browse(<id>)
print("Objective:", ev.total_score, "/", ev.max_possible_score)
print("LLM:     ", ev.llm_total_score, "/", ev.llm_max_score,
      "state=", ev.llm_state, "pending=", ev.subjective_pending)
print("Result:  ", ev.score_percent, "%", "->", ev.result)
for r in ev.response_ids.filtered(lambda x: x.needs_llm):
    print(" -", r.question_id.name, r.llm_state, r.llm_score, "/",
          r.llm_max_score, "->", r.llm_feedback[:120] if r.llm_feedback else "—")
```

---

## 11. Troubleshooting

### 11.1 `Vertex error [401]: "Expected OAuth 2 access token..."`

**Cause:** the code took the bearer branch because `vertex_access_token` was set, but the bearer is either expired (OAuth tokens last ~1h) or wrong type (an `AIza…` key was pasted into the access_token slot).

**Fix (api-key path):**
1. Settings → Technical → Parameters → System Parameters
2. Clear or delete `etp_assessment.vertex_access_token`
3. Confirm `etp_assessment.vertex_api_key` holds the `AIza…` key
4. Retry

**Fix (OAuth path):** generate a fresh `gcloud auth application-default print-access-token` and paste it. It will break again in ~1h — that's the documented limitation.

### 11.2 `Vertex image error [401]: ...` after the text call works

**Cause:** Imagen is Vertex-AI-only. With api-key-only, image generation hits `generativelanguage.googleapis.com/v1beta/models/imagen-…:predict` which doesn't host Imagen.

**Fix:** clear `etp_assessment.vertex_image_model` to disable image gen. Approved drafts will carry their image prompts in the bank question's description; generate the actual images out-of-band when you have bearer + project.

### 11.3 Bucket returns 403 to candidate's browser

`smoke_live.py` stage 4 catches this. The S3 upload succeeded but the bucket's object ACL is not public-read. Either:
- Make bucket objects publicly readable (object ACL or bucket policy), or
- Front with CloudFront and set `etp_assessment.s3_cdn_url` to the CDN base.

### 11.4 Candidate's `result` stays `pending` forever

Check `evaluator.subjective_pending`. If > 0, LLM scoring hasn't finished. Either:
- Broker is down → cron will drain within 1 min
- All retries exhausted → check `response.llm_state='failed'` rows, look at `llm_error` field

### 11.5 RabbitMQ down, no cron firing

Cron interval is 1 min. If it's silent, check `ir.cron` for the row whose `cron_name` matches the LLM auto-score job, confirm it's `active=True`, and check the Odoo log for stack traces.

---

## 12. Recent changes (v19.0.0.15 → v19.0.0.16)

**Background:** The module had migrated from AWS Bedrock to Google Vertex AI Gemini, but file names, function names, and docs still said `bedrock`. The 401 the user hit was a configuration mistake (api_key pasted into the access_token slot), but the misleading names made it harder to diagnose.

**Renames** (all under `custom_addons/etp_assessment/`):
- `services/bedrock_questions.py` → `services/vertex_questions.py`
- `services/bedrock_scoring.py` → `services/vertex_scoring.py`
- `services/bedrock_images.py` → `services/vertex_images.py`
- Function `_call_bedrock()` (was a back-compat shim) — **deleted**; the 3 internal callers now call `_call_vertex()` directly
- All Python imports and identifier references across `models/`, `controllers/`, `scripts/`, and the renamed `services/` files were updated (26 sites via AST-aware rewrite).

**Docs:**
- Module docstrings in the renamed service files rewritten to describe the Vertex AI Gemini single-provider stack and the auth-routing logic.
- `docs/PROMPT_API.md` and `docs/API_CHANGES_FOR_EXTENSION.md` updated: removed `bedrock_inference_arn`/`bedrock_bearer_token`/`bedrock_region`/`bedrock_image_model_id`/`llm_provider`/`openrouter_*` references; added the full Vertex AI System Parameters table with explicit auth-routing guidance and the 401-trap warning.
- This file (`ETP_ASSESSMENT_FLOW.md`) is new — the consolidated engineering reference.

**Not changed** (intentional):
- `data/llm_config_parameters.xml` — was already correct (seeds `vertex_*` keys).
- `consumer.py`, `services/rabbitmq_service.py`, `services/s3_service.py` — provider-agnostic.
- The top-level `/ETP_ASSESSMENT.md` — separately owned high-level overview.
- `README.md` inside the module — describes the legacy binary-scoring flow; the canonical scoring reference is now §4 + §6 of this doc.

---

## 13. Where to look next

| Want to understand | Read |
|---|---|
| Manifest / data file load order | `__manifest__.py` |
| Anti-cheat client-side JS | `views/portal_templates.xml` |
| Portal HTTP endpoints | `controllers/portal.py` |
| Mobile/Flutter JSON-RPC API | `docs/PROMPT_API.md` |
| REST extension (other team) | `docs/API_CHANGES_FOR_EXTENSION.md` |
| Live config smoke test | `scripts/smoke_live.py` |
| Standalone scoring worker | `consumer.py` |
| Question bank import | `models/bank_import.py` |
| Generation prompts (editable) | System Parameters `*_system_prompt` |


---

# §13. Updated Architecture (post-refactor)

## Vertex AI as single LLM provider

All LLM functionality runs on Google Vertex AI. Files were renamed from `bedrock_*.py` → `vertex_*.py` but retain their function names and public API. The auth router (`services/vertex_questions.py:_gemini_request`) picks the endpoint automatically:

| Credential | Endpoint | Header | Use case |
|---|---|---|---|
| `vertex_api_key` starting with `AQ.` | `aiplatform.googleapis.com/v1/publishers/google/models/{model}:{suffix}` | `x-goog-api-key` | Vertex AI Express Mode — text + Imagen via single key |
| `vertex_api_key` starting with `AIza` | `generativelanguage.googleapis.com/v1beta/models/{model}:{suffix}` | `x-goog-api-key` | Gemini Developer API — text only, no Imagen |
| `vertex_access_token` (OAuth bearer) | `{location}-aiplatform.googleapis.com/v1/projects/{project}/locations/{location}/...` | `Authorization: Bearer` | Vertex AI service-account, project-scoped |
| `vertex_service_account_json` (uploaded JSON) | Same as bearer, with auto-minted token | `Authorization: Bearer` | Production — JWT signed, ~1h cached, auto-refreshed |

**Models verified working (June 2026)**:
- Text/scoring (us-central1): `gemini-2.5-pro`, `gemini-2.5-flash`, `gemini-2.5-flash-lite`
- Text Gemini 3 family (location=`global` only): `gemini-3.1-pro-preview`, `gemini-3-flash-preview`
- Image generation (us-central1): `imagen-4.0-generate-001`

Gemini 1.5 family deprecated (Sept 2024 → removed Sept 2025). Gemini 2.0 superseded by 2.5.

## Batched per-evaluator scoring (5-20x cost reduction)

Old per-response model: 1 Vertex call per question × N questions = N calls/candidate.
New per-evaluator model: 1 Vertex call per candidate, all questions batched.

`services/vertex_scoring.py::score_evaluator(env, evaluator)` collects all needs_llm responses for one candidate, attaches images (≤ `MAX_IMAGES_PER_CALL=16`), sends one multimodal call, parses a JSON array of `{id, score, feedback}`. Missing IDs get a synthetic placeholder feedback. Excess images log a warning and degrade to text-only for those specific responses.

Called by:
- `evaluator.action_llm_score()` (manual button) — with fallback to per-response enqueue on batch failure
- `_cron_llm_auto_score()` (cron drain) — groups pending by evaluator, batches each
- RabbitMQ consumer (`consumer.py`) — one message per evaluator, one batch per call

Legacy `score_one_response()` and `rmq_score_subjective()` retained for any in-flight pre-migration messages still in the queue.

## RabbitMQ pipeline with DLX/DLQ

`services/rabbitmq_service.py` — production-grade pattern (modeled on `t2av/`):

- **Topology** (declared idempotently on first connect):
  - Main queue `etp_assessment_score` with `x-dead-letter-exchange` arg
  - Direct DLX `etp_assessment_score.dlx`
  - DLQ `etp_assessment_score.dead` for permanently-failed messages (auditable in RabbitMQ admin)
- **Message body**: `{evaluator_id, action, published_at}` (per-candidate, not per-response)
- **Retry counter** in `x-retry-count` header — consumer re-publishes with incremented value on transient error
- **Chunked publish** (`batch_publish_evaluator_score_tasks`): 50 msgs/chunk + 0.1s sleep — prevents broker overrun on 500-burst submits
- **Env enforcement** (`_require_env`): refuses to publish if `RABBITMQ_HOST`/`USERNAME`/`PASSWORD` unset (no silent fallback to localhost)

**Concurrency cap = `CONSUMER_WORKERS` env var.** 10 workers means at most 10 concurrent Vertex calls — natural backpressure under load. 500 candidates × 1 msg × 10 workers ≈ 50 batches × ~5s each ≈ 4 minutes total, ~10 RPS sustained.

## Race-safety and reliability

- **`_cron_llm_auto_score` single-flight**: `pg_try_advisory_xact_lock(827193)` at the top. Manual triggers overlapping the scheduled cron tick (or two cron workers in multi-process mode) skip cleanly instead of racing on UPDATE.
- **Image fetch hardening** (`_fetch_image_b64`): rejects `Content-Type != image/*` + verifies magic bytes (PNG/JPEG/WebP/GIF/HEIC). Expired Facebook CDN URLs / HTML error pages dropped silently with warning, never forwarded to Gemini (avoids `400 "Provided image is not valid"` cost).
- **Binary image path equally defensive**: same magic-byte verify in `_question_images` binary branch.
- **Email fallback**: candidates with no `work_email`/`private_email`/user email get a saved `mail.mail` row in `state='cancel'` with the candidate's link rendered in the body AND spelled out in `failure_reason`. Admin can copy the link from Technical → Emails instead of digging through database tokens.
- **JSON-RPC compliance**: all routes migrated from `type='json'` (Odoo 19 deprecated alias) to `type='jsonrpc'`.

## Settings UI (Settings → ETP Assessment)

| Block | Fields |
|---|---|
| Vertex AI (LLM) | Text/Scoring Model, Image Model, API Key (masked), Service Account JSON (file upload) |
| Scoring | Pass Threshold (%), Subjective Points/Question, Subjective Pass Threshold |
| S3 Image Hosting | Bucket, Region, Access Key ID, Secret Key (masked), Key Prefix, CDN Base URL, Max Upload Retries |
| System Prompts | Seed Mode Prompt, Scoring Prompt, Skills Prompt (legacy), Questions Prompt (legacy) — live, applied next call, no deploy |

---

# §14. Configuration Reference

## System Parameters (Odoo)

**LLM auth — pick ONE of these credential paths:**

| Key | Required | Default | Notes |
|---|---|---|---|
| `etp_assessment.vertex_api_key` | one-of | — | `AQ.*` (Vertex Express, supports Imagen) OR `AIza*` (Gemini Developer, text only) |
| `etp_assessment.vertex_service_account_json` | one-of | — | Full SA JSON; auto-mints OAuth bearer + auto-refreshes |
| `etp_assessment.vertex_access_token` | one-of | — | Static OAuth bearer (~1h lifetime; for ad-hoc only) |

**LLM models + region:**

| Key | Default | Notes |
|---|---|---|
| `etp_assessment.vertex_project_id` | — | Backfilled from SA JSON. Required for OAuth bearer path |
| `etp_assessment.vertex_location` | `us-central1` | Use `global` for Gemini 3 family |
| `etp_assessment.vertex_model` | `gemini-3-pro` (stale default) | Set to a real model: `gemini-2.5-pro` / `gemini-2.5-flash` / `gemini-2.5-flash-lite` / `gemini-3.1-pro-preview` |
| `etp_assessment.vertex_image_model` | `imagen-4.0-generate-001` | Clear to disable image generation |

**Scoring tunables:**

| Key | Default | Notes |
|---|---|---|
| `etp_assessment.pass_threshold` | `70` | Final % threshold for pass/fail |
| `etp_assessment.subjective_points` | `10` | Max LLM points awarded per subjective question |
| `etp_assessment.subjective_pass_threshold` | `0.7` | LLM raw score (0..1) cutoff for passing a subjective question |
| `etp_assessment.scoring_system_prompt` | (built-in) | Live; overrides default if set |
| `etp_assessment.seed_system_prompt` | (built-in) | Live; overrides default if set |

**S3 (optional but required for browser-served generated images):**

| Key | Required | Default | Notes |
|---|---|---|---|
| `etp_assessment.s3_bucket` | yes (if S3 used) | — | Must allow public read (or be fronted by CDN) |
| `etp_assessment.s3_region` | no | `us-east-1` | |
| `etp_assessment.s3_access_key_id` | yes (if S3 used) | — | IAM perms: `s3:PutObject`, `s3:GetObject`, `s3:DeleteObject` |
| `etp_assessment.s3_secret_key` | yes (if S3 used) | — | |
| `etp_assessment.s3_folder` | no | `etp_assessment` | Key prefix inside bucket |
| `etp_assessment.s3_cdn_url` | no | — | e.g. `https://cdn.example.com` |
| `etp_assessment.s3_max_retries` | no | `3` | Per-upload retry cap |

## Environment Variables (RabbitMQ + consumer)

**Required on both Odoo host AND consumer.py runtime:**

| Var | Required | Default | Notes |
|---|---|---|---|
| `RABBITMQ_HOST` | yes | — | `_require_env` refuses to publish without it |
| `RABBITMQ_USERNAME` | yes | — | |
| `RABBITMQ_PASSWORD` | yes | — | |
| `RABBITMQ_PORT` | no | `5672` | |
| `RABBITMQ_VHOST` | no | `/` | |
| `ETP_SCORE_QUEUE` | no | `etp_assessment_score` | |
| `ETP_SCORE_DLX` | no | `etp_assessment_score.dlx` | |
| `ETP_SCORE_DLQ` | no | `etp_assessment_score.dead` | |
| `RABBITMQ_BATCH_CHUNK` | no | `50` | Publish chunk size |
| `RABBITMQ_CHUNK_DELAY` | no | `0.1` | Inter-chunk sleep (seconds) |

**Required only on consumer.py runtime:**

| Var | Required | Default | Notes |
|---|---|---|---|
| `CONSUMER_WORKERS` | no | `5` | **Concurrency cap on Vertex calls** — set to 10 for production |
| `CONSUMER_MAX_RETRIES` | no | `5` | Per-message retry cap before DLQ |
| `CONSUMER_RETRY_BACKOFF` | no | `30` | Base seconds, exponential (capped 600) |
| `ODOO_URL` | yes | — | Consumer's XML-RPC target |
| `ODOO_DB` | yes | — | |
| `ODOO_USERNAME` | yes | — | Account needs Assessment Manager group |
| `ODOO_PASSWORD` | yes | — | |
| `XMLRPC_TIMEOUT` | no | `600` | Seconds; batched calls can take 10–60s |

## Python dependencies

In addition to standard Odoo 19 deps, the module needs:

| Package | Why |
|---|---|
| `httpx` | Vertex API calls + image URL fetch |
| `pika` | RabbitMQ publisher (Odoo) + consumer (standalone) |
| `boto3` | S3 image upload |
| `PyJWT[crypto]` | JWT signing for Service Account JSON → OAuth bearer |
| `openpyxl` | XLSX bulk-import / seed scripts |
| `defusedxml` (optional but recommended) | XXE protection on uploaded `.docx` resource files |
| `python-dotenv` (optional) | `.env` loading in consumer.py |

Install on a fresh dev machine:
```bash
.venv/bin/pip install httpx pika boto3 'PyJWT[crypto]' openpyxl defusedxml python-dotenv
```

## Setup checklist

1. **Module install/upgrade**: `python src/odoo-bin -c odoo.conf -d <db> -u etp_assessment --stop-after-init`
2. **System Parameters**: Settings → ETP Assessment → fill Vertex API Key (or upload SA JSON) + pick a working model (`gemini-2.5-pro` recommended)
3. **S3** (optional but recommended for production): fill bucket + creds + ensure public read policy on objects
4. **Test live config**: `.venv/bin/python src/odoo-bin shell -c odoo.conf -d <db> < custom_addons/etp_assessment/scripts/smoke_live.py` — exercises Vertex text + Imagen + S3 upload + browser fetchability (the "403 trap")
5. **RabbitMQ** (for production scoring): set env vars on Odoo host; deploy `consumer.py` to a worker box with its own `.env`; set `CONSUMER_WORKERS=10`
6. **Verify scoring path**: Settings → Technical → Scheduled Actions → "ETP Assessment: LLM auto-score submitted candidates" should run every 1 min and report `fully done` cleanly
7. **Unit tests** (post-deploy sanity): `--test-tags=etp_assessment_batch_scoring`
