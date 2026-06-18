# ETP Projects — REST API

All endpoints are `POST`, `Content-Type: application/json`, and require an `access_token` header (validated against `api.access_token` via the `api_auth_gateway` module).

**Base URL:** `<host>/api/v1/etp_projects`

**Common headers**

| Header | Value |
|---|---|
| `Content-Type` | `application/json` |
| `access_token` | `<your api access token>` |

**Common response envelope**

```json
{
  "message": "<human-readable status>",
  "errors": [],
  "status_code": 200,
  "data": { ... }
}
```

| Status | Meaning |
|---|---|
| `200` | OK |
| `400` | Validation error / `UserError` raised by model |
| `401` | Missing or expired `access_token` |
| `404` | Record not found |

---

## 1. AWS Budgets

### 1.1 List AWS Budgets

`POST /aws_budget/list`

Returns AWS budget records sized for a bar-graph (project, allocated budget, consumed, remaining).

**Body (all optional)**

| Field | Type | Default | Notes |
|---|---|---|---|
| `include_inactive` | bool | `false` | When `false`, only `active=True` budgets are returned |
| `budget_ids` | list[int] | — | Filter to specific budget ids |
| `project_ids` | list[int] | — | Filter to budgets of specific projects |

**Response `data`**

```json
{
  "total": 2,
  "records": [
    {
      "id": 1,
      "seq": "AWS-2025-MAIN",
      "project_id": 10,
      "project_name": "Aurora Platform",
      "project_budget": 50000.0,
      "final_budget": 75000.0,
      "total_used_cost": 22500.0,
      "remaining_cost": 52500.0,
      "percent_consumed": 30.0,
      "currency": "INR",
      "currency_symbol": "₹"
    }
  ]
}
```

Notes:
- `seq` is the budget record's own `name` field.
- `project_budget` is the initial allocation (updated when a Token Purchase Request is completed).
- `final_budget` is the "Final Project Budget" (`budget_amount`) used by `total_used_cost` / `remaining_cost` math.
- Sorted by `project_id, name`.

### 1.2 Refresh AWS Cost (existing)

`POST /aws_cost/update_all`

Triggers a fresh fetch from AWS Cost Explorer for the matched budgets and re-evaluates threshold alerts (75/90/100%).

**Body (all optional)**

| Field | Type | Default | Notes |
|---|---|---|---|
| `include_inactive` | bool | `false` | When `false`, only `active=True` budgets are processed |
| `budget_ids` | list[int] | — | Filter to specific budget ids |
| `project_ids` | list[int] | — | Filter to budgets of specific projects |

**Response `data`**

```json
{
  "total_budgets": 2,
  "success_count": 2,
  "error_count": 0,
  "total_created": 14,
  "total_updated": 1,
  "results": [
    {
      "budget_id": 1,
      "budget_name": "AWS-2025-MAIN",
      "project_id": 10,
      "project_name": "Aurora Platform",
      "tag_key": "Project",
      "tag_value": "aurora",
      "status": "success",
      "created": 7,
      "updated": 0,
      "budget_amount": 75000.0,
      "total_consumed": 22500.0,
      "remaining": 52500.0,
      "percent_consumed": 30.0,
      "daily_burn_rate": 1500.0,
      "last_fetched_at": "2026-06-09 09:34:11"
    }
  ]
}
```

---

## 2. Token Purchase Requests

Workflow: **draft → pending → approved → completed** (with **rejected** as a terminal branch from pending).

| Action | Who can call | State guard |
|---|---|---|
| `create` / `submit` | Any authenticated user | draft → pending |
| `approve` / `reject` | User must be in the configured **Token Purchase Approvers** list (Settings → ETP Projects) | pending → approved/rejected |
| `complete` | Typically the configured **Finance / Infra Team Users** | approved → completed |

### 2.1 List Token Purchase Requests

`POST /token_purchase/list`

**Body (all optional)**

