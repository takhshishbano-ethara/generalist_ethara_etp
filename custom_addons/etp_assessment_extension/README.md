# ETP Assessment Extension

REST API layer for the **ETP Assessment** module. Exposes the question bank,
assessments, candidate assignments, responses, and dashboard data over JSON
endpoints, and provides a token-based candidate portal API to take the
assessment from an external client (e.g. Flutter app).

Follows the same conventions as `aurora_extension` / `employee_extension`:
routes under `/api/v1/etp_assessment_ext/...`, `auth="none"` +
`@validate_token`, and the `api_auth_gateway` `return_Response` envelope.

## Companion docs

- [`docs/new_extension.md`](./docs/new_extension.md) — the 5 endpoints added in the
  previous change (violations + deep-detail views).
- [`docs/pending_apis.md`](./docs/pending_apis.md) — the 11 endpoints added in the
  latest change (binary question images, admin candidate token /
  invitation operations, bulk question import, CSV template download,
  employee picker). Includes a full 55-route inventory table.
- [`postman/pending_apis.postman_collection.json`](./postman/pending_apis.postman_collection.json) — ready-to-run Postman collection for the 11 pending endpoints.
- [`postman/`](./postman/) — all Postman v2.1.0 collections (canonical + dated `exports/`) and the `tools/gen_postman.py` generator.
- [`docs/`](./docs/) — additional API documentation and change-log markdown.

## Auth & envelope

- Header: `access_token: <token>` (validated by `@validate_token`).
- Caller must be in `etp_assessment.group_assessment_evaluator` (read) or
  `etp_assessment.group_assessment_manager` (full CRUD).
- Response body (all admin endpoints):

```json
{
  "message": "OK",
  "errors": [],
  "status_code": 200,
  "<payload_key>": { ... }
}
```

The candidate-facing portal endpoints (`/portal/...`) use the per-candidate
`access_token` (passed as a path / query param), **not** the gateway token.

## Endpoints

### Dashboard

#### `GET /api/v1/etp_assessment_ext/dashboard_overview`
KPIs, charts, active work and top candidates — live from `etp.assessment.*`.

Query params (all optional):
- `assessment_id`, `category_id`, `state`, `date_from`, `date_to`

```json
{
  "role": "manager",
  "blocks": [
    {"type": "kpi", "items": [
      {"key": "total_assessments", "label": "Total Assessments", "value": "12",
       "sub_string": "5 in progress"},
      {"key": "total_questions",   "label": "Questions",       "value": "240"},
      {"key": "total_candidates",  "label": "Candidates",      "value": "78",
       "sub_string": "42 submitted"},
      {"key": "completion_rate",   "label": "Completion Rate", "value": "53.8%"}
    ]},
    {"type": "chart", "variant": "doughnut", "title": "Question types",
     "items": [{"label": "Text", "value": 100, "percent": 41.67}]},
    {"type": "chart", "variant": "bar", "title": "Questions per category",
     "items": [{"label": "Frontend", "value": 32, "percent": 13.33}]},
    {"type": "table", "title": "Active assessments",
     "columns": [{"key": "name", "label": "Assessment", "type": "string"}],
     "rows":    [{"id": 4, "name": "Q4 Eval", "evaluators_total": 10,
                  "evaluators_done": 4, "end_date": "2026-06-30"}]},
    {"type": "table", "title": "Top candidates",
     "columns": [...],
     "rows":    [{"name": "John Doe", "total_score": 8, "max_possible": 12}]}
  ]
}
```

### Question bank

#### `GET /api/v1/etp_assessment_ext/categories`
Paginated category list. Query params: `search`, `active`, `page`, `limit`,
`sort_by` (name|sequence|create_date), `sort_order`.

#### `POST /api/v1/etp_assessment_ext/categories`
Body: `{ "name": "...", "sequence": 10, "description": "..." }`

#### `PUT /api/v1/etp_assessment_ext/categories/<id>`
#### `DELETE /api/v1/etp_assessment_ext/categories/<id>`

#### `GET /api/v1/etp_assessment_ext/dimensions`
Master dimensions with their options.

#### `POST /api/v1/etp_assessment_ext/dimensions`
```json
{"name": "Visual Quality", "sequence": 10,
 "options": [{"name": "Response A", "sequence": 1}, ...]}
```

#### `PUT /api/v1/etp_assessment_ext/dimensions/<id>`
#### `DELETE /api/v1/etp_assessment_ext/dimensions/<id>`

#### `POST /api/v1/etp_assessment_ext/dimension_options`
```json
{"dimension_id": 1, "name": "Response B", "sequence": 2}
```

#### `PUT /api/v1/etp_assessment_ext/dimension_options/<id>`
#### `DELETE /api/v1/etp_assessment_ext/dimension_options/<id>`

#### `GET /api/v1/etp_assessment_ext/questions`
Paginated question list. Filters: `category_id`, `question_type`, `search`,
`active`.

#### `POST /api/v1/etp_assessment_ext/questions`
```json
{
  "name": "Compare images",
  "question_type": "image_comparison",
  "category_id": 3,
  "prompt": "Which is better?",
  "description": "...",
  "image_a_url": "https://...",
  "image_b_url": "https://...",
  "dimensions": [
    {"dimension_id": 1, "sequence": 1,
     "options": [{"master_option_id": 5, "is_correct": true}, ...]}
  ]
}
```

