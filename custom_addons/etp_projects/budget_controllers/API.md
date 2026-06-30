# Project Budget Controller API

Base path: `/api/v1/etp_projects/budget`
Source: [`project_budget_controller.py`](./project_budget_controller.py)

All routes are decorated `type="http", auth="none", csrf=False, cors="*"` and protected by `@validate_token` (Authorization header required). Responses use a uniform envelope:

```json
{ "message": "string", "status": 200, "data": { ... } }
```

Request bodies on `POST`/`PATCH` are read by `_read_multipart_or_json()` which accepts either:
- `application/json` — JSON body
- `multipart/form-data` — JSON in a `payload` field plus zero-or-more files in an `attachments` field (uploaded to S3, URLs persisted on the budget as a comma-separated string)

Pagination on listing endpoints: `limit` (default 100, max 500) and `offset` (default 0).

---

## 1. List AI Models

```
GET /api/v1/etp_projects/budget/models
```

Returns the catalogue of AI models that can be referenced from model lines.

### Query Parameters

| Name | Type | Default | Notes |
|---|---|---|---|
| `limit` | int | 100 | max 500 |
| `offset` | int | 0 | |
| `include_inactive` | int (0/1) | 0 | when 0, only `active=True` rows |
| `search` | string | — | ilike against `name` OR `provider` |

### Response (200)

```json
{
  "total": 12,
  "limit": 100,
  "offset": 0,
  "models": [
    { "id": 1, "name": "GPT-4o", "provider": "openai", "active": true }
  ]
}
```

---

## 2. List Infrastructure Types

```
GET /api/v1/etp_projects/budget/infra
```

### Query Parameters

| Name | Type | Default | Notes |
|---|---|---|---|
| `limit` | int | 100 | max 500 |
| `offset` | int | 0 | |
| `include_inactive` | int (0/1) | 0 | |
| `search` | string | — | ilike against `name` OR `code` |

### Response (200)

```json
{
  "total": 3,
  "limit": 100,
  "offset": 0,
  "infra": [
    { "id": 1, "name": "EC2", "code": "ec2", "active": true }
  ]
}
```

> The `etp.infra.type` model has no cost field — per-day cost is N/A at the type level.

---

## 3. List Subscriptions

```
GET /api/v1/etp_projects/budget/subscriptions
```

### Query Parameters

| Name | Type | Default | Notes |
|---|---|---|---|
| `limit` | int | 100 | max 500 |
| `offset` | int | 0 | |
| `include_inactive` | int (0/1) | 0 | |
| `search` | string | — | ilike against `name` |

### Response (200)

`per_day_cost = cost / 30.0` derived on the fly (no stored compute on the catalog model).

```json
{
  "total": 4,
  "limit": 100,
  "offset": 0,
  "subscriptions": [
    { "id": 1, "name": "Cursor Pro", "cost": 20.0, "per_day_cost": 0.6666666666666666, "active": true }
  ]
}
```

---

## 4. List Default Approvers

```
GET /api/v1/etp_projects/budget/default_approvers
```

Returns the users configured as default approvers in **Settings → ETP Projects → Default Approvers** (`ir.config_parameter` key `etp_projects.default_approver_user_ids`, persisted as a comma-separated string).

### Response (200)

```json
{
  "total": 2,
  "approvers": [
    { "id": 7, "name": "Alice", "login": "alice", "email": "alice@example.com" }
  ]
}
```

---

## 5. List Project Budgets

```
GET /api/v1/etp_projects/budget/list
```

### Query Parameters

| Name | Type | Default | Notes |
|---|---|---|---|
| `limit` | int | 100 | |
| `offset` | int | 0 | |
| `project_id` | int | — | filter |
| `budget_type` | string | — | one of the `project_type` selection values (`rnd`, `operations`) |
| `state` | string | — | one of the `state` selection values |
| `search` | string | — | ilike against `name` OR `description` |

### Response (200)

```json
{
  "total": 5,
  "limit": 100,
  "offset": 0,
  "budgets": [
    {
      "id": 12,
      "name": "Acme R&D - Research & Development",
      "project_id": 3,
      "project_name": "Acme R&D",
      "budget_type": "rnd",
      "budget_type_label": "Research & Development",
      "state": "approved",
      "state_label": "Approved",
      "total_tasks": 1500,
      "budget_amount": 25430.0,
      "batch_count": 3,
      "approver_count": 4,
      "create_date": "2026-06-29 12:00:00"
    }
  ]
}
```

---

## 6. Project Budget Detail

```
GET /api/v1/etp_projects/budget/detail?id=<int>
```

