# Employee Onboarding API — Frontend Integration Guide

This document is the single source of truth for integrating the **6-step Employee Onboarding form** with the backend. Hand it to a frontend developer (or paste it into an LLM) and they should have everything they need to build the UI end-to-end.

---

## 1. Overview

The onboarding form has **6 steps** matching the design screens:

| Step | Title | What it captures |
|------|-------|------------------|
| 1 | **Employment** | Employee code, name, department, designation, DOB, gender, contact number, blood group, personal/official email, resume + passport photo |
| 2 | **Family** | Marital status, kids flag, parents' names/DOBs, emergency contact details |
| 3 | **Education** | 10th, 12th/Diploma, highest qualification with score type/value + 3 certificate uploads |
| 4 | **Identity** | Aadhaar, PAN, UAN + Aadhaar/PAN card uploads |
| 5 | **Bank** | Savings/salary flags, account/IFSC/bank name + cancelled cheque upload |
| 6 | **Address** | Current + permanent address + address proof uploads |

All file uploads go to S3 — only the resulting public CDN URL is stored. The same single endpoint handles **create** *and* **update** *and* **per-step save** — driven by whether `employee_id` is included in the request.

---

## 2. Base URL & Auth Header

### Base URL
```
https://projects-stage.ethara.ai
```

### Auth header
Every onboarding API call requires the access token you already received from your existing login flow:

```
access_token: <token>
```

> ⚠️ It is a **custom header named `access_token`**, NOT `Authorization: Bearer ...`.

---

## 3. The `onboarding_completed` flag on login

Your existing login response now includes a new boolean key under `data`:

```json
{
  "status_code": 200,
  "data": {
    "uid": 42,
    "access_token": "...",
    "...other existing keys...": "...",
    "onboarding_completed": false
  }
}
```

| Value | Meaning | Frontend action |
|-------|---------|-----------------|
| `true`  | Every required field and document is filled. | **Do NOT** show the onboarding popup. |
| `false` | One or more required fields/documents are missing. | **Show** the onboarding popup that opens the form. |

**What counts as "completed"** (server checks every login, no caching):
- All starred fields across Steps 1-6 are non-empty: `employee_code, name, department_id, designation_id, birthday, sex, private_phone, blood_group, private_email, work_email, marital, father_name, father_dob, mother_name, mother_dob, emergency_contact, emergency_phone, emergency_contact_relation, tenth_score_type, tenth_score, twelfth_score_type, twelfth_score, highest_qualification, highest_qualification_score_type, highest_qualification_score, aadhaar_number, pan_number, bank_account_number, bank_name, bank_ifsc_code, current_address, permanent_address`.
- If `has_uan` is `true`, `uan_number` must also be set.
- These document slots are uploaded: `resume, passport_photo, tenth_marksheet, twelfth_marksheet, highest_qualification, aadhaar_card, pan_card, cancelled_cheque, permanent_address_proof`.
  - `current_address_proof` is **optional** (the form labels it optional when current address matches permanent).

**Edge cases**:
- A user **without** an employee record (e.g. system admin) → `onboarding_completed: true` (no popup).
- A user whose employee record exists but is empty → `onboarding_completed: false` (popup shown).

```javascript
const { data } = await login(email, password);
if (data.data.onboarding_completed === false) {
  showOnboardingPopup();   // route the user to /onboarding
}
```

---

## 4. Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/api/v2/employee-onboarding/submit` | Create OR update onboarding details + upload documents |
| `GET`  | `/api/v2/employee-onboarding/<employee_id>` | Read the full onboarding payload |
| `GET`  | `/api/v2/employee-onboarding/<employee_id>/documents` | Read only the uploaded documents list |

All three require the `access_token` header.

---

## 5. `POST /api/v2/employee-onboarding/submit`

### Request

| Aspect | Value |
|--------|-------|
| Method | `POST` |
| URL | `https://projects-stage.ethara.ai/api/v2/employee-onboarding/submit` |
| Content-Type | `multipart/form-data` |
| Auth header | `access_token: <token>` |
| Body | Flat form fields + file fields (no nested JSON) |

