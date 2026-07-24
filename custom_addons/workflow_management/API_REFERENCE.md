# Workflow Management - API Reference

> **Base URL**: `http://localhost:7000`
> **Protocol**: JSON-RPC (Odoo 19)
> **Content-Type**: `application/json`

All endpoints expect a JSON-RPC wrapper. Send requests like:

```json
{
  "jsonrpc": "2.0",
  "method": "call",
  "params": {
    // endpoint-specific params here
  }
}
```

Responses are wrapped in:
```json
{
  "jsonrpc": "2.0",
  "id": null,
  "result": {
    // actual response data here
  }
}
```

---

## Authentication

All `auth: user` endpoints require an active Odoo session. Login first:

### Login (Get Session)

```
POST /web/session/authenticate
```

**Request:**
```json
{
  "jsonrpc": "2.0",
  "method": "call",
  "params": {
    "db": "ethara_etp",
    "login": "admin",
    "password": "admin"
  }
}
```

**Response:**
```json
{
  "jsonrpc": "2.0",
  "result": {
    "uid": 2,
    "is_admin": true,
    "name": "Mitchell Admin",
    "username": "admin",
    "session_id": "abc123..."
  }
}
```

> After login, the session cookie is set automatically. Include it in subsequent requests.

---

## 1. Floors

### 1.1 List All Floors

```
POST /api/workflow/floors
Auth: user
```

**Request:**
```json
{
  "jsonrpc": "2.0",
  "method": "call",
  "params": {}
}
```

**Response:**
```json
{
  "jsonrpc": "2.0",
  "result": {
    "status": "success",
    "data": [
      {
        "id": 1,
        "floor_code": "ground",
        "name": "Ground Floor",
        "total_seats": 25,
        "occupied_seats": 18,
        "available_seats": 5,
        "reserved_seats": 2
      },
      {
        "id": 2,
        "floor_code": "first",
        "name": "First Floor",
        "total_seats": 30,
        "occupied_seats": 22,
        "available_seats": 8,
        "reserved_seats": 0
      }
    ]
  }
}
```

---

## 2. Seats

### 2.1 List Seats

```
POST /api/workflow/seats
Auth: user
```

**Request Params (all optional):**

| Param | Type | Description |
|-------|------|-------------|
| `floor_id` | integer | Filter by floor ID |
| `status` | string | Filter by status: `available`, `occupied`, `reserved`, `maintenance` |

**Request:**
```json
{
  "jsonrpc": "2.0",
  "method": "call",
  "params": {
    "floor_id": 1,
    "status": "available"
  }
}
```

**Response:**
```json
{
  "jsonrpc": "2.0",
  "result": {
    "status": "success",
    "data": [
      {
        "id": 3,
        "seat_code": "GF-A-003",
        "floor_id": 1,
        "floor_name": "Ground Floor",
        "zone": "Left-Wing",
        "row": "A",
        "number": 3,
        "status": "available",
        "position_x": 120.5,
        "position_y": 300.0,
        "employee_id": null,
        "employee_name": ""
      },
      {
        "id": 5,
        "seat_code": "GF-A-005",
        "floor_id": 1,
        "floor_name": "Ground Floor",
        "zone": "Right-Wing",
        "row": "A",
        "number": 5,
        "status": "available",
        "position_x": 250.0,
        "position_y": 300.0,
        "employee_id": null,
        "employee_name": ""
      }
    ]
  }
}
```

### 2.2 Assign Seat

Assigns a seat to an employee. If the employee already has a seat, the old one is released automatically.

```
POST /api/workflow/seats/assign
Auth: user
```

**Request Params (required):**

| Param | Type | Description |
|-------|------|-------------|
| `seat_id` | integer | ID of the `workflow.seat` record |
| `employee_id` | integer | ID of the `hr.employee` record |

**Request:**
```json
{
  "jsonrpc": "2.0",
  "method": "call",
  "params": {
    "seat_id": 3,
    "employee_id": 12
  }
}
```

**Success Response:**
```json
{
  "jsonrpc": "2.0",
  "result": {
    "status": "success",
    "message": "Seat GF-A-003 assigned to John Doe."
  }
}
```

**Error Response (seat not available):**
```json
{
  "jsonrpc": "2.0",
  "result": {
    "status": "error",
    "message": "Seat GF-A-003 is occupied and cannot be assigned."
  }
}
```

### 2.3 Release Seat

Releases a seat, making it available. Updates the employee's `current_seat_id` to empty.

```
POST /api/workflow/seats/release
Auth: user
```

**Request Params:**

| Param | Type | Description |
|-------|------|-------------|
| `seat_id` | integer | ID of the seat to release |

