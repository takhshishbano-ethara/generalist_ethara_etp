# Response Fields API

Dynamic per-project response fields that taskers fill during task execution.

---

## Overview

At **project creation**, admins specify a number of response fields (e.g., 3).
This auto-generates labeled fields: **Response A**, **Response B**, **Response C**...
Taskers fill these text fields anytime during the task lifecycle.

### Data Flow

```
Project Creation → project.response.config records (Response A, B, C...)
         ↓
Task Start → task.forge.response records (empty, scaffolded from config)
         ↓
During Task → Tasker fills response values via save endpoint
         ↓
Task End → Validation (all must be filled if project requires it)
```

---

## Models

### `project.response.config`
**Module**: `project_extension`
**Table**: Project-level configuration of response field slots.

| Field | Type | Description |
|-------|------|-------------|
| `project_id` | Many2one (project.project) | Parent project (cascade delete) |
| `sequence` | Integer | 1-indexed ordering |
| `label` | Char | Auto-generated: "Response A", "Response B", etc. |

**SQL Constraint**: `unique(project_id, sequence)`

### `task.forge.response`
**Module**: `task_forge_core`
**Table**: Task-level response values filled by taskers.

| Field | Type | Description |
|-------|------|-------------|
| `task_id` | Many2one (task.forge.log) | Parent task (cascade delete) |
| `config_id` | Many2one (project.response.config) | Which config slot |
| `label` | Char | Snapshot of config label at creation time |
| `sequence` | Integer | Snapshot of sequence |
| `value` | Text | Tasker's response text |
| `project_id` | Related (stored) | From task_id.project_id |
| `employee_id` | Related (stored) | From task_id.employee_id |

**SQL Constraint**: `unique(task_id, config_id)`

### Extended: `project.project`

| Field | Type | Description |
|-------|------|-------------|
| `is_response_required` | Boolean | Whether tasks need response fields |
| `no_of_responses` | Integer | How many response slots |
| `response_config_ids` | One2many | Config records |

### Extended: `task.forge.log`

| Field | Type | Description |
|-------|------|-------------|
| `response_ids` | One2many | Response value records |
| `response_completed` | Boolean (computed, stored) | True when all slots are filled |

---

## Endpoints

### 1. Project Creation — `POST /api/v1/create_project_record`

**File**: `project_extension/controllers/main.py`
**Auth**: `validate_token` (access_token header)

#### New Request Fields

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `is_response_required` | bool/string | No | Enable response fields (`true`, `1`, `"1"`) |
| `no_of_responses` | int | No | Number of response slots (1-26+) |

#### Behavior

When `is_response_required` is truthy and `no_of_responses > 0`, auto-generates `project.response.config` records:
- Sequence 1 → "Response A"
- Sequence 2 → "Response B"
- Sequence 27 → "Response AA"

#### Example Request

```json
{
  "name": "My Annotation Project",
  "is_response_required": true,
  "no_of_responses": 3
}
```

---

### 2. Project Creation — `POST /api/v2/taskforge/projects`

**File**: `task_forge_bridge/controllers/project_controllers.py`
**Auth**: `validate_token`

#### New Request Fields

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `is_response_required` | bool | No | Enable response fields |
| `no_of_responses` | int | No | Number of response slots |

#### Example Request

```json
{
  "name": "New Project",
  "status": "live",
  "is_response_required": true,
  "no_of_responses": 4
}
```

#### Response (200)

```json
{
  "status": 200,
  "message": "Project created",
  "data": {
    "id": 42,
    "name": "New Project",
    "status": "live"
  }
}
```

---

### 3. Project Update — `POST /api/v2/taskforge/projects/update`

**File**: `task_forge_bridge/controllers/project_controllers.py`
**Auth**: `validate_token`

#### New Request Fields

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `is_response_required` | bool | No | Toggle response fields |
| `no_of_responses` | int | No | New count (syncs: adds/removes config records) |

#### Sync Behavior

- If `no_of_responses` increases → new config records appended
- If `no_of_responses` decreases → excess config records removed (highest sequences first)
- Existing tasks are NOT retroactively modified

#### Example Request

```json
{
  "project_id": 42,
  "no_of_responses": 5
}
```

---

### 4. Project Detail — `GET /api/v1/get_project_detail_view`

**File**: `project_extension/controllers/main.py`
**Auth**: `validate_token`

#### New Response Fields