> ❗ **Do NOT manually set `Content-Type`** in `fetch`/`axios`. The browser must set it automatically so the multipart boundary is correct.

### Control fields (top-level)

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `employee_id` | int | Optional | Omit for create. Include to update an existing employee. |
| `step` | int (1–6) | Optional | Tracks user progress. The server only advances `onboarding_step` forward, never backward. |
| `final_submit` | bool | Optional | When `true`, sets `onboarding_status = "submitted"` and `onboarding_step = 6`. Send on the last step. |

### Data fields (all optional except `name` on create)

#### Step 1 — Employment
| Field | Type | Validation | Notes |
|-------|------|-----------|-------|
| `employee_code` | string | unique per employee | e.g. `GRP065` |
| `name` | string | **required when creating** | Full name |
| `department_id` | int | must exist in `hr.department` | |
| `designation_id` | int | must exist in `hr.employee.designation` | |
| `date_of_birth` | date | `YYYY-MM-DD` | |
| `gender` | enum | `male` / `female` / `other` | |
| `contact_number` | string | | Stored on `private_phone` |
| `blood_group` | enum | see Allowed Values | |
| `personal_email` | string | email format | Stored on `private_email` |
| `official_email` | string | email format | Stored on `work_email` |

#### Step 2 — Family
| Field | Type | Notes |
|-------|------|-------|
| `marital_status` | enum | see Allowed Values |
| `has_kids` | bool | `true` / `false` (also accepts `yes`/`no`/`1`/`0`) |
| `father_name` | string | |
| `father_dob` | date | `YYYY-MM-DD` |
| `mother_name` | string | |
| `mother_dob` | date | `YYYY-MM-DD` |
| `emergency_contact_name` | string | |
| `emergency_contact_number` | string | |
| `emergency_contact_relation` | string | e.g. `Father`, `Spouse` |

#### Step 3 — Education
| Field | Type | Notes |
|-------|------|-------|
| `tenth_score_type` | enum | `percentage` / `cgpa` / `grade` |
| `tenth_score` | string | up to 5 chars, e.g. `85`, `9.06`, `A+` |
| `twelfth_score_type` | enum | same enum |
| `twelfth_score` | string | |
| `highest_qualification` | enum | see Allowed Values |
| `highest_qualification_score_type` | enum | same enum |
| `highest_qualification_score` | string | |

#### Step 4 — Identity
| Field | Type | Validation |
|-------|------|-----------|
| `aadhaar_number` | string | **12 digits, numeric** |
| `pan_number` | string | **pattern `AAAAA9999A`** (5 letters, 4 digits, 1 letter) |
| `has_uan` | bool | |
| `uan_number` | string | **12 digits, numeric** |

#### Step 5 — Bank
| Field | Type | Notes |
|-------|------|-------|
| `has_savings_account` | bool | |
| `has_salary_account` | bool | |
| `bank_account_number` | string | |
| `bank_name` | string | |
| `ifsc_code` | string | Uppercased automatically |

#### Step 6 — Address
| Field | Type | Notes |
|-------|------|-------|
| `current_address` | string | Multiline allowed |
| `permanent_address` | string | Multiline allowed |
| `current_same_as_permanent` | bool | If `true`, the UI should hide the current-address proof slot |

### File fields (each optional)

| Field name | Document slot | Accepted types (recommendation) |
|------------|---------------|--------------------------------|
| `resume` | Resume | PDF, DOC, DOCX |
| `passport_photo` | Passport Size Photo | JPG, PNG, WEBP |
| `tenth_marksheet` | 10th Marksheet / Certificate | PDF, JPG, PNG |
| `twelfth_marksheet` | 12th / Diploma Marksheet / Certificate | PDF, JPG, PNG |
| `highest_qualification` | Highest Qualification Certificate | PDF, JPG, PNG |
| `aadhaar_card` | Aadhaar Card | PDF, JPG, PNG |
| `pan_card` | PAN Card | PDF, JPG, PNG |
| `cancelled_cheque` | Cancelled Cheque / Passbook | PDF, JPG, PNG |
| `permanent_address_proof` | Permanent Address Proof | PDF, JPG, PNG |
| `current_address_proof` | Current Address Proof | PDF, JPG, PNG |

