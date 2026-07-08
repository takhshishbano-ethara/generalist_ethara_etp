# greytHR Connector

Two-way connector between Odoo and the greytHR HRMS.

## Features

- **Instance model** (`greythr.instance`) with credentials, test connection and per-source sync buttons.
- **Employee sync (inbound)** — mirrors greytHR employees into `greythr.employee` and links them to `hr.employee` by matching `employee_code`.
- **Employee sync (outbound)** — when `Push New Employees to greytHR` is on, any `hr.employee` created in Odoo is automatically created in greytHR via this instance.
- **Leave type sync** — pulls greytHR leave type catalog and links to `hr.leave.type` via `ethara_leave_code`.
- **Leave balance sync** — per employee, per leave type, per year with opening / granted / availed / applied / lapsed / deducted / encashed / current balance.
- **Leave transaction sync** — every leave credit, debit, opening, closing, encash, lapse.
- **Leave requests (bidirectional)** — new `greythr.leave.request` model. Inbound pulls open/pending requests from greytHR; outbound pushes creates, approvals and refusals of `hr.leave` when `Push Leave Approvals/Refusals to greytHR` is on.
- **Payroll sync (opt-in)** — new `greythr.payroll` model. When `Enable Payroll Sync` is on, pulls monthly payslip totals (gross, net, earnings, deductions, tax, LOP).
- **Daily cron** — runs employees + leave types + balances + transactions + leave requests on every active instance; payroll runs only when the instance flag is on.
- **`hr.employee` extension** — greytHR tab shows balances, transactions, leave requests and payroll for the employee.

## Setup

1. Install the module. Depends on `hr`, `hr_holidays`, `mail`, `ethara_hrms_extension`, and the Python package `requests`.
2. Go to **greytHR → Configuration → Instances**, create an instance.
3. Fill the connection block — see credential mapping below.
4. Click **Test Connection**.
5. Click **Sync All** (or individual sync buttons) — or wait for the daily cron.

## Credential mapping

The instance form has one credential block that covers both greytHR auth flavors.

### Standard configuration (all tenants)

Per the greytHR API v2 docs, admin-generated API credentials use OAuth2 Basic auth against `/uas/v1/oauth2/client-token` on `api.greythr.com`. The tenant slug identifies the company via the `x-greythr-domain` header on every request.

| Field | Value |
|---|---|
| Base URL | `https://api.greythr.com` |
| Company Domain | your greytHR tenant slug (sent as `x-greythr-domain`) |
| Client ID / API Username | client id (or admin API username) |
| Client Secret / API Password | client secret (or admin API password) |
| Auth Mode | `OAuth2 Basic (client_credentials)` |
| Auth Endpoint | `/uas/v1/oauth2/client-token` |

### Example — sandbox credentials

| Field | Value |
|---|---|
| Base URL | `https://api.greythr.com` |
| Company Domain | `tousifapisso` |
| Client ID / API Username | `Newapiuser` |
| Client Secret / API Password | `5ffc80f8-3321-4c89-bf95-270783b26132` |
| Auth Mode | `OAuth2 Basic (client_credentials)` |
| Auth Endpoint | `/uas/v1/oauth2/client-token` |

If greytHR support tells you your tenant uses a different token endpoint or auth style, override **Auth Endpoint** and **Auth Mode** on the instance form.

## Bidirectional toggles

Each toggle lives on the instance form under **Sync Options**.

- **Push New Employees to greytHR** — after creating an `hr.employee` in Odoo, the connector POSTs the employee to greytHR. On success it stores the returned employee id in `hr.employee.employee_code` and creates the mirror `greythr.employee` record.
- **Push Leave Approvals/Refusals to greytHR** — on `hr.leave.create`, `action_approve` and `action_refuse` the connector calls the matching greytHR endpoint. A `greythr.leave.request` row tracks external id, status and push state (Sent / Failed) — retry via the **Push to greytHR** button on the request form.
- **Enable Payroll Sync** — unlocks the **Sync Payroll** button and enables the daily payroll cron for this instance.

Any push failure is caught, logged and stored on the request/instance row — it never blocks the underlying Odoo save.

## Matching greytHR employees to Odoo employees

Records are matched by putting the greytHR employee identifier into `hr.employee.employee_code`. If a match is found at sync time the `greythr.employee` record is auto-linked; otherwise it stays unlinked and can be linked manually with the **Match Odoo Employee** button.

## Access

- **User** — read-only access to all greytHR mirror models.
- **Manager** — full CRUD, sees credentials, tokens and raw payloads.
