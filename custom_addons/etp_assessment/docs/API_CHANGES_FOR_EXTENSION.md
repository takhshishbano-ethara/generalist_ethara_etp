# ETP Assessment — Required API Changes (spec for the extension team)

The `etp_assessment` addon shipped question generation, automated scoring,
and per-assessment rules/proctoring config (June 2026). All model-side
logic lives in `etp_assessment`; the REST layer in
`etp_assessment_extension` is owned by the backend/API team and was NOT
modified. This document specifies the additive changes the extension needs
so the mobile app can use the new features.

Everything below follows the existing house pattern: `@validate_token`
auth, `return_Response` envelope, manager-role guards via
`require_assessment_manager()` from `controllers/common.py`.

## 1. Rules object on assessments

`etp.assessment` gained these fields (all editable while in progress):

| Field | Type | Default |
|---|---|---|
| `rule_block_tab_switch` | bool | True |
| `rule_block_screenshot` | bool | True |
| `rule_block_copy_paste` | bool | True |
| `rule_block_right_click` | bool | True |
| `rule_block_devtools` | bool | True |
| `rule_watermark` | bool | True |
| `violation_action` | selection `auto_submit` / `log_only` | auto_submit |
| `require_justification_image_comparison` | bool | False |
| `llm_auto_score` | bool | False |

Needed in the extension:
- Serialize a `rules` object in assessment list/detail AND in the portal
  state endpoint's assessment brief, e.g.:

```json
"rules": {
  "block_tab_switch": true,
  "block_screenshot": true,
  "block_copy_paste": true,
  "block_right_click": true,
  "block_devtools": true,
  "watermark": true,
  "violation_action": "auto_submit",
  "require_justification_image_comparison": false
}
```

- Accept the same keys on assessment create/update (validate
  `violation_action` against the two allowed values, 400 otherwise).

The client must arm only the enabled detectors, render only the enabled
rules on the instructions screen, and adapt the violation notice wording
(`auto_submit` vs `log_only`).

## 2. Portal behavior changes

`POST portal/<token>/submit`:
- `justification` becomes OPTIONAL when the question is
  `image_comparison` AND the assessment has
  `require_justification_image_comparison = False`. Required for all
  other types (unchanged). Check the same condition the website portal
  uses (`etp_assessment/controllers/portal.py`, submit handler).

`POST portal/<token>/violation`:
- Read `evaluator.assessment_id.violation_action`. When `log_only`,
  record the violation (`is_violated`, `violation_reason`,
  `violation_datetime`) but DO NOT auto-submit/lock — return
  `{"state": "question", "violation_action": "log_only"}` so the
  candidate continues. `auto_submit` keeps current behavior.

## 3. Scoring fields (read-only)

`etp.assessment.response` gained: `llm_score` (int), `llm_max_score`
(int), `llm_feedback` (text), `llm_state` (`pending`/`scored`/`failed`).
`etp.assessment.evaluator` gained: `llm_total_score`, `llm_max_score`,
`llm_state` (+ `scoring`), `llm_scored_at`, `llm_error`.

Needed: include these in response serializers and the candidate-detail
payload. Mechanical `score`/`max_score` are unchanged and independent —
expose both.

## 4. Generation surface (new endpoints)

Model: `etp.assessment.prompt` (+ `.question` drafts). Key fields:
`name`, `source_text` (SOP), `generation_mode` (`seed` default /
`skills` legacy), `golden_example` (required for seed), `max_questions`
(0 = model decides), `category_id` (auto-created on approve when empty),
`state` (`draft`/`skills_ready`/`generating`/`done`).

Draft questions carry `image_prompt_a/b`, `image_a/b` (binary),
`image_state` (`none`/`pending`/`generated`/`failed`), `image_error`.

Suggested routes (manager role), base `/api/v1/etp_assessment_ext`:

| Method | Route | Model call |
|---|---|---|
| GET | `/prompts` | search + serialize, paginate |
| POST | `/prompts` | `create()` |
| GET | `/prompts/<id>` | serialize incl. draft questions |
| PUT | `/prompts/<id>` | `write()` (validate generation_mode) |
| DELETE | `/prompts/<id>` | `unlink()` |
| POST | `/prompts/<id>/generate` | `action_generate_questions()` — seed mode raises UserError without golden_example; map to 400 |
| POST | `/prompts/<id>/extract_skills` | `action_extract_skills()` — only meaningful for `skills` mode; 400 in seed mode |
| POST | `/prompts/<id>/generate_images` | `action_generate_images()` on the draft recordset; per-question failures land on `image_state`/`image_error` |
| POST | `/prompts/questions/decision` | `action_approve()` / `action_deny()` (single ids or all pending of a prompt) |
| POST | `/assessments/<id>/llm_score` | filter submitted evaluators with `llm_state in (pending, failed)` then `action_llm_score()` |
| POST | `/candidates/<id>/llm_score` | `action_llm_score()` (raises UserError if not submitted; map to 400) |
| GET | `/llm/config_status` | see below |

Config status (lets the app show "ready / not configured" without a
failing call): use `_param` from
`odoo.addons.etp_assessment.services.vertex_questions` — it treats the
seeded `PLACEHOLDER_*` values as unset. Single provider — Google Vertex
AI (Gemini for text + scoring, Imagen for images). Check that EITHER
`etp_assessment.vertex_api_key` OR `etp_assessment.vertex_access_token`
is set (the latter additionally requires `vertex_project_id`), and
`vertex_images.is_configured(env)` for images.

All generation endpoints return clean 400s while creds are placeholders
(the services raise ValueError/RuntimeError with readable messages —
catch and map, don't let them 500).

## 5. Server configuration (ops)

System Parameters under `etp_assessment.*`, pre-seeded (noupdate) with
`PLACEHOLDER_*` values; anything containing "PLACEHOLDER" counts as
unset. Single provider — Google Vertex AI (Gemini for text + scoring,
Imagen for images).

| Key | Purpose |
|---|---|
| `vertex_project_id` / `vertex_location` (default `us-central1`) | Vertex AI project + region |
| `vertex_model` (default `gemini-3-pro`) | text + scoring model id |
| `vertex_api_key` | Gemini Developer API key (`AIza…`) — preferred for text |
| `vertex_access_token` | Vertex AI OAuth bearer (~1h lifetime) — required for Imagen |
| `vertex_image_model` (default `imagen-4.0-generate-001`) | image generation; clear to disable |
| `seed_system_prompt` / `scoring_system_prompt` | the two live prompts (research team pastes here; applied next call, no deploy) |
| `skills_system_prompt` / `questions_system_prompt` | legacy two-stage mode only |

Auth routing: provide EITHER `vertex_api_key` (routes to
`generativelanguage.googleapis.com`) OR `vertex_access_token` (routes to
`aiplatform.googleapis.com`, requires `vertex_project_id`). Never paste
an `AIza` key into the access_token slot — that yields a 401
UNAUTHENTICATED. Imagen is Vertex-AI-only, so image generation needs the
OAuth bearer path; with api-key-only, clear `vertex_image_model` to
disable image gen and let approved drafts carry the image prompts in
their description instead.

## 6. Client note

Send the gateway token as the `access-token` header (dash). Underscore
headers (`access_token`) are dropped by some proxy/WSGI setups; the dash
spelling is normalized correctly everywhere.