**Rules**:
- Field name must exactly equal one of the slots above. Unknown file fields are silently ignored.
- Re-uploading the same slot replaces the previously stored URL (only one record exists per `(employee_id, document_type)`).
- The browser sends the original filename; it's persisted as `file_name` for display.

### Allowed values (enums)

```js
const BLOOD_GROUP = [
  { value: "a_pos",  label: "A+"  },
  { value: "a_neg",  label: "A-"  },
  { value: "b_pos",  label: "B+"  },
  { value: "b_neg",  label: "B-"  },
  { value: "ab_pos", label: "AB+" },
  { value: "ab_neg", label: "AB-" },
  { value: "o_pos",  label: "O+"  },
  { value: "o_neg",  label: "O-"  },
];

const GENDER = [
  { value: "male",   label: "Male"   },
  { value: "female", label: "Female" },
  { value: "other",  label: "Other"  },
];

const MARITAL_STATUS = [
  { value: "single",     label: "Single"            },
  { value: "married",    label: "Married"           },
  { value: "cohabitant", label: "Legal Cohabitant"  },
  { value: "widower",    label: "Widower"           },
  { value: "divorced",   label: "Divorced"          },
];

const SCORE_TYPE = [
  { value: "percentage", label: "Percentage" },
  { value: "cgpa",       label: "CGPA"       },
  { value: "grade",      label: "Grade"      },
];

const QUALIFICATION = [
  { value: "high_school",   label: "High School"        },
  { value: "intermediate",  label: "Intermediate / 12th" },
  { value: "diploma",       label: "Diploma"            },
  { value: "bachelors",     label: "Bachelor's Degree"  },
  { value: "masters",       label: "Master's Degree"    },
  { value: "phd",           label: "PhD"                },
  { value: "other",         label: "Other"              },
];

const ONBOARDING_STATUS = [
  { value: "draft",     label: "Draft"     },
  { value: "submitted", label: "Submitted" },
];
```

### Success response — 200

```json
{
  "message": "Onboarding details saved successfully.",
  "errors": [],
  "status_code": 200,
  "employee_id": 42,
  "onboarding_status": "draft",
  "onboarding_step": 3,
  "uploaded_documents": [
    {
      "id": 17,
      "document_type": "tenth_marksheet",
      "document_label": "10th Marksheet / Certificate",
      "file_name": "10th_marksheet.pdf",
      "file_url": "https://cdn.ethara.ai/employee_onboarding/42/tenth_marksheet/1718627812345678901_a3f9c12_10th_marksheet.pdf",
      "uploaded_at": "2026-06-17T07:35:12"
    }
  ]
}
```

### Partial success response — 200 (some document uploads failed)

```json
{
  "message": "Onboarding saved with some document errors.",
  "errors": [],
  "status_code": 200,
  "employee_id": 42,
  "onboarding_status": "draft",
  "onboarding_step": 3,
  "uploaded_documents": [
    { "id": 17, "document_type": "tenth_marksheet", "file_url": "...", "file_name": "10th.pdf", "uploaded_at": "..." }
  ],
  "document_errors": [
    { "document_type": "twelfth_marksheet", "error": "Uploaded file is empty." },
    { "document_type": "aadhaar_card",      "error": "No S3 connector is configured." }
  ]
}
```

> The employee record is still saved. The frontend should surface the per-document errors next to each failed slot so the user can retry.

### Error responses

**400 — Validation failure (e.g. invalid Aadhaar/PAN/UAN, or no name on create)**
```json
{
  "message": "Aadhaar Number must be exactly 12 digits.",
  "errors": [],
  "status_code": 400
}
```
```json
{
  "message": "'name' is required to create a new employee.",
  "errors": [],
  "status_code": 400
}
```

