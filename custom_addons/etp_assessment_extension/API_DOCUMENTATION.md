# ETP Assessment Extension — API Documentation

> **Audience**: Frontend developers integrating the assessment platform (admin dashboard + candidate-facing portal) with an external client (Flutter app, web SPA, etc.).
>
> **Module**: `etp_assessment_extension`
> **Odoo version**: 19.0
> **License**: LGPL-3
> **Maintainer**: Ethara
>
> Every endpoint, payload shape, and validation rule in this document has been verified against the live module running on a dev instance. Sample responses are real captures, not invented.

---

## Table of contents

1. [Module overview](#1-module-overview)
2. [Base URL configuration](#2-base-url-configuration)
3. [Authentication](#3-authentication)
4. [Response envelope](#4-response-envelope)
5. [Pagination, filtering, sorting](#5-pagination-filtering-sorting)
6. [Endpoint reference](#6-endpoint-reference)
   - 6.1 [Auth](#61-auth-from-api_auth_gateway)
   - 6.2 [Dashboard](#62-dashboard)
   - 6.3 [Categories](#63-categories)
   - 6.4 [Dimensions + Options](#64-dimensions--options)
   - 6.5 [Questions](#65-questions)
   - 6.6 [Assessments](#66-assessments)
   - 6.7 [Candidates](#67-candidates)
   - 6.8 [Responses & Analytics](#68-responses--analytics)
   - 6.9 [Candidate Portal](#69-candidate-portal)
7. [Database models used](#7-database-models-used)
8. [Controller → route mapping](#8-controller--route-mapping)
9. [API dependency flow](#9-api-dependency-flow)
10. [Common error handling](#10-common-error-handling)
11. [Frontend integration notes](#11-frontend-integration-notes)
12. [Testing instructions](#12-testing-instructions)
13. [Environment configuration](#13-environment-configuration)

---

## 1. Module overview

`etp_assessment_extension` is a **pure REST API layer** that sits on top of the [`etp_assessment`](../etp_assessment) module. It exposes the existing question bank, assessments, candidate assignments, responses, dashboard KPIs, and the candidate portal flow over JSON.

### What it provides

| Surface | Purpose |
|---|---|
| **Admin API** | CRUD over categories, dimensions, questions, assessments, candidates, plus dashboard + analytics |
| **Candidate Portal API** | Token-authenticated flow for a candidate to start, answer, submit, and report violations during an assessment |

### What it does **not** add

- No new database tables (only `_inherit` model extensions for serializers)
- No backend UI / views / menu items (`"application": False` in manifest)
- No new business logic — every action delegates to the source `etp_assessment` model methods

### Dependencies

```
etp_assessment_extension
 ├── etp_assessment       (question bank, assessments, evaluator, response models)
 ├── api_auth_gateway     (token issuance + @validate_token decorator)
 ├── base, web, hr, mail  (Odoo standard)
```

---

## 2. Base URL configuration

### Local development

```
http://localhost:8071
```

(Defined in `odoo.conf` → `http_port = 8071`.)

### Other environments

Replace the host. Path prefix is constant across environments:

```
<HOST>/api/v1/etp_assessment_ext/...
<HOST>/api/v1/auth_token         (login — provided by api_auth_gateway)
```

### Suggested ApiConstants (Flutter / TS)

```dart
// Dart
static const String authToken             = '/api/v1/auth_token';
static const String assessmentDashboard   = '/v1/etp_assessment_ext/dashboard_overview';
static const String assessmentCategories  = '/v1/etp_assessment_ext/categories';
static const String assessmentDimensions  = '/v1/etp_assessment_ext/dimensions';
static const String assessmentDimOptions  = '/v1/etp_assessment_ext/dimension_options';
static const String assessmentQuestions   = '/v1/etp_assessment_ext/questions';
static const String assessmentList        = '/v1/etp_assessment_ext/assessments';
static const String assessmentResponses   = '/v1/etp_assessment_ext/responses';
static const String assessmentAnalytics   = '/v1/etp_assessment_ext/analytics/dimensions';
static const String assessmentPortalRoot  = '/v1/etp_assessment_ext/portal';
```

---

## 3. Authentication

Two distinct auth schemes are in play:

### 3.1 Admin token (most endpoints)

| Step | Detail |
|---|---|
| **Acquire** | `POST /api/v1/auth_token` with `{"login": "...", "password": "..."}` |
| **Use** | Send returned `access_token` in the `Access-Token` request header |
| **Lifetime** | ~100 hours per `api.access_token.update_access_token` (100,000s ≈ 27h actually; treat as opaque, refresh when 401) |
| **Refresh on 401** | Hit `/api/v1/auth_token` again |

> **Header name gotcha**: Use **`Access-Token`** (with dash). Werkzeug's WSGI environment normalizes `Access-Token` → `HTTP_ACCESS_TOKEN`, which `request.httprequest.headers.get('access_token')` reads back. Sending `access_token:` (underscore) gets stripped by some HTTP layers and fails silently.

**Permissions** (set on the Odoo user record):

| Group | Read | Write/Create/Delete |
|---|---|---|
| `etp_assessment.group_assessment_evaluator` | ✓ | ✗ |
| `etp_assessment.group_assessment_manager` | ✓ | ✓ |

The manager group **implies** the evaluator group. Endpoints listed as "manager only" return `403` for evaluator-only callers.

### 3.2 Portal token (candidate endpoints only)

| Step | Detail |
|---|---|
| **Acquire** | Read `etp.assessment.evaluator.access_token` after `POST /assessments/<id>/start` (also delivered to the candidate by email) |
| **Use** | Pass as a **path segment** in the URL — e.g. `/portal/<TOKEN>/begin` |
| **No header** | Portal routes are `auth="public"`. Do **not** send the admin `Access-Token`. |

### 3.3 Full auth flow

```
┌──────────────┐       login + password        ┌─────────────────┐
│   Frontend   │ ────────────────────────────▶ │  /auth_token    │
└──────────────┘                                └─────────────────┘
        │                                              │
        │  ◀───── access_token ──────────────────────┘
        │
        │  Access-Token: <admin_token>
        ▼
┌─────────────────────────────────────────────────────┐
│  /api/v1/etp_assessment_ext/* (admin endpoints)     │
└─────────────────────────────────────────────────────┘
        │
        │  POST /assessments/<id>/start
        │  → creates etp.assessment.evaluator rows
        │    each with its own access_token,
        │    emailed to the candidate
        ▼
┌─────────────────────────────────────────────────────┐
│ Candidate clicks email link / opens portal screen   │
│ Frontend gets candidate's `access_token` (path arg) │
└─────────────────────────────────────────────────────┘
        │
        │  No header
        ▼
┌─────────────────────────────────────────────────────┐
│  /api/v1/etp_assessment_ext/portal/<token>/*        │
└─────────────────────────────────────────────────────┘
```

---

## 4. Response envelope

Every response from this module (including errors) returns the `return_Response` envelope from `api_auth_gateway`:

```json
{
  "message": "OK",
  "errors": [],
  "status_code": 200,
  "<payload_key_1>": ...,
  "<payload_key_2>": ...
}
```

- `message` — human-readable summary
- `errors` — array of per-row / per-field error strings (CSV import, validation)
- `status_code` — HTTP status (also reflected in the HTTP response code)
- Payload keys are spread at the **top level** (e.g. `category`, `assessment`, `blocks`, `data`), **not** under a `data` wrapper

### Dashboard / list endpoints use a generic block contract

```json
{
  "message": "OK",
  "errors": [],
  "status_code": 200,
  "role": "manager",
  "blocks": [
    {"type": "kpi",   "items": [...] },
    {"type": "chart", "variant": "doughnut", "title": "...", "items": [...] },
    {"type": "table", "title": "...", "columns": [...], "rows": [...], "pagination": {...}}
  ]
}
```

| Block `type` | Required keys |
|---|---|
| `kpi` | `items: [{key, label, value, sub_string}]` |
| `chart` | `variant` (doughnut/bar), `title`, `items: [{label, key, value, percent}]` |
| `table` | `title`, `columns: [{key, label, type}]`, `rows`, optional `pagination` |

### `role` field on list/dashboard responses

- `"manager"` — caller is in `group_assessment_manager`
- `"evaluator"` — caller is in `group_assessment_evaluator` only
- `null` — caller is in neither (read endpoints will return 403)

---

## 5. Pagination, filtering, sorting

All list endpoints share these conventions.

### Pagination

| Query param | Default | Max | Notes |
|---|---|---|---|
| `page` | 1 | — | 1-indexed |
| `limit` | 20 | 200 | Clamped server-side |

Response includes:

```json
"pagination": {
  "total_records": 234,
  "page": 1,
  "limit": 20,
  "total_pages": 12
}
```

### Sorting

| Query param | Allowed values per endpoint | Default |
|---|---|---|
| `sort_by` | See per-endpoint list | endpoint-specific |
| `sort_order` | `asc` / `desc` | `desc` (most endpoints) |

Invalid `sort_by` → `400` with explicit allowed list.

### Filtering

Common filters (per endpoint):

- `search` — substring match on `name` (and `prompt`/`description` for questions)
- `state` — exact match against allowed selection values
- `active` — `true`/`false` for archived records

---

## 6. Endpoint reference

For every endpoint below: **base path** is the configured base URL (e.g. `http://localhost:8071`). Path is shown relative.

---

### 6.1 Auth (from `api_auth_gateway`)

#### 🔑 POST `/api/v1/auth_token`

| Attribute | Value |
|---|---|
| **Name** | Get Admin Access Token |
| **Purpose** | Authenticate an Odoo user (login/password) and issue a long-lived API token |
| **Auth required** | None |
| **Headers** | `Content-Type: application/json` |
| **Validation** | `login` (string, required), `password` (string, required) |

**Request body**

```json
{
  "login": "admin",
  "password": "admin",
  "browser_name": "Chrome",
  "os_name": "macOS",
  "location": "SF"
}
```

`browser_name`, `os_name`, `location` are optional metadata stored on `api.access_token`.

**Success — `200`**

```json
{
  "message": "Success",
  "errors": [],
  "data": {
    "uid": 2,
    "email": "admin",
    "name": "Administrator",
    "mobile": "",
    "access_token": "access_token_95e6bf1b43230aeca63a09fb255b68f99a70326b",
    "refresh_token": "access_token_ca0ebaf9814c08dc4adf3e1453a0968236bf21b2",
    "address": "",
    "user_role": "",
    "user_type": "",
    "profile_pic": "",
    "permissions": []
  },
  "status_code": 200
}
```

**Errors**

| Status | Condition |
|---|---|
| `400` | `login` or `password` missing, user not found, password wrong, user deactivated |

**Frontend usage**

1. On login screen: send credentials, persist `access_token` in secure storage.
2. Attach `Access-Token: <token>` header to every subsequent admin call.
3. On any `401 token seems to have expired or invalid`, force re-login.

---

### 6.2 Dashboard

#### 📊 GET `/api/v1/etp_assessment_ext/dashboard_overview`

| Attribute | Value |
|---|---|
| **Name** | Dashboard Overview |
| **Purpose** | Block-based KPI + charts + active assessments + leaderboard |
| **Auth required** | Admin token, group: evaluator+ |
| **Headers** | `Access-Token: <token>` |

**Query parameters** (all optional)

| Param | Type | Description |
|---|---|---|
| `assessment_id` | int | Scope all counts to a single assessment |
| `category_id` | int | Scope to a category |
| `state` | string | `draft` / `in_progress` / `done` / `cancelled` |
| `date_from` | YYYY-MM-DD | Assessments with `start_date >= date_from` |
| `date_to` | YYYY-MM-DD | Assessments with `end_date <= date_to` |

**Sample response — `200`**

```json
{
  "message": "OK",
  "errors": [],
  "status_code": 200,
  "role": "manager",
  "blocks": [
    {
      "type": "kpi",
      "items": [
        {"key": "total_assessments", "label": "Total Assessments", "value": "1",  "sub_string": "0 in progress, 1 done"},
        {"key": "total_questions",   "label": "Questions",        "value": "1",  "sub_string": ""},
        {"key": "total_candidates",  "label": "Candidates",       "value": "2",  "sub_string": "2 submitted"},
        {"key": "total_responses",   "label": "Responses",        "value": "2",  "sub_string": ""},
        {"key": "completion_rate",   "label": "Completion Rate",  "value": "100.0%", "sub_string": "1 violation(s)"}
      ]
    },
    {
      "type": "chart", "variant": "doughnut", "title": "Question types",
      "items": [
        {"label": "Image Comparison", "key": "image_comparison", "value": 0, "percent": 0.0},
        {"label": "Text",             "key": "text",              "value": 1, "percent": 100.0}
      ]
    },
    {
      "type": "chart", "variant": "bar", "title": "Questions per category",
      "items": [{"label": "Frontend", "key": "1", "value": 1, "percent": 100.0}]
    },
    {
      "type": "table", "title": "Active assessments",
      "columns": [
        {"key": "name",             "label": "Assessment", "type": "string"},
        {"key": "evaluators_total", "label": "Candidates", "type": "integer"},
        {"key": "evaluators_done",  "label": "Submitted",  "type": "integer"},
        {"key": "end_date",         "label": "End Date",   "type": "datetime"}
      ],
      "rows": []
    },
    {
      "type": "table", "title": "Top candidates",
      "columns": [
        {"key": "name",            "label": "Candidate",   "type": "string"},
        {"key": "assessment_name", "label": "Assessment",  "type": "string"},
        {"key": "total_score",     "label": "Score",       "type": "integer"},
        {"key": "max_possible",    "label": "Max",         "type": "integer"},
        {"key": "total_questions", "label": "Questions",   "type": "integer"}
      ],
      "rows": [...]
    }
  ]
}
```

**Errors**

| Status | Condition |
|---|---|
| `401` | Missing or expired admin token |
| `403` | User not in `group_assessment_evaluator` (or manager) |

**Frontend usage**

Render each block by `type`. KPI cards, doughnut, bar, two tables — directly maps to dashboard widgets. The `key` field on each item is stable; use it as widget id.

---

### 6.3 Categories

Base: `/api/v1/etp_assessment_ext/categories`

#### GET `/categories` — list

| Attribute | Value |
|---|---|
| **Auth** | evaluator+ |
| **sort_by allowed** | `name` · `sequence` · `create_date` |
| **Default sort** | `sequence asc` |
| **Query params** | `search`, `active` (true/false), `page`, `limit`, `sort_by`, `sort_order` |

**Sample response**

```json
{
  "message": "OK",
  "errors": [],
  "status_code": 200,
  "role": "manager",
  "blocks": [{
    "type": "table",
    "title": "Categories",
    "columns": [
      {"key": "name",           "label": "Name",       "type": "string"},
      {"key": "sequence",       "label": "Sequence",   "type": "integer"},
      {"key": "question_count", "label": "Questions",  "type": "integer"},
      {"key": "active",         "label": "Active",     "type": "boolean"},
      {"key": "create_date",    "label": "Created",    "type": "datetime"}
    ],
    "rows": [
      {
        "id": 4,
        "name": "Frontend Eval",
        "sequence": 10,
        "active": true,
        "description": "...",
        "question_count": 3,
        "create_date": "2026-06-09T09:00:56.772397",
        "write_date": "2026-06-09T09:00:56.772397"
      }
    ],
    "pagination": {"total_records": 4, "page": 1, "limit": 20, "total_pages": 1}
  }]
}
```

#### GET `/categories/<id>` — detail

Returns `{"category": {...}}`.

| Status | Condition |
|---|---|
| `404` | Category does not exist |

#### POST `/categories` — create

| Attribute | Value |
|---|---|
| **Auth** | manager |
| **Validation** | `name` (string, required) |

**Body**

```json
{"name": "Frontend Eval", "sequence": 10, "description": "...", "active": true}
```

**Success — `200`** → `{"category": {...}}` with `message: "Category created"`.

#### PUT / PATCH `/categories/<id>` — update

Partial update. Any of `name`, `sequence`, `description`, `active` may be sent.

#### DELETE `/categories/<id>`

| Status | Condition |
|---|---|
| `200` | `"Category deleted"` |
| `400` | Category has attached questions (referential integrity) |
| `404` | Not found |

---

### 6.4 Dimensions + Options

Master dimensions are reusable across questions. Each dimension has master options (e.g. "Response A", "Response B"). When a dimension is attached to a question, per-question copies of the options are created so that `is_correct` can be marked per-question.

#### GET `/dimensions` — list

Query params: `search`, `active`, `page`, `limit`, `sort_by` (`name`/`sequence`/`create_date`), `sort_order`.

Each row includes nested `options` array (sorted by sequence) plus `option_count` and `options_display` (comma-joined names).

#### GET `/dimensions/<id>` — detail

#### POST `/dimensions`

Body shape:

```json
{
  "name": "Visual Quality",
  "sequence": 10,
  "active": true,
  "options": [
    {"name": "Response A", "sequence": 1},
    {"name": "Response B", "sequence": 2},
    {"name": "Tie",        "sequence": 3}
  ]
}
```

If `options` omitted, dimension is created with zero options (add later via `/dimension_options`).

#### PUT / PATCH `/dimensions/<id>`

Updates `name`, `sequence`, `active`. **Does not update options** — use the option endpoints below for that.

#### DELETE `/dimensions/<id>`

`400` if referenced by a question (ondelete=restrict).

#### POST `/dimension_options`

Body:

```json
{"dimension_id": 1, "name": "Both equally good", "sequence": 4}
```

Validation: `dimension_id` (int, required), `name` (string, required).

#### PUT / PATCH `/dimension_options/<id>`

Body: `name`, `sequence`.

#### DELETE `/dimension_options/<id>`

---

### 6.5 Questions

Question bank. Each question belongs to a category and links to one or more dimensions, with per-question options marking the correct answer.

#### GET `/questions` — list

| Filter | Type |
|---|---|
| `search` | string — matches `name`, `prompt`, `description` (ilike OR) |
| `category_id` | int |
| `question_type` | `image_comparison` / `text` / `coding` / `image_text` / `video` |
| `active` | bool |
| `sort_by` | `name` / `sequence` / `create_date` / `write_date` |

#### GET `/questions/<id>` — detail (with nested dimensions + options)

```json
{
  "question": {
    "id": 1,
    "name": "Compare image renders",
    "sequence": 10,
    "question_type": "image_comparison",
    "question_type_label": "Image Comparison",
    "prompt": "Which image better follows the brief?",
    "description": "...",
    "active": true,
    "category_id": 4,
    "category_name": "Frontend Eval",
    "image_a_url": "https://...",
    "image_b_url": "https://...",
    "code_snippet": "",
    "code_language": "python",
    "video_url": "",
    "dimension_count": 1,
    "dimensions": [{
      "id": 1,
      "dimension_id": 5,
      "dimension_name": "Visual Quality",
      "sequence": 1,
      "options": [
        {"id": 4, "master_option_id": 17, "name": "Response A", "sequence": 1, "is_correct": true,  "score": 1},
        {"id": 5, "master_option_id": 18, "name": "Response B", "sequence": 2, "is_correct": false, "score": 0}
      ]
    }],
    "create_date": "2026-06-09T09:01:21.637981",
    "write_date": "2026-06-09T09:01:21.637981"
  }
}
```

#### POST `/questions`

| Validation | Requirement |
|---|---|
| `name` | string, required |
| `prompt` | string, required |
| `question_type` | one of `image_comparison` / `text` / `coding` / `image_text` / `video` |

**Body**

```json
{
  "name": "Compare image renders",
  "question_type": "image_comparison",
  "category_id": 4,
  "prompt": "Which image better follows the brief?",
  "description": "Look at lighting, composition.",
  "image_a_url": "https://...",
  "image_b_url": "https://...",
  "code_snippet": null,
  "code_language": "python",
  "video_url": null,
  "active": true,
  "dimensions": [
    {
      "dimension_id": 1,
      "sequence": 1,
      "options": [
        {"master_option_id": 1, "is_correct": true,  "sequence": 1},
        {"master_option_id": 2, "is_correct": false, "sequence": 2}
      ]
    }
  ]
}
```

**Business rules**

- If `dimensions` is omitted, question is created with no dimensions (add later by `PUT`).
- If a dimension item has no `options` array, options are auto-populated from the master dimension (with `is_correct=False`).
- **Exactly one option per dimension may be `is_correct: true`** — enforced by `_check_single_correct_per_dimension` constraint on the source model. Violating this returns `400`.
- A dimension can be attached to a question **at most once** (`_check_unique_dimension_per_question`).

#### PUT / PATCH `/questions/<id>`

Partial. If `dimensions` array is sent, **all existing dimensions on the question are replaced** (`_apply_dimensions` does an `unlink` first, then recreates). Send the full desired state.

#### DELETE `/questions/<id>`

---

### 6.6 Assessments

#### GET `/assessments` — list

| Filter | Type |
|---|---|
| `search` | string (name ilike) |
| `state` | `draft` / `in_progress` / `done` / `cancelled` |
| `category_id` | int |
| `date_from` | start_date >= |
| `date_to` | end_date <= |
| `sort_by` | `name` / `create_date` / `start_date` / `end_date` / `state` |

#### GET `/assessments/<id>`

```json
{
  "assessment": {
    "id": 1,
    "name": "Q4 Evaluation",
    "state": "in_progress",
    "state_label": "In Progress",
    "category_id": 4,
    "category_name": "Frontend Eval",
    "question_limit": 10,
    "total_questions_available": 25,
    "duration_minutes": 60,
    "start_date": "2026-06-10T09:00:00",
    "end_date": "2026-06-30T18:00:00",
    "deadline": null,
    "question_ids": [1, 2, 3, ...],
    "candidate_ids": [2, 3],
    "evaluators_total": 2,
    "evaluators_done": 1,
    "progress_percent": 50.0,
    "response_count": 5,
    "create_date": "...",
    "write_date": "..."
  }
}
```

#### POST `/assessments`

| Validation | Requirement |
|---|---|
| `name` | string, required |
| `category_id` | int, required |

**Body**

```json
{
  "name": "Q4 Evaluation",
  "category_id": 4,
  "question_limit": 10,
  "duration_minutes": 60,
  "start_date": "2026-06-10 09:00:00",
  "end_date":   "2026-06-30 18:00:00",
  "deadline":   "2026-06-30",
  "candidate_ids": [2, 3, 12]
}
```

- `question_limit = 0` → use all active questions in the category.
- `duration_minutes = 0` → no time limit per candidate.
- `candidate_ids` — `hr.employee.id` list. Optional at creation.

**Business rules**

- `_check_dates`: `end_date > start_date` if both set.
- `_check_question_limit`: must be ≥ 0.

#### PUT / PATCH `/assessments/<id>`

Partial; can update everything in the create body.

#### DELETE `/assessments/<id>`

Only allowed when state is `draft` or `cancelled`. Otherwise `400`.

#### POST `/assessments/<id>/start`

**Transitions** `draft → in_progress`. Performs in one transaction:

1. Validates: assessment must have ≥1 candidate, a category, and ≥1 available question.
2. Verifies requested `question_limit` ≤ available questions.
3. Locks the selected `question_ids` on the assessment.
4. For each candidate: creates one `etp.assessment.evaluator` record with:
   - A unique `access_token` (UUID4) — emailed to the candidate
   - A shuffled `question_order` (JSON-stringified list of question IDs)
   - `total_questions` = the shuffled length
   - State `pending`
5. Sends invitation emails via the `etp_assessment.email_assessment_invitation` mail template.

Returns the updated `{"assessment": {...}}` with state `in_progress`.

| Error condition | Status |
|---|---|
| Not in `draft` | 400 — `"Only draft assessments can be started."` |
| No candidates | 400 — `"Please assign at least one candidate before starting."` |
| No category | 400 |
| No active questions in category | 400 |
| `question_limit > available` | 400 |

#### POST `/assessments/<id>/done`

Transitions `in_progress → done`. **Usually unnecessary** — the assessment auto-transitions to `done` when every candidate's assignment reaches `submitted` (handled by `EtpAssessmentResponse._check_assessment_complete`).

| Error | Status |
|---|---|
| Not in `in_progress` | 400 |

#### POST `/assessments/<id>/cancel`

Allowed from any non-terminal state. Transitions to `cancelled`. Existing responses + evaluator assignments are kept.

| Error | Status |
|---|---|
| Already `done` or `cancelled` | 400 |

#### POST `/assessments/<id>/reset_draft`

Transitions `cancelled → draft`. Side-effects: **deletes all evaluator assignments + responses**, clears the assessment's question pool. Use with care.

| Error | Status |
|---|---|
| Not in `cancelled` | 400 |

---

### 6.7 Candidates

These endpoints are nested under an assessment.

#### GET `/assessments/<id>/candidates`

Query params: `state` (`pending`/`in_progress`/`submitted`), `page`, `limit`.

Returns a generic table block. **The per-candidate `access_token` is included in each row** so the admin/frontend can deep-link the candidate to the portal:

```json
{
  "id": 2,
  "assessment_id": 1,
  "employee_id": 3,
  "employee_name": "Test Candidate Two",
  "employee_email": "test2@example.com",
  "state": "submitted",
  "state_label": "Submitted",
  "access_token": "f49c9654-0276-4685-a816-13cca715dfa5",
  "started_at": "2026-06-09T09:01:55",
  "deadline_datetime": "2026-06-09T09:31:55",
  "total_questions": 1,
  "answered_count": 1,
  "progress_percent": 100.0,
  "total_score": 1,
  "max_possible_score": 1,
  "is_locked": true,
  "is_violated": false,
  "violation_reason": "",
  "violation_datetime": null
}
```

> **Important**: This endpoint returns rows **only after** `POST /assessments/<id>/start` has been called (assignments are created during `start`, not when employees are added). Before start, an empty `rows: []` is returned even if `candidate_ids` is populated.

#### POST `/assessments/<id>/candidates`

| Validation | Requirement |
|---|---|
| `employee_ids` | list of int, required, non-empty |

**Body**

```json
{"employee_ids": [12, 17, 21]}
```

**Response**

```json
{
  "added_employee_ids": [17],
  "already_assigned_ids": [12, 21],
  "candidate_ids": [12, 17, 21]
}
```

#### DELETE `/assessments/<id>/candidates/<employee_id>`

Removes an employee from the assessment's `evaluator_ids` (M2M). Only allowed when state is `draft`. Use cancel + reset_draft if the assessment is already running.

| Error | Status |
|---|---|
| Not in `draft` | 400 |
| Employee not assigned | 404 |

#### POST `/assessments/<id>/candidates/bulk_import`

Multipart upload of a CSV.

**Content-Type**: `multipart/form-data`
**Form fields**:

- `file` (required, file part) — CSV with columns: `name`, `email` (required); `job_title`, `department` (optional)

Alternative body: `file_b64` (base64 string in form params or query) for clients that can't do multipart.

**CSV format**

```csv
name,email,job_title,department
John Doe,john@example.com,Evaluator,Engineering
Jane Smith,jane@example.com,Senior Evaluator,Design
```

**Behavior**

- For each row, looks up `hr.employee` by `work_email`.
- If not found, **creates a new `hr.employee`** with the given `name`, `work_email`, optional `job_title`, and `department_id` (matched by name).
- Adds all resolved employees to the assessment's `evaluator_ids`.

**Response**

```json
{
  "message": "2 candidate(s) added, 0 already assigned, 2 new employee(s) created.",
  "errors": ["Row 5: 'name' and 'email' are required."],
  "status_code": 200,
  "added_employee_ids": [12, 13],
  "already_assigned_ids": [],
  "created_employees": [
    {"id": 12, "name": "John Doe",   "email": "john@example.com"},
    {"id": 13, "name": "Jane Smith", "email": "jane@example.com"}
  ],
  "candidate_ids": [12, 13]
}
```

Per-row errors are pushed into `errors[]` but the import continues. CSV parse failures return `400`.

---

### 6.8 Responses & Analytics

#### GET `/responses`

| Filter | Type |
|---|---|
| `assessment_id` | int |
| `evaluator_id` | `hr.employee` id |
| `assignment_id` | `etp.assessment.evaluator` id |
| `question_id` | int |
| `state` | `draft` / `submitted` |
| `sort_by` | `create_date` / `score` / `state` |

#### GET `/responses/<id>`

Full response detail including per-dimension `lines`:

```json
{
  "response": {
    "id": 1,
    "assessment_id": 1,
    "assessment_name": "Q4 Evaluation",
    "assessment_evaluator_id": 2,
    "evaluator_id": 3,
    "evaluator_name": "Test Candidate Two",
    "question_id": 1,
    "question_name": "Compare image renders",
    "justification": "I picked Option A because...",
    "state": "submitted",
    "state_label": "Submitted",
    "score": 1,
    "max_score": 1,
    "lines": [
      {
        "id": 1,
        "dimension_id": 5,
        "dimension_name": "Visual Quality",
        "selected_option_id": 17,
        "selected_option_name": "Response A"
      }
    ],
    "create_date": "...",
    "write_date": "..."
  }
}
```

#### GET `/analytics/dimensions`

Per-dimension accuracy. Optional query: `assessment_id`.

```json
{
  "blocks": [{
    "type": "table",
    "title": "Per-dimension accuracy",
    "columns": [
      {"key": "name",     "label": "Dimension",  "type": "string"},
      {"key": "total",    "label": "Total",      "type": "integer"},
      {"key": "correct",  "label": "Correct",    "type": "integer"},
      {"key": "accuracy", "label": "Accuracy %", "type": "float"}
    ],
    "rows": [
      {"dimension_id": 5, "name": "Visual Quality", "total": 12, "correct": 9, "accuracy": 75.0}
    ]
  }]
}
```

Rows are sorted by `accuracy` descending. Only submitted responses are counted.

---

### 6.9 Candidate Portal

All portal endpoints are `auth="public"`. **The `<token>` URL segment is the candidate's per-assignment `access_token`.** No `Access-Token` header.

The portal endpoints return a `state` field at the top level so the frontend can decide which screen to render:

| `state` | Frontend action |
|---|---|
| `invalid` | Show "invalid token" error screen |
| `closed` | Assessment is not `in_progress`. Show "closed" screen. |
| `instructions` | Candidate hasn't started. Show rules + Start button. |
| `question` | Render current question; capture answer. |
| `done` | All questions answered. Show success. |
| `expired` | Time ran out; remaining were auto-submitted. |
| `locked` | Already submitted (either normally or via violation). |

#### GET `/portal/<token>`

Single endpoint that returns the candidate's current screen state. Frontend should poll this on screen open and after each submit.

**Auto-side-effects**: if the candidate's deadline has passed, this endpoint **auto-submits remaining questions** before returning `state: "expired"`.

**Sample — `state: "question"`**

```json
{
  "message": "OK",
  "errors": [],
  "status_code": 200,
  "state": "question",
  "assessment": {"id": 1, "name": "Q4 Evaluation", "duration_minutes": 30, "state": "in_progress"},
  "evaluator": {
    "id": 2,
    "name": "Test Candidate Two",
    "email": "test2@example.com",
    "state": "pending",
    "started_at": "2026-06-09T09:01:55",
    "deadline_iso": "2026-06-09T09:31:55Z",
    "total_questions": 1,
    "answered_count": 0,
    "progress_percent": 0.0,
    "is_locked": false,
    "is_violated": false,
    "violation_reason": ""
  },
  "question": {
    "id": 1,
    "name": "Compare image renders",
    "question_type": "image_comparison",
    "prompt": "Which image better follows the brief?",
    "description": "...",
    "image_a_url": "https://...",
    "image_b_url": "https://...",
    "code_snippet": "",
    "code_language": "python",
    "video_url": "",
    "dimensions": [{
      "dimension_id": 5,
      "name": "Visual Quality",
      "sequence": 1,
      "options": [
        {"id": 17, "name": "Response A", "sequence": 1},
        {"id": 18, "name": "Response B", "sequence": 2},
        {"id": 19, "name": "Tie",        "sequence": 3}
      ]
    }]
  },
  "current_index": 1,
  "total_questions": 1
}
```

> **Note**: portal exposes the **master option IDs** (`etp.assessment.dimension.option.id`) — candidate selects one of those per dimension. Server compares to the per-question correct option to score.

#### POST `/portal/<token>/begin`

Marks `started_at = now()`, which starts the candidate's timer (`deadline_datetime = started_at + duration_minutes`).

Idempotent: if `started_at` is already set, the call is a no-op and returns the current state.

| Error | Status |
|---|---|
| Invalid token | 404 |
| Assessment not in_progress | 400 (`state: "closed"`) |
| Already locked | 400 (`state: "locked"`) |

#### POST `/portal/<token>/submit`

Submit the candidate's answer for one question.

**Body**

```json
{
  "question_id": 1,
  "justification": "Response A handles the lighting better.",
  "selections": [
    {"dimension_id": 5, "option_id": 17}
  ]
}
```

| Validation | Requirement |
|---|---|
| `question_id` | int, must be in the candidate's shuffled question order |
| `justification` | non-empty string |
| `selections` | non-empty list of `{dimension_id, option_id}` |

**Business rules**

- Creating a duplicate response for the same `(assignment, question)` is rejected with 400.
- If a draft response already exists for this question (e.g. partial save), the submit replaces its `line_ids` and `justification` and transitions to `submitted`.
- After every submit, the server checks whether all questions in `question_order` are now `submitted`. If yes:
  - Marks the evaluator assignment `submitted` + `is_locked = true`.
  - If all candidates on the assessment are submitted → assessment auto-transitions to `done`.
- Score is computed by comparing the selected master option to the question's `is_correct` mark per dimension.

**Sample response**

```json
{
  "message": "Response submitted",
  "errors": [],
  "status_code": 200,
  "evaluator": { ... full evaluator dict ... },
  "response_id": 1,
  "score": 1,
  "max_score": 1
}
```

#### GET `/portal/<token>/progress`

Per-question status (no auto side-effects). Useful for a "questions list" sidebar.

```json
{
  "evaluator": { ... },
  "total_questions": 3,
  "answered_count": 1,
  "items": [
    {"index": 1, "question_id": 1, "question_name": "...", "status": "submitted", "score": 1, "max_score": 1},
    {"index": 2, "question_id": 2, "question_name": "...", "status": "pending",   "score": 0, "max_score": 0},
    {"index": 3, "question_id": 3, "question_name": "...", "status": "pending",   "score": 0, "max_score": 0}
  ]
}
```

#### POST `/portal/<token>/violation`

Used by the candidate's frontend to log anti-cheat events (tab switch, devtools open, screen recording, etc.). **Side-effect**: auto-submits remaining un-answered questions and **locks the assignment**.

**Body**

```json
{"violation_reason": "tab_switch detected"}
```

**Response**

```json
{
  "message": "Violation recorded - assessment auto-submitted.",
  "errors": [],
  "status_code": 200,
  "state": "locked",
  "evaluator": {
    ...,
    "is_violated": true,
    "violation_reason": "tab_switch detected",
    "is_locked": true,
    "state": "submitted"
  }
}
```

The violation reason is also logged to the Odoo server log at WARNING level for audit trail.

---

## 7. Database models used

All models come from the source `etp_assessment` module — the extension does not introduce new schemas, only inherits to add `to_api_dict()` / `to_portal_dict()` / `to_brief_dict()` serializers.

| Model | Purpose | Key fields read by API |
|---|---|---|
| `etp.assessment.category` | Question category | `name`, `sequence`, `active`, `description`, `question_count` |
| `etp.assessment.dimension` | Master dimension | `name`, `sequence`, `active`, `option_ids`, `option_count`, `options_display` |
| `etp.assessment.dimension.option` | Master option | `name`, `sequence`, `dimension_id` |
| `etp.assessment.question` | Question bank entry | `name`, `question_type`, `prompt`, `description`, `image_a_url`, `image_b_url`, `code_snippet`, `code_language`, `video_url`, `category_id` |
| `etp.assessment.question.dimension` | Question↔Dimension link | `dimension_id`, `sequence`, `option_line_ids` |
| `etp.assessment.question.dimension.option` | Per-question option (correctness flag) | `master_option_id`, `is_correct`, `score`, `sequence` |
| `etp.assessment` | Assessment record | `name`, `state`, `category_id`, `question_limit`, `duration_minutes`, `start_date`, `end_date`, `evaluator_ids`, `assessment_evaluator_ids`, `question_ids` |
| `etp.assessment.evaluator` | Per-candidate assignment | `employee_id`, `access_token`, `question_order`, `started_at`, `deadline_datetime`, `state`, `total_questions`, `answered_count`, `total_score`, `max_possible_score`, `is_locked`, `is_violated`, `violation_reason` |
| `etp.assessment.response` | Per-question submission | `question_id`, `assessment_evaluator_id`, `justification`, `state`, `score`, `max_score`, `line_ids` |
| `etp.assessment.response.line` | Per-dimension selection | `dimension_id`, `selected_option_id` |
| `hr.employee` | Candidate identity | `name`, `work_email`, `private_email`, `job_title`, `department_id` |
| `hr.department` | Department lookup (CSV import) | `name` |
| `api.access_token` | Admin bearer token | `user_id`, `access_token`, `expiry` |

---

## 8. Controller → route mapping

| Controller file | Routes |
|---|---|
| `controllers/dashboard_overview.py` | `GET /dashboard_overview` |
| `controllers/categories.py` | `GET/POST/PUT/PATCH/DELETE /categories[, /categories/<id>]` |
| `controllers/dimensions.py` | `GET/POST/PUT/PATCH/DELETE /dimensions[, /dimensions/<id>]`<br>`POST/PUT/PATCH/DELETE /dimension_options[, /dimension_options/<id>]` |
| `controllers/questions.py` | `GET/POST/PUT/PATCH/DELETE /questions[, /questions/<id>]` |
| `controllers/assessments.py` | `GET/POST/PUT/PATCH/DELETE /assessments[, /assessments/<id>]`<br>`POST /assessments/<id>/{start,done,cancel,reset_draft}` |
| `controllers/candidates.py` | `GET/POST /assessments/<id>/candidates`<br>`DELETE /assessments/<id>/candidates/<employee_id>`<br>`POST /assessments/<id>/candidates/bulk_import` |
| `controllers/responses.py` | `GET /responses[, /responses/<id>]`<br>`GET /analytics/dimensions` |
| `controllers/portal.py` | `GET/POST /portal/<token>/{,begin,submit,progress,violation}` |
| `controllers/common.py` | Shared helpers (`require_assessment_user`, `paginate`, `parse_json_body`, etc.) |

---

## 9. API dependency flow

The most common end-to-end sequence frontends need to implement:

```
1. /auth_token                                    → admin token
2. /categories               (GET)                → pick category for an assessment
3. /dimensions               (GET)                → pick dimensions for a question
4. /questions                (POST)               → seed question bank
5. /assessments              (POST)               → create assessment in `draft`
6. /assessments/<id>/candidates                  → add candidates (POST or bulk_import)
                              /candidates/bulk_import
7. /assessments/<id>/start   (POST)               → emails go out, evaluator rows created
8. (candidate clicks email link)
   ─ /portal/<candidate_token>        (GET)       → state="instructions"
   ─ /portal/<candidate_token>/begin  (POST)      → state="question"
   ─ /portal/<candidate_token>/submit (POST) ×N   → state cycles "question" → "question" → ... → "done"/"locked"
9. /assessments/<id>/candidates  (GET)            → admin polls progress
   /responses                    (GET)            → admin views answers
   /analytics/dimensions         (GET)            → per-dimension accuracy
10. Auto: when last candidate submits, assessment → "done"
```

---

## 10. Common error handling

### Status codes

| Code | Meaning |
|---|---|
| `200` | Success (also used for "soft failures" like a CSV import with row errors — check the `errors` array) |
| `400` | Bad request — invalid payload, business rule violation, or wrong state transition |
| `401` | Missing or expired admin token |
| `403` | Authenticated but not in required group |
| `404` | Resource not found (or invalid portal token) |
| `500` | Unhandled exception — check Odoo logs |

### Recommended frontend behavior

| Response shape | Action |
|---|---|
| `200`, `errors: []` | Use payload |
| `200`, `errors: [...]` | Use payload, show non-blocking warnings (CSV import row failures) |
| `400` | Show `message` to user as form/validation error |
| `401` | Force re-auth — clear stored token, redirect to login |
| `403` | Show "permission denied" — likely a group misconfiguration |
| `404` (admin) | Show "not found" |
| `404` (portal `state: "invalid"`) | Show "this link is invalid or expired" |
| `5xx` | Show generic error, retry with backoff, log to monitoring |

### Always check `message` and `errors`

The HTTP status mirrors `status_code` in the body, but **read the body** for the human-readable message. For partial-success endpoints (CSV import, bulk operations) `errors` is non-empty even on 200.

---

## 11. Frontend integration notes

### Header gotcha

Use `Access-Token` (dash), not `access_token` (underscore). See [Authentication](#3-authentication) for explanation.

### Block-based UI

The dashboard, list endpoints, and analytics endpoints all return a uniform `blocks` array. Implement one block renderer per `type` (kpi / chart / table) and reuse across screens.

### Pagination

For infinite-scroll lists, request `page=1` initially, then increment. Stop when `page > pagination.total_pages`.

### Portal state machine

Implement the candidate flow as a state machine driven by `GET /portal/<token>`. The endpoint can mutate state (auto-submit on timeout), so re-fetch after every user action.

### Anti-cheat events

On the candidate page:

- `visibilitychange` (document hidden) → `POST /portal/<token>/violation` with `tab_switch`
- `blur` on window → same
- Detect devtools open (window size delta, keydown F12 / Ctrl+Shift+I) → `dev_tools_open`
- Block right-click + copy/paste with `event.preventDefault()` (server-side check is just the violation log)

Violations are terminal — the candidate is locked. Show a hard error screen and stop polling.

### Datetime handling

- `start_date`, `end_date`, `started_at`, `create_date`, `write_date` come as ISO-8601 strings in server local time (no timezone suffix).
- `deadline_iso` on portal responses comes with `Z` suffix — treat as UTC. Use it to drive the candidate-side countdown.

### Token storage (admin)

- Store the admin `access_token` in secure storage (Flutter: `flutter_secure_storage`; Web: `httpOnly` cookie via a backend proxy or `sessionStorage` for short-lived dev).
- Never put it in URL params or logs.

### Token sharing (candidate)

The candidate's `access_token` is in the URL path. Treat the full portal URL (`<base>/portal/<token>`) as the secret — anyone with it can take the assessment as that candidate.

---

## 12. Testing instructions

### Postman collection

Pre-built collection covering every endpoint:

```
custom_addons/etp_assessment_extension/etp_assessment_extension.postman_collection.json
```

**Import**: Postman → Import → drag the file. The collection has a pre-configured `accessToken` variable that the **Auth → Get Access Token** request auto-populates via a Postman test script. Run that request first; every other admin call already references `{{accessToken}}`.

For portal requests, run **Candidates → List Candidates**, copy any `access_token` value, and paste it into the `candidateToken` collection variable.

### cURL — full end-to-end smoke

```bash
BASE=http://localhost:8071

# 1. Get admin token
TOKEN=$(curl -s -X POST $BASE/api/v1/auth_token \
  -H 'Content-Type: application/json' \
  -d '{"login":"admin","password":"admin"}' \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['data']['access_token'])")

H="Access-Token: $TOKEN"
EXT="$BASE/api/v1/etp_assessment_ext"

# 2. Dashboard
curl -s -H "$H" "$EXT/dashboard_overview" | python3 -m json.tool

# 3. Create a category
curl -s -X POST -H "$H" -H 'Content-Type: application/json' \
  -d '{"name":"Demo Cat","sequence":10}' "$EXT/categories"

# 4. Create a dimension with options
curl -s -X POST -H "$H" -H 'Content-Type: application/json' \
  -d '{"name":"Demo Dim","options":[{"name":"A","sequence":1},{"name":"B","sequence":2}]}' \
  "$EXT/dimensions"

# 5. Create a question (assuming category_id=1, dimension_id=1, master option_ids=1,2)
curl -s -X POST -H "$H" -H 'Content-Type: application/json' -d '{
  "name":"Demo Q","question_type":"text","category_id":1,"prompt":"Pick one.",
  "dimensions":[{"dimension_id":1,"sequence":1,"options":[
    {"master_option_id":1,"is_correct":true,"sequence":1},
    {"master_option_id":2,"is_correct":false,"sequence":2}
  ]}]
}' "$EXT/questions"

# 6. Create an assessment
curl -s -X POST -H "$H" -H 'Content-Type: application/json' \
  -d '{"name":"Demo Assessment","category_id":1,"duration_minutes":30}' "$EXT/assessments"

# 7. Bulk import candidates
cat > /tmp/cands.csv <<EOF
name,email,job_title,department
John Doe,john@example.com,Evaluator,Engineering
EOF
curl -s -X POST -H "$H" -F "file=@/tmp/cands.csv" \
  "$EXT/assessments/1/candidates/bulk_import"

# 8. Start the assessment
curl -s -X POST -H "$H" "$EXT/assessments/1/start"

# 9. Get the candidate's token
CTOK=$(curl -s -H "$H" "$EXT/assessments/1/candidates" \
  | python3 -c "import sys,json;d=json.load(sys.stdin);print(d['blocks'][0]['rows'][0]['access_token'])")

# 10. Portal flow
curl -s "$EXT/portal/$CTOK"                # instructions
curl -s -X POST "$EXT/portal/$CTOK/begin"  # start timer
curl -s "$EXT/portal/$CTOK"                # question
curl -s -X POST -H 'Content-Type: application/json' -d '{
  "question_id":1,
  "justification":"because A is correct",
  "selections":[{"dimension_id":1,"option_id":1}]
}' "$EXT/portal/$CTOK/submit"

# 11. Verify
curl -s -H "$H" "$EXT/responses?assessment_id=1"
curl -s -H "$H" "$EXT/analytics/dimensions?assessment_id=1"
```

### Health check

Hit any protected endpoint with no header. A `401` with the expected envelope proves the module is loaded and routes are registered:

```bash
curl -s http://localhost:8071/api/v1/etp_assessment_ext/dashboard_overview
# {"message": "missing access token in request header", "errors": [], "status_code": 401}
```

A `404` instead would mean the module didn't load — restart Odoo with `-u etp_assessment_extension`.

---

## 13. Environment configuration

### Required Odoo system parameters

| Key | Used by | Purpose |
|---|---|---|
| `web.base.url` | `etp_assessment` email template | Base URL embedded in candidate invitation emails. **Must match the host clients use to reach the portal.** Set under Settings → Technical → Parameters → System Parameters. |

### Required user setup

- The Odoo user calling admin endpoints must be in **ETP Assessment / Manager** (`etp_assessment.group_assessment_manager`) for writes, or at least **ETP Assessment / Candidate (Evaluator)** (`etp_assessment.group_assessment_evaluator`) for reads.
- Set via Settings → Users & Companies → Users → tick the group on the user form.

### Email configuration

- For `POST /assessments/<id>/start` to actually deliver invitation emails, configure an outgoing mail server in Settings → Technical → Email → Outgoing Mail Servers.
- The email template is `etp_assessment.email_assessment_invitation`. Customize subject/body under Settings → Technical → Email → Email Templates.
- If no mail server is configured, the start action still succeeds (creates evaluator rows + tokens) but logs a warning per failed send. Admins can still hand out portal URLs manually using the tokens from `/assessments/<id>/candidates`.

### CORS

All endpoints set `cors="*"`. Adjust if you need to restrict origins — edit the `@http.route` decorators in [`controllers/`](controllers).

### Install / reinstall

```bash
cd <project_root>

# First install
./odoo.sh stop
source .venv/bin/activate && python src/odoo-bin -c odoo.conf -d <db_name> \
    -i etp_assessment_extension --stop-after-init --no-http
./odoo.sh start

# Upgrade after code changes
./odoo.sh stop
source .venv/bin/activate && python src/odoo-bin -c odoo.conf -d <db_name> \
    -u etp_assessment_extension --stop-after-init --no-http
./odoo.sh start
```

---

## Quick reference card

| What | How |
|---|---|
| **Login** | `POST /api/v1/auth_token` `{login, password}` → returns `data.access_token` |
| **Header for admin endpoints** | `Access-Token: <admin_token>` |
| **Header for portal endpoints** | None (token is in URL path) |
| **Health check** | `curl http://localhost:8071/api/v1/etp_assessment_ext/dashboard_overview` → expect `401 missing access token` |
| **Response envelope** | `{ message, errors, status_code, ...payload }` |
| **Dashboard / list contract** | `{ role, blocks: [{type, ...}] }` |
| **Pagination** | `?page=1&limit=20` (limit capped at 200) |
| **Sort** | `?sort_by=...&sort_order=asc|desc` |

---

**Document version**: 1.0
**Last verified against module**: `etp_assessment_extension 19.0.1.0.0`
**Generated from source**: [controllers/](controllers), [models/](models), live curl smoke tests on 2026-06-09.