### Query Parameters

| Name | Type | Required | Notes |
|---|---|---|---|
| `id` | int | yes | budget id |

### Response (200)

Full serialization via `_budget_to_dict`:

```json
{
  "data": {
    "id": 12,
    "name": "Acme R&D - Research & Development",
    "description": "Q3 research budget",
    "project_id": 3,
    "project_name": "Acme R&D",
    "budget_type": "rnd",
    "budget_type_label": "Research & Development",
    "state": "approved",
    "state_label": "Approved",
    "priority": "high",
    "priority_label": "High",
    "total_tasks": 1500,
    "buffer_pct": 10.0,
    "budget_amount": 25430.0,
    "approver_ids": [7, 8],
    "approvers": [
      { "id": 7, "name": "Alice", "email": "alice@example.com" }
    ],
    "model_lines": [
      {
        "id": 1, "ai_model_id": 1, "model_name": "GPT-4o", "provider": "openai",
        "cost_type": "per_task", "per_task_cost": 0.05,
        "per_trajectory_cost": 0.0, "iterations": 0
      }
    ],
    "infra_lines": [
      {
        "id": 1, "infra_id": 1, "infra_name": "EC2",
        "description": "g4dn.xlarge", "cost": 1500.0, "per_day_cost": 50.0
      }
    ],
    "subscription_lines": [
      {
        "id": 1, "subscription_id": 1, "subscription_name": "Cursor Pro",
        "cost_per_seat": 20.0, "assigned_to": [7, 8],
        "seat_count": 2, "monthly_total": 40.0, "per_day_cost": 1.333
      }
    ],
    "batches": [
      {
        "id": 4, "name": "PB-0001/01", "total_tasks": 500,
        "start_date": "2026-07-01", "end_date": "2026-07-31",
        "buffer_pct": 10.0, "state": "approved"
      }
    ],
    "attachment_ids": ["https://s3.../doc1.pdf"],
    "create_date": "2026-06-29 12:00:00",
    "write_date": "2026-06-29 12:00:00"
  }
}
```

### Errors

| Status | Condition |
|---|---|
| 400 | `id` missing |
| 404 | budget not found |

---

## 7. Create Project Budget

```
POST /api/v1/etp_projects/budget/create
```

Creates a project budget plus its phase (batch) budgets, then auto-submits a single budget request on the **first** batch. Two requests for approval are sent to approvers: the creation notice (`mail_template_project_budget_created`) and the request-submitted mail (`mail_template_batch_request_submitted`).

### Content Types

- `application/json` — full JSON payload
- `multipart/form-data` — JSON in the `payload` field, files in the `attachments` field

### Request Body

| Field | Type | Required | Notes |
|---|---|---|---|
| `project_id` | int | yes | must exist in `project.project` |
| `budget_type` | string | yes | one of the `project_type` selection (`rnd`, `operations`) |
| `total_no_of_tasks` (alias `total_tasks`) | int | yes | > 0; must equal sum of `batches[*].no_of_task` |
| `description` | string | no | persisted; also used as initial request `justification` (fallback `"Auto-generated from project budget '<name>' creation."`) |
| `buffer_pct` | float | no | default 0; used as fallback for each batch |
| `priority` | string | no | default `normal`; one of `low`, `normal`, `high`, `urgent` |
| `approver_ids` | array of int | no | merged with default approvers from settings. Must resolve to a non-empty set. |
| `models` | array of model entries | no | per-model cost lines |
| `infra` | array of infra entries | no | per-infra budget lines |
| `subscription` (alias `subscriptions`) | array of subscription entries | no | per-subscription assigned-seat lines |
| `batches` | array of batch entries | yes | one or more phases |
| `attachment_ids` | string (comma-separated URLs) | no | merged with files uploaded in `attachments` |

#### Model entry

```json
{
  "ai_model_id": 1,
  "cost_type": "per_task",
  "per_task_cost": 0.05,
  "per_trajectory_cost": 0.0,
  "iterations": 0
}
```

For `cost_type = "per_trajectory"`, `per_task_cost` is computed server-side as `per_trajectory_cost * iterations`.

#### Infra entry

```json
{ "infra_type_id": 1, "budget_amount": 1500.0, "description": "g4dn.xlarge" }
```

#### Subscription entry

```json
{ "subscription_id": 1, "assigned_user_ids": [7, 8], "cost_per_subscription": 20.0 }
```

`cost_per_subscription` is **optional**. If provided, the catalog row is updated (`etp.subscription.cost`) before the budget is created so all per-seat math uses the new value.

#### Batch entry

