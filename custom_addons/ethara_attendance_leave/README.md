# Leave Management — REST API

REST endpoints for the **employee-wise leave management** feature (Leave Balances,
allocation, bulk upload). Implemented in `controllers/leave_management.py`
(module `ethara_attendance_leave`).

These mirror the Odoo backend **Time Off → Leave Management** screens so the
frontend can show the same employee-wise layout.

---

## Base URL

| Environment | Base URL |
|---|---|
| Local | `http://localhost:8069` |
| Stage | `https://projects-stage.ethara.ai` |

All paths below are absolute, e.g. `http://localhost:8069/api/v1/leave_mgmt/...`.

---

## Authentication

Every endpoint requires a valid access token and an **HR** or **CTO** role.

**1. Get a token**

```
POST /api/v1/auth_token
Content-Type: application/json

{ "login": "admin@ethara.ai", "password": "admin" }
```

The token is in `data.access_token`.

**2. Send it on every request** as the `Access-Token` header (dash form — a header
named `access_token` with an underscore is dropped by the server):

```
Access-Token: access_token_xxxxxxxxxxxxxxxxxxxx
```

Allowed roles: `role_hr_technical` (HR), `role_cto_technical` (CTO). Anything
else → `403`.

---

## Response envelope

All responses use the gateway envelope:

```json
{ "message": "Success", "errors": [], "data": { }, "status_code": 200 }
```

| Status | Meaning |
|---|---|
| 200 | Success |
| 400 | Bad / missing parameters, or a validation failure |
| 401 | Missing or expired token |
| 403 | Caller is not HR/CTO |
| 404 | Resource not found (e.g. employee) |

---

## Endpoints

| # | Method | Path | Purpose |
|---|---|---|---|
| 1 | GET  | `/api/v1/leave_mgmt/leave_types` | Leave-type catalog + entitlements |
| 2 | GET  | `/api/v1/leave_mgmt/employees` | Employee list + balance summary |
| 3 | GET  | `/api/v1/leave_mgmt/employee/<id>/buckets` | One employee's balance cards |
| 4 | POST | `/api/v1/leave_mgmt/allocate` | Assign/add days to an employee |
| 5 | POST | `/api/v1/leave_mgmt/bulk_upload` | Bulk upload allocations (.xlsx) |

---

### 1. Leave types (catalog)

```
GET /api/v1/leave_mgmt/leave_types
```

The 11 Ethara leave types and each type's annual entitlement.

**`data`:**

```json
{
  "leave_types": [
    {
      "id": 74,
      "name": "Sick Leave",
      "code": "sl",
      "default_annual_days": 12.0,
      "requires_allocation": true,
      "allow_half_day": false,
      "request_unit": "day",
      "color": 1
    }
  ]
}
```

| Field | Type | Notes |
|---|---|---|
| `id` | int | `hr.leave.type` id |
| `name` | string | Display name |
| `code` | string | `sl, cl, el, lop, marriage, maternity, bereavement, comp_off, wfh, menstrual, restricted_holiday` |
| `default_annual_days` | float | Entitlement / max allocatable. `0` = not set yet |
| `requires_allocation` | bool | Always true for Ethara types |
| `allow_half_day` | bool | Half-day allowed |
| `request_unit` | string | `day` / `half_day` |
| `color` | int | Odoo color index |

---

### 2. Employees + balance summary

```
GET /api/v1/leave_mgmt/employees
```

All active employees with a roll-up of their balances (for the list screen).

**`data`:**

```json
{
  "employees": [
    {
      "id": 1,
      "name": "Administrator",
      "email": "admin@ethara.ai",
      "department": "",
      "job_title": "",
      "user_id": 2,
      "total_allocated": 39.0,
      "total_remaining": 39.0,
      "bucket_count": 3
    }
  ]
}
```