| Field | Type | Default | Notes |
|---|---|---|---|
| `state` | string \| list[string] | — | One or more of `draft`, `pending`, `approved`, `rejected`, `completed` |
| `project_id` | int | — | Filter by project |
| `budget_id` | int | — | Filter by budget |
| `requester_id` | int | — | Filter by requester user id |
| `mine_only` | bool | `false` | Filter to requests created by the authenticated user |
| `limit` | int | `100` | Clamped to `1..500` |
| `offset` | int | `0` | |

**Response `data`**

```json
{
  "total": 42,
  "limit": 100,
  "offset": 0,
  "count": 42,
  "results": [ { /* request payload, see §2.2 */ } ]
}
```

### 2.2 Get Token Purchase Request

`POST /token_purchase/get`

**Body**

| Field | Type | Required |
|---|---|---|
| `id` | int | yes |

**Response `data`** — single request payload + supporting documents metadata:

```json
{
  "id": 17,
  "name": "TPR/2026/0001",
  "state": "approved",
  "budget_id": 1,
  "budget_name": "AWS-2025-MAIN",
  "project_id": 10,
  "project_name": "Aurora Platform",
  "currency_id": 19,
  "currency": "INR",
  "model_name": "GPT-4 Turbo",
  "requested_amount": 5000.0,
  "approved_amount": 0.0,
  "description": "Quarterly model purchase for the ingestion pipeline.",
  "cost_center": "",
  "rejection_reason": "",
  "requester_id": 12,
  "requester_name": "Alice Engineer",
  "approver_id": 4,
  "approver_name": "Carol CTO",
  "approval_date": "2026-06-09 09:34:11",
  "completed_by_id": false,
  "completed_by_name": "",
  "completed_date": "",
  "balance_before": 0.0,
  "create_date": "2026-06-09 08:00:00",
  "supporting_document_ids": [],
  "supporting_document_count": 0,
  "supporting_documents": [
    {
      "id": 88,
      "name": "invoice.pdf",
      "mimetype": "application/pdf",
      "file_size": 31245
    }
  ]
}
```

### 2.3 Create Token Purchase Request

`POST /token_purchase/create`

**Body**

| Field | Type | Required | Notes |
|---|---|---|---|
| `budget_id` | int | yes | Must reference an existing `etp.project.aws.budget` |
| `model_name` | string | yes | |
| `requested_amount` | number | yes | Must be `> 0` |
| `description` | string | yes | Business justification |
| `submit` | bool | no | When `true`, immediately calls `submit` (state becomes `pending`) |

**Response `data`**

```json
{
  "request": { /* request payload, see §2.2 */ },
  "submitted": true
}
```

If `submit=true` and submission fails (e.g. no configured approvers), HTTP returns `400` with the created request still in the body and `submitted: false`.

### 2.4 Submit Token Purchase Request

`POST /token_purchase/submit`

Transitions `draft → pending` and emails configured approvers.

**Body**

| Field | Type | Required |
|---|---|---|
| `id` | int | yes |

**Response `data`** — `{ "request": { /* request payload */ } }`

Errors (400):
- Not in `draft` state.
- Missing required field (`model_name`, `requested_amount`, `description`).
- No configured approvers (configure in **Settings → ETP Projects → Token Purchase Approvers**).

### 2.5 Approve Token Purchase Request

`POST /token_purchase/approve`

Transitions `pending → approved`. Caller MUST be in the configured Token Purchase Approvers list. Emails the finance team and notifies the requester.

**Body**

| Field | Type | Required |
|---|---|---|
| `id` | int | yes |

**Response `data`** — `{ "request": { /* request payload */ } }`

Errors (400):
- `You are not in the configured Token Purchase Approvers list.`
- `Request is no longer pending approval.`

### 2.6 Reject Token Purchase Request

`POST /token_purchase/reject`

Transitions `pending → rejected`. Caller MUST be in the configured Token Purchase Approvers list. Notifies the requester.

**Body**