**401 — Missing or expired token**
```json
{
  "message": "missing access token in request header",
  "errors": [],
  "status_code": 401
}
```
```json
{
  "message": "token seems to have expired or invalid",
  "errors": [],
  "status_code": 401
}
```

**404 — Employee not found (when `employee_id` is sent)**
```json
{
  "message": "Employee 42 not found.",
  "errors": [],
  "status_code": 404
}
```

---

## 6. `GET /api/v2/employee-onboarding/<employee_id>`

Returns the **entire onboarding payload** grouped by step, plus every uploaded document.

### Request
```http
GET https://projects-stage.ethara.ai/api/v2/employee-onboarding/42
access_token: <token>
```

### Success response — 200

```json
{
  "message": "Employee onboarding details fetched successfully.",
  "errors": [],
  "status_code": 200,
  "employee_id": 42,
  "onboarding": {
    "status": "submitted",
    "step": 6,
    "submitted_at": "2026-06-17T08:12:54"
  },
  "employment": {
    "employee_code": "GRP065",
    "name": "Daksh Pathak",
    "department_id": 3,
    "department_name": "Engineering",
    "designation_id": 7,
    "designation_name": "Jr. Flutter Developer",
    "date_of_birth": "2002-05-07",
    "gender": "male",
    "contact_number": "7000472097",
    "blood_group": "ab_pos",
    "personal_email": "daksh.pathak@ethara.ai",
    "official_email": "daksh.pathak@ethara.ai"
  },
  "family": {
    "marital_status": "single",
    "has_kids": false,
    "father_name": "Rajendra Pathak",
    "father_dob": "1965-09-28",
    "mother_name": "Sadhana Pathak",
    "mother_dob": "1971-09-07",
    "emergency_contact_name": "Rajendra Pathak",
    "emergency_contact_number": "8319173103",
    "emergency_contact_relation": "Father"
  },
  "education": {
    "tenth_score_type": "percentage",
    "tenth_score": "85",
    "twelfth_score_type": "percentage",
    "twelfth_score": "72",
    "highest_qualification": "bachelors",
    "highest_qualification_score_type": "cgpa",
    "highest_qualification_score": "9.06"
  },
  "identity": {
    "aadhaar_number": "234512341234",
    "pan_number": "ABCDE1234F",
    "has_uan": true,
    "uan_number": "021004365428"
  },
  "bank": {
    "has_savings_account": true,
    "has_salary_account": true,
    "bank_account_number": "50100628284343",
    "bank_name": "HDFC Bank",
    "ifsc_code": "HDFC0009462"
  },
  "address": {
    "current_address": "The park residency, sector 22, Gurgaon",
    "permanent_address": "VTC Ratlam, PO Ratlam, MP, 457001",
    "current_same_as_permanent": false
  },
  "documents": [
    { "id": 11, "document_type": "aadhaar_card",          "document_label": "Aadhaar Card",                           "file_name": "aadhaar.pdf",  "file_url": "https://cdn.../aadhaar_card/...pdf",          "uploaded_at": "2026-06-17T07:32:00" },
    { "id": 14, "document_type": "cancelled_cheque",      "document_label": "Cancelled Cheque / Passbook",            "file_name": "cheque.jpg",   "file_url": "https://cdn.../cancelled_cheque/...jpg",      "uploaded_at": "2026-06-17T07:51:00" },
    { "id": 16, "document_type": "current_address_proof", "document_label": "Current Address Proof",                  "file_name": "curr.pdf",     "file_url": "https://cdn.../current_address_proof/...pdf", "uploaded_at": "2026-06-17T08:10:00" },
    { "id": 19, "document_type": "highest_qualification", "document_label": "Highest Qualification Certificate",      "file_name": "degree.pdf",   "file_url": "https://cdn.../highest_qualification/...pdf", "uploaded_at": "2026-06-17T07:25:00" },
    { "id": 12, "document_type": "pan_card",              "document_label": "PAN Card",                                "file_name": "pan.jpg",      "file_url": "https://cdn.../pan_card/...jpg",              "uploaded_at": "2026-06-17T07:32:30" },
    { "id": 15, "document_type": "passport_photo",        "document_label": "Passport Size Photo",                    "file_name": "photo.jpg",    "file_url": "https://cdn.../passport_photo/...jpg",        "uploaded_at": "2026-06-17T07:10:00" },
    { "id": 18, "document_type": "permanent_address_proof","document_label": "Permanent Address Proof",               "file_name": "perm.pdf",     "file_url": "https://cdn.../permanent_address_proof/...pdf","uploaded_at": "2026-06-17T08:09:00" },
    { "id": 10, "document_type": "resume",                "document_label": "Resume",                                 "file_name": "Daksh.pdf",    "file_url": "https://cdn.../resume/...pdf",                "uploaded_at": "2026-06-17T07:09:00" },
    { "id": 13, "document_type": "tenth_marksheet",       "document_label": "10th Marksheet / Certificate",           "file_name": "10th.pdf",     "file_url": "https://cdn.../tenth_marksheet/...pdf",       "uploaded_at": "2026-06-17T07:22:00" },
    { "id": 20, "document_type": "twelfth_marksheet",     "document_label": "12th / Diploma Marksheet / Certificate", "file_name": "12th.pdf",     "file_url": "https://cdn.../twelfth_marksheet/...pdf",     "uploaded_at": "2026-06-17T07:23:30" }
  ],
  "documents_by_type": {
    "resume":                  { "id": 10, "document_type": "resume",                 "file_url": "https://cdn.../resume/...pdf",                 "file_name": "Daksh.pdf",   "uploaded_at": "2026-06-17T07:09:00", "document_label": "Resume" },
    "passport_photo":          { "id": 15, "document_type": "passport_photo",         "file_url": "https://cdn.../passport_photo/...jpg",         "file_name": "photo.jpg",   "uploaded_at": "2026-06-17T07:10:00", "document_label": "Passport Size Photo" },
    "tenth_marksheet":         { "id": 13, "document_type": "tenth_marksheet",        "file_url": "https://cdn.../tenth_marksheet/...pdf",        "file_name": "10th.pdf",    "uploaded_at": "2026-06-17T07:22:00", "document_label": "10th Marksheet / Certificate" },
    "twelfth_marksheet":       { "id": 20, "document_type": "twelfth_marksheet",      "file_url": "https://cdn.../twelfth_marksheet/...pdf",      "file_name": "12th.pdf",    "uploaded_at": "2026-06-17T07:23:30", "document_label": "12th / Diploma Marksheet / Certificate" },
    "highest_qualification":   { "id": 19, "document_type": "highest_qualification",  "file_url": "https://cdn.../highest_qualification/...pdf",  "file_name": "degree.pdf",  "uploaded_at": "2026-06-17T07:25:00", "document_label": "Highest Qualification Certificate" },
    "aadhaar_card":            { "id": 11, "document_type": "aadhaar_card",           "file_url": "https://cdn.../aadhaar_card/...pdf",           "file_name": "aadhaar.pdf", "uploaded_at": "2026-06-17T07:32:00", "document_label": "Aadhaar Card" },
    "pan_card":                { "id": 12, "document_type": "pan_card",               "file_url": "https://cdn.../pan_card/...jpg",               "file_name": "pan.jpg",     "uploaded_at": "2026-06-17T07:32:30", "document_label": "PAN Card" },
    "cancelled_cheque":        { "id": 14, "document_type": "cancelled_cheque",       "file_url": "https://cdn.../cancelled_cheque/...jpg",       "file_name": "cheque.jpg",  "uploaded_at": "2026-06-17T07:51:00", "document_label": "Cancelled Cheque / Passbook" },
    "permanent_address_proof": { "id": 18, "document_type": "permanent_address_proof","file_url": "https://cdn.../permanent_address_proof/...pdf","file_name": "perm.pdf",    "uploaded_at": "2026-06-17T08:09:00", "document_label": "Permanent Address Proof" },
    "current_address_proof":   { "id": 16, "document_type": "current_address_proof",  "file_url": "https://cdn.../current_address_proof/...pdf",  "file_name": "curr.pdf",    "uploaded_at": "2026-06-17T08:10:00", "document_label": "Current Address Proof" }
  }
}
```