**Request:**
```json
{
  "jsonrpc": "2.0",
  "method": "call",
  "params": {
    "seat_id": 3
  }
}
```

**Success Response:**
```json
{
  "jsonrpc": "2.0",
  "result": {
    "status": "success",
    "message": "Seat GF-A-003 released."
  }
}
```

---

## 3. Employees

### 3.1 List Employees

```
POST /api/workflow/employees
Auth: user
```

**Request Params (all optional):**

| Param | Type | Description |
|-------|------|-------------|
| `department` | string | Filter by department name (partial match) |
| `project_category` | string | Filter: `stem` or `non_stem` |

**Request:**
```json
{
  "jsonrpc": "2.0",
  "method": "call",
  "params": {
    "project_category": "stem"
  }
}
```

**Response:**
```json
{
  "jsonrpc": "2.0",
  "result": {
    "status": "success",
    "data": [
      {
        "id": 12,
        "name": "John Doe",
        "email": "john@ethara.com",
        "department": "Engineering",
        "job_title": "Senior Developer",
        "project_category": "stem",
        "current_project": "Project Alpha",
        "platform": "linux",
        "tasking_status": "active",
        "allocation_status": "fully_allocated",
        "current_seat": "GF-A-003",
        "main_location": "Office A",
        "current_location": "Floor 2"
      },
      {
        "id": 15,
        "name": "Jane Smith",
        "email": "jane@ethara.com",
        "department": "Engineering",
        "job_title": "Backend Developer",
        "project_category": "stem",
        "current_project": "Project Beta",
        "platform": "mac",
        "tasking_status": "active",
        "allocation_status": "partially_allocated",
        "current_seat": "",
        "main_location": "Office A",
        "current_location": ""
      }
    ]
  }
}
```

---

## 4. Room Bookings

### 4.1 List Bookings

```
POST /api/workflow/bookings
Auth: user
```

**Request Params (all optional):**

| Param | Type | Description |
|-------|------|-------------|
| `room_code` | string | Filter by room code (e.g. `CONF-01`) |
| `status` | string | Filter: `confirmed`, `cancelled`, `completed` |

**Request:**
```json
{
  "jsonrpc": "2.0",
  "method": "call",
  "params": {
    "status": "confirmed"
  }
}
```

**Response:**
```json
{
  "jsonrpc": "2.0",
  "result": {
    "status": "success",
    "data": [
      {
        "id": 1,
        "room_code": "CONF-01",
        "name": "Conference Room A",
        "floor": "Ground Floor",
        "organizer": "Mitchell Admin",
        "organizer_email": "admin@ethara.com",
        "reason": "Sprint Planning",
        "booking_start": "2026-07-23 14:00:00",
        "booking_end": "2026-07-23 15:00:00",
        "duration_minutes": 60,
        "status": "confirmed"
      }
    ]
  }
}
```

### 4.2 Create Booking

```
POST /api/workflow/bookings/create
Auth: user
```

**Request Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `room_code` | string | Yes | Room identifier (e.g. `CONF-01`) |
| `name` | string | Yes | Room display name |
| `booking_start` | string | Yes | ISO datetime `YYYY-MM-DD HH:MM:SS` |
| `booking_end` | string | Yes | ISO datetime `YYYY-MM-DD HH:MM:SS` |
| `reason` | string | No | Purpose of booking |
| `floor_id` | integer | No | Link to a floor record |

**Request:**
```json
{
  "jsonrpc": "2.0",
  "method": "call",
  "params": {
    "room_code": "CONF-02",
    "name": "Board Room B",
    "booking_start": "2026-07-24 10:00:00",
    "booking_end": "2026-07-24 11:30:00",
    "reason": "Client Demo",
    "floor_id": 2
  }
}
```

**Success Response:**
```json
{
  "jsonrpc": "2.0",
  "result": {
    "status": "success",
    "message": "Booking CONF-02 created.",
    "booking_id": 5
  }
}
```

**Error Response (overlap):**
```json
{
  "jsonrpc": "2.0",
  "result": {
    "status": "error",
    "message": "Room CONF-02 is already booked from 2026-07-24 10:00:00 to 2026-07-24 11:30:00."
  }
}
```

### 4.3 Cancel Booking

```
POST /api/workflow/bookings/cancel
Auth: user
```

**Request Params:**

| Param | Type | Description |
|-------|------|-------------|
| `booking_id` | integer | ID of the booking to cancel |

**Request:**
```json
{
  "jsonrpc": "2.0",
  "method": "call",
  "params": {
    "booking_id": 5
  }
}
```

