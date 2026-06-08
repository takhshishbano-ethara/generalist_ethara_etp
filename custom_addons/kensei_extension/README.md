# Kensei Extension

Thin REST add-on that exposes role-scoped analytics dashboards for
`kensei2.kensei2` tasks. Mirrors the shape of `gohan_extension` so the same
Flutter dashboard machinery can target Kensei.

## Module shape

```
kensei_extension/
├── __init__.py
├── __manifest__.py
├── models/
│   ├── __init__.py
│   └── kensei2_task.py            _inherit "kensei2.kensei2"
├── controllers/
│   ├── __init__.py
│   ├── analytics_dashboard.py     role scope + shared helpers + main analytics route
│   ├── dashboard_overview.py      project-scoped KPI overview
│   ├── task_view_dashboard.py     paginated task list with filters and columns
│   └── reference_data.py          domains, personas, taskers reference lists
└── security/
    └── ir.model.access.csv        header only (no new models)
```

`kensei2` remains the full application (models, views, security, controllers
for chat / sandbox / SSE). `kensei_extension` is the thin REST layer on top,
following the same `<core>` / `<core>_extension` split as `gohan` / `gohan_extension`.

## Endpoints

All routes use `type='http', auth='none', methods=['GET'], csrf=False, cors='*'`
and are gated by `@validate_token` from `api_auth_gateway`. Response envelope
is produced by `return_Response`.

| Route | Purpose |
|---|---|
| `GET /api/v1/kensei_ext/analytics_dashboard` | Role-scoped analytics: status chart, completion heatmap, QC verdict distribution, QC team leaderboard, completion timeline, total task trend, not-submitted trend, team overview, team members |
| `GET /api/v1/kensei_ext/dashboard_overview` | Project-scoped KPI overview: totals, status chart, submission trend, team breakdown |
| `GET /api/v1/kensei_ext/task_view_dashboard` | Paginated kensei2 task list with status / qc_status / task_type / difficulty / l1_classification / persona_id / tasker_id / search filters, sort and pagination, plus column metadata |
| `GET /api/v1/kensei_ext/domains` | `kensei2.domain` reference list with parent_id / search / pagination |
| `GET /api/v1/kensei_ext/personas` | `kensei2.persona` reference list with active / search / pagination |
| `GET /api/v1/kensei_ext/taskers` | `res.users` reference list scoped to taskers under the caller's projects |

## Role scoping

`_user_role_tag(env)` maps `env.user.user_role` to one of `full | pl | qr | tasker`
based on the api_auth_gateway role xmlids. `_scope(env)` returns
`(tag, domain, projects)` where `domain` is applied to `kensei2.kensei2` searches
and `projects` is the `project.project` recordset visible to the caller:

- **full** (`role_cto_technical`, `role_tpm_technical`): all tasks, all projects.
- **pl**: tasks owned by taskers of projects where the caller's `hr.employee`
  is in `project_lead`.
- **qr**: same shape but matched on `project_qc_reviewer`.
- **tasker**: only the caller's own tasks; projects where the caller's employee
  is in `project_tasker`.

Callers without any of the above roles receive `403`.

## Field mapping vs gohan

`kensei2.kensei2` does not carry score, duration or URL fields, so the
following gohan analytics blocks are intentionally omitted:

| gohan block | Status here |
|---|---|
| `aht_overview`, `average_duration_analytics` | dropped (no duration field) |
| `url_overview`, `url_analytics` | dropped (no url field) |
| `average_score_analytics`, `quality_analytics` | dropped (no score field) |
| `failed_task_analytics` | replaced by `not_submitted_task_analytics` using `task_status='NotSubmitted'` |

Field equivalences applied across all controllers:

| gohan.job | kensei2.kensei2 |
|---|---|
| `state` (done / submitted / ...) | `task_status` (`Submitted`, `NotSubmitted`) |
| `qc_verdict` (shippable / fixes / not_shippable) | `qc_status` (`pending`, `passed`, `failed`) |
| `completed_at` | `batch_completed_at` |
| `category_id` | `l1_classification` (m2o `kensei2.domain`) |
| `user_id` | `user_id` (related from `employee_id.user_id`) |
| `gohan.category` | `kensei2.domain` |

`APPROVED_VERDICTS = ('passed',)`, `REWORK_VERDICTS = ('failed',)`,
`DECIDED_VERDICTS = ('passed', 'failed')`. `pending` is excluded from
approval / rework percentages.

## Model inherit

`models/kensei2_task.py` adds two methods to `kensei2.kensei2`:

- `_performance_scope_domain()` — role-aware Odoo domain reused by callers
  that want the same scoping as the REST layer.
- `get_performance_metrics()` — returns `{total_task_count, task_done,
  approval_percentage, rework_percentage, approved_count, rework_count,
  qc_reviewed_count}` for the caller's scope.

## Dependencies

`base`, `web`, `kensei2`, `api_auth_gateway`, `task_forge_bridge`,
`project_extension`. `task_forge_bridge` and `project_extension` are listed
so `project.project` field extensions (`project_lead`, `project_qc_reviewer`,
`project_tasker`, `project_aire`, `project_swe`) are guaranteed available
when this module loads.