```json
{
  "name": "Phase 1",
  "no_of_task": 500,
  "start_date": "2026-07-01",
  "end_date": "2026-07-31",
  "buffer_pct": 10.0
}
```

`end_date` must be ≥ `start_date`. `name` is optional (sequence-assigned when absent).

### Server-side mission logic

1. Validate `project_id` exists and no duplicate `(project_id, project_type)` row exists.
2. Validate `total_no_of_tasks` > 0 and `sum(batches[*].no_of_task) == total_no_of_tasks`.
3. **Default approvers + payload approvers** are merged: `list(dict.fromkeys(default_ids + payload_ids))`. Defaults come from `_get_default_approver_user_ids()` which reads `ir.config_parameter` `etp_projects.default_approver_user_ids`. If the merged list is empty, returns 400.
4. Compute `budget_amount = total_tasks * Σ per_task_cost + Σ infra_amount + Σ subscription_monthly_total`.
5. Create the project budget; **infra and subscription lines are mirrored onto every batch** as `(0,0,vals)` clones (model lines are auto-copied by the `etp.batch.budget.create()` override).
6. On the **first batch** only, create an `etp.batch.budget.request.wizard` carrying model + infra request lines (model `requested_amount = first_batch.total_tasks * per_task_cost`; infra `requested_amount = budget_amount`) and `requested_total = budget.budget_amount`, then call `wizard.action_submit()` which posts `mail_template_batch_request_submitted` to approvers and sets request state to `pending`.
7. Subscription lines are written directly onto the created request via sudo write (the wizard does not natively support subscription lines).
8. `mail_template_project_budget_created` is also posted to approvers + CTO (creation notice — sent in addition to the request-submitted mail).

### Response (200)

`_budget_to_dict(budget)` plus an `initial_request` block when a request was created:

```json
{
  "data": {
    "id": 12,
    "name": "Acme R&D - Research & Development",
    "...": "...",
    "initial_request": {
      "id": 31,
      "name": "BBR-0001",
      "batch_id": 4,
      "state": "pending",
      "requested_total": 25430.0,
      "priority": "high"
    }
  }
}
```

### Errors (400)

- `project_id is required.`
- `Project <id> does not exist.`
- `budget_type must be one of [...]`.
- `total_no_of_tasks must be > 0.`
- `Project budget already exists for project <id> and type <type>.`
- `approver_ids must be a list of user ids.`
- `Approver user ids do not exist: [...]`.
- `No approvers resolved. Provide approver_ids or configure default approvers in Settings > ETP Projects.`
- `priority must be one of ['low', 'normal', 'high', 'urgent'].`
- `models / infra / subscription / batches must be a list.`
- `Sum of batch no_of_task (X) must equal total_no_of_tasks (Y).`
- `batches[i] requires both start_date and end_date.`
- `batches[i].end_date cannot precede start_date.`
- Subscription/infra/model id missing in their respective catalogs.

### Example

```bash
curl -X POST 'https://host/api/v1/etp_projects/budget/create' \
  -H 'Authorization: Bearer <token>' \
  -H 'Content-Type: application/json' \
  -d '{
    "project_id": 3,
    "budget_type": "rnd",
    "priority": "high",
    "description": "Q3 research budget",
    "total_no_of_tasks": 1500,
    "buffer_pct": 10,
    "approver_ids": [12],
    "models": [{ "ai_model_id": 1, "cost_type": "per_task", "per_task_cost": 0.05 }],
    "infra":  [{ "infra_type_id": 1, "budget_amount": 1500, "description": "g4dn" }],
    "subscription": [{ "subscription_id": 1, "assigned_user_ids": [7, 8] }],
    "batches": [
      { "name": "Phase 1", "no_of_task": 500, "start_date": "2026-07-01", "end_date": "2026-07-31" },
      { "name": "Phase 2", "no_of_task": 1000, "start_date": "2026-08-01", "end_date": "2026-09-30" }
    ]
  }'
```

---

## 8. Update Project Budget

```
PATCH  /api/v1/etp_projects/budget/update
POST   /api/v1/etp_projects/budget/update
```

Patch endpoint — every body field is optional except `id`. No request is auto-created on update.

### Content Types

Same as create: JSON or multipart.

### Request Body

