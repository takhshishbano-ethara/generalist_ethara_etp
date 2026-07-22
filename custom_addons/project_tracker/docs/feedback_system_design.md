# Feedback System — Design Proposal (Project Tracker)

> **Status: DESIGN ONLY — not implemented.** This document proposes a scalable,
> task-level feedback system for the `project_tracker` module. It follows the
> module's existing architecture (Odoo 19 ORM, `mail.thread` chatter/audit,
> the `project_tracker.*` model namespace, and the `group_project_tracker_*`
> role ladder) so it can be built later with no rework.

## 1. Goals & scope

- Let any Tracker user raise **feedback against a single task** (a
  `project.tracker.allocation` record — the unit the whole module revolves around).
- Classify feedback into fixed **categories** and move it through a small
  **status workflow**, with an **optional rating**, free-text comments, author
  and timestamps.
- Keep a full **audit history** (who changed what, when) — reusing the chatter
  the rest of the module already relies on, not a bespoke log.
- Fire **notifications** to the right people on the events that matter.
- Expose a clean **API** so a portal, the OWL dashboard, or an external service
  can submit and query feedback later.

Non-goals (v1): cross-task/aggregate feedback, SLA timers, external reviewer
identities. The schema leaves room for these (§8).

## 2. Fit with existing architecture

| Concern | Reuse (do NOT reinvent) |
|---|---|
| Audit history | `mail.thread` + `mail.activity.mixin` — the allocation model already uses the chatter as its audit trail. Every status/field change is tracked via `tracking=True`, exactly like `project.tracker.allocation`. |
| Notifications | `mail.thread.message_post` + `mail.activity` (activities for "action needed"), and the existing `team_notification_service` bus channel used by the Tracker for in-app toasts. |
| Roles / access | `project_tracker.group_project_tracker_tasker / _ql / _pl / _admin` (+ the `etp_user_roles.*` groups already bound in `security/`). |
| Model conventions | `_name = 'project.tracker.feedback'`, snake_case fields, stored computes with `@api.depends`, `models.Constraint` for SQL constraints (Odoo 19 style). |
| UI | A "Feedback" tab/smart-button on the allocation form (mirrors the existing Stage tabs), plus a stat button showing the open-feedback count. A list/kanban action under the Project Tracker menu. No new JS framework — standard views + the existing list-stats widget. |

## 3. Data model

### 3.1 `project.tracker.feedback` (new)

| Field | Type | Notes |
|---|---|---|
| `name` | Char (computed, stored) | Display ref, e.g. `FB/00042`. Sequence-backed. |
| `allocation_id` | Many2one `project.tracker.allocation` | **Required**, `ondelete='cascade'`, `index=True`. The task this is about. |
| `project_id` | Many2one `project.tracker.project` | `related='allocation_id.project_id'`, stored — slice/report by project. |
| `task_ref` | Char | `related='allocation_id.task_id'`, stored — searchable. |
| `category` | Selection | `ui_issue`, `workflow_issue`, `bug`, `suggestion`, `performance`, `other`. Required, indexed. |
| `title` | Char | Short summary. Required. |
| `description` | Text / Html | Details. |
| `rating` | Selection `0..5` (or Integer) | **Optional** (`0`/False = not rated). |
| `state` | Selection | `open` → `in_progress` → `resolved` → `closed`. Default `open`, indexed, `tracking=True`. |
| `author_id` | Many2one `res.users` | Default `env.user`, readonly. Who raised it. |
| `assignee_id` | Many2one `res.users` | Who is handling it (nullable). |
| `resolution` | Text | How it was resolved (shown when `state in (resolved, closed)`). |
| `resolved_by_id` | Many2one `res.users` | Auto-set when moving to `resolved`. |
| `resolved_date` | Datetime | Auto-set on `resolved`. |
| `active` | Boolean | Archive instead of delete. |
| `create_date` / `write_date` / `create_uid` / `write_uid` | — | Standard Odoo audit columns (free). |

Inherits: `['mail.thread', 'mail.activity.mixin']` → chatter = **audit history**;
every tracked-field change is logged automatically with actor + timestamp.

### 3.2 Reverse relation on the allocation (extension, additive/backward-compatible)

```python
class ProjectTrackerAllocation(models.Model):
    _inherit = 'project.tracker.allocation'
    feedback_ids = fields.One2many('project.tracker.feedback', 'allocation_id')
    feedback_count = fields.Integer(compute='_compute_feedback_count')       # stat button
    open_feedback_count = fields.Integer(compute='_compute_feedback_count')  # badge
```

No existing field changes → **backward compatible**.

### 3.3 Indexes / constraints

- Index: `allocation_id`, `state`, `category`, `project_id` (all filter/group axes).
- `models.Constraint('CHECK (rating >= 0 AND rating <= 5)', ...)`.
- Sequence `project.tracker.feedback` for `name`.

## 4. Status workflow

