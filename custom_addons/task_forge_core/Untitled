# Founder Overview Dashboard — API Reference

Backing file: `controllers/main_dashboard.py`
Implements the single-page **Founder Overview** dashboard (Figma:
`Ethara-R-D-–-Figma-04-30-2026` + `Ethara-R-D-–-Figma-05-01-2026`).

All endpoints are **read-only** HTTP GET, gated by:

1. `api_auth_gateway.validate_token` — caller must pass a valid `access_token`
   HTTP header.
2. CTO role check — `user.has_group('etp_user_roles.group_cto')`.

Non-CTO callers receive `403 Forbidden`.

---

## Endpoint shape at a glance

| Group        | Endpoints                                                                                        |
|--------------|--------------------------------------------------------------------------------------------------|
| **Cards**    | `/summary` — all 6 KPI cards + header in ONE call                                                |
| **Cards**    | `/qc_feedback_summary` — 2 KPI counts + chip rows for the QC Feedback tab                        |
| **Lists**    | `/tasks_timeseries`, `/active_blockers`, `/project_health`, `/performance_ranking`, `/qc_feedback`, `/performance_ranking_panel`, `/qc_feedback_justification_breakdown`, `/qc_feedback_prompt_breakdown` — each independent with its own filters |

Cards endpoints never carry list-level filters (`search`, `sort`,
`page`, etc.). List endpoints each own their own filter surface so the
frontend can drive each widget independently.

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

Tokens come from the existing `api_auth_gateway` login flow. If the
user behind the token is not in the `CTO` group, the dashboard
endpoints return 403.

---

## Common query parameters (every endpoint)

| Param       | Values                                           | Default | Notes |
|-------------|--------------------------------------------------|---------|-------|
| `range`     | `7d` \| `30d` \| `90d` \| `custom`               | `7d`    | Date window ending today. `/active_blockers` ignores `range` (always shows currently-open). |
| `date_from` | `YYYY-MM-DD`                                     | —       | Required when `range=custom`. |
| `date_to`   | `YYYY-MM-DD`                                     | —       | Required when `range=custom`. |
| `category`  | `stem` \| `non_stem` \| `technical` \| `all`     | `all`   | Maps to `project.project.project_category`. |

If `range=custom` is given without both dates, the endpoint silently
falls back to the last 7 days.

---

## Response envelope

```json
{
  "message": "...",
  "errors": [],
  "status_code": 200,
  "data": { ... endpoint-specific payload ... }
}
```

Error responses:

```json
{
  "message": "Founder dashboard requires CTO role",
  "errors": [],
  "status_code": 403
}
```

Unhandled exceptions are logged server-side with the full stack; the
client only sees a short sanitised message and `status_code: 400`.

---

## Endpoint index

| # | Method | Path                                                             | Powers (Figma)                                     |
|---|--------|------------------------------------------------------------------|----------------------------------------------------|
| 1 | GET    | `/api/v2/taskforge/main/summary`                                 | Header + 6 KPI cards (cards-only, no list filters) |
| 2 | GET    | `/api/v2/taskforge/main/tasks_timeseries`                        | "Tasks Completed" line chart                       |
| 3 | GET    | `/api/v2/taskforge/main/active_blockers`                         | "Active Blockers" right table                      |
| 4 | GET    | `/api/v2/taskforge/main/project_health`                          | "Project Health & AHT" table                       |
| 5 | GET    | `/api/v2/taskforge/main/performance_ranking`                     | "Performance Ranking" tab (legacy top/low split)   |
| 6 | GET    | `/api/v2/taskforge/main/qc_feedback`                             | "QC Feedback" tab — project × quality aggregation  |
| 7 | GET    | `/api/v2/taskforge/main/performance_ranking_panel`               | Performance Ranking **panel** (4 ranked cards)     |
| 8 | GET    | `/api/v2/taskforge/main/qc_feedback_summary`                     | QC Feedback tab — 2 KPIs + chip rows               |
| 9 | GET    | `/api/v2/taskforge/main/qc_feedback_justification_breakdown`     | QC Feedback → Justification tab — per-member table |
| 10| GET    | `/api/v2/taskforge/main/qc_feedback_prompt_breakdown`            | QC Feedback → Prompt tab — per-member table        |

---

## 1. `GET /api/v2/taskforge/main/summary` — KPI cards

Populates the page header + all 6 KPI cards in one call.

**Query params**: common set only (`range`, `date_from`, `date_to`,
`category`). No list-level filters are accepted here.

**Response `data`**:

```json
{
  "header": {
    "label": "Founder overview",
    "total_members": 487
  },
  "live_tasking": {
    "online_now": 3823,
    "total_taskers": 8203,
    "active_projects_count": 12
  },
  "tasks_completed": {
    "today": 159,
    "yesterday": 321,
    "is_live": true
  },
  "active_projects": {
    "count": 159,
    "at_risk": 2
  },
  "open_blockers": {
    "count": 23,
    "overdue": 4
  },
  "total_workforce": {
    "total": 3487,
    "taskers": 3102,
    "qr": 264,
    "pl": 121,
    "active_percent": 100
  },
  "pending_leaves": {
    "total": 89,
    "pl": 2,
    "qr": 4,
    "tasker": 142
  }
}
```

**Field sources**

| Field | Backing data |
|-------|--------------|
| `header.total_members` | `hr.employee` count with `task_forge_active=True` |
| `live_tasking.online_now` | distinct `employee_id` on `task.forge.log` with `state='in_progress'` |
| `live_tasking.total_taskers` | `_workforce_breakdown.taskers` |
| `live_tasking.active_projects_count` | distinct `project_id` on current `in_progress` tasks |
| `tasks_completed.today` / `yesterday` | `task.forge.log.search_count` grouped by `date` |
| `active_projects.count` | `project.project` with `task_forge_status='live'` |
| `active_projects.at_risk` | live projects where `open_blockers>5` OR `overdue_tasks>3` |
| `open_blockers.count` | `task.forge.blocker.state ∈ OPEN_BLOCKER_STATES` |
| `open_blockers.overdue` | above + `create_date <= now - 3 days` |
| `total_workforce` | iterates `task_forge_active=True` employees, bucketed by `_get_task_forge_role()` |
| `pending_leaves` | `hr.leave.state='confirm'`, bucketed by role |

---

## 2. `GET /api/v2/taskforge/main/tasks_timeseries` — Line chart

Daily count of completed tasks. Missing days are filled with `count=0`
so the X-axis is contiguous.

**Own filters** (in addition to the common set):

| Param        | Values        | Default | Purpose |
|--------------|---------------|---------|---------|
| `project_id` | integer       | —       | Restrict chart to one project |

**Response `data`**:

```json
{
  "date_from": "2026-04-24",
  "date_to": "2026-04-30",
  "series": [
    { "date": "2026-04-24", "count": 1402 },
    { "date": "2026-04-25", "count": 1511 }
  ],
  "total": 8332,
  "peak": { "date": "2026-04-28", "count": 1847 }
}
```

One aggregated SQL call via `read_group` on `task.forge.log` with
`groupby=['date:day']`.

---

## 3. `GET /api/v2/taskforge/main/active_blockers` — Blockers table

Paginated list of currently-open blockers.

**Own filters**:

| Param        | Values                              | Default       | Purpose |
|--------------|-------------------------------------|---------------|---------|
| `page`       | integer ≥ 1                         | `1`           | Pagination |
| `limit`      | integer 1–100                       | `5`           | Page size |
| `priority`   | `0` \| `1` \| `2` \| `3`            | —             | Filter by priority |
| `status`     | comma list of blocker states        | OPEN states   | e.g. `status=pending,escalated_to_pl` |
| `project_id` | integer                             | —             | Single-project view |
| `employee_id`| integer                             | —             | Blockers raised by one employee |
| `search`     | string                              | —             | ilike across `name`, `blocker_reason`, `project_id.name` |
| `sort`       | `priority_desc` (default) \| `priority_asc` \| `newest` \| `oldest` \| `days_open_desc` \| `days_open_asc` | `priority_desc` | Safe whitelist |

Valid `status` values: `pending`, `escalated_to_pl`, `escalated_to_cto`,
`ack`, `escalated` (legacy), `resolved`, `validated`, `no_issue`.

**Response `data`**:

```json
{
  "total_open": 23,
  "page": 1,
  "limit": 5,
  "items": [
    {
      "id": 512,
      "priority": "2",
      "priority_label": "High",
      "priority_color": "red",
      "title": "API rate limit exceeded on Multi-Mango",
      "project_id": 7,
      "project_name": "Lambda",
      "status": "pending",
      "status_label": "Raised",
      "status_tone": "danger",
      "days_open": 4,
      "employee_name": "Aisha K."
    }
  ]
}
```

---

## 4. `GET /api/v2/taskforge/main/project_health` — Project Health table

One row per live project with completed / pending / overdue / quality
counts computed via 3 `read_group` calls (not N+1).

**Own filters**:

| Param        | Values                              | Default | Purpose |
|--------------|-------------------------------------|---------|---------|
| `page`       | integer ≥ 1                         | `1`     | Pagination |
| `limit`      | integer 1–200                       | `50`    | Page size |
| `health`     | `healthy` \| `warning` \| `at_risk` | —       | Post-classification filter |
| `project_id` | integer                             | —       | Single-project view |
| `search`     | string                              | —       | ilike on `project.name` |
| `sort`       | see list below                      | `name_asc` | DB sort OR in-memory sort on computed fields |