**Success Response:**
```json
{
  "jsonrpc": "2.0",
  "result": {
    "status": "success",
    "message": "Booking cancelled."
  }
}
```

**Error Response:**
```json
{
  "jsonrpc": "2.0",
  "result": {
    "status": "error",
    "message": "Only confirmed bookings can be cancelled."
  }
}
```

### 4.4 Active Bookings (Currently In-Progress)

Returns bookings where current time is between `booking_start` and `booking_end`.

```
POST /api/workflow/bookings/active
Auth: user
```

**Request Params (optional):**

| Param | Type | Description |
|-------|------|-------------|
| `room_code` | string | Check specific room availability |

**Request:**
```json
{
  "jsonrpc": "2.0",
  "method": "call",
  "params": {
    "room_code": "CONF-01"
  }
}
```

**Response (room is busy):**
```json
{
  "jsonrpc": "2.0",
  "result": {
    "status": "success",
    "data": [
      {
        "id": 1,
        "room_code": "CONF-01",
        "name": "Conference Room A",
        "organizer": "Mitchell Admin",
        "reason": "Sprint Planning",
        "booking_end": "2026-07-23 15:00:00"
      }
    ]
  }
}
```

**Response (room is free):**
```json
{
  "jsonrpc": "2.0",
  "result": {
    "status": "success",
    "data": []
  }
}
```

---

## 5. Seat Transfers

The transfer workflow has 5 states: `draft` → `submitted` → `approved` → `completed` (or `rejected` → back to `draft`).

### 5.1 List Transfers

```
POST /api/workflow/transfers
Auth: user
```

**Request Params (optional):**

| Param | Type | Description |
|-------|------|-------------|
| `state` | string | Filter: `draft`, `submitted`, `approved`, `rejected`, `completed` |

**Request:**
```json
{
  "jsonrpc": "2.0",
  "method": "call",
  "params": {
    "state": "submitted"
  }
}
```

**Response:**
```json
{
  "jsonrpc": "2.0",
  "result": {
    "status": "success",
    "data": [
      {
        "id": 3,
        "name": "TRN-0003",
        "employee": "John Doe",
        "from_seat": "GF-A-003",
        "to_seat": "F1-B-010",
        "requested_by": "Mitchell Admin",
        "reason": "Project relocation to first floor",
        "state": "submitted",
        "created_at": "2026-07-23 09:30:00",
        "decided_by": "",
        "decided_at": ""
      }
    ]
  }
}
```

### 5.2 Create Transfer Request

Creates a new transfer in `draft` state. `from_seat` is auto-filled from the employee's current seat.

```
POST /api/workflow/transfers/create
Auth: user
```

**Request Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `employee_id` | integer | Yes | Employee to transfer |
| `to_seat_id` | integer | Yes | Target seat ID (must be available) |
| `reason` | string | No | Reason for transfer |

**Request:**
```json
{
  "jsonrpc": "2.0",
  "method": "call",
  "params": {
    "employee_id": 12,
    "to_seat_id": 18,
    "reason": "Team relocation"
  }
}
```

**Success Response:**
```json
{
  "jsonrpc": "2.0",
  "result": {
    "status": "success",
    "message": "Transfer request TRN-0004 created.",
    "transfer_id": 4
  }
}
```

### 5.3 Submit Transfer for Approval

Moves transfer from `draft` → `submitted`. Validates that target seat is still available.

```
POST /api/workflow/transfers/submit
Auth: user
```

**Request Params:**

| Param | Type | Description |
|-------|------|-------------|
| `transfer_id` | integer | ID of the transfer to submit |

**Request:**
```json
{
  "jsonrpc": "2.0",
  "method": "call",
  "params": {
    "transfer_id": 4
  }
}
```

**Success Response:**
```json
{
  "jsonrpc": "2.0",
  "result": {
    "status": "success",
    "message": "Transfer submitted for approval."
  }
}
```

**Error Response:**
```json
{
  "jsonrpc": "2.0",
  "result": {
    "status": "error",
    "message": "Target seat F1-B-010 is not available."
  }
}
```

### 5.4 Approve Transfer (Manager/Admin)

Approves the transfer AND executes the seat swap immediately. Old seat is released, new seat is assigned.

```
POST /api/workflow/transfers/approve
Auth: user (Manager or Admin group required)
```

**Request Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `transfer_id` | integer | Yes | ID of the transfer |
| `notes` | string | No | Approval notes |

**Request:**
```json
{
  "jsonrpc": "2.0",
  "method": "call",
  "params": {
    "transfer_id": 4,
    "notes": "Approved - team needs to be co-located"
  }
}
```

