# Task Rubric Rating — API Reference

Backing files:
- `task_forge_core/models/task_forge_rubric_rating.py` (NEW — rating record)
- `task_forge_core/models/task_log.py` (extended — `rubric_rating_ids`, `rubric_completed`)
- `task_forge_core/controllers/task_controllers.py` (extended — `/tasks/end` + new GET)
- `project_extension/models/rubric.py` (extended — `description`, `is_required`)
- `project_extension/controllers/main.py` (extended — `get_project_detail_view` rubric block)

Implements the Figma tasker-side rubric rating flow
(`Ethara-R-D-–-Figma-05-01-2026_12_42_PM` pre-submit &
`Ethara-R-D-–-Figma-05-01-2026_12_43_PM` post-submit).

All endpoints are HTTP, gated by:

1. `api_auth_gateway.validate_token` — caller must pass a valid
   `access_token` HTTP header.
2. The tasker's token identity must match the `log.employee_id.user_id`
   for the task being rated. Other callers receive `403 Forbidden`.

---

## Endpoint shape at a glance

| Group   | Endpoint                                                 | Method | Purpose                                                   |
|---------|----------------------------------------------------------|--------|-----------------------------------------------------------|
| Read    | `/api/v1/get_project_detail_view`                        | GET    | **Already exists.** Returns rubric tree for a project.    |
| Read    | `/api/v2/taskforge/tasks/<task_id>/rubric_ratings`       | GET    | Rubric tree + any ratings saved so far for that task.     |
| Write   | `/api/v2/taskforge/tasks/end`                            | POST   | **Already exists.** Extended to accept `rubric_ratings`.  |

Submission is **embedded in `/tasks/end`** — one atomic POST carries
prompt, justification, screenshots, pause_time, and the full rubric
payload. Either all succeed or all fail. Ratings are **locked** once
the log reaches `state='completed'`.

---

## Base URL

```
http://<host>:8069
```

Local dev default (from `odoo-19/etp.conf`): `http://localhost:8069`.

---

## Authentication header

Every request needs:

```
access_token: <token from api.access_token table>
```

The rating endpoints additionally enforce that the token's user is the
same employee recorded on the task log. Cross-user attempts fail 403.

---

## Response envelope

Same as the rest of `task_forge_core`:

```json
{
  "message": "...",
  "errors": [],
  "status_code": 200,
  "data": { ... endpoint-specific payload ... }
}
```

On validation failure `errors` contains human-readable strings;
`data` is `{}`. Server stack traces are never leaked to the client.

---

## Data model

### `rubric.category` (project_extension — extended)

| Field                 | Type      | Notes                                                |
|-----------------------|-----------|------------------------------------------------------|
| `name`                | Char      | Figma card title (e.g. `Omni Elo`).                  |
| `description`         | Text      | **NEW.** Small blurb under the card title.           |
| `sequence`            | Integer   | Display order.                                       |
| `project_id`          | M2O       | `project.project`.                                   |
| `option_ids`          | O2M       | Response options shared across the card.             |
| `dimension_ids`       | O2M       | Dimensions rated with those options.                 |

### `rubric.category.option` (unchanged)

| Field        | Type    | Notes                                 |
|--------------|---------|---------------------------------------|
| `name`       | Char    | Figma radio label (e.g. `Response A`).|
| `value`      | Integer | Numeric score for analytics.          |
| `sequence`   | Integer | Display order.                        |
| `category_id`| M2O     | Parent card.                          |

### `rubric.dimension` (project_extension — extended)

| Field         | Type    | Notes                                                                        |
|---------------|---------|------------------------------------------------------------------------------|
| `name`        | Char    | Figma dimension row name (e.g. `Truthfulness`).                              |
| `description` | Text    | Sub-line under the dimension name.                                           |
| `sequence`    | Integer | Display order.                                                               |
| `is_required` | Boolean | **NEW.** Defaults `True`. Only dimensions with `True` must be rated to pass. |
| `category_id` | M2O     | Parent card.                                                                 |
| `option_ids`  | M2M     | Options eligible for this dimension (auto-synced from category).             |

### `task.forge.rubric.rating` (NEW — task_forge_core)