**Sort values**:
- DB sort (on `project.project`): `name_asc`, `name_desc`, `newest`, `oldest`
- In-memory sort (computed metrics): `completed_desc`/`_asc`, `blockers_desc`/`_asc`, `overdue_desc`/`_asc`, `aht_desc`/`_asc`, `hours_desc`/`_asc`, `quality_desc`/`_asc`, `members_desc`/`_asc`

**Response `data`**:

```json
{
  "date_from": "2026-04-24",
  "date_to": "2026-04-30",
  "total": 42,
  "page": 1,
  "limit": 50,
  "items": [
    {
      "project_id": 7,
      "project_name": "260209-omni-elo-without",
      "category": "Non Stem",
      "category_value": "non_stem",
      "members": 91,
      "completed": 4594,
      "pending": 0,
      "overdue": 4,
      "blockers_open": 1,
      "taskers_avatars": [
        { "id": 14, "name": "Aisha K." }
      ],
      "quality_percent": 0,
      "aht_min": 6.6,
      "hours_total": 460.4,
      "health": "healthy"
    }
  ]
}
```

**Health classification**:

```
blockers_open > 5  OR overdue > 3  -> at_risk
blockers_open > 2  OR overdue > 1  -> warning
else                                -> healthy
```

---

## 5. `GET /api/v2/taskforge/main/performance_ranking` — Ranking tab (legacy split)

Ranks every employee who has at least 1 task in the selected range.
Zero-task employees are excluded entirely (fix for a legacy bug that
mis-flagged inactive employees as low performers).

**Own filters**:

| Param           | Values                    | Default | Purpose |
|-----------------|---------------------------|---------|---------|
| `top_n`         | integer 1–50              | `10`    | Size of `top_taskers` list |
| `low_threshold` | float (percent)           | `50`    | Cutoff for `low_performers` list |
| `project_id`    | integer                   | —       | Restrict to one project |
| `employee_id`   | integer                   | —       | Restrict to one employee |
| `search`        | string                    | —       | ilike on employee name |
| `sort`          | see list below            | `productivity_desc` | In-memory sort |

**Sort values**: `productivity_desc`/`_asc`, `total_desc`/`_asc`,
`completed_desc`/`_asc`, `minutes_desc`/`_asc`.

**Response `data`**:

```json
{
  "date_from": "2026-04-01",
  "date_to": "2026-04-30",
  "top_taskers": [
    {
      "employee_id": 104,
      "employee_name": "Aisha K.",
      "total_tasks": 182,
      "completed": 181,
      "total_minutes": 1084,
      "productivity": 99.5
    }
  ],
  "low_performers": [
    {
      "employee_id": 221,
      "employee_name": "Dev L.",
      "total_tasks": 40,
      "completed": 12,
      "total_minutes": 110,
      "productivity": 30.0
    }
  ],
  "total_evaluated": 187
}
```

`productivity = round(completed / total_tasks * 100, 1)`.

---

## 6. `GET /api/v2/taskforge/main/qc_feedback` — QC tab (project × quality)

Per-project quality aggregation: average `quality_score` on completed
tasks plus count of QR `no_issue` resolutions.

**Own filters**:

| Param        | Values              | Default | Purpose |
|--------------|---------------------|---------|---------|
| `page`       | integer ≥ 1         | `1`     | Pagination |
| `limit`      | integer 1–200       | `50`    | Page size |
| `project_id` | integer             | —       | Single-project view |
| `search`     | string              | —       | ilike on `project.name` |
| `sort`       | see list below      | `quality_desc` | In-memory sort |

**Sort values**: `quality_desc`/`_asc`, `scored_desc`/`_asc`,
`no_issue_desc`/`_asc`, `name_asc`/`name_desc`.

**Response `data`**:

```json
{
  "date_from": "2026-04-01",
  "date_to": "2026-04-30",
  "overall_avg_quality": 8.3,
  "total_tasks_scored": 1204,
  "total": 42,
  "page": 1,
  "limit": 50,
  "items": [
    {
      "project_id": 7,
      "project_name": "260209-omni-elo-without",
      "tasks_scored": 412,
      "avg_quality": 9.1,
      "qr_no_issue_count": 6
    }
  ]
}
```

Only tasks with `state='completed'` AND `quality_score > 0` are
counted. `qr_no_issue_count` uses `task.forge.blocker` rows where
`state='no_issue'` and `qr_action_at` falls inside the range.

---