```json
{
  "data": {
    "id": 42,
    "name": "My Project",
    "is_response_required": true,
    "no_of_responses": 3,
    "response_configs": [
      {"id": 1, "label": "Response A", "sequence": 1},
      {"id": 2, "label": "Response B", "sequence": 2},
      {"id": 3, "label": "Response C", "sequence": 3}
    ]
  }
}
```

---

### 5. Task Start — `POST /api/v2/taskforge/tasks/start`

**File**: `task_forge_core/controllers/task_controllers.py`
**Auth**: `validate_token`

#### Behavior

When a task is started for a project with `is_response_required=True`:
- Automatically scaffolds empty `task.forge.response` records from project config
- Each record gets: `label` (snapshot), `sequence` (snapshot), `value` = empty

No new request fields needed — scaffolding is automatic.

#### Response includes scaffolded responses:

```json
{
  "data": {
    "id": 100,
    "task_name": "Annotate sample 1",
    "responses": [
      {"id": 1, "config_id": 1, "label": "Response A", "sequence": 1, "value": ""},
      {"id": 2, "config_id": 2, "label": "Response B", "sequence": 2, "value": ""},
      {"id": 3, "config_id": 3, "label": "Response C", "sequence": 3, "value": ""}
    ],
    "response_completed": false
  }
}
```

---

### 6. Save Responses — `POST /api/v2/taskforge/tasks/responses` ⭐ NEW

**File**: `task_forge_core/controllers/task_controllers.py`
**Auth**: `validate_token`
**Method**: POST
**Content-Type**: application/json

Allows taskers to save/update response values **anytime** while task is in progress.

#### Request Body

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `task_id` | int | Yes | Task log ID |
| `responses` | array | Yes | List of response objects |
| `responses[].config_id` | int | Yes | Response config ID |
| `responses[].value` | string | Yes | Response text value |

#### Example Request

```json
{
  "task_id": 100,
  "responses": [
    {"config_id": 1, "value": "The model produced a coherent paragraph about climate change."},
    {"config_id": 2, "value": "Minor grammatical errors in second sentence."}
  ]
}
```

#### Success Response (200)

```json
{
  "status": 200,
  "message": "Responses saved",
  "data": {
    "data": {
      "id": 100,
      "task_name": "Annotate sample 1",
      "responses": [
        {"id": 1, "config_id": 1, "label": "Response A", "sequence": 1, "value": "The model produced a coherent paragraph about climate change."},
        {"id": 2, "config_id": 2, "label": "Response B", "sequence": 2, "value": "Minor grammatical errors in second sentence."},
        {"id": 3, "config_id": 3, "label": "Response C", "sequence": 3, "value": ""}
      ],
      "response_completed": false
    }
  }
}
```

#### Error Responses

| Status | Condition |
|--------|-----------|
| 400 | `responses` is not a list |
| 400 | Item missing `config_id` |
| 400 | Task is not in progress |
| 403 | Not the task owner |
| 404 | Task not found |
| 404 | Employee profile not found |
| 404 | Config ID not found for this task |

#### Validation Rules

- V1: `task_id` is required (int)
- V2: `responses` must be a list of objects
- V3: Each object must have `config_id`
- V4: Task must be in `in_progress` state
- V5: Task must belong to the calling employee
- V6: `config_id` must correspond to a scaffolded response for this task

#### Partial saves allowed

You can save a subset of responses. Only provided config_ids are updated.

---

### 7. Task End — `POST /api/v2/taskforge/tasks/end`

**File**: `task_forge_core/controllers/task_controllers.py`
**Auth**: `validate_token`

#### Accepts Responses (Optional)

The task end endpoint can also **save responses** in the same call. If `responses` is provided, values are persisted before validation — allowing the frontend to submit everything at once.

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `task_id` | int | Yes | Task log ID |
| `responses` | array/JSON string | No | Response values to save before completing |
| `responses[].config_id` | int | Yes (per item) | Response config ID |
| `responses[].value` | string | Yes (per item) | Response text value |
| `end_screenshot` | file | No | End screenshot upload |
| `rubric_ratings` | JSON string | No | Rubric ratings array |
| `blocker_reason` | string | No | If provided, creates blocker instead of completing |

#### Example Request (with responses)

```json
{
  "task_id": 100,
  "responses": [
    {"config_id": 1, "value": "The model produced a coherent paragraph about climate change."},
    {"config_id": 2, "value": "Minor grammatical errors in second sentence."},
    {"config_id": 3, "value": "Overall quality is acceptable."}
  ],
  "rubric_ratings": "[{\"dimension_id\": 1, \"option_id\": 2}]"
}
```