**Success Response:**
```json
{
  "jsonrpc": "2.0",
  "result": {
    "status": "success",
    "message": "Transfer TRN-0004 approved and executed."
  }
}
```

### 5.5 Reject Transfer (Manager/Admin)

```
POST /api/workflow/transfers/reject
Auth: user (Manager or Admin group required)
```

**Request Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `transfer_id` | integer | Yes | ID of the transfer |
| `notes` | string | No | Rejection reason |

**Request:**
```json
{
  "jsonrpc": "2.0",
  "method": "call",
  "params": {
    "transfer_id": 4,
    "notes": "No seats available in requested zone"
  }
}
```

**Success Response:**
```json
{
  "jsonrpc": "2.0",
  "result": {
    "status": "success",
    "message": "Transfer TRN-0004 rejected."
  }
}
```

### 5.6 Pending Transfer Count

Quick endpoint to show badge count in the UI.

```
POST /api/workflow/transfers/pending-count
Auth: user
```

**Request:**
```json
{
  "jsonrpc": "2.0",
  "method": "call",
  "params": {}
}
```

**Response:**
```json
{
  "jsonrpc": "2.0",
  "result": {
    "status": "success",
    "count": 3
  }
}
```

---

## 6. Audit Logs

### 6.1 List Audit Logs

```
POST /api/workflow/audit-logs
Auth: user
```

**Request Params (all optional):**

| Param | Type | Description |
|-------|------|-------------|
| `category` | string | Filter: `seat_management`, `seat_transfer`, `room_booking`, `employee_management`, `attendance`, `system` |
| `limit` | integer | Max records to return (default: 50) |

**Request:**
```json
{
  "jsonrpc": "2.0",
  "method": "call",
  "params": {
    "category": "seat_transfer",
    "limit": 10
  }
}
```

**Response:**
```json
{
  "jsonrpc": "2.0",
  "result": {
    "status": "success",
    "data": [
      {
        "id": 15,
        "event": "transfer_approved",
        "category": "seat_transfer",
        "status": "success",
        "user_name": "Mitchell Admin",
        "details": {
          "reference": "TRN-0003",
          "employee": "John Doe",
          "from_seat": "GF-A-003",
          "to_seat": "F1-B-010",
          "approved_by": "Mitchell Admin"
        },
        "timestamp": "2026-07-23 10:15:30"
      },
      {
        "id": 14,
        "event": "transfer_submitted",
        "category": "seat_transfer",
        "status": "success",
        "user_name": "Mitchell Admin",
        "details": {
          "reference": "TRN-0003",
          "employee": "John Doe",
          "to_seat": "F1-B-010"
        },
        "timestamp": "2026-07-23 10:14:22"
      }
    ]
  }
}
```

**Audit Event Types:**

| Event | Category | When |
|-------|----------|------|
| `employee_created` | employee_management | New employee created |
| `seat_assigned` | seat_management | Seat assigned to employee |
| `seat_released` | seat_management | Seat released from employee |
| `transfer_submitted` | seat_transfer | Transfer request submitted |
| `transfer_approved` | seat_transfer | Transfer approved by manager |
| `transfer_rejected` | seat_transfer | Transfer rejected |
| `booking_created` | room_booking | New room booking created |
| `booking_cancelled` | room_booking | Booking cancelled |
| `attendance_synced` | attendance | ESSL sync completed |

---

## 7. Public Endpoints (No Authentication)

These endpoints require no login. Useful for public dashboards and guest views.

### 7.1 Public: List Floors

```
POST /api/workflow/public/floors
Auth: none
```

**Request:**
```json
{
  "jsonrpc": "2.0",
  "method": "call",
  "params": {}
}
```

**Response:**
```json
{
  "jsonrpc": "2.0",
  "result": {
    "status": "success",
    "data": [
      {
        "floor_code": "ground",
        "name": "Ground Floor",
        "total_seats": 25,
        "occupied_seats": 18,
        "available_seats": 5
      },
      {
        "floor_code": "first",
        "name": "First Floor",
        "total_seats": 30,
        "occupied_seats": 22,
        "available_seats": 8
      }
    ]
  }
}
```

### 7.2 Public: Seats by Floor Code

```
POST /api/workflow/public/seats/ground
Auth: none
```

**Request:**
```json
{
  "jsonrpc": "2.0",
  "method": "call",
  "params": {}
}
```

