# Task Forge Log API Changes

## Files Modified

- `custom_addons/task_forge_core/models/task_log.py`
- `custom_addons/task_forge_core/controllers/task_controllers.py`

---

## 1. Task Status — New States Added

The `state` field on `task.forge.log` now includes QC review states:

| State Value | Display Label | Description |
|-------------|--------------|-------------|
| `in_progress` | In Progress | Task is being worked on |
| `completed` | Completed | Tasker finished the task (pending QC review) |
| `qc_approved` | QC Approved | QC reviewed and approved |
| `qc_rejected` | QC Rejected | QC reviewed and rejected |
| `blocker` | Blocker | Tasker reported a blocker |
| `returned` | Returned | Task returned to tasker |
| `ack` | Acknowledged | Acknowledged |
| `escalated` | Escalated | Escalated to higher authority |
| `overdue` | Overdue | Task is overdue |

### State Flow

```
in_progress → completed → qc_approved
                        → qc_rejected
```

---

## 2. New Model Fields (QC Review)

| Field | Type | Description |
|-------|------|-------------|
| `reviewed_by_id` | Many2one (hr.employee) | Employee who performed the review |
| `review_date` | Datetime | When the review was done |
| `rejection_reason` | Text | Reason provided by QC when rejecting |

---

## 3. New API Endpoint — QC Task Review

### `POST /api/v2/taskforge/tasks/review`

QC/PL/Admin reviews a completed task — either approves or rejects it.

**Headers:**
```
Authorization: Bearer <token>
```

**Request Body:**

#### Approve:
```json
{
  "task_id": 42,
  "action": "approve"
}
```

#### Reject:
```json
{
  "task_id": 42,
  "action": "reject",
  "rejection_reason": "Prompt does not match the screenshot evidence"
}
```

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `task_id` | int | Yes | ID of the task to review |
| `action` | string | Yes | `"approve"` or `"reject"` |
| `rejection_reason` | string | Yes (on reject) | Reason for rejection |

**Access Control:**
- Only roles `qr`, `ql`, `pl`, `admin` can call this endpoint
- The task's employee must be in the reviewer's team (admin bypasses)
- Only tasks with `state = 'completed'` can be reviewed

**Success Response (approve):**
```json
{
  "message": "Task approved",
  "status": 200,
  "data": {
    "data": { /* full _format_task response */ }
  }
}
```

**Success Response (reject):**
```json
{
  "message": "Task rejected",
  "status": 200,
  "data": {
    "data": { /* full _format_task response */ }
  }
}
```

**Error Responses:**

| Status | Message |
|--------|---------|
| 400 | `action must be 'approve' or 'reject'` |
| 400 | `Only completed tasks can be reviewed` |
| 400 | `rejection_reason is required when rejecting` |
| 403 | `Only QC/PL/Admin can review tasks` |
| 403 | `Access denied: task not in your team` |
| 404 | `Employee profile not found` |
| 404 | `Task not found` |

**Side Effects:**
- Updates `state` to `qc_approved` or `qc_rejected`
- Records `reviewed_by_id` and `review_date`
- Sends notification to the tasker

---

## 4. `_format_task` — Updated Response Fields

All task GET endpoints now include QC review data:

| Field | Type | Description |
|-------|------|-------------|
| `status` | string | Now can return "QC Approved" or "QC Rejected" |
| `qc_status` | string | Derived: `"qc_approved"`, `"qc_rejected"`, or `"pending"` |
| `reviewed_by_id` | int | Reviewer employee ID (0 if not reviewed) |
| `reviewed_by_name` | string | Reviewer name (empty if not reviewed) |
| `review_date` | string | Review datetime in IST (empty if not reviewed) |
| `rejection_reason` | string | Rejection reason (empty if not rejected) |

---

## 5. Sample Response (QC Rejected Task)

```json
{
  "id": 42,
  "sequence": "TF-0042",
  "task_name": "Review document section 3",
  "prompt": "...",
  "justification": "...",
  "employee_id": 5,
  "employee_name": "John Doe",
  "project_id": 10,
  "project_name": "Project Alpha",
  "date": "2026-05-05",
  "status": "QC Rejected",
  "start_time": "2026-05-05 14:30:00",
  "end_time": "2026-05-05 15:10:00",
  "pause_time": "120",
  "time_taken_mins": 2.0,
  "is_justification_required": true,
  "start_screenshot_url": "https://...",
  "end_screenshot_url": "https://...",
  "blocker_reason": "",
  "blocker_count": 0,
  "blocker_status": "",
  "quality_score": 0,
  "prompt_justification": "",
  "feedback_note": "",
  "created_at": "2026-05-05 14:30:00",
  "image_url_lines": [],
  "responses": [],
  "response_completed": true,
  "is_timer_enabled": false,
  "task_score": 0,
  "comment": "",
  "grammar_checked": false,
  "grammar_is_perfect": false,
  "rubric_completed": true,
  "rubric_ratings": [],
  "bug_reports": [],
  "qc_status": "qc_rejected",
  "reviewed_by_id": 7,
  "reviewed_by_name": "Jane QC",
  "review_date": "2026-05-05 16:45:00",
  "rejection_reason": "Prompt does not match the screenshot evidence"
}
```

---

## 6. Sample Response (QC Approved Task)

```json
{
  "id": 43,
  "sequence": "TF-0043",
  "task_name": "Annotate image batch 5",
  "status": "QC Approved",
  "qc_status": "qc_approved",
  "reviewed_by_id": 7,
  "reviewed_by_name": "Jane QC",
  "review_date": "2026-05-05 17:00:00",
  "rejection_reason": ""
}
```