## 7. `GET /api/v2/taskforge/main/performance_ranking_panel` — Ranking panel (4 cards)

Powers the four ranked tables on the Performance Ranking tab (Figma
05-01-2026): **Top Project Leads**, **Top Quality Reviewers**, **Top
Performing Taskers**, **Improvement Needed**.

Ranks every employee who has at least 1 task in the selected range.
The Improvement Needed list additionally requires `total ≥ min_tasks`
so zero-task / very-low-activity employees don't pollute the bottom
bucket.

**Own filters**:

| Param         | Values              | Default | Purpose |
|---------------|---------------------|---------|---------|
| `top_n`       | integer 1–50        | `5`     | Size of each "Top …" card |
| `bottom_n`    | integer 1–50        | `5`     | Size of the "Improvement Needed" card |
| `min_tasks`   | integer ≥ 0         | `5`     | Floor to qualify for Improvement Needed |

**Response `data`**:

```json
{
  "date_from": "2026-04-01",
  "date_to": "2026-04-30",
  "range": "30d",
  "category": "all",
  "top_project_leads": [
    {
      "rank": 1,
      "employee_id": 501,
      "employee_name": "Arjun Mehta",
      "completed": 4812,
      "total": 5100,
      "percentage": 94.4
    }
  ],
  "top_quality_reviewers": [
    {
      "rank": 1,
      "employee_id": 802,
      "employee_name": "Sneha Kulkarni",
      "taskers": 42,
      "avg_quality": 94.2,
      "qr_score": 94.2
    }
  ],
  "top_taskers": [
    {
      "rank": 1,
      "employee_id": 104,
      "employee_name": "Neha Gupta",
      "completed": 312,
      "worked": 320,
      "percentage": 97.5
    }
  ],
  "improvement_needed": [
    {
      "rank": 1,
      "employee_id": 998,
      "employee_name": "Ravi Tiwari",
      "completed": 12,
      "worked": 89,
      "percentage": 13.5
    }
  ]
}
```

**Field sources**

| Figma column | Backing data |
|--------------|--------------|
| Project Leads → name | `hr.employee.name` found via `project.project.project_lead` M2M on live projects |
| Project Leads → COMPLETED / TOTAL | `task.forge.log.read_group` on PL's projects (`state='completed'` vs all) |
| Project Leads → PERCENTAGE COMPLETION | `round(completed / total * 100, 1)` |
| Quality Reviewers → name | employees where `_get_task_forge_role() == 'qr'` |
| Quality Reviewers → TASKERS | `hr.employee` count where `task_forge_qr_id == qr.id` AND `task_forge_active` |
| Quality Reviewers → AVG QUAL | `avg(task.forge.log.quality_score)` over QR's team, `state='completed'`, `quality_score>0` |
| Quality Reviewers → QR SCORE | v1 mirrors `avg_quality`; a weighted formula can be swapped in without changing the response shape |
| Taskers → COMPLETED / WORKED | `read_group` on `task.forge.log.employee_id`: completed count vs all-state count |
| Taskers → PERCENTAGE COMPLETION | `round(completed / worked * 100, 1)` |
| Improvement Needed | same maps as Taskers, filtered to `worked >= min_tasks`, sorted by `percentage asc` |

> `qr_score` is returned as a separate field from `avg_quality` so the
> frontend can keep showing both columns once a weighted formula lands.

---

## 8. `GET /api/v2/taskforge/main/qc_feedback_summary` — QC Feedback cards

Powers the two KPI counters and the chip rows at the top of the QC
Feedback tab (both Justification and Prompt sub-tabs).

**Query params**: common set plus one optional list scope:

| Param        | Values   | Default | Purpose |
|--------------|----------|---------|---------|
| `project_id` | integer  | —       | Restrict counts to one project |

**Response `data`**:

```json
{
  "date_from": "2026-04-01",
  "date_to": "2026-04-30",
  "range": "30d",
  "category": "all",
  "justification_qc_count": 1836,
  "prompt_qc_count": 742,
  "justification_categories": [],
  "prompt_rejection_reasons": [
    { "key": "Vague / Generic", "label": "Vague / Generic", "count": 891 },
    { "key": "Too Short",       "label": "Too Short",       "count": 387 },
    { "key": "Copy-Paste",      "label": "Copy-Paste",      "count": 214 },
    { "key": "Contradictory",   "label": "Contradictory",   "count": 132 },
    { "key": "Grammar",         "label": "Grammar",         "count": 132 }
  ],
  "data_availability": {
    "justification_categories": "pending_persistence",
    "prompt_rejection_reasons": "live"
  }
}
```

**Field sources**