**Response:**
```json
{
  "jsonrpc": "2.0",
  "result": {
    "status": "success",
    "data": [
      {
        "seat_code": "GF-A-001",
        "zone": "Left-Wing",
        "row": "A",
        "number": 1,
        "status": "occupied",
        "position_x": 100.0,
        "position_y": 200.0,
        "employee_name": "John Doe"
      },
      {
        "seat_code": "GF-A-002",
        "zone": "Left-Wing",
        "row": "A",
        "number": 2,
        "status": "available",
        "position_x": 150.0,
        "position_y": 200.0,
        "employee_name": ""
      }
    ]
  }
}
```

> Note: Public endpoints show employee names but NOT emails or contact details.

### 7.3 Public: Active Room Bookings

```
POST /api/workflow/public/bookings/active
Auth: none
```

**Request:**
```json
{
  "jsonrpc": "2.0",
  "method": "call",
  "params": {}
}
```

**Response:**
```json
{
  "jsonrpc": "2.0",
  "result": {
    "status": "success",
    "data": [
      {
        "room_code": "CONF-01",
        "name": "Conference Room A",
        "organizer": "Mitchell Admin",
        "booking_end": "2026-07-23 15:00:00"
      }
    ]
  }
}
```

---

## API Summary Table

| # | Endpoint | Method | Auth | Description |
|---|----------|--------|------|-------------|
| 1 | `/api/workflow/floors` | POST | user | List all floors with seat stats |
| 2 | `/api/workflow/seats` | POST | user | List/filter seats by floor or status |
| 3 | `/api/workflow/seats/assign` | POST | user | Assign seat to employee |
| 4 | `/api/workflow/seats/release` | POST | user | Release seat |
| 5 | `/api/workflow/employees` | POST | user | List employees with workflow fields |
| 6 | `/api/workflow/bookings` | POST | user | List room bookings |
| 7 | `/api/workflow/bookings/create` | POST | user | Create room booking |
| 8 | `/api/workflow/bookings/cancel` | POST | user | Cancel booking |
| 9 | `/api/workflow/bookings/active` | POST | user | Currently active bookings |
| 10 | `/api/workflow/transfers` | POST | user | List seat transfers |
| 11 | `/api/workflow/transfers/create` | POST | user | Create transfer request |
| 12 | `/api/workflow/transfers/submit` | POST | user | Submit transfer for approval |
| 13 | `/api/workflow/transfers/approve` | POST | user | Approve transfer (Manager+) |
| 14 | `/api/workflow/transfers/reject` | POST | user | Reject transfer (Manager+) |
| 15 | `/api/workflow/transfers/pending-count` | POST | user | Count of pending transfers |
| 16 | `/api/workflow/audit-logs` | POST | user | List audit logs |
| 17 | `/api/workflow/public/floors` | POST | none | Public floor data |
| 18 | `/api/workflow/public/seats/<code>` | POST | none | Public seat data by floor |
| 19 | `/api/workflow/public/bookings/active` | POST | none | Public active bookings |

---

## Security Roles

| Role | Can Do |
|------|--------|
| **User** | View floors/seats/bookings, create transfer requests & bookings |
| **Manager** | Everything User can + approve/reject transfers, manage seats (write) |
| **Admin** | Everything + create/delete floors/seats, audit logs, attendance sync |

---

## Transfer Workflow State Machine

```
  ┌─────────┐     submit      ┌───────────┐     approve     ┌──────────┐     auto      ┌───────────┐
  │  DRAFT  │ ──────────────> │ SUBMITTED │ ─────────────> │ APPROVED │ ──────────> │ COMPLETED │
  └─────────┘                 └───────────┘                 └──────────┘             └───────────┘
       ^                            │
       │          reject            │
       │                     ┌──────────┐
       └──── reset ──────── │ REJECTED │
                             └──────────┘
```

- **Draft**: Request created, can be edited
- **Submitted**: Waiting for manager approval, target seat validated
- **Approved**: Manager approved, seat swap executed immediately
- **Completed**: Transfer done (auto after approval)
- **Rejected**: Manager rejected, can be reset to draft and re-submitted

---

## Error Handling

All errors follow this pattern:

```json
{
  "jsonrpc": "2.0",
  "result": {
    "status": "error",
    "message": "Human-readable error description"
  }
}
```

Common errors:
- `"Seat not found."` — Invalid seat_id
- `"Employee not found."` — Invalid employee_id
- `"Seat GF-A-003 is occupied and cannot be assigned."` — Seat already taken
- `"Target seat F1-B-010 is not available."` — Seat not free for transfer
- `"Only draft requests can be submitted."` — Wrong state transition
- `"Only confirmed bookings can be cancelled."` — Booking already cancelled/completed
- `"Room CONF-01 is already booked from ... to ..."` — Time overlap
- `"Booking not found."` — Invalid booking_id
- `"Transfer not found."` — Invalid transfer_id