> Use `documents_by_type` as a fast lookup map keyed by slot name. The `documents` array is the same data sorted alphabetically for easy iteration in a table.

Fields not yet filled in are `null`. Booleans for un-set checkbox fields default to `false`.

### Error responses

**404 — Employee not found**
```json
{
  "message": "Employee 42 not found.",
  "errors": [],
  "status_code": 404
}
```

---

## 7. `GET /api/v2/employee-onboarding/<employee_id>/documents`

Lighter endpoint — returns only the documents. Use this when only the document list is needed (e.g. a "My Documents" tab).

### Request
```http
GET https://projects-stage.ethara.ai/api/v2/employee-onboarding/42/documents
access_token: <token>
```

### Success response — 200
```json
{
  "message": "Employee documents fetched successfully.",
  "errors": [],
  "status_code": 200,
  "employee_id": 42,
  "employee_name": "Daksh Pathak",
  "document_count": 3,
  "documents": [
    {
      "id": 10,
      "document_type": "resume",
      "document_label": "Resume",
      "file_name": "Daksh.pdf",
      "file_url": "https://cdn.ethara.ai/employee_onboarding/42/resume/...pdf",
      "uploaded_at": "2026-06-17T07:09:00"
    },
    {
      "id": 11,
      "document_type": "aadhaar_card",
      "document_label": "Aadhaar Card",
      "file_name": "aadhaar.pdf",
      "file_url": "https://cdn.ethara.ai/employee_onboarding/42/aadhaar_card/...pdf",
      "uploaded_at": "2026-06-17T07:32:00"
    },
    {
      "id": 12,
      "document_type": "pan_card",
      "document_label": "PAN Card",
      "file_name": "pan.jpg",
      "file_url": "https://cdn.ethara.ai/employee_onboarding/42/pan_card/...jpg",
      "uploaded_at": "2026-06-17T07:32:30"
    }
  ]
}
```