| Field | Backing data |
|-------|--------------|
| `justification_qc_count` | `task.forge.log` count where `justification_text IS NOT NULL` in range |
| `prompt_qc_count` | `task.forge.log` count where `prompt_text IS NOT NULL` in range |
| `prompt_rejection_reasons[]` | `preference.ranking.read_group(['rejection_reason'])` where `submitted_at` in range (and `project_id` if supplied and supported on the model). Ordered by count desc. |
| `justification_categories[]` | **Always empty** until `task_forge_core` persists category data. The `data_availability` flag tells the frontend to render a "Data not yet collected" state. |

Rejection reasons include the new Figma-aligned Selection values
(`Vague / Generic`, `Too Short`, `Copy-Paste`, `Contradictory`,
`Grammar`) added to `preference.ranking.rejection_reason` alongside
the legacy values (`Image Handling`, `Missing Reference Text`,
`Safety Concerns`, …), so historical rows remain queryable.

---

## 9. `GET /api/v2/taskforge/main/qc_feedback_justification_breakdown` — Justification table

Per-project × per-member breakdown for the Justification sub-tab.
"Issues" columns are returned as `null` until grammar_check results
are persisted to the DB — the frontend should render them as `—`.

**Own filters**:

| Param        | Values                           | Default          | Purpose |
|--------------|----------------------------------|------------------|---------|
| `page`       | integer ≥ 1                      | `1`              | Pagination |
| `limit`      | integer 1–200                    | `50`             | Page size |
| `project_id` | integer                          | —                | Single-project view |
| `search`     | string                           | —                | ilike on `hr.employee.name` |
| `sort`       | see list below                   | `qc_tasks_desc`  | Safe whitelist |

**Sort values**: `qc_tasks_desc` (default), `qc_tasks_asc`, `name_asc`,
`name_desc`.

**Response `data`**:

```json
{
  "date_from": "2026-04-01",
  "date_to": "2026-04-30",
  "total": 1836,
  "page": 1,
  "limit": 50,
  "items": [
    {
      "project_id": 7,
      "project_name": "260209-omni-elo-without",
      "employee_id": 1044,
      "member_name": "Mittapalli Ravali",
      "qc_tasks": 31,
      "issues_first_hit": null,
      "total_issues": null,
      "first_hit_pct": null,
      "top_categories": []
    }
  ],
  "data_availability": "partial"
}
```

**Field sources**

| Field | Backing data |
|-------|--------------|
| `project_id` / `project_name` | `task.forge.log.project_id` grouped |
| `employee_id` / `member_name` | `task.forge.log.employee_id` grouped |
| `qc_tasks` | `task.forge.log` count per (project, employee) where `justification_text IS NOT NULL` in range |
| `issues_first_hit` / `total_issues` / `first_hit_pct` / `top_categories` | **Not yet sourced.** Return `null` / `[]`. Will light up once `task_forge_core` persists grammar_check results (see open schema proposal). |

`data_availability: "partial"` is the frontend signal to render the
count columns live and the issue columns as "—".

---

## 10. `GET /api/v2/taskforge/main/qc_feedback_prompt_breakdown` — Prompt table

Per-project × per-member breakdown for the Prompt sub-tab. Backed by
**`preference.ranking`** rows with `submitted_at` in the selected
range. All columns are live data.

**Own filters**:

| Param        | Values                           | Default       | Purpose |
|--------------|----------------------------------|---------------|---------|
| `page`       | integer ≥ 1                      | `1`           | Pagination |
| `limit`      | integer 1–200                    | `50`          | Page size |
| `project_id` | integer                          | —             | Single-project view (only applied when `preference.ranking` exposes `project_id` in the active deployment) |
| `search`     | string                           | —             | ilike on `hr.employee.name` |
| `sort`       | see list below                   | `rej_pct_desc`| Safe whitelist |

**Sort values**: `rej_pct_desc` (default), `rej_pct_asc`,
`prompts_desc`, `prompts_asc`, `name_asc`, `name_desc`.

**Response `data`**:

```json
{
  "date_from": "2026-04-01",
  "date_to": "2026-04-30",
  "total": 742,
  "page": 1,
  "limit": 50,
  "items": [
    {
      "project_id": null,
      "project_name": null,
      "employee_id": 1201,
      "member_name": "Anil Kapoor",
      "prompts": 84,
      "rejected": 52,
      "rej_pct": 61.9,
      "top_reason": { "key": "Vague / Generic", "count": 14 }
    }
  ],
  "data_availability": "live",
  "project_scope_supported": false
}
```

**Field sources**

