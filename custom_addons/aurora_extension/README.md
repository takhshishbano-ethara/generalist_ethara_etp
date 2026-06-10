# Aurora Extension

REST API that powers the **Aurora / Milo-Bench** project dashboard in the
Flutter app. It exposes the Aurora benchmark as a standard ETP **internal
project** so the existing project-detail machinery (Overview / Analytics /
Tasks tabs) renders it with no bespoke frontend code.

All data is computed **live from `aurora.evaluation.instance`** — this module
defines no models of its own. The response contracts mirror
`crowley_sourcing_extension` exactly (so the same `InternalOverviewTab` /
`InternalAnalyticsTab` / task-view widgets work), and every endpoint uses the
`api_auth_gateway` `return_Response` envelope behind `@validate_token`.

## Auth & envelope

- Header: `access-token: <token>` (validated by `@validate_token`).
- Caller must be in `aurora.group_aurora_user` (else `403`). `role` is
  `"admin"` if the caller is in `aurora.group_aurora_admin`, else `"user"`.
- Success/error body always carries `message`, `errors`, `status_code`:

```json
{ "message": "OK", "errors": [], "status_code": 200, ...payload }
```

- Validation errors return `400`; an unexpected server error returns the same
  JSON envelope with `status_code: 500` and `errors: [<detail>]` (never an
  HTML error page).

## The Aurora project (auto-created)

`data/aurora_project_data.xml` creates, on install, a `project.project` named
**Aurora** classified `internal`, plus `etp.external.project.api.map` rows
mapping its tabs to the endpoints below. The records are `noupdate="1"` (team
edits — name, member assignments — survive upgrades). Internal projects are
managed from the base Project app; assign team members there so the project
shows for non-admin users.

| Tab | Endpoint |
|---|---|
| Overview | `/v1/aurora_ext/dashboard_overview` |
| Analytics | `/v1/aurora_ext/analytics_dashboard` |
| Tasks | `/v1/aurora_ext/instances` |
| Logs | shared `/v1/get_notification_grouped` (not in this module) |

## Endpoints

All are `GET`, `auth="none"`, `@validate_token`, `cors="*"`.

### `GET /api/v1/aurora_ext/dashboard_overview`
Optional query params: `start_date`, `end_date` (`YYYY-MM-DD`), `month`
(`YYYY-MM`). Aurora is not row-scoped — all Aurora users see every instance;
the params only filter `create_date`. Returns a single `overview` wrapper with
every section key always present (crowley_sourcing parity); sections Aurora has
no source for are `{}`.

```json
{
  "overview": {
    "role": "admin",
    "kpi": {"count": 4, "items": [
      {"key": "total_instances", "label": "Total Instances", "value": "120", "sub_string": "3 evaluation run(s)", "pattern": "", "sign": ""},
      {"key": "resolved", "label": "Resolved", "value": "44/120", "sub_string": "36.67% resolve rate", ...},
      {"key": "in_progress", "label": "In Progress", "value": "12", "sub_string": "2 errored", ...},
      {"key": "repos_covered", "label": "Repos Covered", "value": "7", "sub_string": "distinct org/repo pairs", ...}
    ]},
    "task_progress": {"label": "Stage Funnel", "total": 120, "count": 3,
      "items": [{"key": "pending|in_progress|resolved", "label": "...", "value": 0, "percentage": 0.0}],
      "conversion_pct": 36.67, "rejected_rework": 4},
    "recent_activity": {"label": "Recent Activity", "count": "8", "items": [
      {"actor_id": 2, "actor_name": "...", "actor_initials": "AB",
       "action": "resolved|unresolved|failed|processing|updated",
       "task_code": "...", "timestamp": "2026-...", "time_ago": "2h ago"}]},
    "budget": {}, "burn_rate": {}, "accepted_per_day": {}, "approved_per_week": {},
    "coordination_events": {}, "tasks_done_chart": {}, "burned_amount_chart": {}, "my_activity": {}
  }
}
```

### `GET /api/v1/aurora_ext/analytics_dashboard`
Optional query params: `range` (`7d` | `30d` (default) | `90d`) **or**
`start_date` + `end_date`. Scope filters `create_date` to the range.