| Field                     | Type     | Notes                                                         |
|---------------------------|----------|---------------------------------------------------------------|
| `log_id`                  | M2O      | `task.forge.log`, required, `ondelete=cascade`, indexed.      |
| `dimension_id`            | M2O      | `rubric.dimension`, required, `ondelete=restrict`.            |
| `option_id`               | M2O      | `rubric.category.option`, required, `ondelete=restrict`.      |
| `category_id`             | M2O      | `related='dimension_id.category_id'`, stored.                 |
| `project_id`              | M2O      | `related='log_id.project_id'`, stored.                        |
| `employee_id`             | M2O      | `related='log_id.employee_id'`, stored.                       |
| `dimension_name_snapshot` | Char     | Snapshot at write-time. Survives rubric edits/unlinks.        |
| `option_name_snapshot`    | Char     | Snapshot at write-time.                                       |
| `option_value_snapshot`   | Integer  | Snapshot at write-time.                                       |

**SQL constraint:**
`UNIQUE (log_id, dimension_id)` — a dimension can be rated at most once per task.

**Immutability:** `write()` and `unlink()` are overridden to raise
`UserError` if `log_id.state == 'completed'`. Rating rows are created
inside the same transaction as `/tasks/end`'s state transition so the
lock only affects **subsequent** calls.

### `task.forge.log` (extended)

| Field               | Type     | Notes                                                                  |
|---------------------|----------|------------------------------------------------------------------------|
| `rubric_rating_ids` | O2M      | `task.forge.rubric.rating`, `log_id`.                                  |
| `rubric_completed`  | Boolean  | Computed & stored. `True` when every required dimension has a rating.  |

---

## Validation rules

Applied inside `/tasks/end` when `task.project_id.is_rubrics_required == True`:

| Rule | Error message (returned in `errors[]`)                                  |
|------|-------------------------------------------------------------------------|
| R1   | `rubric_ratings` field is required.                                     |
| R2   | `rubric_ratings` must be a JSON array of objects.                       |
| R3   | Each object must contain `dimension_id` (int) and `option_id` (int).    |
| R4   | Duplicate `dimension_id` in payload.                                    |
| R5   | `dimension_id` does not belong to this project's rubric.                |
| R6   | `option_id` does not belong to the same category as its `dimension_id`. |
| R7   | Missing ratings for required dimensions: `[<ids>]`.                     |

**Completeness**: the server collects every dimension where
`is_required=True` across all of the project's `rubric_category_ids`.
The payload **must** cover that exact set (R7). Optional dimensions may
be included or omitted at the tasker's discretion.

**Atomicity**: if any rule above fails, nothing is written, the task
stays `in_progress`, and the request returns 400.

---

## `GET /api/v2/taskforge/tasks/<int:task_id>/rubric_ratings`

Returns the rubric structure of the task's project plus any ratings
that have already been recorded for the task. Safe to poll; use after
page reload to rehydrate the tasker's prior selections.

### Query params

_None._ The task ID is the path parameter.

### Access

- Admin/CTO: any task.
- Tasker: only their own task (`log.employee_id.user_id == request.env.user`).
- Everyone else: 403.

### Example request

```bash
curl -H "access_token: $TOKEN" \
  http://localhost:8069/api/v2/taskforge/tasks/42/rubric_ratings
```

### Example response

```json
{
  "message": "Success",
  "errors": [],
  "status_code": 200,
  "data": {
    "task_id": 42,
    "project_id": 5,
    "project_name": "Multi-Mango Batch 7",
    "is_rubrics_required": true,
    "is_justification_required": true,
    "rubric_completed": false,
    "rubric_categories": [
      {
        "id": 1,
        "name": "Omni Elo",
        "description": "Evaluate the response on 6 quality dimensions.",
        "sequence": 10,
        "options": [
          {"id": 11, "name": "Response A", "value": 1, "sequence": 10},
          {"id": 12, "name": "Response B", "value": 2, "sequence": 20},
          {"id": 13, "name": "Response C", "value": 3, "sequence": 30},
          {"id": 14, "name": "Response D", "value": 4, "sequence": 40}
        ],
        "dimensions": [
          {
            "id": 101,
            "name": "Truthfulness",
            "description": "Factually accurate and verifiable.",
            "sequence": 10,
            "is_required": true,
            "options": [
              {"id": 11, "name": "Response A", "value": 1},
              {"id": 12, "name": "Response B", "value": 2},
              {"id": 13, "name": "Response C", "value": 3},
              {"id": 14, "name": "Response D", "value": 4}
            ]
          }
        ]
      }
    ],
    "ratings": [
      {
        "dimension_id": 101,
        "option_id": 11,
        "dimension_name": "Truthfulness",
        "option_name": "Response A",
        "option_value": 1
      }
    ]
  }
}
```

### Field sources