| Field | Type | Required |
|---|---|---|
| `id` | int | yes |
| `rejection_reason` | string | no |

**Response `data`** — `{ "request": { /* request payload */ } }`

### 2.7 Complete Token Purchase Request

`POST /token_purchase/complete`

Finalizes an approved request: records the approved amount + cost center, attaches supporting documents, and **increments the linked budget's `project_budget`** by `approved_amount`.

**Body**

| Field | Type | Required | Notes |
|---|---|---|---|
| `id` | int | yes | |
| `approved_amount` | number | yes | Must be `> 0` |
| `cost_center` | string | yes | |
| `attachment_ids` | list[int] | no | Existing `ir.attachment` ids to attach |
| `supporting_documents` | list[object] | no | New base64-encoded files to upload |

**`supporting_documents[]` item**

| Field | Type | Required | Notes |
|---|---|---|---|
| `filename` | string | yes | |
| `data_b64` | string | yes | Standard base64 of the file bytes |
| `mimetype` | string | no | Default `application/octet-stream` |

You must supply at least one document (via `attachment_ids` or `supporting_documents`).

**Response `data`**

```json
{
  "request": { /* request payload */ },
  "attachment_ids_created": [88, 89]
}
```

On any error during completion, attachments newly created by this call are rolled back (unlinked).

---

## 3. Dashboard

All dashboard endpoints aggregate exclusively from the `etp_projects` module's own data (`etp.project.aws.budget`, `etp.project.aws.cost.line`, `etp.project.token.purchase.request`). Token-level metrics (`token_in`, `token_out`, `blended_rate`) are not tracked at this layer and are returned as `null`.

Common scoping fields (most endpoints):

| Field | Type | Default | Notes |
|---|---|---|---|
| `project_ids` | list[int] | — | Filter to specific projects |
| `include_inactive` | bool | `false` | When `false`, only active budgets are considered |
| `start` / `end` | string (`YYYY-MM-DD`) | — | Date window. If only `end` is missing it defaults to today; if `start` is missing, falls back to `end - graph_days + 1` |
| `graph_days` | int | endpoint-specific | Clamped to `1..365` |

### 3.1 KPIs

`POST /dashboard/kpis`

Top-line portfolio cards: total spend, project budget, remaining, % consumed, daily burn, runway days, forecast for next calendar month.

**Body (all optional)**: `project_ids`, `include_inactive`.

**Response `data`**

```json
{
  "currency": "INR",
  "currency_symbol": "\u20b9",
  "total_spend": 22500.0,
  "total_project_budget": 75000.0,
  "total_remaining": 52500.0,
  "percent_consumed": 30.0,
  "daily_burn_rate": 1500.0,
  "runway_days": 35.0,
  "forecast_next_month": 45000.0,
  "budget_count": 2,
  "project_count": 2
}
```

Notes:
- `runway_days` is `null` when `daily_burn_rate` is `0`.
- `forecast_next_month` = `daily_burn_rate × days_in_next_calendar_month`.

### 3.2 Budget Timeline (Funded vs Remaining)

`POST /dashboard/budget_timeline`

Line/area series of `available_balance`, `consumed_to_date`, `added_to_date` per day, with `top_up` events on days where a TPR completes.

**Body (all optional)**

| Field | Type | Default | Notes |
|---|---|---|---|
| `project_id` | int | — | When set, delegates to the single-project model helper and ignores `project_ids` / `include_inactive` |
| `project_ids` | list[int] | — | Portfolio mode |
| `include_inactive` | bool | `false` | |
| `start` / `end` | date | end=today, start=end-29 | |
| `graph_days` | int | `30` | Used only when `start` is omitted |

**Response `data` (portfolio mode)**