```
        submit                start                 resolve               close
 (none) ─────▶  open  ──────────────▶ in_progress ──────────▶ resolved ─────────▶ closed
                  ▲                        │                      │                   │
                  └──────── reopen ────────┴──────────────────────┴───────────────────┘
```

- Transitions are **button actions** (`action_start`, `action_resolve`,
  `action_close`, `action_reopen`) — never free edits of `state` — each guarded
  by `@api.constrains`/group checks and each `message_post`-ing a line to the
  chatter (audit).
- **Permissions:** author or QL/PL can `start`; QL/PL/assignee can `resolve`/`close`;
  author or QL/PL can `reopen`. `base.group_system` may do anything.
- Terminal `closed` is editable only by QL/PL (reopen), mirroring the Tracker's
  "locked once done" pattern.

## 5. Notification flow

| Event | Recipient | Channel |
|---|---|---|
| Feedback created | Task's PL + QL (`allocation.assigned_pl_id`, QL) | `message_post` (chatter follower) + `mail.activity` "To review" on the PL |
| Assigned | `assignee_id` | activity "To handle" + bus toast (`team_notification_service`) |
| State → resolved | `author_id` | `message_post` + toast |
| State → closed | `author_id` + followers | `message_post` |
| Comment added | followers | native chatter follower notification |

Followers are seeded on create (author, PL, QL) via `message_subscribe`, so all
downstream notifications reuse Odoo's follower machinery — no custom recipient
resolution.

## 6. API design (for future implementation)

Two layers, both thin wrappers over the ORM (no business logic duplicated):

### 6.1 JSON controller (portal / OWL / external), `controllers/feedback.py`

| Method & route | Auth | Body / Params | Returns |
|---|---|---|---|
| `POST /project_tracker/feedback/submit` | user | `{allocation_id, category, title, description, rating?}` | `{id, name, state}` |
| `GET  /project_tracker/feedback/list` | user | `{allocation_id? , project_id?, state?, category?, limit, offset}` | `{count, items:[...]}` (paginated) |
| `GET  /project_tracker/feedback/<id>` | user | — | full record + chatter summary |
| `POST /project_tracker/feedback/<id>/transition` | user | `{action: start\|resolve\|close\|reopen, resolution?}` | `{id, state}` |
| `POST /project_tracker/feedback/<id>/comment` | user | `{body}` | `{message_id}` (→ `message_post`) |

- All routes `type="json", auth="user"`, CSRF handled by Odoo.
- Server-side **validation** on submit: allocation exists & visible to the user
  (record rules enforce this automatically), category in whitelist, title
  non-empty, rating in 0..5. Errors returned as `{error: "..."}` with a clear
  message — mirroring the module's existing controller error style.
- The whitelist/read-group pattern from `controllers/tracker.py` (`_LIST_STATS`)
  is reused so the client can render feedback stat cards with zero new plumbing.

### 6.2 ORM (server-to-server / other modules)

Public model methods: `submit_feedback(vals)`, `action_start/resolve/close/reopen`,
`_compute_feedback_count`. External Odoo code calls these directly; the JSON
controller is a pass-through to them so there is a **single source of truth**.

## 7. UI surface (later)

- Allocation form: **Feedback** smart button (count) + a **Feedback** tab with an
  embedded list (title, category badge, state badge, rating, author, date).
- Top-level **Feedback** menu (QL/PL): list + kanban grouped by `state`, search
  filters by category/state/project, group-bys — reusing the standard view
  toolbar (search, sort, pagination all free).
- Dashboard: an optional "Open Feedback" stat card via the existing list-stats
  controller pattern.

## 8. Scalability & future-proofing

- **Volume:** feedback is a thin satellite table indexed on its filter axes;
  `_read_group` powers all counts/aggregates (same pattern as the dashboard),
  so it scales like the allocation table.
- **Extensible category/state:** Selections centralised as class constants
  (`CATEGORY_SELECTION`, `STATE_SELECTION`) so adding values is one-line.
- **Room to grow (no schema break):** `assignee_id`, `resolution`, `rating`
  already present; SLA/`deadline`, `severity`, `duplicate_of_id`, and
  attachment support (native `ir.attachment` via chatter) can be added additively.
- **Reporting:** because `project_id`/`category`/`state` are stored+indexed, a
  future pivot/graph view or dashboard funnel needs no model change.

## 9. Build checklist (when implemented)

1. `models/project_tracker_feedback.py` (+ allocation `_inherit` extension).
2. `data/feedback_sequence.xml` (ir.sequence for `name`).
3. `security/ir.model.access.csv` rows (tasker: create+read own; QL/PL: full).
4. `security/project_tracker_security.xml` record rule: authors see their own +
   QL/PL/admin see all (mirrors existing allocation rules).
5. `views/project_tracker_feedback_views.xml` (list/kanban/form/search + menu).
6. Allocation form: smart button + Feedback tab (inherit existing form).
7. `controllers/feedback.py` (JSON API §6.1).
8. Tests: workflow transitions, permission matrix, API validation, notifications.