| Response field                    | Source                                                    |
|-----------------------------------|-----------------------------------------------------------|
| `task_id`                         | URL path.                                                 |
| `project_id` / `project_name`     | `task.forge.log.project_id`.                              |
| `is_rubrics_required`             | `project.project.is_rubrics_required`.                    |
| `is_justification_required`       | `project.project.is_justification_required`.              |
| `rubric_completed`                | `task.forge.log.rubric_completed` (stored compute).       |
| `rubric_categories[...]`          | `project.project.rubric_category_ids` expanded.           |
| `ratings[...]`                    | `task.forge.log.rubric_rating_ids` with snapshot fields.  |

### Error cases

| HTTP | Cause                                                              |
|------|--------------------------------------------------------------------|
| 400  | Task not found.                                                    |
| 403  | Caller is not admin/CTO and not the task's assigned tasker.        |

---

## `POST /api/v2/taskforge/tasks/end` (extended)

**Existing behaviour is unchanged** for projects where
`is_rubrics_required == False`. The new `rubric_ratings` field is
ignored in that case.

For rubric-required projects, the field is **mandatory**. Missing
or malformed payloads cause the entire `/tasks/end` call to fail and
the task stays `in_progress`.

### Request (multipart/form-data)

| Field             | Type             | Required                 | Notes                                                                 |
|-------------------|------------------|--------------------------|-----------------------------------------------------------------------|
| `task_id`         | int (string)     | Yes                      | `task.forge.log.id`.                                                  |
| `prompt`          | string           | Per existing rules       | Tasker's prompt text.                                                 |
| `justification`   | string           | Per existing rules       | Tasker's justification text.                                          |
| `pause_time`      | string           | No                       | Integer-looking string (existing Char-typed field).                   |
| `end_screenshot`  | file             | Per existing rules       | Binary upload.                                                        |
| `rubric_ratings`  | JSON-string      | If `is_rubrics_required` | Array of `{dimension_id, option_id}`. See validation rules above.     |
| `blocker_reason`  | string           | No                       | Existing blocker path — rubric is skipped when submitting a blocker.  |

### Payload example

```json
{
  "task_id": 42,
  "prompt": "Selected response B because it is concise and factual.",
  "justification": "All four candidates covered the question; B was shortest and used no speculative language.",
  "pause_time": "0",
  "rubric_ratings": [
    {"dimension_id": 101, "option_id": 11},
    {"dimension_id": 102, "option_id": 12},
    {"dimension_id": 103, "option_id": 11},
    {"dimension_id": 104, "option_id": 13},
    {"dimension_id": 105, "option_id": 11},
    {"dimension_id": 106, "option_id": 12}
  ]
}
```

Because the controller uses `type='http'` multipart, the array above
must be sent as the **string form of its JSON** inside the
`rubric_ratings` form field. The server parses it with
`json.loads(...)`.

### cURL example — happy path

```bash
curl -X POST "http://localhost:8069/api/v2/taskforge/tasks/end" \
  -H "access_token: $TOKEN" \
  -F "task_id=42" \
  -F "prompt=Selected response B because it is concise and factual." \
  -F "justification=All four candidates covered the question; B was shortest." \
  -F "pause_time=0" \
  -F "end_screenshot=@/tmp/shot.png" \
  -F 'rubric_ratings=[{"dimension_id":101,"option_id":11},{"dimension_id":102,"option_id":12},{"dimension_id":103,"option_id":11},{"dimension_id":104,"option_id":13},{"dimension_id":105,"option_id":11},{"dimension_id":106,"option_id":12}]'
```

### Success response

```json
{
  "message": "Success",
  "errors": [],
  "status_code": 200,
  "data": {
    "task_id": 42,
    "state": "completed",
    "rubric_completed": true,
    "rubric_ratings_saved": 6
  }
}
```

### Failure responses

**Missing payload (R1):**
```json
{
  "message": "Invalid rubric payload",
  "errors": ["rubric_ratings is required for this project."],
  "status_code": 400,
  "data": {}
}
```

**Incomplete set (R7):**
```json
{
  "message": "Invalid rubric payload",
  "errors": ["Missing ratings for required dimensions: [104, 105]."],
  "status_code": 400,
  "data": {}
}
```

**Cross-project dimension (R5):**
```json
{
  "message": "Invalid rubric payload",
  "errors": ["Dimension 999 does not belong to this project's rubric."],
  "status_code": 400,
  "data": {}
}
```

**Option/dimension mismatch (R6):**
```json
{
  "message": "Invalid rubric payload",
  "errors": ["Option 22 does not belong to the category of dimension 101."],
  "status_code": 400,
  "data": {}
}
```