```json
{
  "title": "Funded vs Remaining Balance",
  "range": "30d",
  "available_now": 50500.0,
  "window": {"start": "2026-05-13", "end": "2026-06-11"},
  "series": [
    {
      "date": "2026-05-13",
      "available_balance": 50000.0,
      "consumed_to_date": 0.0,
      "added_to_date": 50000.0,
      "event": {}
    },
    {
      "date": "2026-06-09",
      "available_balance": 55000.0,
      "consumed_to_date": 0.0,
      "added_to_date": 55000.0,
      "event": {
        "type": "top_up",
        "label": "+5000.00",
        "added": 5000.0,
        "available_after": 55000.0,
        "spent_since_last_topup": 0.0
      }
    }
  ],
  "currency": "INR",
  "currency_symbol": "\u20b9",
  "budget_count": 2,
  "project_count": 2
}
```

When called with `project_id`, response shape matches `_get_budget_timeline_for_project` (same series structure, no `budget_count` / `project_count`).

### 3.3 Daily Burn

`POST /dashboard/daily_burn`

Per-day burn rate stacked by AWS service. Monthly cost-line totals are spread evenly across the calendar days of their period.

**Body (all optional)**: `project_ids`, `include_inactive`, `start`, `end`, `graph_days` (default `30`).

**Response `data`**

```json
{
  "title": "Daily Burn",
  "window": {"start": "2026-05-13", "end": "2026-06-11"},
  "services": ["Amazon EC2", "Amazon S3"],
  "avg_per_day": 750.0,
  "series": [
    {
      "date": "2026-05-13",
      "total_inr": 720.0,
      "by_service": [
        {"service_name": "Amazon EC2", "amount": 600.0},
        {"service_name": "Amazon S3", "amount": 120.0}
      ]
    }
  ],
  "currency": "INR",
  "currency_symbol": "\u20b9",
  "note": "Monthly cost-line totals spread evenly across calendar days of each period."
}
```

### 3.4 Spend by Model

`POST /dashboard/spend_by_model`

Bar chart of total approved-amount per `model_name` across completed TPRs, with average per request (use for the "avg consume" bar).

**Body (all optional)**: `project_ids`, `start`, `end` (filter on `completed_date`).

**Response `data`**

```json
{
  "title": "Spend By Model",
  "window": {"start": "", "end": ""},
  "total_spend": 18500.0,
  "bars": [
    {"model_name": "GPT-4 Turbo", "total": 12000.0, "request_count": 3, "avg_per_request": 4000.0},
    {"model_name": "Claude 3.5 Sonnet", "total": 6500.0, "request_count": 2, "avg_per_request": 3250.0}
  ],
  "currency": "INR",
  "currency_symbol": "\u20b9"
}
```

### 3.5 Model Cost Table

`POST /dashboard/model_cost_table`

Tabular view: model name, spend, share %, request count, avg per request, plus the unavailable token columns kept in the response as `null` for forward-compatibility.

**Body (all optional)**: `project_ids`, `start`, `end`.

**Response `data`**

```json
{
  "title": "Model Cost Consumption",
  "window": {"start": "", "end": ""},
  "total_spend": 18500.0,
  "rows": [
    {
      "model_name": "GPT-4 Turbo",
      "spend": 12000.0,
      "share_pct": 64.86,
      "request_count": 3,
      "avg_per_request": 4000.0,
      "token_in": null,
      "token_out": null,
      "blended_rate": null,
      "cost": 12000.0
    }
  ],
  "currency": "INR",
  "currency_symbol": "\u20b9",
  "notes": {
    "token_in": "Not tracked in etp_projects.",
    "token_out": "Not tracked in etp_projects.",
    "blended_rate": "Not tracked in etp_projects."
  }
}
```

### 3.6 Budget Utilization by Project

`POST /dashboard/budget_utilization_by_project`

One row per budget with budget / consumed / remaining / percent + a `health` indicator (`green` < 75%, `amber` < 90%, `red` >= 90%). Sorted by `percent_consumed` descending.

**Body (all optional)**: `project_ids`, `include_inactive`.

**Response `data`**