#### `PUT /api/v1/etp_assessment_ext/questions/<id>`
#### `DELETE /api/v1/etp_assessment_ext/questions/<id>`

### Assessments

#### `GET /api/v1/etp_assessment_ext/assessments`
Paginated list. Filters: `state`, `category_id`, `search`, `page`, `limit`.

#### `POST /api/v1/etp_assessment_ext/assessments`
```json
{
  "name": "Q4 Evaluation",
  "category_id": 3,
  "question_limit": 10,
  "duration_minutes": 60,
  "start_date": "2026-06-10 09:00:00",
  "end_date":   "2026-06-30 18:00:00",
  "candidate_ids": [12, 17]
}
```

#### `GET /api/v1/etp_assessment_ext/assessments/<id>`
#### `PUT /api/v1/etp_assessment_ext/assessments/<id>`
#### `DELETE /api/v1/etp_assessment_ext/assessments/<id>`

#### `POST /api/v1/etp_assessment_ext/assessments/<id>/start`
Selects questions, shuffles per candidate, sends invitation emails.

#### `POST /api/v1/etp_assessment_ext/assessments/<id>/done`
#### `POST /api/v1/etp_assessment_ext/assessments/<id>/cancel`
#### `POST /api/v1/etp_assessment_ext/assessments/<id>/reset_draft`

### Candidates

#### `GET /api/v1/etp_assessment_ext/assessments/<id>/candidates`
List candidates with state, progress, score.

#### `POST /api/v1/etp_assessment_ext/assessments/<id>/candidates`
```json
{"employee_ids": [12, 17]}
```

#### `DELETE /api/v1/etp_assessment_ext/assessments/<id>/candidates/<employee_id>`

#### `POST /api/v1/etp_assessment_ext/assessments/<id>/candidates/bulk_import`
Multipart form upload: `file=<candidates.csv>` with `name,email,job_title,department`.
Creates `hr.employee` records on the fly when an email is new.

### Responses

#### `GET /api/v1/etp_assessment_ext/responses`
Paginated, filterable response list (table contract).
Filters: `assessment_id`, `evaluator_id`, `question_id`, `state`.

#### `GET /api/v1/etp_assessment_ext/responses/<id>`
Single response with per-dimension selected options + score.

#### `GET /api/v1/etp_assessment_ext/analytics/dimensions`
Per-dimension accuracy: total / correct / accuracy percent.
Filter: `assessment_id`.

### Candidate portal (token-auth)

The candidate's `access_token` is sent in the invitation email and must be
passed as a path component. **No gateway access_token header required.**

#### `GET /api/v1/etp_assessment_ext/portal/<token>`
Returns the candidate's current state: instructions / question / done /
locked / expired. Frontend uses this to decide which screen to render.

```json
{
  "state": "question",          // instructions | question | done | locked | expired | invalid | closed
  "assessment": {"id": 4, "name": "Q4 Eval", "duration_minutes": 60},
  "evaluator":  {"id": 22, "name": "Jane", "started_at": "...",
                 "deadline_iso": "2026-06-10T11:00:00Z",
                 "answered_count": 3, "total_questions": 10,
                 "progress_percent": 30, "is_locked": false},
  "question":   {"id": 91, "name": "...", "question_type": "image_comparison",
                 "prompt": "...", "description": "...",
                 "image_a_url": "...", "image_b_url": "...",
                 "code_snippet": null, "code_language": null,
                 "video_url": null,
                 "dimensions": [
                    {"dimension_id": 1, "name": "Visual Quality",
                     "options": [{"id": 5, "name": "Response A"}, ...]}
                 ]},
  "current_index": 4, "total_questions": 10
}
```

#### `POST /api/v1/etp_assessment_ext/portal/<token>/begin`
Marks `started_at = now()` (timer starts).

#### `POST /api/v1/etp_assessment_ext/portal/<token>/submit`
```json
{
  "question_id": 91,
  "justification": "Response A handles the lighting better.",
  "selections": [
    {"dimension_id": 1, "option_id": 5},
    {"dimension_id": 2, "option_id": 8}
  ]
}
```

#### `GET /api/v1/etp_assessment_ext/portal/<token>/progress`
Per-question status (pending / submitted) in shuffled order.

#### `POST /api/v1/etp_assessment_ext/portal/<token>/violation`
```json
{"violation_reason": "tab_switch"}
```
Marks the candidate violated and auto-submits remaining questions.

## Frontend integration

Add to `ApiConstants`:

```dart
static const String assessmentDashboard = '/v1/etp_assessment_ext/dashboard_overview';
static const String assessmentCategories = '/v1/etp_assessment_ext/categories';
static const String assessmentDimensions = '/v1/etp_assessment_ext/dimensions';
static const String assessmentQuestions = '/v1/etp_assessment_ext/questions';
static const String assessmentList = '/v1/etp_assessment_ext/assessments';
static const String assessmentPortalRoot = '/v1/etp_assessment_ext/portal';
```

## Permissions

- Read endpoints: caller must be in `etp_assessment.group_assessment_evaluator`
  (or a parent group).
- Write / state-action endpoints: caller must be in
  `etp_assessment.group_assessment_manager`.
- Portal endpoints: per-candidate `access_token`, no group check.
