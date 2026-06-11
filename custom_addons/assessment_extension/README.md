# assessment_extension

Backend APIs for the PEN-driven assessment authoring workflow.

## Scope

This addon implements **five PEN modules** as REST endpoints. All routes are
manager-gated (`etp_assessment.group_assessment_manager`).

| Module | Method | URL |
| --- | --- | --- |
| MOD-Lock-Confirm (preflight)            | GET   | `/api/v1/assessment_extension/assessments/<id>/lock_preflight` |
| MOD-Lock-Confirm (commit)               | POST  | `/api/v1/assessment_extension/assessments/<id>/lock` |
| MOD-Schedule-Confirm (preflight)        | GET   | `/api/v1/assessment_extension/assessments/<id>/send_preflight` |
| MOD-Schedule-Confirm (send)             | POST  | `/api/v1/assessment_extension/assessments/<id>/send` |
| MOD-View-System-Prompt                  | GET   | `/api/v1/assessment_extension/system_prompt/current` |
| MOD-Review-Question (detail)            | GET   | `/api/v1/assessment_extension/review_question/<id>` |
| MOD-Review-Question (approve)           | POST  | `/api/v1/assessment_extension/review_question/<id>/approve` |
| MOD-Review-Question (regenerate)        | POST  | `/api/v1/assessment_extension/review_question/<id>/regenerate` |
| MOD-Review-Question (Prompt, edit)      | PATCH | `/api/v1/assessment_extension/review_question/<id>` |
| MOD-Review-Question (Prompt, bulk)      | POST  | `/api/v1/assessment_extension/assessments/<id>/approve_all` |

## Auth

Every endpoint stacks `@http.route` → `@validate_token` →
`@validate_request` (where applicable) → `require_assessment_manager()`.
The body / query is exposed as `kwargs['jdata']`.

## Idempotency

`POST .../lock`, `POST .../send`, `POST .../approve`, and
`POST .../approve_all` are idempotent. Replays return `200` with
`outcome: 'noop_*'` and never re-send emails / re-stamp timestamps.

`POST .../regenerate` is NOT idempotent — a second call while a review
is already `regenerating` returns `400 REGENERATING_IN_PROGRESS`.

## Install

```bash
./odoo-bin -d <db> -i assessment_extension --stop-after-init
```

Seeds installed:

* 4 canonical Eval-Compare dimensions (`dim_instruction_following`,
  `dim_visual_quality`, `dim_less_ai_generated`, `dim_overall`) with
  options `A` / `B`.
* 1 current system prompt (`Generalist v3`).

## Coexistence

This addon is independent of the older `etp_assessment_extension`
addon. Both can be installed in the same database — URL prefixes do
not collide (`/api/v1/etp_assessment_ext/` vs
`/api/v1/assessment_extension/`).

## Integration note

Use HTTP header `access-token: <token>` (hyphen). Underscored header
names are stripped by Werkzeug 3.1.
