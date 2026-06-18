# Budget Consolidation — REST API

Five endpoints that together power the **AWS Budget Consolidation** screen: list the consolidated budget table, refresh raw AWS cost data, read a status-only snapshot without hitting AWS, pull the dashboard roll-up payload, and export the table to an Excel workbook uploaded to S3.

**Base URL:** `<host>/api/v1/etp_projects`

**Common headers**

| Header | Value |
|---|---|
| `Content-Type` | `application/json` |
| `access_token` | `<your api access token>` |

> `aws_cost/update_all` is currently published with `auth='none'` and **no** `access_token` validation (it is invoked by an internal scheduler/UI button). The other three endpoints are protected by `@validate_token` from `api_auth_gateway` and require a valid `access_token` header.

**Common JSON envelope** (used by every endpoint, including the Excel export)

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
| `400` | Validation error / `UserError` raised by model / unexpected exception |
| `401` | Missing or expired `access_token` (validated endpoints only) |

**Common filter body** (used by `list`, `update_all`, `status_all`, and `export`)

| Field | Type | Default | Notes |
|---|---|---|---|
| `include_inactive` | bool | `false` | When `false`, only `active=True` budgets are processed |
| `budget_ids` | list[int] | — | Filter to specific `etp.project.aws.budget` ids |
| `project_ids` | list[int] | — | Filter to budgets of specific `project.project` ids |

---

## 1. List AWS Budgets

`POST /aws_budget/list`

Returns the per-budget rows that feed the consolidation table (project, allocated budget, consumed, remaining, % consumed, burn rate, runway).

