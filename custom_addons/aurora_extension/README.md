# Aurora Extension

REST API that powers the Flutter **Aurora / Milo-Bench** showcase page
(`lib/features/rl_dashboard/.../aurora_showcase_page.dart`), replacing its
hardcoded static data with live + admin-editable backend data.

Follows the same conventions as `talos_extension` / `crowley_extension`:
routes under `/api/v1/aurora_ext/...`, `auth="none"` + `@validate_token`,
and the `api_auth_gateway` `return_Response` envelope.

## Auth & envelope

- Header: `access_token: <token>` (validated by `@validate_token`).
- Caller must be in `aurora.group_aurora_user`.
- Response body (all endpoints):

```json
{
  "message": "OK",
  "errors": [],
  "status_code": 200,
  "<payload_key>": { ... }
}
```

`<payload_key>` is `overview`, `instances`, or `content`.

## Endpoints

### `GET /api/v1/aurora_ext/dashboard_overview`
Live KPIs + status distribution, computed from `aurora.evaluation.instance`.

```json
{
  "overview": {
    "kpi": { "count": "4", "items": [
      {"key": "total_instances", "label": "Total Instances", "value": 120, "sub_string": "3 evaluation run(s)"},
      {"key": "resolved", "label": "Resolved", "value": "44/120", "sub_string": "36.67% resolve rate"}
    ]},
    "quality_tiers": { "label": "Instances by Status", "total": 120, "count": "4",
      "items": [ {"label": "Resolved", "count": 44, "percentage": "36.67% of instances", "fill_percent": 36.67} ] }
  }
}
```

### `GET /api/v1/aurora_ext/instances`
Paginated benchmark instances. **Same dynamic-table contract as
`talos_ext/task_view_dashboard`** — `role`, `columns` (`[{key,label,type}]`),
`rows`, `pagination`. Selection fields are emitted as raw value + `_label`.

**Query params:** `search`, `status` (csv: pending|building|built|running|resolved|unresolved|error),
`resolved` (true/false), `evaluation_id` (int), `sort_by`
(created_date|updated_date|instance_id|status), `sort_order` (asc|desc),
`page` (default 1), `limit` (≤200, default 20).

```json
{
  "role": "admin",
  "columns": [
    {"key": "instance_id", "label": "Instance", "type": "string"},
    {"key": "repo", "label": "Repository", "type": "string"},
    {"key": "pr_range", "label": "PR Range", "type": "string"},
    {"key": "status_label", "label": "Status", "type": "string"},
    {"key": "resolved", "label": "Resolved", "type": "boolean"},
    {"key": "f2p_count", "label": "F2P", "type": "integer"},
    {"key": "p2p_count", "label": "P2P", "type": "integer"},
    {"key": "s2p_count", "label": "S2P", "type": "integer"},
    {"key": "n2p_count", "label": "N2P", "type": "integer"},
    {"key": "updated_at", "label": "Updated", "type": "datetime"}
  ],
  "rows": [{
    "id": 91, "instance_id": "AMReX-Codes__amrex-4238",
    "org": "AMReX-Codes", "repo": "amrex",
    "repo_url": "https://github.com/AMReX-Codes/amrex",
    "pr_numbers": ["4238","4242"],
    "pr_urls": ["https://github.com/AMReX-Codes/amrex/pull/4238"],
    "tag_start": "...", "tag_end": "...", "pr_range": "...",
    "language": "", "category": "",
    "status": "resolved", "status_label": "Resolved", "resolved": true,
    "f2p_count": 3, "p2p_count": 120, "s2p_count": 0, "n2p_count": 0,
    "error_message": "", "created_at": "2026-...", "updated_at": "2026-..."
  }],
  "pagination": {"total_records": 120, "page": 1, "limit": 20, "total_pages": 6}
}
```

> The `dashboard_overview` endpoint above uses the KPI-card style
> (`{key, label, value, sub_string}` items) — that mirrors
> `talos_ext/dashboard_overview` / `crowley_ext/dashboard_overview`, the
> sibling overview endpoints, not the table contract.

### `GET /api/v1/aurora_ext/content`
Seeded, admin-editable descriptive content (from `aurora.showcase.*` models).

```json
{
  "content": {
    "resources":   [{"label":"GitHub","title":"Harness","url":"..."}],
    "methodologies":[{"principle_number":1,"title":"...","bullets":["..."]}],
    "pipeline_steps":[{"phase":1,"title":"...","bullets":["..."]}],
    "model_stats": [{"key":"kimi","name":"Kimi K2.5","overall_pass_rate":"36.4%",
                     "short_trajectories":"49.7%","long_trajectories":"23.1%",
                     "avg_cost_per_instance":"~$0.06"}],
    "pr_ranges":   ["2-5","6-10","11-20","21-40","41-100"]
  }
}
```

## Known data gaps (intentionally not fabricated)

The original Flutter page showed **paper benchmark** numbers that the backend
does **not** track per instance. These are surfaced as empty / via seeded
content, never invented:

| Frontend field | Status |
|---|---|
| `language`, `category` | returned as `""` (not stored on `aurora.evaluation.instance`). |
| per-model `glm5Run`/`kimiRun` run1/2/3, pass@3, trajectory | not stored in Odoo; the instances endpoint returns real test counts (F2P/P2P/S2P/N2P) + resolved status instead. |
| `avgFilesModified`, `avgToolCalls`, `avgTurns`, `estimatedTime` | trajectory metrics, not stored. |
| Kimi/GLM-5 summary cards, methodology, pipeline, resources | served from seeded `aurora.showcase.*` (editable in Odoo). |

To make the per-model / trajectory fields live later, persist them on a new
model (e.g. `aurora.instance.model.run`) and extend the instances serializer.

## Frontend integration

Add to `ApiConstants`:

```dart
static const String auroraOverview   = '/v1/aurora_ext/dashboard_overview';
static const String auroraInstances  = '/v1/aurora_ext/instances';
static const String auroraContent    = '/v1/aurora_ext/content';
```

Then add `data/` + `domain/` layers under the `rl_dashboard` feature and
replace the static `aurora_data.dart` lists with the bloc-loaded entities.