#### Flow

1. Save responses (if `responses` provided in request body)
2. Validate all response fields are filled (if project requires it)
3. Validate rubric ratings (if project requires it)
4. Complete task or raise blocker

#### Validation

If the task's project has `is_response_required=True`:
- All response records must have a non-empty `value`
- If any are missing, returns 400 with list of missing labels
- Skipped if `blocker_reason` is provided (blockers bypass validation)

#### Error Response (400)

```json
{
  "status": 400,
  "message": "All response fields must be filled before completing. Missing: Response B, Response C"
}
```

---

### 8. Task Create (Bulk) — `POST /api/v2/taskforge/tasks/create`

**File**: `task_forge_core/controllers/task_controllers.py`
**Auth**: `validate_token`

#### Behavior

Same scaffolding as task start — auto-creates empty response records from project config.

---

### 9. Task List / Detail

All task responses are included in the standard `_format_task()` serializer, which is used by:

- `GET /api/v2/taskforge/tasks` (list)
- `POST /api/v2/taskforge/tasks/start` (start response)
- `POST /api/v2/taskforge/tasks/end` (end response)
- `POST /api/v2/taskforge/tasks/responses` (save response)
- `POST /api/v2/taskforge/tasks/rate` (rate response)
- `DELETE /api/v2/taskforge/tasks/delete` (delete response)

#### Response Format in `_format_task()`

```json
{
  "id": 100,
  "task_name": "...",
  "responses": [
    {
      "id": 1,
      "config_id": 1,
      "label": "Response A",
      "sequence": 1,
      "value": "filled text or empty string"
    }
  ],
  "response_completed": true
}
```

---

## Label Generation

Labels follow alphabetical pattern, supporting unlimited fields:

| Sequence | Label |
|----------|-------|
| 1 | Response A |
| 2 | Response B |
| 26 | Response Z |
| 27 | Response AA |
| 28 | Response AB |
| 52 | Response AZ |
| 53 | Response BA |

---

## Typical Workflow

```
1. Admin creates project with is_response_required=true, no_of_responses=3
   → project.response.config: [{seq:1, "Response A"}, {seq:2, "Response B"}, {seq:3, "Response C"}]

2. Tasker starts task (POST /api/v2/taskforge/tasks/start)
   → task.forge.response: [{config:1, value:""}, {config:2, value:""}, {config:3, value:""}]

3. Tasker fills some responses (POST /api/v2/taskforge/tasks/responses)
   → Updates value for specified config_ids

4. Tasker fills remaining (same endpoint, partial or full)
   → response_completed flips to true when all are filled

5. Tasker ends task (POST /api/v2/taskforge/tasks/end)
   → Validates all filled → succeeds
   → If any empty → returns 400 with missing labels
```

---

## Notes

- **No manifest changes required** — all dependencies already declared
- **Security**: Both models have full CRUD access for all groups (same as rubric pattern)
- **Backward compatible** — existing tasks without responses return `responses: []`, `response_completed: false`
- **Idempotent scaffolding** — `scaffold_for_task()` skips already-created responses (safe to call twice)
- **Snapshot labels** — response records store label at creation time, immune to later config edits

---

## Timer Enable/Disable

A project-level boolean that controls whether the timer UI is visible on the task screen.

### Model Field

**File**: `project_extension/models/project.py`

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `is_timer_enabled` | Boolean | False | Show/hide timer on task screen |

### Affected Endpoints

#### Project Creation — `POST /api/v1/create_project_record`

Accepts `is_timer_enabled` (truthy: `true`, `1`, `"1"`).

```json
{
  "name": "My Project",
  "is_timer_enabled": true
}
```

#### Project Creation — `POST /api/v2/taskforge/projects`

Accepts `is_timer_enabled` (bool).

```json
{
  "name": "My Project",
  "is_timer_enabled": true
}
```

#### Project Update — `POST /api/v2/taskforge/projects/update`

Accepts `is_timer_enabled` (bool). Can toggle on/off anytime.

```json
{
  "project_id": 42,
  "is_timer_enabled": false
}
```

#### Project Detail — `GET /api/v1/get_project_detail_view`

Returns `is_timer_enabled` in response:

```json
{
  "data": {
    "id": 42,
    "is_timer_enabled": true
  }
}
```

#### Task Responses (all endpoints using `_format_task()`)

Every task response includes:

```json
{
  "id": 100,
  "task_name": "...",
  "is_timer_enabled": true
}
```

Frontend uses this flag to decide whether to render the timer component.
