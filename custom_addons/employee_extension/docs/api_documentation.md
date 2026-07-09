# Employee Extension - API Documentation

> **Module:** `employee_extension`
> **Version:** 19.0.1.0.0
> **Base URL:** `YOUR_BASE_URL` (e.g. `http://localhost:8069`)
> **Auth:** All endpoints require a valid token passed via the `token` header.

---

## Table of Contents

| # | Endpoint | Method | Section |
|---|---|---|---|
| 1 | `/api/v2/employees` | POST | [Create Employee](#1-create-employee) |
| 2 | `/api/v2/employees/bulk` | POST | [Bulk Create Employees](#2-bulk-create-employees) |
| 3 | `/api/v1/employees/:id` | PUT / PATCH | [Update Employee](#3-update-employee) |
| 4 | `/api/v1/employees/:id` | GET | [Get Employee](#4-get-employee-by-id) |
| 5 | `/api/v1/employees` | GET | [List Employees](#5-list-employees) |
| 6 | `/api/v1/employees/:id` | DELETE | [Delete Employee](#6-delete-archive-employee) |
| 7 | `/api/v1/employees/:id/offboard` | POST | [Offboard Employee](#7-offboard-employee) |
| 8 | `/api/v1/allocation/request` | POST | [Create Allocation Request](#8-create-allocation-request) |
| 9 | `/api/v1/allocation/request/:id/submit` | POST | [Submit Allocation Request](#9-submit-allocation-request) |
| 10 | `/api/v1/allocation/request/approve` | POST | [Approve Allocation Request](#10-approve-allocation-request) |
| 11 | `/api/v1/allocation/request/reject` | POST | [Reject Allocation Request](#11-reject-allocation-request) |
| 12 | `/api/v1/allocation/request` | GET | [Get Allocation Request](#12-get-allocation-request-by-id) |
| 13 | `/api/v1/allocation/requests` | GET | [List Allocation Requests](#13-list-allocation-requests) |
| 14 | `/api/v1/allocation/request/:id/reset` | POST | [Reset Allocation Request](#14-reset-allocation-request-to-draft) |

---

## Employee APIs

---

### 1. Create Employee

**`POST /api/v2/employees`**

Creates a new employee and its linked `res.users` record.

#### Request Body

| Field | Type | Required | Notes |
|---|---|---|---|
| `name` | string | Yes | Employee full name |
| `email` | email | Yes | Must end with `@ethara.ai` |
| `job_title` | int | Yes | Designation ID (`hr.designation`) |
| `role_id` | int | Yes | User role ID (`api.role`) |
| `department_id` | int | No | Department ID |
| `project_id` | int | No | Project ID |
| `pl_id` | int | No | Project Lead (employee) ID |
| `qr_id` | int | No | QC Reviewer (employee) ID |
| `work_location_name` | string | No | Work location name |

#### cURL

```bash
curl -X POST 'YOUR_BASE_URL/api/v2/employees' \
  -H 'Content-Type: application/json' \
  -H 'token: YOUR_TOKEN' \
  -d '{
    "name": "John Doe",
    "email": "john.doe@ethara.ai",
    "job_title": 1,
    "role_id": 1,
    "department_id": 1,
    "project_id": 1,
    "pl_id": 1,
    "qr_id": 1,
    "work_location_name": "Riyadh Office"
  }'
```

#### Success Response (200)

```json
{
  "message": "Employee created successfully",
  "data": {
    "id": 1,
    "name": "John Doe",
    "email": "john.doe@ethara.ai",
    "job_title": "Developer"
  }
}
```

#### Error Responses

| Status | Condition |
|---|---|
| 400 | Email not `@ethara.ai` domain |
| 400 | User with this email already exists |
| 400 | Missing required fields |

---

### 2. Bulk Create Employees

**`POST /api/v2/employees/bulk`**

Creates multiple employees in a single request. Processes each row independently - failures in one row do not block others.

#### Request Body

| Field | Type | Required | Notes |
|---|---|---|---|
| `employees` | array | Yes | Array of employee objects |

**Each employee object:**

| Field | Type | Required | Notes |
|---|---|---|---|
| `name` | string | Yes | |
| `email` | string | Yes | Must end with `@ethara.ai` |
| `job_title` | string | No | |
| `password` | string | No | Defaults to `Ethara@123` |
| `department_id` | int | No | |
| `work_location_name` | string | No | |
| `pl_id` | int | No | Project Lead ID |
| `qr_id` | int | No | QC Reviewer ID |

#### cURL

```bash
curl -X POST 'YOUR_BASE_URL/api/v2/employees/bulk' \
  -H 'Content-Type: application/json' \
  -H 'token: YOUR_TOKEN' \
  -d '{
    "employees": [
      {
        "name": "Alice Smith",
        "email": "alice.smith@ethara.ai",
        "job_title": "Developer",
        "department_id": 1,
        "work_location_name": "Riyadh Office"
      },
      {
        "name": "Bob Jones",
        "email": "bob.jones@ethara.ai",
        "job_title": "Designer",
        "department_id": 2
      }
    ]
  }'
```

#### Success Response (200)

```json
{
  "message": "Bulk create complete: 2 created, 0 errors",
  "data": {
    "created": [
      { "id": 1, "name": "Alice Smith", "email": "alice.smith@ethara.ai" },
      { "id": 2, "name": "Bob Jones", "email": "bob.jones@ethara.ai" }
    ],
    "errors": []
  }
}
```

---

### 3. Update Employee

**`PUT /api/v1/employees/:id`** or **`PATCH /api/v1/employees/:id`**

Updates an existing employee. All fields are optional.

#### Path Parameters

| Param | Type | Description |
|---|---|---|
| `id` | int | Employee ID |

#### Request Body

| Field | Type | Required |
|---|---|---|
| `name` | string | No |
| `job_title` | string | No |
| `department_id` | int | No |
| `project_id` | int | No |
| `work_location_name` | string | No |
| `pl_id` | int | No |
| `qr_id` | int | No |

#### cURL

```bash
curl -X PUT 'YOUR_BASE_URL/api/v1/employees/1' \
  -H 'Content-Type: application/json' \
  -H 'token: YOUR_TOKEN' \
  -d '{
    "name": "John Doe Updated",
    "job_title": "Senior Developer",
    "department_id": 2,
    "project_id": 3,
    "work_location_name": "Jeddah Office",
    "pl_id": 5,
    "qr_id": 6
  }'
```

#### Success Response (200)

```json
{
  "message": "Employee updated successfully",
  "data": {
    "id": 1,
    "name": "John Doe Updated",
    "job_title": "Senior Developer",
    "work_location": "Jeddah Office"
  }
}
```

#### Error Responses

| Status | Condition |
|---|---|
| 404 | Employee not found |

---

### 4. Get Employee by ID

**`GET /api/v1/employees/:id`**

#### Path Parameters

| Param | Type | Description |
|---|---|---|
| `id` | int | Employee ID |

#### cURL

```bash
curl -X GET 'YOUR_BASE_URL/api/v1/employees/1' \
  -H 'token: YOUR_TOKEN'
```

#### Success Response (200)

```json
{
  "message": "Employee details",
  "data": {
    "id": 1,
    "name": "John Doe",
    "email": "john.doe@ethara.ai",
    "designation_id": 1,
    "job_title": "Developer",
    "department": "Engineering",
    "work_location": "Riyadh Office",
    "offboarding_state": "active",
    "is_offboarded": false,
    "offboard_date": null,
    "pl_id": 1,
    "pl_name": "Lead Name",
    "qr_id": 2,
    "qr_name": "Reviewer Name",
    "active": true
  }
}
```

#### Error Responses

| Status | Condition |
|---|---|
| 404 | Employee not found |

---

### 5. List Employees

**`GET /api/v1/employees`**

Returns a filtered list of employees.

#### Query Parameters

| Param | Type | Required | Default | Values |
|---|---|---|---|---|
| `active` | string | No | — | `true`, `false` |
| `offboarding_state` | string | No | — | `active`, `offboarding`, `offboarded` |
| `department_id` | int | No | — | |
| `limit` | int | No | 100 | |

#### cURL

```bash
# All employees
curl -X GET 'YOUR_BASE_URL/api/v1/employees' \
  -H 'token: YOUR_TOKEN'

# Active employees only
curl -X GET 'YOUR_BASE_URL/api/v1/employees?active=true' \
  -H 'token: YOUR_TOKEN'

# By offboarding state
curl -X GET 'YOUR_BASE_URL/api/v1/employees?offboarding_state=offboarding' \
  -H 'token: YOUR_TOKEN'

# By department with limit
curl -X GET 'YOUR_BASE_URL/api/v1/employees?department_id=1&limit=50' \
  -H 'token: YOUR_TOKEN'
```

#### Success Response (200)

```json
{
  "message": "5 employees found",
  "data": [
    {
      "id": 1,
      "name": "John Doe",
      "email": "john.doe@ethara.ai",
      "job_title": "Developer",
      "department": "Engineering",
      "offboarding_state": "active",
      "is_offboarded": false,
      "active": true
    }
  ]
}
```

---

### 6. Delete (Archive) Employee

**`DELETE /api/v1/employees/:id`**

Soft-deletes an employee by setting `active = False`. The record is not permanently removed.

#### Path Parameters

| Param | Type | Description |
|---|---|---|
| `id` | int | Employee ID |

#### cURL

```bash
curl -X DELETE 'YOUR_BASE_URL/api/v1/employees/1' \
  -H 'token: YOUR_TOKEN'
```

#### Success Response (200)

```json
{
  "message": "Employee archived successfully"
}
```

#### Error Responses

| Status | Condition |
|---|---|
| 404 | Employee not found |

---

### 7. Offboard Employee

**`POST /api/v1/employees/:id/offboard`**

Manages the offboarding lifecycle of an employee.

**State Flow:** `active` --> `offboarding` --> `offboarded` (use `reactivate` to return to `active`)

#### Path Parameters

| Param | Type | Description |
|---|---|---|
| `id` | int | Employee ID |

#### Request Body

| Field | Type | Required | Values |
|---|---|---|---|
| `action` | string | Yes | `start`, `complete`, `reactivate` |

#### Actions

| Action | From State | To State | Side Effects |
|---|---|---|---|
| `start` | `active` | `offboarding` | Sets `offboard_date` to today |
| `complete` | `offboarding` | `offboarded` | Sets `active = False` |
| `reactivate` | `offboarding` / `offboarded` | `active` | Sets `active = True`, clears `offboard_date` |

#### cURL

```bash
# Start offboarding
curl -X POST 'YOUR_BASE_URL/api/v1/employees/1/offboard' \
  -H 'Content-Type: application/json' \
  -H 'token: YOUR_TOKEN' \
  -d '{"action": "start"}'

# Complete offboarding
curl -X POST 'YOUR_BASE_URL/api/v1/employees/1/offboard' \
  -H 'Content-Type: application/json' \
  -H 'token: YOUR_TOKEN' \
  -d '{"action": "complete"}'

# Reactivate employee
curl -X POST 'YOUR_BASE_URL/api/v1/employees/1/offboard' \
  -H 'Content-Type: application/json' \
  -H 'token: YOUR_TOKEN' \
  -d '{"action": "reactivate"}'
```

#### Success Response (200) - Start

```json
{
  "message": "Offboarding started",
  "data": {
    "id": 1,
    "offboarding_state": "offboarding",
    "offboard_date": "2026-04-08"
  }
}
```

#### Error Responses

| Status | Condition |
|---|---|
| 400 | Invalid state transition (e.g. `start` when already `offboarding`) |
| 400 | Invalid action value |
| 404 | Employee not found |

---

## Allocation Request APIs

---

### 8. Create Allocation Request

**`POST /api/v1/allocation/request`**

Creates a new employee allocation request in `draft` state.

#### Request Body

| Field | Type | Required | Notes |
|---|---|---|---|
| `project_id` | int | Yes | Project ID |
| `role_id` | int | Yes | Requested role ID (`api.role`) |
| `quantity` | int | Yes | Must be > 0 |
| `justification` | string | Yes | Reason for the request |

#### cURL

```bash
curl -X POST 'YOUR_BASE_URL/api/v1/allocation/request' \
  -H 'Content-Type: application/json' \
  -H 'token: YOUR_TOKEN' \
  -d '{
    "project_id": 1,
    "role_id": 2,
    "quantity": 3,
    "justification": "Need additional developers for sprint deadline"
  }'
```

#### Success Response (200)

```json
{
  "message": "Allocation request created",
  "data": {
    "id": 1,
    "name": "AR-0001",
    "project_id": 1,
    "project_name": "Project Alpha",
    "quantity": 3,
    "state": "draft"
  }
}
```

---

### 9. Submit Allocation Request

**`POST /api/v1/allocation/request/:id/submit`**

Submits a draft allocation request for approval. Changes state from `draft` to `submitted`.

#### Path Parameters

| Param | Type | Description |
|---|---|---|
| `id` | int | Allocation request ID |

#### cURL

```bash
curl -X POST 'YOUR_BASE_URL/api/v1/allocation/request/1/submit' \
  -H 'token: YOUR_TOKEN'
```

#### Success Response (200)

```json
{
  "message": "Allocation request submitted",
  "data": {
    "id": 1,
    "name": "AR-0001",
    "state": "submitted"
  }
}
```

#### Error Responses

| Status | Condition |
|---|---|
| 400 | State is not `draft` |
| 404 | Request not found |

---

### 10. Approve Allocation Request

**`POST /api/v1/allocation/request/approve`**

> **Role Required:** CTO (`api_auth_gateway.role_cto_technical`)

Approves a submitted allocation request and assigns employees.

#### Request Body

| Field | Type | Required | Notes |
|---|---|---|---|
| `request_id` | int | Yes | Allocation request ID |
| `assign_employees` | list[int] | Yes | List of employee IDs to assign |
| `notes` | string | Yes | Approval notes |

#### cURL

```bash
curl -X POST 'YOUR_BASE_URL/api/v1/allocation/request/approve' \
  -H 'Content-Type: application/json' \
  -H 'token: YOUR_TOKEN' \
  -d '{
    "request_id": 1,
    "assign_employees": [1, 2, 3],
    "notes": "Approved. Assigning 3 developers."
  }'
```

#### Success Response (200)

```json
{
  "message": "Allocation request approved",
  "data": {
    "id": 1,
    "name": "AR-0001",
    "state": "approved",
    "approved_by": "CTO Name",
    "approval_date": "2026-04-08T10:30:00"
  }
}
```

#### Error Responses

| Status | Condition |
|---|---|
| 400 | State is not `submitted` |
| 403 | User does not have CTO role |
| 404 | Request not found |

#### Side Effects

When approved, employees are automatically assigned to the project based on the requested role:
- **PL roles** (Technical/STEM/Non-STEM) --> Added to `project_lead`
- **QC roles** (Technical/STEM/Non-STEM) --> Added to `project_qc_reviewer`
- **Tasker roles** (Technical/STEM/Non-STEM) --> Added to `project_tasker`

---

### 11. Reject Allocation Request

**`POST /api/v1/allocation/request/reject`**

> **Role Required:** CTO (`api_auth_gateway.role_cto_technical`)

Rejects a submitted allocation request.

#### Request Body

| Field | Type | Required |
|---|---|---|
| `request_id` | int | Yes |
| `notes` | string | No |

#### cURL

```bash
curl -X POST 'YOUR_BASE_URL/api/v1/allocation/request/reject' \
  -H 'Content-Type: application/json' \
  -H 'token: YOUR_TOKEN' \
  -d '{
    "request_id": 1,
    "notes": "Insufficient budget for this quarter"
  }'
```

#### Success Response (200)

```json
{
  "message": "Allocation request rejected",
  "data": {
    "id": 1,
    "name": "AR-0001",
    "state": "rejected",
    "notes": "Insufficient budget for this quarter"
  }
}
```

#### Error Responses

| Status | Condition |
|---|---|
| 400 | State is not `submitted` |
| 403 | User does not have CTO role |
| 404 | Request not found |

---

### 12. Get Allocation Request by ID

**`GET /api/v1/allocation/request`**

> **Role Required:** CTO (`api_auth_gateway.role_cto_technical`)

Retrieves full details of a specific allocation request.

#### Request Body

| Field | Type | Required |
|---|---|---|
| `request_id` | int | Yes |

#### cURL

```bash
curl -X GET 'YOUR_BASE_URL/api/v1/allocation/request' \
  -H 'Content-Type: application/json' \
  -H 'token: YOUR_TOKEN' \
  -d '{"request_id": 1}'
```

#### Success Response (200)

```json
{
  "message": "Allocation request details",
  "data": {
    "id": 1,
    "name": "AR-0001",
    "project_id": 1,
    "project_name": "Project Alpha",
    "role_id": 2,
    "role_name": "Developer",
    "quantity": 3,
    "justification": "Need additional developers for sprint deadline",
    "state": "approved",
    "requested_by": "Manager Name",
    "approved_by": "CTO Name",
    "approval_date": "2026-04-08T10:30:00",
    "notes": "Approved. Assigning 3 developers.",
    "created_at": "2026-04-07T09:00:00",
    "assign_employees": [
      { "id": 1, "name": "Alice Smith" },
      { "id": 2, "name": "Bob Jones" }
    ]
  }
}
```

#### Error Responses

| Status | Condition |
|---|---|
| 403 | User does not have CTO role |
| 404 | Request not found |

---

### 13. List Allocation Requests

**`GET /api/v1/allocation/requests`**

Returns a filtered list of allocation requests.

#### Query Parameters

| Param | Type | Required | Default | Values |
|---|---|---|---|---|
| `state` | string | No | — | `draft`, `submitted`, `approved`, `rejected` |
| `project_id` | int | No | — | |
| `limit` | int | No | 100 | |

#### cURL

```bash
# All requests
curl -X GET 'YOUR_BASE_URL/api/v1/allocation/requests' \
  -H 'token: YOUR_TOKEN'

# By state
curl -X GET 'YOUR_BASE_URL/api/v1/allocation/requests?state=submitted' \
  -H 'token: YOUR_TOKEN'

# By project
curl -X GET 'YOUR_BASE_URL/api/v1/allocation/requests?project_id=1' \
  -H 'token: YOUR_TOKEN'

# Combined filters
curl -X GET 'YOUR_BASE_URL/api/v1/allocation/requests?state=approved&project_id=1&limit=50' \
  -H 'token: YOUR_TOKEN'
```

#### Success Response (200)

```json
{
  "message": "3 allocation requests found",
  "data": [
    {
      "id": 1,
      "name": "AR-0001",
      "project_name": "Project Alpha",
      "role_name": "Developer",
      "quantity": 3,
      "state": "approved",
      "requested_by": "Manager Name",
      "created_at": "2026-04-07T09:00:00",
      "assign_employees": [
        { "id": 1, "name": "Alice Smith" }
      ]
    }
  ]
}
```

---

### 14. Reset Allocation Request to Draft

**`POST /api/v1/allocation/request/:id/reset`**

Resets an allocation request back to `draft` state. Only works for `rejected` or `approved` requests.

#### Path Parameters

| Param | Type | Description |
|---|---|---|
| `id` | int | Allocation request ID |

#### cURL

```bash
curl -X POST 'YOUR_BASE_URL/api/v1/allocation/request/1/reset' \
  -H 'token: YOUR_TOKEN'
```

#### Success Response (200)

```json
{
  "message": "Allocation request reset to draft",
  "data": {
    "id": 1,
    "name": "AR-0001",
    "state": "draft"
  }
}
```

#### Error Responses

| Status | Condition |
|---|---|
| 400 | State is not `rejected` or `approved` |
| 404 | Request not found |

---

## Quick Reference

| # | Endpoint | Method | Role Restriction |
|---|---|---|---|
| 1 | `/api/v2/employees` | POST | - |
| 2 | `/api/v2/employees/bulk` | POST | - |
| 3 | `/api/v1/employees/:id` | PUT / PATCH | - |
| 4 | `/api/v1/employees/:id` | GET | - |
| 5 | `/api/v1/employees` | GET | - |
| 6 | `/api/v1/employees/:id` | DELETE | - |
| 7 | `/api/v1/employees/:id/offboard` | POST | - |
| 8 | `/api/v1/allocation/request` | POST | - |
| 9 | `/api/v1/allocation/request/:id/submit` | POST | - |
| 10 | `/api/v1/allocation/request/approve` | POST | **CTO only** |
| 11 | `/api/v1/allocation/request/reject` | POST | **CTO only** |
| 12 | `/api/v1/allocation/request` | GET | **CTO only** |
| 13 | `/api/v1/allocation/requests` | GET | - |
| 14 | `/api/v1/allocation/request/:id/reset` | POST | - |

---

## Allocation Request State Flow

```
draft --> submitted --> approved
                  \--> rejected

approved / rejected --> draft (via reset)
```

---

## Common Error Response Format

```json
{
  "message": "Error description here",
  "status": 400
}
```

| Status Code | Meaning |
|---|---|
| 200 | Success |
| 400 | Bad request / Validation error |
| 403 | Insufficient permissions |
| 404 | Resource not found |

---

## Self-Registration (Public, OTP-gated)

These endpoints are **public** (`auth='public'`, no token) and power the
employee/candidate self sign-up screen. Registration is a **two-step** flow:

```
1. POST /api/v2/employees/send_otp      -> emails a 6-digit code
2. POST /api/v2/employees/self_register -> submit details + the code
```

The code is verified against the **login email** of the account being created:
- **employee** -> the `@ethara.ai` **work email**
- **candidate** -> the **personal email**

> Policy: 6-digit code, valid **10 minutes**, max **5** verify attempts, **60s**
> resend cooldown. Requesting a new code invalidates the previous one. Expired
> codes are purged daily by the `Registration OTP: Purge Expired Codes` cron.

---

### S1. Send Registration OTP

**`POST /api/v2/employees/send_otp`**

Generates a one-time code, stores `(email, code, expiry)`, and emails the code
to `email`. Send this to the same address you will register with.

#### Request Body

| Field | Type | Required | Notes |
|---|---|---|---|
| `email` | email | Yes | Employee: `@ethara.ai` work email. Candidate: personal email. |

#### cURL

```bash
curl -X POST 'YOUR_BASE_URL/api/v2/employees/send_otp' \
  -H 'Content-Type: application/json' \
  -d '{ "email": "john.doe@ethara.ai" }'
```

#### Success Response (200)

```json
{
  "message": "A verification code has been sent to john.doe@ethara.ai. It is valid for 10 minutes.",
  "email": "john.doe@ethara.ai",
  "expires_in_seconds": 600,
  "status_code": 200
}
```

#### Error Responses

| Status | Condition |
|---|---|
| 400 | `email` missing or not a valid email |
| 429 | Resend requested before the 60-second cooldown elapsed |
| 502 | Email could not be delivered (check SMTP / the address) |

---

### S2. Self-Register (with OTP)

**`POST /api/v2/employees/self_register`**

Creates the employee/candidate account **only if** the supplied `otp` matches
the code sent to that account's login email. All previously-required fields are
unchanged; the single new field is **`otp`**.

#### New / Relevant Fields

| Field | Type | Required | Notes |
|---|---|---|---|
| `type` | string | Yes | `employee` or `candidate` |
| `otp` | string | Yes | 6-digit code from `send_otp` |
| `work_email` | email | employee | Code is verified against this for `type=employee` |
| `personal_email` | email | candidate | Code is verified against this for `type=candidate` |
| ... | | | (all other existing registration fields) |

#### cURL (employee, abridged)

```bash
curl -X POST 'YOUR_BASE_URL/api/v2/employees/self_register' \
  -H 'Content-Type: application/json' \
  -d '{
    "type": "employee",
    "otp": "482913",
    "name": "John Doe",
    "work_email": "john.doe@ethara.ai",
    "personal_email": "john@gmail.com",
    "password": "Secret123",
    "confirm_password": "Secret123",
    "...": "gender, phone, department_id, designation_id, aadhaar_number, birthday, aadhaar_file_base64, ..."
  }'
```

#### OTP-related Error Responses (400)

| Message | Condition |
|---|---|
| `OTP is required. Please verify your email first.` | `otp` missing |
| `No OTP found for this email. Please request a new one.` | No code was sent (or it was purged) |
| `Invalid OTP. N attempt(s) remaining.` | Wrong code |
| `Too many incorrect attempts. Please request a new OTP.` | > 5 wrong tries |
| `OTP has expired. Please request a new one.` | Code older than 10 minutes |

> A successful OTP check does **not** consume the code — if a later step fails
> (Aadhaar mismatch, duplicate account, ...) you can resubmit with the same code
> while it is still valid.