### Post-submit lock

After a successful `/tasks/end`, any attempt to edit or delete a rating
row (via XML-RPC, shell, or future API) raises:

```
UserError: Cannot modify rubric ratings of a completed task (log id=42).
```

---

## `GET /api/v1/get_project_detail_view` (already exists — rubric block extended)

The `rubric_categories` block in the existing response is extended with
the two new fields. No other field changes; payload is backwards
compatible.

### Before

```json
"rubric_categories": [
  {
    "id": 1,
    "name": "Omni Elo",
    "sequence": 10,
    "options": [ { "id": 11, "name": "Response A", "value": 1, "sequence": 10 } ],
    "dimensions": [
      { "id": 101, "name": "Truthfulness", "description": "...", "sequence": 10,
        "options": [ { "id": 11, "name": "Response A", "value": 1 } ] }
    ]
  }
]
```

### After

```json
"rubric_categories": [
  {
    "id": 1,
    "name": "Omni Elo",
    "description": "Evaluate the response on 6 quality dimensions.",
    "sequence": 10,
    "options": [ { "id": 11, "name": "Response A", "value": 1, "sequence": 10 } ],
    "dimensions": [
      { "id": 101, "name": "Truthfulness", "description": "...", "sequence": 10,
        "is_required": true,
        "options": [ { "id": 11, "name": "Response A", "value": 1 } ] }
    ]
  }
]
```

| New field                     | Source                              |
|-------------------------------|-------------------------------------|
| `rubric_categories[].description`               | `rubric.category.description`       |
| `rubric_categories[].dimensions[].is_required`  | `rubric.dimension.is_required`      |

---

## End-to-end flow

1. **Frontend opens a task** → `GET /api/v2/taskforge/tasks/<id>/rubric_ratings`
   → renders the radio grid using `rubric_categories` + re-selects any
   saved `ratings`.
2. Tasker fills prompt + justification + picks radios.
3. Frontend grays out **Submit Task** until every required dimension
   has a selection locally.
4. **Submit** → `POST /api/v2/taskforge/tasks/end` with all fields
   plus `rubric_ratings` JSON.
5. Server validates in one transaction; on success, task is
   `completed`, rating rows are created with snapshot fields, locks
   engage.
6. Any subsequent `GET …/rubric_ratings` returns the same data with
   `rubric_completed: true` — useful for read-only review screens.

---

## Known gaps / future work

| # | Topic                        | Notes                                                                                      |
|---|------------------------------|--------------------------------------------------------------------------------------------|
| 1 | QR/PL rubric override        | No endpoint for QR to amend a tasker's rating. Add later if the QC workflow needs it.      |
| 2 | Per-dimension option subsets | `rubric.dimension.option_ids` is auto-synced to the full category option set. Relaxing the sync would let a dimension use a subset of options. |
| 3 | Weighted scoring             | `option.value` is stored but no aggregate compute exists. Add `task.forge.log.rubric_score` when the formula is decided. |
| 4 | Bulk analytics endpoint      | No rubric-specific analytics route yet — can reuse `main_dashboard.py` helpers later.      |

---

## Restart / upgrade

Both modules carry schema changes:

```bash
cd odoo-19 && python odoo-bin -c etp.conf -u project_extension,task_forge_core
```

---

## Smoke-test script

```bash
#!/usr/bin/env bash
set -euo pipefail
: "${TOKEN:?TOKEN env var is required}"
BASE="${BASE:-http://localhost:8069}"
TASK_ID="${TASK_ID:?TASK_ID env var is required}"

echo "--- GET rubric_ratings (pre-submit) ---"
curl -sS -H "access_token: $TOKEN" \
  "$BASE/api/v2/taskforge/tasks/$TASK_ID/rubric_ratings" | jq .

echo "--- POST /tasks/end (happy path) ---"
curl -sS -X POST -H "access_token: $TOKEN" \
  -F "task_id=$TASK_ID" \
  -F "prompt=Test prompt." \
  -F "justification=Test justification." \
  -F "pause_time=0" \
  -F 'rubric_ratings=[{"dimension_id":101,"option_id":11}]' \
  "$BASE/api/v2/taskforge/tasks/end" | jq .

echo "--- GET rubric_ratings (post-submit, should show rubric_completed) ---"
curl -sS -H "access_token: $TOKEN" \
  "$BASE/api/v2/taskforge/tasks/$TASK_ID/rubric_ratings" | jq .
```