| Field | Type | Notes |
|---|---|---|
| `id` | int | **required** |
| `project_id` | int | revalidated against `project.project` |
| `budget_type` | string | one of the `project_type` selection |
| `state` | string | one of the `state` selection |
| `name` | string | non-empty |
| `description` | string | |
| `buffer_pct` | float | |
| `priority` | string | one of `low`, `normal`, `high`, `urgent` |
| `total_no_of_tasks` (alias `total_tasks`) | int | > 0; triggers `budget_amount` recomputation |
| `approver_ids` | array of int | merged with default approvers (defaults always re-applied) |
| `models` | array | full replace (`(5,0,0)` then re-create) |
| `infra` | array | full replace |
| `subscription` (alias `subscriptions`) | array | full replace; catalog cost write-back when `cost_per_subscription` provided |
| `batches` | array | full replace (existing batches unlinked, then re-created) |
| `attachment_ids` | string | comma-separated URLs |
| `append_attachments` | int (0/1) | when 1, new attachments are appended to existing |

If `total_tasks` or any of `models / infra / subscription` is provided, `budget_amount` is recomputed.

### Response (200)

```json
{ "data": { /* _budget_to_dict(budget) */ } }
```

### Errors (400/404)

Same validation classes as create plus:

- `id is required.`
- 404 `Project budget not found.`
- `name cannot be empty.`
- `state must be one of [...]`.

---

## 9. List Budget Requests

```
GET /api/v1/etp_projects/budget/requests/list
```

### Query Parameters

| Name | Type | Default | Notes |
|---|---|---|---|
| `limit` | int | 100 | max 500 |
| `offset` | int | 0 | |
| `project_id` | int | — | filter |
| `batch_id` | int | — | filter |
| `project_budget_id` | int | — | filter |
| `state` | string | — | one of the `state` selection values |
| `search` | string | — | ilike against `name` OR `justification` |

### Response (200)

```json
{
  "total": 8,
  "limit": 100,
  "offset": 0,
  "requests": [
    {
      "id": 31,
      "name": "BBR-0001",
      "state": "pending",
      "priority": "high",
      "batch_id": 4,
      "project_id": 3,
      "project_budget_id": 12,
      "requested_total": 25430.0,
      "approved_total": 0.0,
      "request_date": "2026-06-29 12:00:00"
    }
  ]
}
```

> Exact summary shape comes from `_request_to_summary(r)` (see `project_budget_controller.py`).

---

## 10. Budget Request Detail

```
GET /api/v1/etp_projects/budget/requests/detail?id=<int>
```

### Query Parameters

| Name | Type | Required |
|---|---|---|
| `id` | int | yes |

### Response (200)

`_request_to_detail(rec)` — full nested serialization including:

- Core fields: `id, name, state, priority, justification, subject, message, request_date, approval_date, rejection_reason`
- Relations: `batch`, `project`, `project_budget`, `requester`, `approver`, `parent_request`
- Totals: `total_tasks, buffer_pct, requested_total, approved_total, remaining_amount`
- Line arrays: `model_lines[]` (incl. `requested_amount`/`approved_amount`), `infra_lines[]` (incl. `per_day_requested`/`per_day_approved`), `subscription_lines[]` (incl. `subscription_id`, `assigned_user_ids`, `requested_amount`, `approved_amount`, `final_amount`, `per_day_cost`)
- `attachment_ids`

### Errors

| Status | Condition |
|---|---|
| 400 | `id` missing |
| 404 | request not found |

---

## Cross-cutting notes

### Authentication

Every route is decorated with `@validate_token`. Send `Authorization: Bearer <token>` (or the project's configured scheme). Routes do **not** use Odoo session cookies (`auth="none"`).

### Mail templates

| Template XML ID | Fires from | Audience |
|---|---|---|
| `etp_projects.mail_template_project_budget_created` | `create_budget` (creation notice) | approvers + CTO |
| `etp_projects.mail_template_batch_request_submitted` | `wizard.action_submit()` on first-batch request | approvers |
| `etp_projects.mail_template_batch_request_approved` | request approve flow | requester + approvers |
| `etp_projects.mail_template_batch_request_rejected` | request reject flow | requester |
| `etp_projects.mail_template_batch_threshold` | consumption alerts (80% / 100%) | approvers + CTO |

### Default approvers configuration

Set in **Settings → ETP Projects → Default Approvers** (`etp_default_approver_user_ids` Many2many). The settings record persists the comma-joined IDs to `ir.config_parameter` key `etp_projects.default_approver_user_ids`. Both create and update auto-merge those defaults into the budget's approver list.

### Budget envelope formula

```
budget_amount = total_tasks * Σ per_task_cost
              + Σ infra.budget_amount
              + Σ subscription.final_amount      // = cost_per_seat * len(assigned_user_ids)
```

`per_day_cost` on stored line models is `budget_amount / 30.0` (infra), `final_amount / 30.0` (subscription).