```json
{
  "title": "Budget Utilization by Project",
  "total_rows": 2,
  "rows": [
    {
      "project_id": 10,
      "project_name": "Aurora Platform",
      "currency": "INR",
      "currency_symbol": "\u20b9",
      "budget_id": 1,
      "budget_name": "AWS-2025-MAIN",
      "budget_amount": 75000.0,
      "total_consumed": 60000.0,
      "remaining": 15000.0,
      "percent_consumed": 80.0,
      "health": "amber"
    }
  ]
}
```

### 3.7 Project × Model Consumption

`POST /dashboard/project_model_consumption`

For each project, the per-model breakdown of completed TPRs (model totals + share %). Suitable for stacked bar / drilldown.

**Body (all optional)**: `project_ids`, `start`, `end`.

**Response `data`**

```json
{
  "title": "Project-wise Model Consumption",
  "window": {"start": "", "end": ""},
  "projects": [
    {
      "project_id": 10,
      "project_name": "Aurora Platform",
      "total": 18500.0,
      "models": [
        {"model_name": "GPT-4 Turbo", "total": 12000.0, "request_count": 3, "share_pct": 64.86},
        {"model_name": "Claude 3.5 Sonnet", "total": 6500.0, "request_count": 2, "share_pct": 35.14}
      ]
    }
  ],
  "currency": "INR",
  "currency_symbol": "\u20b9"
}
```

### 3.8 Project Forecast vs Funded

`POST /dashboard/project_forecast_vs_funded`

Per-project funded vs forecast (next calendar month) and variance, ranked by variance descending (largest projected overshoot first).

**Body (all optional)**: `project_ids`, `include_inactive`.

**Response `data`**

```json
{
  "title": "Project Forecast vs Funded",
  "days_in_next_month": 31,
  "rows": [
    {
      "project_id": 10,
      "project_name": "Aurora Platform",
      "currency": "INR",
      "currency_symbol": "\u20b9",
      "budget_id": 1,
      "funded": 75000.0,
      "consumed": 60000.0,
      "remaining": 15000.0,
      "forecast_next_month": 46500.0,
      "variance": 31500.0,
      "daily_burn_rate": 1500.0
    }
  ]
}
```

### 3.9 Efficiency: Cost per Successful Output

`POST /dashboard/efficiency_cost_per_output`

Per project: AWS consumed cost divided by the count of completed TPRs (proxy for "successful output" since task-level success is not tracked in etp_projects).

**Body (all optional)**: `project_ids`, `include_inactive`.

**Response `data`**

```json
{
  "title": "Efficiency: Cost per Successful Output",
  "rows": [
    {
      "project_id": 10,
      "project_name": "Aurora Platform",
      "currency": "INR",
      "currency_symbol": "\u20b9",
      "budget_id": 1,
      "total_consumed": 60000.0,
      "successful_outputs": 5,
      "cost_per_output": 12000.0
    }
  ],
  "note": "Successful output = completed token purchase request (TPR). No task-level success tracking in etp_projects."
}
```

`cost_per_output` is `null` when `successful_outputs == 0`.

### 3.10 Spend Bridge (Why Spend Moved)

`POST /dashboard/spend_bridge`

Bridge waterfall decomposing the change in spend between two periods into `Volume`, `Model Mix`, and `Rate` effects.

- `Volume` per model = `(count_B - count_A) × avg_A`
- `Rate` per model = `count_B × (avg_B - avg_A)`
- `Model Mix` = residual = `(Now − Prev) − Volume − Rate`

**Body (all optional)**

| Field | Type | Default |
|---|---|---|
| `project_ids` | list[int] | — |
| `period_b_end` | date | today |
| `period_b_start` | date | first day of `period_b_end`'s month |
| `period_a_end` | date | `period_b_start − 1 day` |
| `period_a_start` | date | first day of `period_a_end`'s month |

**Response `data`**