### Error responses

**404 — Employee not found**
```json
{
  "message": "Employee 42 not found.",
  "errors": [],
  "status_code": 404
}
```

---

## 8. Recommended UI flow

```
Login → response.data.onboarding_completed === false ? show popup : skip

If user opens the form:

[Step 1] User fills employment + uploads resume/photo
   └─> POST /submit (no employee_id)  → response has employee_id (e.g. 42)
         Store employee_id in state / localStorage.

[Step 2] User fills family
   └─> POST /submit (employee_id=42, step=2)

[Step 3] User fills education + uploads 3 docs
   └─> POST /submit (employee_id=42, step=3)

[Step 4] User fills identity + uploads aadhaar/pan
   └─> POST /submit (employee_id=42, step=4)

[Step 5] User fills bank + uploads cheque
   └─> POST /submit (employee_id=42, step=5)

[Step 6] User fills address + uploads proofs + clicks "Submit"
   └─> POST /submit (employee_id=42, step=6, final_submit=true)
         Response onboarding_status="submitted".

[Review screen] Show all data + downloadable docs
   └─> GET /api/v2/employee-onboarding/42
```

Calling `POST /submit` with **only** the fields the user filled on that step is the recommended pattern — every field is independently nullable and any subset is valid.

---

## 9. Sample integrations

### Vanilla `fetch` (browser)

```javascript
const BASE_URL = "https://projects-stage.ethara.ai";

async function submitOnboardingStep({
  token,
  employeeId,
  step,
  finalSubmit = false,
  fields = {},   // flat object: { name, employee_code, ... }
  files = {},    // { resume: File, passport_photo: File, ... }
}) {
  const form = new FormData();
  if (employeeId) form.append("employee_id", employeeId);
  if (step) form.append("step", String(step));
  if (finalSubmit) form.append("final_submit", "true");

  Object.entries(fields).forEach(([k, v]) => {
    if (v !== undefined && v !== null && v !== "") form.append(k, String(v));
  });
  Object.entries(files).forEach(([slot, file]) => {
    if (file) form.append(slot, file, file.name);
  });

  const res = await fetch(`${BASE_URL}/api/v2/employee-onboarding/submit`, {
    method: "POST",
    headers: { access_token: token },   // DON'T set Content-Type
    body: form,
  });
  const json = await res.json();
  if (json.status_code !== 200) throw new Error(json.message);
  return json;
}
```

