# Talos Extension

JSON API surface for the Talos dashboard, mirroring the Crowley Extension
response shape on top of Talos's own models (`talos.talos`, `talos.persona`,
`talos.turn`).

## Endpoints

All endpoints live under `/api/v1/talos_ext/` and use `type="http"`,
`auth="none"` with `@validate_token` from `api_auth_gateway`.

| Method | Path                                          | Purpose                                          |
|--------|-----------------------------------------------|--------------------------------------------------|
| GET    | `/api/v1/talos_ext/dashboard_overview`        | KPI cards + task progress + approvals per week   |
| GET    | `/api/v1/talos_ext/task_view_dashboard`       | Paginated list of `talos.talos` records          |
| GET    | `/api/v1/talos_ext/team_overview`             | Team size + role breakdown across talos members  |

## Response shape

The JSON keys returned by each endpoint intentionally mirror
`crowley_extension` so downstream dashboards can reuse the same component
contracts. Talos-specific values are derived from `task_status`,
`qc_status`, and persona / role assignments.

## Role scoping

- `talos.group_talos_admin` or `etp_user_roles.group_quality_lead` -> full
- `etp_user_roles.group_project_lead` -> pl (currently full scope)
- `etp_user_roles.group_tasker` -> tasker (only own records)
- Anyone else -> 403