**Body:** common filter body (all optional).

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
      "currency_symbol": "\u20b9",
      "daily_burn_rate": 1500.0,
      "runway_days": 35,
      "runway_days_exact": 35.0,
      "runway_depletes_on": "2026-07-16"
    }
  ]
}
```

Notes:

- `seq` is the budget record's own `name` field.
- `project_budget` is the initial allocation; auto-incremented when a Token Purchase Request is completed.
- `final_budget` = `budget_amount` (the "Final Project Budget" used by all consumed / remaining math).
- `daily_burn_rate`, `runway_days`, `runway_days_exact`, `runway_depletes_on` come from `_budget_snapshot()` over a trailing 14-day window of real `etp.project.aws.cost.line` rows. They are `0` / `""` when there is no consumption.
- Records sorted by `project_id, name`.

---

## 2. Refresh AWS Cost

`POST /aws_cost/update_all`

Triggers a fresh fetch from AWS Cost Explorer for the matched budgets and re-evaluates threshold alerts (75 / 90 / 100%). Per-budget results are returned in the response — partial failures do **not** fail the call.

> Auth: currently `auth='none'`. Add `access_token` validation if you expose this endpoint publicly.

**Body:** common filter body (all optional).

**Response `data`**

```json
{
  "total_budgets": 5,
  "success_count": 3,
  "error_count": 2,
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
    },
    {
      "budget_id": 5,
      "budget_name": "Fenrir",
      "project_id": 14,
      "project_name": "Fenrir",
      "tag_key": "Project",
      "tag_value": "fenrir",
      "status": "error",
      "error": "Set AWS Access Key ID, Secret, and Region first.",
      "created": 0,
      "updated": 0,
      "budget_amount": 25000.0,
      "total_consumed": 0.0,
      "remaining": 25000.0,
      "percent_consumed": 0.0,
      "daily_burn_rate": 0.0,
      "last_fetched_at": ""
    }
  ]
}
```

Notes:

- Every row — success or error — carries the persisted budget snapshot (`budget_amount`, `total_consumed`, `remaining`, `percent_consumed`, `daily_burn_rate`, `last_fetched_at`). This lets the consolidation UI render the full table even when some budgets failed to refresh.
- Threshold alerts (75 / 90 / 100%) run after a successful fetch but are wrapped in their own `try/except` — an alert-side failure is logged via `_logger.exception` and **does not** flip the row to `error`.
- `error` reasons typically come from `etp.project.aws.budget._fetch_cost_one()`:
  - `Set AWS Access Key ID, Secret, and Region first.`
  - `Set Tag Key and Tag Value first.`
  - `Python package 'boto3' is not installed.`
  - `AWS Cost Explorer failed: <upstream message>`

---

## 2.5. Read-only Status Snapshot

`POST /aws_cost/status_all`

Returns the **same row shape as §2 `aws_cost/update_all`** but **without** calling AWS Cost Explorer and **without** firing threshold alerts. Every row reflects the current DB state of the budget record (computed from already-stored `etp.project.aws.cost.line` rows). Cheap to call — ideal for dashboard polling, UI refreshes, and any read-only consumer that just needs the latest persisted snapshot.

> Auth: `@validate_token`.

**Body:** common filter body (all optional).

**Response `data`** — identical structure to §2, with these fixed values:

- Every row carries `status: "success"`, `created: 0`, `updated: 0`.
- Top-level counters: `success_count == total_budgets`, `error_count == 0`, `total_created == 0`, `total_updated == 0`.
- `last_fetched_at` reflects the budget's persisted timestamp (may be `""` if the budget has never been fetched).

```json
{
  "total_budgets": 5,
  "success_count": 5,
  "error_count": 0,
  "total_created": 0,
  "total_updated": 0,
  "results": [
    {
      "budget_id": 1,
      "budget_name": "AWS-2025-MAIN",
      "project_id": 10,
      "project_name": "Aurora Platform",
      "tag_key": "Project",
      "tag_value": "aurora",
      "status": "success",
      "created": 0,
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

Notes:

- This endpoint **never** talks to AWS — no boto3 / Cost Explorer call. It is safe to poll.
- Threshold alert mail (75 / 90 / 100%) is **not** evaluated here. Use §2 `update_all` (or the scheduler) when you need a real refresh + alerts.
- If no budgets match the filter, the response is still `200` with `total_budgets: 0` and an empty `results` array.

---

## 3. Budget Consolidation Dashboard

`POST /budget_consolidation`

Single roll-up that returns the KPIs, two bar series, and the per-project budget table (with nested per-model breakdown) needed by the Budget Consolidation dashboard.

**Body** (all optional)

| Field | Type | Default | Meaning |
|---|---|---|---|
| `project_ids` | list[int] \| omitted | all | Restrict to these projects. Omit (or pass `null`) for portfolio-wide. |
| `include_inactive` | bool | `false` | When `true`, include archived (`active=False`) budgets. |
| `needs_attention_threshold_pct` | number | `80` | A project is counted as "needs attention" when its utilisation (`spend / budget × 100`) is at or above this value. |

**Response payload**

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

- **Per-model attribution methodology.** `etp_projects` does not track per-model consumption directly. For both the `spend_by_model` bar and each project's `models` list, the project's actual AWS consumption (`etp.project.aws.cost.line` total) is distributed across that project's models proportionally to the completed Token Purchase Request `approved_amount` per model. Projects with no completed TPRs (or no `model_name`) attribute their consumption to `"(no model)"`. As a result, `sum(spend_by_model[*].amount) == kpis.total_spend` and `sum(models[*].spend) == budget_by_projects[*].spend`.
- `top_model` (KPI) and `budget_by_projects[*].top_model` are picked by attributed spend (highest amount).
- `share_pct` in `spend_by_model` and `spend_by_project` is over `kpis.total_spend`. Inside `budget_by_projects[*].models`, `share_pct` is over that project's attributed spend.
- `runway_days` per project = `floor((budget - spend) / sum(daily_burn_rate))`. Returns `null` when the project's combined daily burn rate is `0`.
- `utilization_pct` and `percent_consumed` return `0` when their denominator is `0` (no budget recorded).
- `budget_by_projects` is sorted by `utilization_pct` desc; `spend_by_model` and `spend_by_project` are sorted by `amount` desc.
- Currency is taken from the first budget in scope, falling back to `base.INR`.

---

## 4. Export to Excel (.xlsx)

`POST /aws_budget/export`

Builds a styled `.xlsx` workbook of the consolidation table — same row set as `aws_budget/list`, with a KPI block on top and totals on the last row — uploads it to S3 via the shared `generate_s3_link` helper from `api_auth_gateway`, and returns the public download link in the standard JSON envelope. Use this directly from the dashboard's **Export** button.

> Auth: `@validate_token`. Server requires the `xlsxwriter` Python package (declared in the module manifest's `external_dependencies`) and a configured `s3.connector` record used by `generate_s3_link`.

**Body:** common filter body (all optional).

**Success response** (`status_code: 200`)

```json
{
  "message": "AWS budget export generated.",
  "errors": [],
  "status_code": 200,
  "data": {
    "data": {
      "download_url": "https://cdn.example.com/reports/aws_budget_export_20260611_143015.xlsx",
      "filename": "aws_budget_export_20260611_143015.xlsx",
      "size": 18243,
      "total_budgets": 12
    }
  }
}
```

| Field | Type | Notes |
|---|---|---|
| `data.data.download_url` | string | Public CDN/S3 URL of the uploaded workbook (returned by `generate_s3_link`) |
| `data.data.filename` | string | `aws_budget_export_<YYYYMMDD_HHMMSS>.xlsx` |
| `data.data.size` | int | Workbook size in bytes (pre-upload) |
| `data.data.total_budgets` | int | Number of budget rows written to the workbook |

**Error responses**

| Status | Reason |
|---|---|
| `400` | `Python package 'xlsxwriter' is not installed on the server.` |
| `400` | `'budget_ids' must be a list of integers.` |
| `400` | `'project_ids' must be a list of integers.` |
| `400` | `Something went wrong.` (unexpected exception while building the workbook; details in `errors`) |
| `500` | `Failed to upload export to S3.` (`generate_s3_link` raised; details in `errors`) |
| `500` | `S3 upload returned an empty link.` (`generate_s3_link` returned falsy) |

### Workbook layout

Two worksheets: `AWS Budgets` (consolidation table) and `Service Spend` (model/service-wise breakdown).

#### Sheet 1 — `AWS Budgets`

- **Row 1:** merged title banner — `AWS Budget Consolidation`.
- **Rows 3–5: KPI block.**
  - Row 3: `Total Budgets`, `Projects`, `Currency`
  - Row 4: `Total Budget`, `Total Consumed`, `Total Remaining`
  - Row 5: `Overall % Consumed`
- **Row 7: column headers** (frozen, autofilter applied across the data range).

| # | Column | Source | Format |
|---|---|---|---|
| 1 | `#` | row index (1-based) | integer |
| 2 | `Budget Seq` | `etp.project.aws.budget.name` | text |
| 3 | `Project` | `project_id.name` | text |
| 4 | `Currency` | `currency_id.name` | text |
| 5 | `Project Budget` | `project_budget` | number, `#,##0.00` |
| 6 | `Final Budget` | `budget_amount` | number, `#,##0.00` |
| 7 | `Total Used Cost` | `total_consumed` | number, `#,##0.00` |
| 8 | `Remaining` | `remaining` | number, `#,##0.00` |
| 9 | `% Consumed` | `percent_consumed` | number, `0.00"%"` |
| 10 | `Daily Burn Rate` | `_budget_snapshot().daily_burn_rate` | number, `#,##0.00` |
| 11 | `Runway Days` | `_budget_snapshot().runway_days` | integer |
| 12 | `Runway Depletes On` | `_budget_snapshot().runway_depletes_on` | ISO date string |
| 13 | `Tag Key` | `tag_key` | text |
| 14 | `Tag Value` | `tag_value` | text |
| 15 | `Last Fetched At` | `last_fetched_at` (`YYYY-MM-DD HH:MM:SS`) | text |

- **Totals row** (immediately below the last data row): sums of `Project Budget`, `Final Budget`, `Total Used Cost`, `Remaining`, and the overall `% Consumed`.
- Rows sorted by `project_id, name` (same ordering as `aws_budget/list`).
- Window: header row + project/seq columns are frozen, autofilter is enabled across the table.

#### Sheet 2 — `Service Spend`

Model/service-wise spend breakdown aggregated from `etp.project.aws.cost.line` (grouped by `budget_id`, `service_name`, summed across all stored periods).

- **Row 1:** merged title banner — `Service-Wise Spend`.
- **Row 3: KPI block** — `Total Services` (distinct `(budget, service)` pairs in scope), `Top Service` (highest INR spend across the scope), `Top Spend (INR)`.
- **Row 5: column headers** (frozen, autofilter applied across the data range).

| # | Column | Source | Format |
|---|---|---|---|
| 1 | `#` | row index (1-based) | integer |
| 2 | `Project` | `budget_id.project_id.name` | text |
| 3 | `Budget Seq` | `budget_id.name` | text |
| 4 | `Service` | `service_name` (from AWS Cost Explorer `GroupBy=SERVICE`) | text |
| 5 | `Total Cost (USD)` | sum of `amount_source` for that `(budget, service)` | number, `#,##0.00` |
| 6 | `Total Cost (INR)` | sum of `amount_inr` for that `(budget, service)` | number, `#,##0.00` |
| 7 | `% of Budget` | `total_inr / budget_amount * 100` | number, `0.00"%"` |

- **Grand-Total row** (immediately below the last data row): sums of `Total Cost (USD)` and `Total Cost (INR)` across the whole scope.
- Rows sorted by project name, budget seq, then `Total Cost (INR)` **descending** so each budget's top spender lands at the top of its block.
- Window: header row + the first three identifying columns (`#`, `Project`, `Budget Seq`) are frozen, autofilter is enabled across the table.

> "Service" here corresponds to AWS Cost Explorer's `SERVICE` dimension — i.e. things like `Amazon Bedrock`, `Amazon EC2`, `AWS Lambda`. If your project spends on AI models through `Amazon Bedrock` / `Amazon SageMaker`, those line items show up under those service names; AWS does not surface per-model-id costs in the standard `GetCostAndUsage` SERVICE grouping.

---

## Curl examples

```bash
# 1. Refresh AWS Cost Explorer for all active budgets
curl -X POST "$BASE/api/v1/etp_projects/aws_cost/update_all" \
  -H "Content-Type: application/json" \
  -d '{}'

# 2. List the consolidation table for two projects
curl -X POST "$BASE/api/v1/etp_projects/aws_budget/list" \
  -H "Content-Type: application/json" \
  -H "access_token: $TOKEN" \
  -d '{"project_ids":[10,11]}'

# 3. Pull the consolidation dashboard payload with a stricter attention threshold
curl -X POST "$BASE/api/v1/etp_projects/budget_consolidation" \
  -H "Content-Type: application/json" \
  -H "access_token: $TOKEN" \
  -d '{"project_ids":[10,11], "needs_attention_threshold_pct": 90}'

# 4. Generate the Excel export for the same scope and get the S3 download link
curl -X POST "$BASE/api/v1/etp_projects/aws_budget/export" \
  -H "Content-Type: application/json" \
  -H "access_token: $TOKEN" \
  -d '{"project_ids":[10,11]}'
# Response: { "data": { "data": { "download_url": "https://...", "filename": "...", "size": ..., "total_budgets": ... } }, ... }

# 5. Cheap read-only snapshot (no AWS call, no threshold alerts) — same shape as #1
curl -X POST "$BASE/api/v1/etp_projects/aws_cost/status_all" \
  -H "Content-Type: application/json" \
  -H "access_token: $TOKEN" \
  -d '{"project_ids":[10,11]}'
```