### `axios`

```javascript
import axios from "axios";
const BASE_URL = "https://projects-stage.ethara.ai";

export async function submitOnboarding({ token, employeeId, step, finalSubmit, fields, files }) {
  const form = new FormData();
  if (employeeId) form.append("employee_id", employeeId);
  if (step) form.append("step", step);
  if (finalSubmit) form.append("final_submit", "true");

  Object.entries(fields).forEach(([k, v]) => {
    if (v !== undefined && v !== null && v !== "") form.append(k, v);
  });
  Object.entries(files).forEach(([slot, file]) => file && form.append(slot, file));

  const { data } = await axios.post(
    `${BASE_URL}/api/v2/employee-onboarding/submit`,
    form,
    { headers: { access_token: token } } // axios auto-detects multipart from FormData
  );
  return data;
}

export async function getOnboarding({ token, employeeId }) {
  const { data } = await axios.get(
    `${BASE_URL}/api/v2/employee-onboarding/${employeeId}`,
    { headers: { access_token: token } }
  );
  return data;
}

export async function getOnboardingDocuments({ token, employeeId }) {
  const { data } = await axios.get(
    `${BASE_URL}/api/v2/employee-onboarding/${employeeId}/documents`,
    { headers: { access_token: token } }
  );
  return data;
}
```

### Example — Step 1 (create) with axios

```javascript
const res = await submitOnboarding({
  token: accessToken,
  step: 1,
  fields: {
    employee_code: "GRP065",
    name: "Daksh Pathak",
    department_id: 3,
    designation_id: 7,
    date_of_birth: "2002-05-07",
    gender: "male",
    contact_number: "7000472097",
    blood_group: "ab_pos",
    personal_email: "daksh.pathak@ethara.ai",
    official_email: "daksh.pathak@ethara.ai",
  },
  files: {
    resume: resumeFileInputRef.current.files[0],
    passport_photo: photoFileInputRef.current.files[0],
  },
});
const employeeId = res.employee_id;   // save for next steps
```

### Example — Step 6 (final submit)

```javascript
await submitOnboarding({
  token: accessToken,
  employeeId,
  step: 6,
  finalSubmit: true,
  fields: {
    current_address: "The park residency, sector 22, Gurgaon",
    permanent_address: "VTC Ratlam, PO Ratlam, MP, 457001",
    current_same_as_permanent: false,
  },
  files: {
    permanent_address_proof: permFileInputRef.current.files[0],
    current_address_proof: currFileInputRef.current.files[0],
  },
});
```

---

## 10. cURL reference

```bash
HOST="https://projects-stage.ethara.ai"
TOKEN="<your access_token>"

# Create employee (Step 1) with two files
curl -X POST "$HOST/api/v2/employee-onboarding/submit" \
  -H "access_token: $TOKEN" \
  -F "step=1" \
  -F "employee_code=GRP065" \
  -F "name=Daksh Pathak" \
  -F "date_of_birth=2002-05-07" \
  -F "gender=male" \
  -F "blood_group=ab_pos" \
  -F "personal_email=daksh.pathak@ethara.ai" \
  -F "official_email=daksh.pathak@ethara.ai" \
  -F "contact_number=7000472097" \
  -F "resume=@/path/to/resume.pdf" \
  -F "passport_photo=@/path/to/photo.jpg"

# Update an existing employee (Step 4) with documents
curl -X POST "$HOST/api/v2/employee-onboarding/submit" \
  -H "access_token: $TOKEN" \
  -F "employee_id=42" \
  -F "step=4" \
  -F "aadhaar_number=234512341234" \
  -F "pan_number=ABCDE1234F" \
  -F "has_uan=true" \
  -F "uan_number=021004365428" \
  -F "aadhaar_card=@/path/to/aadhaar.pdf" \
  -F "pan_card=@/path/to/pan.jpg"

# Final submit (Step 6)
curl -X POST "$HOST/api/v2/employee-onboarding/submit" \
  -H "access_token: $TOKEN" \
  -F "employee_id=42" -F "step=6" -F "final_submit=true" \
  -F "current_address=..." -F "permanent_address=..." \
  -F "permanent_address_proof=@/path/to/perm.pdf"

# GET full payload
curl "$HOST/api/v2/employee-onboarding/42" -H "access_token: $TOKEN"

# GET just the docs
curl "$HOST/api/v2/employee-onboarding/42/documents" -H "access_token: $TOKEN"
```

