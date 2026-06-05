# Crowley Extension

JSON API surface for the ETP dashboard.

## Endpoints

All endpoints live under `/crowley_extension/api/v1/` and use
`type="json"` with `auth="user"` unless noted.

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/crowley_extension/api/v1/dashboard/summary` | Aggregate counts/costs for the ETP overview cards |
| POST | `/crowley_extension/api/v1/generations` | Paginated list of `crowley.generation` records |
| POST | `/crowley_extension/api/v1/generations/<int:gen_id>` | Single generation detail with attempts |
| POST | `/crowley_extension/api/v1/attempts` | Paginated list of `crowley.attempt` records |
| POST | `/crowley_extension/api/v1/costs/timeseries` | Cost timeseries for charts |
| POST | `/crowley_extension/api/v1/live/status` | Current live generation status |

Endpoints are skeletons — fill in the model queries as the dashboard
requirements firm up.
