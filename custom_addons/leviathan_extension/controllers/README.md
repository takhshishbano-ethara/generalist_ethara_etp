# Leviathan Extension API

REST endpoints layered on top of the `leviathan` module. All endpoints live in
[`main.py`](./main.py) under `LeviathanExtensionController` and share a common
authentication and response envelope provided by `api_auth_gateway`.

- Base URL prefix: `/api/v1/leviathan_ext`
- Transport: `http` routes with `csrf=False`, `cors="*"`
- Content-type: `application/json` (request body **and** response)

---

## Authentication

Every endpoint is decorated with `@validate_token` from
`api_auth_gateway.controllers.utility`. The route uses `auth="none"` because
`@validate_token` performs the authentication itself by reading the
`access_token` header, looking up `api.access_token`, checking expiry, and
calling `request.update_env(user=...)`.

| Header | Required | Notes |
|---|---|---|
| `access_token` | Yes | Issued by `api_auth_gateway` |
| `Content-Type` | Yes for POST | `application/json` |

If the token is missing or expired, the decorator returns a 401 envelope and
the endpoint body is never invoked.

---

## Role-based authorization

Authorization uses the `api.role` model (`res.users.user_role`), **not** Odoo
groups. Three role buckets are recognised:

| Bucket | XMLIDs | Used by |
|---|---|---|
| Admin | `api_auth_gateway.role_cto_technical`, `role_pl_technical`, `role_pl_stem`, `role_pl_non_stem` | `_require_admin` |
| Leviathan user | Admin bucket + `role_qc_technical`, `role_qc_stem`, `role_qc_non_stem`, `role_tasker_technical`, `role_tasker_stem`, `role_tasker_non_stem` | `_require_leviathan_user` |
| Tasker | `role_tasker_technical`, `role_tasker_stem`, `role_tasker_non_stem` | `_require_tasker` |

A 403 envelope is returned when the caller's `user_role` is not in the
required bucket.

> The `res.users` override in [`models/res_users.py`](../models/res_users.py)
> keeps the legacy `leviathan.group_leviathan_admin` / `_user` groups in sync
> with the `user_role` on user create/write. Admin + PL + QC roles map to the
> admin group; Tasker roles map to the user group.

---

## Standard response envelope

All responses are produced via `return_Response(message, status, errors, data)`:

```json
{
  "message": "OK",
  "errors": [],
  "status_code": 200,
  "...": "additional fields from `data`"
}
```

On `status=400` the `message` is auto-joined from `errors` when `errors` is
non-empty. Soft warnings (e.g. unresolved role filter, post-claim
`action_run` failure) are surfaced in `errors` even when the call as a whole
returns `200`.

---

## Endpoints

### 1. `POST /api/v1/leviathan_ext/jobs/create`

Create a single Leviathan job. **Admin only.**

Request body (JSON, validated by `@validate_request`):

| Field | Type | Required | Notes |
|---|---|---|---|
| `url` | string | yes | Auto-prefixed with `https://` if no scheme |
| `category_id` | int | yes | Must reference an existing `leviathan.category` |
| `tasker_id` | int | no | If supplied, must reference an existing user; assigning a tasker auto-promotes state to `draft` |

Behavior:

- Rejects (400) when an active job already exists for the same URL (any state
  except `submitted` / `cancelled`).
- Returns the created job via the minimal serializer (`id`, `name`, `url`,
  `state`, `category_id`, `category_name`, `user_id`, `user_name`).

Status codes: `200`, `400` (validation / duplicate / create failure), `401`,
`403`.

---

### 2. `POST /api/v1/leviathan_ext/jobs/bulk_create`

Bulk-create jobs from a CSV or XLSX file. **Admin only.**

Request body (JSON):

| Field | Type | Required | Notes |
|---|---|---|---|
| `file` | string | yes | Base64-encoded file content. `data:...;base64,` prefix is accepted and stripped |
| `filename` | string | yes | Must end in `.csv` or `.xlsx` |

Spreadsheet columns (header row, case-insensitive):

| Column | Required | Notes |
|---|---|---|
| `url` | yes | Empty → row skipped |
| `category_id` | no | Numeric id |
| `category` | no | Category name (matched `=ilike`); only used if `category_id` is empty |
| `tasker` / `tasker_id` | no | Numeric id, login, or email |

Per-row behavior mirrors the wizard logic in `leviathan/wizards/import_wizard.py`:

- URL without scheme is prefixed with `https://`.
- Duplicate active URLs are skipped (state not in `submitted` / `cancelled`).
- Unknown category names create the job **without** a category and add a
  per-row warning.
- Per-row create exceptions are captured in `row_errors`; processing continues.

Response data:

```json
{
  "created": 12,
  "skipped_count": 2,
  "error_count": 1,
  "jobs": [{...}],
  "skipped": [{"row": 3, "reason": "empty url"}],
  "row_errors": [{"row": 7, "url": "...", "error": "..."}]
}
```

Status codes: `200`, `400` (unsupported extension / invalid base64 / parse
failure / missing required column / empty file), `401`, `403`.

---