---

## 11. Validation rules summary

| Field | Rule |
|-------|------|
| `aadhaar_number` | Exactly 12 digits, numeric only |
| `pan_number` | Pattern `^[A-Z]{5}[0-9]{4}[A-Z]$` — auto-uppercased server-side |
| `uan_number` | Exactly 12 digits, numeric only |
| `ifsc_code` | Auto-uppercased server-side |
| `personal_email`, `official_email` | Stored as lowercase |
| `employee_code` | Unique per employee |
| `name` | Required when creating (no `employee_id`) |
| Booleans | Accept `true`/`false`, `yes`/`no`, `1`/`0`, `on`/`off` (case-insensitive) |
| Dates | `YYYY-MM-DD` |

If a validation check fails the request returns **HTTP 400** with `message` describing the issue. Surface `message` to the user.

---

## 12. Gotchas / FAQ

**Q. Why does my fetch call return 400 with "missing access token"?**
You set `Authorization: Bearer ...` instead of the `access_token` header. The backend reads `access_token` only.

**Q. Browser sets `Content-Type` to `text/plain` and the server can't read fields.**
You explicitly set the `Content-Type` header — remove it. Let the browser auto-set the multipart boundary.

**Q. Re-uploading a document — does it create a duplicate?**
No. The `(employee_id, document_type)` pair is unique. Re-upload **replaces** the previous URL.

**Q. How do I delete a document?**
Not exposed via API yet. If needed, ask the backend team to add `DELETE /api/v2/employee-onboarding/<id>/documents/<document_type>`.

**Q. Can I send some fields as JSON instead of form-data?**
No. The endpoint only parses `multipart/form-data`. Booleans, ints, dates — all go as form strings.

**Q. What's the max file size?**
Driven by Odoo's `--limit-request` / nginx config — usually around 50 MB. For onboarding docs that's plenty.

**Q. Can the same user fill the form for someone else?**
Yes — any authenticated HR user can call `/submit` for any `employee_id`. The token determines who is *making* the call, not who is being onboarded.

**Q. What if S3 is down or not configured?**
The non-file fields still save. The response includes `document_errors` listing each slot that couldn't be uploaded. Retry that slot later.

**Q. How often does `onboarding_completed` refresh on login?**
Every time — it's computed fresh on every `/api/v1/auth_token` call, no caching.

---

## 13. Quick checklist for frontend integration

- [ ] On every login, check `data.onboarding_completed`. Show popup when `false`.
- [ ] Token sent as `access_token` header on every onboarding call.
- [ ] Don't manually set `Content-Type` — let the browser do it for multipart.
- [ ] Save `employee_id` after Step 1 and reuse it for Steps 2-6.
- [ ] Send `step=N` on every save so the backend tracks progress.
- [ ] Send `final_submit=true` only on Step 6 — switches status to `submitted`.
- [ ] On success, parse `uploaded_documents` to show "Uploaded" badges per slot.
- [ ] Handle `document_errors` in the partial-success response — show per-slot retry.
- [ ] Display backend `message` to the user on 400/401/404.
- [ ] Booleans serialize as `"true"` / `"false"` strings — both work.
- [ ] Dates serialize as `YYYY-MM-DD` strings.
- [ ] When `current_same_as_permanent` is `true`, skip the current-address proof slot UI.