| Field | Type | Notes |
|---|---|---|
| `id` | int | `hr.employee` id (use for #3) |
| `name` | string | Employee name |
| `email` | string | Work email (may be empty) |
| `department` | string | Department name (may be empty) |
| `job_title` | string | Job position (may be empty) |
| `user_id` | int \| false | Linked `res.users` id, or `false` |
| `total_allocated` | float | Sum of allocated days across types |
| `total_remaining` | float | Sum of remaining days across types |
| `bucket_count` | int | Number of types with a non-zero allocation |

---

### 3. Employee balance cards

```
GET /api/v1/leave_mgmt/employee/<employee_id>/buckets
```

One entry per leave type for that employee (all 11 returned).

**`data`:**

```json
{
  "employee": { "id": 1, "name": "Administrator", "email": "admin@ethara.ai" },
  "buckets": [
    {
      "leave_type_id": 74,
      "leave_type": "Sick Leave",
      "code": "sl",
      "allocated": 12.0,
      "taken": 0.0,
      "remaining": 12.0,
      "color": 1
    }
  ]
}
```

| Card field | Type | Notes |
|---|---|---|
| `leave_type_id` | int | `hr.leave.type` id |
| `leave_type` | string | Display name (card title) |
| `code` | string | Ethara code |
| `allocated` | float | Total validated allocation (days) |
| `taken` | float | Days already taken |
| `remaining` | float | `allocated - taken` |
| `color` | int | Odoo color index |

> **"How many can we still give":** the buckets payload does **not** include the
> entitlement. Combine with endpoint #1: `can_give = max(default_annual_days - allocated, 0)`.

**Errors:** `404` if the employee id doesn't exist.

---

### 4. Allocate days to an employee

```
POST /api/v1/leave_mgmt/allocate
Content-Type: application/json
Access-Token: <token>

{
  "employee_id": 1,
  "leave_type": "sl",          // id, ethara code, or name
  "days": 12,
  "date_from": "2026-01-01",   // optional
  "date_to":   "2026-12-31"    // optional
}
```

Creates a validated allocation for the given days and applies the validations below.

> **Note — adds, not sets.** This endpoint *creates* an allocation each call
> (calling it twice with `5` results in a total of `10`). It is not a
> set-to-target operation like the inline grid in Odoo.

**`data`:**

```json
{
  "allocation_id": 12,
  "employee_id": 1,
  "leave_type": "Sick Leave",
  "days": 12.0,
  "state": "validate"
}
```

**Validations** (return `400` with a message):

- `days` must be a number ≥ 0.
- The leave type must have an entitlement (`default_annual_days > 0`), else
  *"no entitlement is set … set Default Annual Days first"*.
- Total allocated may not exceed the entitlement.
- Total allocated may not drop below days already taken.

---

### 5. Bulk upload allocations (.xlsx)

```
POST /api/v1/leave_mgmt/bulk_upload
Content-Type: application/json
Access-Token: <token>

{
  "file": "<base64-encoded .xlsx>",
  "filename": "leave_allocations.xlsx"   // optional
}
```

Accepts the **grid-format** sheet: one row per employee, one column per leave
type (same layout as the Employees list).

**Columns:** `Employee (email or ID)`, then `Sick`, `Casual`, `Earned`, `LOP`,
`Marriage`, `Maternity`, `Bereavement`, `Comp-Off`, `WFH`, `Menstrual`,
`Restricted`. Each non-empty cell sets that employee's balance for that type
(blank = unchanged). All validations from #4 apply per cell.

**`data`:**

```json
{
  "summary": "3 employee row(s) applied.\n\n1 row(s) skipped:\nRow 4: No employee found for \"ghost@nobody.com\"."
}
```

Bad rows are skipped (and listed in `summary`) without aborting the rest.

---

## Quick cURL

```bash
# token
TOKEN=$(curl -s -X POST http://localhost:8069/api/v1/auth_token \
  -H 'Content-Type: application/json' \
  -d '{"login":"admin@ethara.ai","password":"admin"}' \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['data']['access_token'])")

# leave types
curl -s http://localhost:8069/api/v1/leave_mgmt/leave_types -H "Access-Token: $TOKEN"

# employees
curl -s http://localhost:8069/api/v1/leave_mgmt/employees -H "Access-Token: $TOKEN"

# one employee's cards
curl -s http://localhost:8069/api/v1/leave_mgmt/employee/1/buckets -H "Access-Token: $TOKEN"

# allocate
curl -s -X POST http://localhost:8069/api/v1/leave_mgmt/allocate \
  -H "Access-Token: $TOKEN" -H 'Content-Type: application/json' \
  -d '{"employee_id":1,"leave_type":"sl","days":12}'
```