### 3. `GET /api/v1/leviathan_ext/categories`

List Leviathan categories. **Token only** (no role gate).

Query params:

| Param | Default | Notes |
|---|---|---|
| `active` | `true` | `false`/`0`/`no` returns archived categories too |
| `search` | – | Case-insensitive `ilike` on `name` |
| `limit` | 100 | Capped at 500 |
| `offset` | 0 | – |

Response: `{ categories: [...], count, total, offset, limit }`. Each category
includes `id`, `name`, `technical_key`, `active`.

Status codes: `200`, `401`.

---

### 4. `GET /api/v1/leviathan_ext/users`

List `res.users` filtered by `api.role`. **Leviathan user required.**

Query params:

| Param | Notes |
|---|---|
| `role` | Single role id, name, or `user_type` |
| `roles` | Comma-separated list; combined with `role` |
| `active` | Default `true`; `false` includes archived users |
| `search` | Case-insensitive `ilike` on `name`/`login`/`email` |
| `has_role` | `true` → only users with a `user_role`; `false` → only users without one |
| `limit` | Default 50, max 500 |
| `offset` | Default 0 |

If a `role`/`roles` filter is given and **none** resolve, the endpoint returns
`400` with the resolution warnings in `errors`. Partial resolution is allowed:
unresolved specs are reported via warnings, resolved specs are applied.

Response data:

```json
{
  "users": [{...}],
  "count": 17,
  "total": 17,
  "offset": 0,
  "limit": 50,
  "roles_resolved": [{...}],
  "available_roles": [{...}]
}
```

`_serialize_user` emits: `id`, `name`, `login`, `email`, `active`,
`partner_id`, `company_id`, `company_name`, `user_role_id`, `user_role_name`,
`user_type`, `project_type`, `user_role` (full role object or `null`). No
legacy group fields.

Status codes: `200`, `400` (all roles unresolved), `401`, `403`.

---

### 5. `GET /api/v1/leviathan_ext/jobs`

List Leviathan jobs with filters and pagination. **Leviathan user required.**

Query params:

| Param | Notes |
|---|---|
| `state` | Comma-separated list of states |
| `name` | `ilike` |
| `url` | `ilike` |
| `search` | OR over `name` / `url` / `site_name` |
| `user_id` | Numeric id, or `unassigned`/`false`/`null`/`none` for `user_id = False` |
| `has_user` | `true`/`false` |
| `category_id` | Numeric id |
| `category` | Category name |
| `qc_verdict` | `passed` / `failed` / `pending` |
| `grade` | – |
| `score_min`, `score_max` | – |
| `date_from`, `date_to` | ISO dates against `create_date` |
| `order` | One of: `create_date desc/asc`, `write_date desc/asc`, `name asc/desc`, `score desc/asc`, `state asc/desc`, `completed_at desc/asc`. Default `create_date desc`. Invalid → 400 |
| `limit` | Default 50, max 500 |
| `offset` | Default 0 |

Response: `{ jobs, count, total, offset, limit, order }` with each entry being
the job summary serializer. Warnings from filter resolution surface in
`errors`.

Status codes: `200`, `400` (invalid `order`), `401`, `403`.

---

### 6. `GET /api/v1/leviathan_ext/jobs/<int:job_id>`

Return a full job detail. **Leviathan user required.**

Response: `{ job: _serialize_job_detail(job) }` containing every persisted
field on `leviathan.job` (text, JSON, datetime, M2o, booleans).

Status codes: `200`, `401`, `403`, `404`.

---

### 7. `POST /api/v1/leviathan_ext/jobs/<int:job_id>/claim`

Self-assign a `not_assigned` job to the calling tasker and start extraction.
**Tasker only** (one of the three `api.role` tasker xmlids).

Behavior:

1. 404 if the job does not exist.
2. 409 if `job.state != "not_assigned"`.
3. Writes `user_id = request.env.user.id`. The `leviathan.job.write` override
   calls `_smart_state_on_assign` which promotes the state to `draft`.
4. Calls `job.sudo().with_context(force_extract=True).action_run()` to skip
   the rerun wizard prompt. Failures are caught, logged, and surfaced as a
   warning in the response `errors` (the claim itself is preserved).
5. Returns the full job detail.

Status codes: `200`, `400` (write failure), `401`, `403`, `404`, `409`.

---

## Conventions for adding new endpoints

- Always `type="http", auth="none", csrf=False, cors="*"` + `@validate_token`.
- Use `@validate_request({...})` for JSON-body endpoints with stable schemas;
  fall back to manual parsing only when the schema is variable.
- Authorize with the helpers `_require_admin` / `_require_leviathan_user` /
  `_require_tasker` — return the envelope as-is when they return non-`None`.
- Wrap mutating ORM calls in `try/except` and use `_logger.exception` so the
  full traceback lands in the Odoo log while a clean message is returned to
  the client.
- Use `.sudo()` for cross-user reads (token authorization has already
  enforced the policy).
- Surface non-fatal issues via the `errors` list on a 2xx response; only fail
  the call (4xx) when the request cannot be honoured at all.