| Field | Backing data |
|-------|--------------|
| `employee_id` / `member_name` | `preference.ranking.employee_id` grouped |
| `project_id` / `project_name` | `preference.ranking.project_id` when present on the model; `null` otherwise (see `project_scope_supported`) |
| `prompts` | count of `preference.ranking` rows per (project, employee) in range |
| `rejected` | subset where `rejection_reason IS NOT NULL` (or `qc_task_status='fail'`) |
| `rej_pct` | `round(rejected / prompts * 100, 1)` |
| `top_reason` | most-frequent `rejection_reason` per (project, employee), with `count` |

The endpoint auto-probes `preference.ranking._fields` for `project_id`.
If absent, `project_scope_supported` is `false`, per-row `project_id`
returns `null`, and the `project_id` query param is ignored.

---

## cURL examples

Set your token once:

```bash
export BASE="http://localhost:8069"
export TOKEN="REPLACE_WITH_CTO_ACCESS_TOKEN"
```

### 1. Summary (cards)

```bash
curl -sS -H "access_token: $TOKEN" \
  "$BASE/api/v2/taskforge/main/summary" | jq
```

30-day Non-STEM:

```bash
curl -sS -H "access_token: $TOKEN" \
  "$BASE/api/v2/taskforge/main/summary?range=30d&category=non_stem" | jq
```

Custom date range:

```bash
curl -sS -H "access_token: $TOKEN" \
  "$BASE/api/v2/taskforge/main/summary?range=custom&date_from=2026-04-01&date_to=2026-04-30" | jq
```

### 2. Tasks Completed time series

```bash
curl -sS -H "access_token: $TOKEN" \
  "$BASE/api/v2/taskforge/main/tasks_timeseries?range=7d" | jq
```

90-day STEM view:

```bash
curl -sS -H "access_token: $TOKEN" \
  "$BASE/api/v2/taskforge/main/tasks_timeseries?range=90d&category=stem" | jq
```

Single-project chart:

```bash
curl -sS -H "access_token: $TOKEN" \
  "$BASE/api/v2/taskforge/main/tasks_timeseries?range=30d&project_id=7" | jq
```

### 3. Active Blockers

Default (priority desc, 5 per page):

```bash
curl -sS -H "access_token: $TOKEN" \
  "$BASE/api/v2/taskforge/main/active_blockers?limit=5&page=1" | jq
```

Only critical priority:

```bash
curl -sS -H "access_token: $TOKEN" \
  "$BASE/api/v2/taskforge/main/active_blockers?priority=3&limit=20" | jq
```

Only escalated-to-CTO, sorted by oldest:

```bash
curl -sS -H "access_token: $TOKEN" \
  "$BASE/api/v2/taskforge/main/active_blockers?status=escalated_to_cto&sort=oldest" | jq
```

Blockers for one project:

```bash
curl -sS -H "access_token: $TOKEN" \
  "$BASE/api/v2/taskforge/main/active_blockers?project_id=7&limit=50" | jq
```

Free-text search:

```bash
curl -sS -H "access_token: $TOKEN" \
  "$BASE/api/v2/taskforge/main/active_blockers?search=rate%20limit" | jq
```

Blockers raised by one employee:

```bash
curl -sS -H "access_token: $TOKEN" \
  "$BASE/api/v2/taskforge/main/active_blockers?employee_id=104" | jq
```

### 4. Project Health

Default:

```bash
curl -sS -H "access_token: $TOKEN" \
  "$BASE/api/v2/taskforge/main/project_health?range=7d&limit=50" | jq
```

Only at-risk in last 30 days:

```bash
curl -sS -H "access_token: $TOKEN" \
  "$BASE/api/v2/taskforge/main/project_health?range=30d&health=at_risk" | jq
```

Sorted by most overdue first:

```bash
curl -sS -H "access_token: $TOKEN" \
  "$BASE/api/v2/taskforge/main/project_health?range=30d&sort=overdue_desc" | jq
```

Search by project name:

```bash
curl -sS -H "access_token: $TOKEN" \
  "$BASE/api/v2/taskforge/main/project_health?search=omni" | jq
```

### 5. Performance Ranking (legacy split)

Default (top 10 by productivity):

```bash
curl -sS -H "access_token: $TOKEN" \
  "$BASE/api/v2/taskforge/main/performance_ranking?range=7d&top_n=10" | jq
```

Lower the low-performer threshold to 40 %:

```bash
curl -sS -H "access_token: $TOKEN" \
  "$BASE/api/v2/taskforge/main/performance_ranking?range=30d&top_n=10&low_threshold=40" | jq
```

Drill into one employee:

```bash
curl -sS -H "access_token: $TOKEN" \
  "$BASE/api/v2/taskforge/main/performance_ranking?range=30d&employee_id=104" | jq
```

Sort by total tasks, not productivity:

```bash
curl -sS -H "access_token: $TOKEN" \
  "$BASE/api/v2/taskforge/main/performance_ranking?range=30d&sort=total_desc" | jq
```

Ranking inside one project only:

```bash
curl -sS -H "access_token: $TOKEN" \
  "$BASE/api/v2/taskforge/main/performance_ranking?range=30d&project_id=7" | jq
```

### 6. QC Feedback (project × quality)

Default:

```bash
curl -sS -H "access_token: $TOKEN" \
  "$BASE/api/v2/taskforge/main/qc_feedback?range=30d" | jq
```

Technical category only:

```bash
curl -sS -H "access_token: $TOKEN" \
  "$BASE/api/v2/taskforge/main/qc_feedback?range=30d&category=technical" | jq
```

Sort by number of tasks scored:

```bash
curl -sS -H "access_token: $TOKEN" \
  "$BASE/api/v2/taskforge/main/qc_feedback?range=30d&sort=scored_desc" | jq
```

Paginated:

```bash
curl -sS -H "access_token: $TOKEN" \
  "$BASE/api/v2/taskforge/main/qc_feedback?range=30d&page=2&limit=20" | jq
```

Single project:

```bash
curl -sS -H "access_token: $TOKEN" \
  "$BASE/api/v2/taskforge/main/qc_feedback?range=30d&project_id=7" | jq
```

### 7. Performance Ranking Panel

Default (top 5 each, min 5 tasks to qualify for Improvement Needed):

```bash
curl -sS -H "access_token: $TOKEN" \
  "$BASE/api/v2/taskforge/main/performance_ranking_panel?range=30d" | jq
```

Wider top / bottom lists:

```bash
curl -sS -H "access_token: $TOKEN" \
  "$BASE/api/v2/taskforge/main/performance_ranking_panel?range=30d&top_n=10&bottom_n=10" | jq
```

Non-STEM only, stricter floor:

```bash
curl -sS -H "access_token: $TOKEN" \
  "$BASE/api/v2/taskforge/main/performance_ranking_panel?range=30d&category=non_stem&min_tasks=20" | jq
```

Custom range:

```bash
curl -sS -H "access_token: $TOKEN" \
  "$BASE/api/v2/taskforge/main/performance_ranking_panel?range=custom&date_from=2026-04-01&date_to=2026-04-30" | jq
```

### 8. QC Feedback Summary (cards)

Default:

```bash
curl -sS -H "access_token: $TOKEN" \
  "$BASE/api/v2/taskforge/main/qc_feedback_summary?range=30d" | jq
```

Single project:

```bash
curl -sS -H "access_token: $TOKEN" \
  "$BASE/api/v2/taskforge/main/qc_feedback_summary?range=30d&project_id=7" | jq
```

STEM only:

```bash
curl -sS -H "access_token: $TOKEN" \
  "$BASE/api/v2/taskforge/main/qc_feedback_summary?range=30d&category=stem" | jq
```

### 9. QC Feedback — Justification breakdown

Default (most QC tasks first):

```bash
curl -sS -H "access_token: $TOKEN" \
  "$BASE/api/v2/taskforge/main/qc_feedback_justification_breakdown?range=30d" | jq
```

Search for a member:

```bash
curl -sS -H "access_token: $TOKEN" \
  "$BASE/api/v2/taskforge/main/qc_feedback_justification_breakdown?range=30d&search=ravali" | jq
```

One project, alphabetical:

```bash
curl -sS -H "access_token: $TOKEN" \
  "$BASE/api/v2/taskforge/main/qc_feedback_justification_breakdown?range=30d&project_id=7&sort=name_asc" | jq
```

Second page, 20 per page:

```bash
curl -sS -H "access_token: $TOKEN" \
  "$BASE/api/v2/taskforge/main/qc_feedback_justification_breakdown?range=30d&page=2&limit=20" | jq
```

### 10. QC Feedback — Prompt breakdown

Default (highest rejection rate first):

```bash
curl -sS -H "access_token: $TOKEN" \
  "$BASE/api/v2/taskforge/main/qc_feedback_prompt_breakdown?range=30d" | jq
```

Sorted by total prompts submitted:

```bash
curl -sS -H "access_token: $TOKEN" \
  "$BASE/api/v2/taskforge/main/qc_feedback_prompt_breakdown?range=30d&sort=prompts_desc" | jq
```

Name search:

```bash
curl -sS -H "access_token: $TOKEN" \
  "$BASE/api/v2/taskforge/main/qc_feedback_prompt_breakdown?range=30d&search=anil" | jq
```

Custom range, second page:

```bash
curl -sS -H "access_token: $TOKEN" \
  "$BASE/api/v2/taskforge/main/qc_feedback_prompt_breakdown?range=custom&date_from=2026-04-01&date_to=2026-04-30&page=2&limit=20" | jq
```

---

## Bulk smoke-test script

```bash
#!/usr/bin/env bash
set -u
BASE="${BASE:-http://localhost:8069}"
TOKEN="${TOKEN:?set TOKEN env var}"

endpoints=(
  "/api/v2/taskforge/main/summary"
  "/api/v2/taskforge/main/tasks_timeseries?range=7d"
  "/api/v2/taskforge/main/active_blockers?limit=5"
  "/api/v2/taskforge/main/project_health?range=7d&limit=10"
  "/api/v2/taskforge/main/performance_ranking?range=30d&top_n=10"
  "/api/v2/taskforge/main/qc_feedback?range=30d"
  "/api/v2/taskforge/main/performance_ranking_panel?range=30d"
  "/api/v2/taskforge/main/qc_feedback_summary?range=30d"
  "/api/v2/taskforge/main/qc_feedback_justification_breakdown?range=30d&limit=5"
  "/api/v2/taskforge/main/qc_feedback_prompt_breakdown?range=30d&limit=5"
)

for ep in "${endpoints[@]}"; do
  printf '%-72s ' "$ep"
  curl -sS -o /tmp/resp.json -w "HTTP %{http_code}\n" \
    -H "access_token: $TOKEN" "$BASE$ep"
  head -c 200 /tmp/resp.json
  printf '\n---\n'
done
```

---

## Correctness improvements over legacy `dashboard_controllers.py`

| # | Fix | Where |
|---|-----|-------|
| 1 | Open blockers now include `escalated_to_pl`, `escalated_to_cto` (legacy undercount) | `OPEN_BLOCKER_STATES` constant |
| 2 | Excludes zero-task employees from `low_performers` / Improvement Needed | `/performance_ranking`, `/performance_ranking_panel` (`min_tasks` floor) |
| 3 | Stops leaking raw exceptions to clients — logs stack, returns generic message | `_safe_error` |
| 4 | N+1 loops replaced with `read_group` aggregation                      | `/project_health`, `/performance_ranking`, `/qc_feedback`, `/tasks_timeseries`, `/qc_feedback_justification_breakdown`, `/qc_feedback_prompt_breakdown` |
| 5 | Date-range filtering on every endpoint (legacy founder was today-only) | all endpoints |
| 6 | Category filter (STEM / Non-STEM / Technical) wired end-to-end         | `_category_domain` |
| 7 | Line-chart response always returns a contiguous day series (no gaps)   | `/tasks_timeseries` |
| 8 | Cards vs Lists separation: `/summary` + `/qc_feedback_summary` are card-only; each list endpoint has its own filter surface (`page`, `limit`, `search`, `sort`, `project_id`, `employee_id`, `status`, `priority`, `health`, `top_n`, `bottom_n`, `min_tasks`) | all list endpoints |
| 9 | Safe-whitelist `sort` param on every list endpoint (no SQL injection via order-by) | all list endpoints |
| 10 | Figma-aligned prompt rejection taxonomy added to `preference.ranking.rejection_reason` alongside legacy values — historical rows remain queryable | `preference_ranking/models/models.py` |
| 11 | Graceful `data_availability` flag for metrics that can't yet be sourced (justification categories, first-hit counts) — frontend knows when to render "—" | `/qc_feedback_summary`, `/qc_feedback_justification_breakdown` |
| 12 | Schema auto-probe on `preference.ranking.project_id` so the prompt breakdown works whether or not the model carries a project link in this deployment | `/qc_feedback_prompt_breakdown` |

---

## Known data gaps (pending persistence)

The following Figma columns / chips are returned as `null` / `[]` with
`data_availability: "partial"` or `"pending_persistence"`:

| Metric | Reason | Unblock path |
|--------|--------|--------------|
| `justification_categories[]` chip row | `/api/v2/taskforge/tasks/grammar_check` returns category counts but **does not persist them** | Add `task.forge.qc.issue` child model (or counter fields on `task.forge.log`) and write to it from `grammar_check`. Endpoint 8 will auto-populate. |
| `issues_first_hit`, `total_issues`, `first_hit_pct`, `top_categories` per member | Same as above — per-log counters never stored | Same as above. Endpoint 9 will auto-populate once fields exist. |

Endpoints 8 and 9 are wired to consume these columns as soon as the
fields exist; no further controller change required.

---

## Odoo restart required

After pulling this code:

```bash
cd odoo-19
python odoo-bin -c etp.conf -u preference_ranking,task_forge_core
```

The `-u` is required this time because `preference_ranking`'s
`rejection_reason` Selection gained new values.