```json
{
  "title": "Spend Bridge: Why spend moved",
  "period_a": {"start": "2026-05-01", "end": "2026-05-31", "total": 10000.0},
  "period_b": {"start": "2026-06-01", "end": "2026-06-11", "total": 14000.0},
  "bridge": [
    {"label": "Previous (A)", "value": 10000.0, "cumulative": 10000.0, "type": "anchor"},
    {"label": "Volume",       "value":  2500.0, "cumulative": 12500.0, "type": "delta"},
    {"label": "Model Mix",    "value":   500.0, "cumulative": 13000.0, "type": "delta"},
    {"label": "Rate",         "value":  1000.0, "cumulative": 14000.0, "type": "delta"},
    {"label": "Now (B)",      "value": 14000.0, "cumulative": 14000.0, "type": "anchor"}
  ],
  "per_model": [
    {
      "model_name": "GPT-4 Turbo",
      "prev": 6000.0, "now": 9000.0, "delta": 3000.0,
      "volume_effect": 2000.0, "rate_effect": 1000.0
    }
  ],
  "currency": "INR",
  "currency_symbol": "\u20b9"
}
```

### 3.11 Period over Period by Model

`POST /dashboard/period_over_period_by_model`

Matrix of completed-TPR spend by model across N periods. When `periods` is omitted, the last 3 calendar months are used.

**Body (all optional)**

| Field | Type | Default | Notes |
|---|---|---|---|
| `project_ids` | list[int] | — | |
| `periods` | list[object] | last 3 calendar months | Each item: `{"label": "2026-04", "start": "2026-04-01", "end": "2026-04-30"}` |

**Response `data`**

```json
{
  "title": "Period over Period by Model",
  "periods": [
    {"label": "2026-04", "start": "2026-04-01", "end": "2026-04-30", "total": 8000.0},
    {"label": "2026-05", "start": "2026-05-01", "end": "2026-05-31", "total": 10000.0},
    {"label": "2026-06", "start": "2026-06-01", "end": "2026-06-30", "total": 14000.0}
  ],
  "rows": [
    {
      "model_name": "GPT-4 Turbo",
      "total": 21000.0,
      "cells": [
        {"period_label": "2026-04", "amount": 5000.0},
        {"period_label": "2026-05", "amount": 7000.0},
        {"period_label": "2026-06", "amount": 9000.0}
      ]
    }
  ],
  "currency": "INR",
  "currency_symbol": "\u20b9"
}
```

### 3.12 Budget Movement (Allocation Ledger)

`POST /dashboard/budget_movement`

Time-ordered ledger of completed TPRs (top-ups) across the portfolio. When called with a single `project_id`, delegates to `_get_allocation_ledger_for_project` and returns its shape.

**Body (all optional)**

| Field | Type | Default | Notes |
|---|---|---|---|
| `project_id` | int | — | Single-project mode (delegates to model helper) |
| `project_ids` | list[int] | — | Portfolio mode |
| `limit` | int | `100` | Clamped to `1..500` |
| `offset` | int | `0` | |

**Response `data` (portfolio mode)**

```json
{
  "title": "Allocation Ledger (Portfolio)",
  "total": 12,
  "limit": 100,
  "offset": 0,
  "count": 12,
  "entries": [
    {
      "datetime": "2026-06-09T09:34:11Z",
      "action": "top_up",
      "action_label": "Aurora Platform TPR/2026/0001",
      "tpr_id": 17,
      "tpr_name": "TPR/2026/0001",
      "project_id": 10,
      "project_name": "Aurora Platform",
      "model_name": "GPT-4 Turbo",
      "amount": 5000.0,
      "balance_before": 50000.0
    }
  ]
}
```

### 3.13 Portfolio Drilldown

`POST /dashboard/portfolio_drilldown`

Per-project row that combines allocation, spend, utilization, forecast, variance, burn, runway, health, top model contribution, and the nested per-model breakdown. Sorted by `util_pct` descending.

**Body (all optional)**: `project_ids`, `include_inactive`.

**Response `data`**