```json
{
  "role": "admin",
  "kpi": {"count": 4, "items": [
    {"key": "resolve_rate", "label": "Resolve Rate", "value": "36.67%", ...},
    {"key": "total_instances", ...}, {"key": "resolved", ...}, {"key": "errored", ...}
  ]},
  "spend_by_category": {"title": "Resolution Mix", "type": "stacked_bar", "total": 120,
    "items": [{"key": "resolved|unresolved|error", "label": "...", "value": "44 (37%)",
               "amount": 44, "percentage": 36.67, "color_token": "success|danger|warn"}]},
  "daily_burn_rate": {"title": "Instances Created per Day", "type": "bar", "headline": "120",
    "headline_caption": "instances in range", "legend": [...],
    "data": [{"date": "2026-...", "total": 4.0, "segments": [4.0]}]},
  "qc_pass_rate_by_ql": {}, "tasks_submitted_per_day": {}, "qc_verdict_mix": {}, "qc_verdicts_per_day": {}
}
```

> The Flutter `InternalAnalyticsTab` only renders `kpi` / `spend_by_category` /
> `qc_pass_rate_by_ql` / `daily_burn_rate`, so Aurora's charts live under those
> keys (resolution mix → `spend_by_category`, instances/day → `daily_burn_rate`).
> The remaining crowley keys are kept (empty) for schema parity.

### `GET /api/v1/aurora_ext/instances`
Paginated, filterable, sortable benchmark instances — flat `role` / `columns` /
`rows` / `total_records` / `page` / `limit` (crowley_sourcing task-view
contract). Message: `"Task view fetched successfully."`

**Query params:** `search` (instance_id/org/repo), `status` (csv:
pending|building|built|running|resolved|unresolved|error), `resolved`
(true/false), `evaluation_id` (int), `sort_by`
(created_date|updated_date|instance_id|status), `sort_order` (asc|desc),
`page` (default 1), `limit` (≤200, default 20). Unknown `sort_by` → `400`.

```json
{
  "role": "admin",
  "columns": [{"key": "instance_id", "label": "Instance", "type": "string"}, ...],
  "rows": [{
    "id": 91, "instance_id": "AMReX-Codes__amrex-4238", "org": "AMReX-Codes",
    "repo": "amrex", "repo_url": "https://github.com/AMReX-Codes/amrex",
    "pr_numbers": ["4238"], "pr_urls": ["...pull/4238"], "pr_range": "v1..v2",
    "status": "resolved", "status_label": "Resolved", "resolved": true,
    "f2p_count": 3, "p2p_count": 120, "s2p_count": 0, "n2p_count": 0,
    "language": "", "category": "", "error_message": "",
    "created_at": "2026-...", "updated_at": "2026-..."
  }],
  "total_records": 120, "page": 1, "limit": 20
}
```

## Known data gaps (intentionally not fabricated)

The original Flutter page showed paper-benchmark numbers the backend does not
track per instance. They are surfaced as empty, never invented:

| Field | Status |
|---|---|
| `language`, `category` | `""` (not stored on `aurora.evaluation.instance`). |
| per-model run1/2/3, pass@3, trajectory metrics | not stored; the instances endpoint returns real F2P/P2P/S2P/N2P + resolved status instead. |
| cost / QC / reviewer / per-week sections | returned as `{}` (Aurora records no cost/review data). |

## Tests

`tests/` (run with `--test-tags /aurora_extension`):

- `test_units.py` — pure helpers (coercion, pct, ranges, date filters, sort/
  domain builders).
- `test_builders.py` — record-backed section builders + the instances serializer.
- `test_endpoints.py` — end-to-end HTTP per endpoint: contracts, filters,
  param validation, auth/role gating, and the JSON 500 envelope.

```bash
cd ethara-etp
venv/bin/python src/odoo-bin -c odoo.conf -d ethara_dev -u aurora_extension \
  --test-enable --test-tags /aurora_extension --http-port 8169 \
  --max-cron-threads 0 --stop-after-init
```