```json
{
  "title": "Portfolio Budget Drilldown",
  "days_in_next_month": 31,
  "rows": [
    {
      "project_id": 10,
      "project_name": "Aurora Platform",
      "currency": "INR",
      "currency_symbol": "\u20b9",
      "budget_id": 1,
      "budget_name": "AWS-2025-MAIN",
      "allocation": 75000.0,
      "spend": 60000.0,
      "util_pct": 80.0,
      "remaining": 15000.0,
      "forecast_next_month": 46500.0,
      "variance": 31500.0,
      "burn_rate_per_day": 1500.0,
      "runway_days": 10.0,
      "health": "amber",
      "top_model": "GPT-4 Turbo",
      "top_model_attr_pct": 64.86,
      "models": [
        {"model_name": "GPT-4 Turbo", "total": 12000.0, "request_count": 3, "share_pct": 64.86},
        {"model_name": "Claude 3.5 Sonnet", "total": 6500.0, "request_count": 2, "share_pct": 35.14}
      ]
    }
  ]
}
```

`runway_days` is `null` when `burn_rate_per_day` is `0`.

---

## 4. Budget Consolidation

A single roll-up endpoint that returns the KPIs, two bar series, and the per-project budget table (with nested per-model breakdown) needed by the Budget Consolidation dashboard.

### 4.1 `POST /api/v1/etp_projects/budget_consolidation`

Request body (all fields optional):

| field | type | default | meaning |
|---|---|---|---|
| `project_ids` | `int[]` \| omitted | all | Restrict to these projects. Omit (or pass `null`) for portfolio-wide. |
| `include_inactive` | `bool` | `false` | When `true`, include archived (`active=False`) budgets. |
| `needs_attention_threshold_pct` | `number` | `80` | A project is counted as "needs attention" when its utilisation (`spend / budget * 100`) is at or above this value. |

Response payload:

```json
{
  "kpis": {
    "total_spend": 1234567.89,
    "total_remaining": 765432.11,
    "percent_consumed": 61.73,
    "total_budget": 2000000.0,
    "active_project_count": 12,
    "needs_attention_count": 3,
    "needs_attention_threshold_pct": 80.0,
    "top_model": {"model_name": "claude-opus-4-7", "amount": 487654.32, "share_pct": 39.5},
    "currency": "INR",
    "currency_symbol": "\u20b9"
  },
  "spend_by_model": [
    {"model_name": "claude-opus-4-7", "amount": 487654.32, "share_pct": 39.5},
    {"model_name": "gpt-4o",          "amount": 312345.10, "share_pct": 25.3},
    {"model_name": "(no model)",      "amount":  98765.43, "share_pct":  8.0}
  ],
  "spend_by_project": [
    {"project_id": 10, "project_name": "Aurora",  "amount": 612345.67, "share_pct": 49.6},
    {"project_id": 11, "project_name": "Mercury", "amount": 322221.22, "share_pct": 26.1}
  ],
  "budget_by_projects": [
    {
      "project_id": 10,
      "project_name": "Aurora",
      "spend": 612345.67,
      "budget": 700000.0,
      "remaining": 87654.33,
      "utilization_pct": 87.48,
      "runway_days": 17,
      "top_model": "claude-opus-4-7",
      "models": [
        {"model_name": "claude-opus-4-7", "spend": 367654.32, "share_pct": 60.04},
        {"model_name": "gpt-4o",          "spend": 244691.35, "share_pct": 39.96}
      ]
    }
  ]
}
```

Notes:

- **Per-model attribution methodology.** etp_projects does not track per-model consumption directly. For both the `spend_by_model` bar and each project's `models` list, the project's actual AWS consumption (`etp.project.aws.cost.line` total) is distributed across that project's models proportionally to the completed Token Purchase Request `approved_amount` per model. Projects with no completed TPRs (or no `model_name`) attribute their consumption to `"(no model)"`. As a result, `sum(spend_by_model[*].amount) == kpis.total_spend` and `sum(models[*].spend) == budget_by_projects[*].spend`.
- `top_model` (KPI) and `budget_by_projects[*].top_model` are picked by attributed spend (highest amount).
- `share_pct` in `spend_by_model` and `spend_by_project` is over `kpis.total_spend`. Inside `budget_by_projects[*].models`, `share_pct` is over that project's attributed spend.
- `runway_days` per project = `floor((budget - spend) / sum(daily_burn_rate))`. Returns `null` when the project's combined daily burn rate is `0`.
- `utilization_pct` and `percent_consumed` return `0` when their denominator is `0` (no budget recorded).
- `budget_by_projects` is sorted by `utilization_pct` desc; `spend_by_model` and `spend_by_project` are sorted by `amount` desc.
- Currency is taken from the first budget in scope, falling back to `base.INR`.

---

## Curl examples

```bash
# List bar-graph data for two projects
curl -X POST "$BASE/api/v1/etp_projects/aws_budget/list" \
  -H "Content-Type: application/json" \
  -H "access_token: $TOKEN" \
  -d '{"project_ids":[10,11]}'

# Create and submit a token purchase request in one call
curl -X POST "$BASE/api/v1/etp_projects/token_purchase/create" \
  -H "Content-Type: application/json" \
  -H "access_token: $TOKEN" \
  -d '{
        "budget_id": 1,
        "model_name": "GPT-4 Turbo",
        "requested_amount": 5000,
        "description": "Quarterly model purchase",
        "submit": true
      }'

# Approve a request (caller must be a configured approver)
curl -X POST "$BASE/api/v1/etp_projects/token_purchase/approve" \
  -H "Content-Type: application/json" \
  -H "access_token: $TOKEN" \
  -d '{"id": 17}'

# Complete with an uploaded receipt
curl -X POST "$BASE/api/v1/etp_projects/token_purchase/complete" \
  -H "Content-Type: application/json" \
  -H "access_token: $TOKEN" \
  -d '{
        "id": 17,
        "approved_amount": 5000,
        "cost_center": "AI/Aurora",
        "supporting_documents": [
          {"filename": "invoice.pdf", "data_b64": "JVBERi0xLjQK...", "mimetype": "application/pdf"}
        ]
      }'

# Dashboard KPIs (portfolio-wide)
curl -X POST "$BASE/api/v1/etp_projects/dashboard/kpis" \
  -H "Content-Type: application/json" \
  -H "access_token: $TOKEN" \
  -d '{}'

# Funded-vs-Remaining line (single project, 60-day window)
curl -X POST "$BASE/api/v1/etp_projects/dashboard/budget_timeline" \
  -H "Content-Type: application/json" \
  -H "access_token: $TOKEN" \
  -d '{"project_id": 10, "graph_days": 60}'

# Daily burn for two projects in May
curl -X POST "$BASE/api/v1/etp_projects/dashboard/daily_burn" \
  -H "Content-Type: application/json" \
  -H "access_token: $TOKEN" \
  -d '{"project_ids":[10,11], "start": "2026-05-01", "end": "2026-05-31"}'

# Spend bridge: May vs June month-to-date
curl -X POST "$BASE/api/v1/etp_projects/dashboard/spend_bridge" \
  -H "Content-Type: application/json" \
  -H "access_token: $TOKEN" \
  -d '{
        "period_a_start": "2026-05-01", "period_a_end": "2026-05-31",
        "period_b_start": "2026-06-01", "period_b_end": "2026-06-11"
      }'

# Portfolio drilldown with per-model breakdown
curl -X POST "$BASE/api/v1/etp_projects/dashboard/portfolio_drilldown" \
  -H "Content-Type: application/json" \
  -H "access_token: $TOKEN" \
  -d '{}'

# Budget Consolidation (KPIs + bars + table) for two projects, attention threshold 90
curl -X POST "$BASE/api/v1/etp_projects/budget_consolidation" \
  -H "Content-Type: application/json" \
  -H "access_token: $TOKEN" \
  -d '{"project_ids":[10,11], "needs_attention_threshold_pct": 90}'
```
