# assessment_extension

Backend (models + JSON endpoints) for **five** monitoring surfaces of the
Assessment design package (`/Users/alok/Egon/assessment`):

| Code | Spec | Surface |
|---|---|---|
| **SCR-096** | `screens/monitoring/SCR-096-candidate-result.md` | Per-candidate drill-in (drawer index + wide question REVIEW) |
| **SCR-097** | `screens/monitoring/SCR-097-item-analysis.md` | Per-question cohort performance |
| **SCR-097** | (drawer in same spec) | Distribution drawer + flag-for-regeneration |
| **SCR-098** | `screens/monitoring/SCR-098-cto-override-approvals.md` | CTO sign-off inbox for self-case overrides |
| **SCR-099** | `screens/monitoring/SCR-099-candidate-history.md` | One candidate's history across all assessments |
| **MOD-Score-Override** | `modals/MOD-Score-Override.md` | Override commit (direct OR self-case → CTO) |

UI is **out of scope** — built by the frontend team against these endpoints.

---

## Models

New (`etp.assessment.*` namespace, alongside `etp_assessment`):

- `etp.assessment.day.session` — one row per (candidate, day) — submitted-count, day mean, per-type means, status (locked / in_progress / submitted / passed / failed / incomplete). WORKFLOW §7.5, §12.5.
- `etp.assessment.submission` — one row per (candidate, question) — `llm_score`, `confidence`, `low_confidence`, `llm_rationale`, `sub_scores`, `override_score` / `override_by` / `override_at` / `override_reason`, computed `final_score`, lifecycle Submitted → Scored → Overridden. WORKFLOW §7.4, §12.4, §6.
- `etp.assessment.override.request` — pending self-case override awaiting CTO sign-off (the §6.5 escalation target).

Inherited (extending `etp_assessment`):

- `etp.assessment` ← adds `code`, `cohort_label`, `pass_threshold` (70), `period_days` (5), `questions_per_day` (25), `low_confidence_threshold` (0.6), `override_delta_threshold` (10), `monitor_state`, `at_risk_count`.
- `etp.assessment.question` ← adds `code`, `task_type` (eval_compare / prompt_writing / bbox_labeling), `difficulty`, `day_number`, `correct_answer` (JSON), `wrong_answer` (JSON), `flagged_bad`, computed `response_count` / `avg_score` / `low_confidence_pct` / `is_suspect`.
- `etp.assessment.evaluator` ← adds `cohort_batch`, `last_activity_at`, computed `overall_mean` / `submitted_total` / `submissions_expected` / `is_at_risk` / `review_count`.

---

## Roles

| Group XML id | Purpose |
|---|---|
| `assessment_extension.group_assessment_pl` | Project Lead — own assessments only. |
| `assessment_extension.group_assessment_hr_admin` | HR Admin — org-wide oversight (implies PL). |
| `assessment_extension.group_assessment_cto` | CTO — self-case sign-off (implies HR Admin). |

PLs are blocked from directly committing an override on their own direct
report — those route to the CTO via `etp.assessment.override.request`
(WORKFLOW §6.5).

---

## REST endpoints

All endpoints follow the `api_auth_gateway` envelope and are guarded by
`@validate_token` (send `Access-Token: <token>` header).

### SCR-096 — Candidate Result drill-in

```
GET  /api/v1/assessment_ext/candidate_result?assessment_id=N&candidate_id=N
GET  /api/v1/assessment_ext/question_review?submission_id=N
```

`candidate_result` returns the 560 px drawer payload — header (candidate
identity + at-risk pill + summary line + "{n} to review" jump count) and
the five day rows (status pill, submitted-of-25 fraction, day mean, per-type
mini chips, and the question list for each opened day). Each question row
carries the LLM score (banded), low-confidence flag, override badge if any.

`question_review` returns the wide 1160 px REVIEW payload for one submission
— question media + locked answer key on the left, candidate answer + LLM
scoring panel (sub-score breakdown + rationale + confidence chip) on the
right, plus prev/next neighbor ids and the `is_self_case`/`can_override_directly`
guard verdict for the footer override button.

### SCR-097 — Item Analysis + Distribution Drawer

```
GET  /api/v1/assessment_ext/item_analysis?assessment_id=N[&day=N][&task_type=eval_compare|prompt_writing|bbox_labeling]
GET  /api/v1/assessment_ext/distribution?question_id=N[&assessment_id=N]
POST /api/v1/assessment_ext/flag_question                  body: {question_id, flagged?, assessment_id?}
```

`item_analysis` returns the 4-card KPI strip (Questions analysed / Mean
question score / Flagged items / Mean confidence) + the one-row-per-question
table. Auto-flag rule applied: a row is flagged-suspect when `avg<25` OR
`avg>95` with collapsed variance OR `low_conf_pct>40` (≥3 responses). Rows
sort flagged-first then avg ascending.

`distribution` returns the per-question drawer payload — five-bin histogram
+ stat row + a why-flagged callout + up to 3 anonymised example answers +
the locked correct/wrong answer pair.

`flag_question` sets `Question.flagged_bad = true` without unlocking the
question (WORKFLOW §7.2). Does NOT touch any candidate score.

### SCR-098 — CTO Override Approvals

```
GET  /api/v1/assessment_ext/override_approvals[?state=pending|approved|rejected|all]
POST /api/v1/assessment_ext/override_approvals/<id>/approve   body: {note?}
POST /api/v1/assessment_ext/override_approvals/<id>/reject    body: {note?}
```

`override_approvals` (CTO-only) returns the pending list with the requesting
PL, candidate, question, score-change `{from, to, delta, to_band}`, reason +
note, and an info banner.

`approve` commits the requested score on the underlying submission as a
`override_by = CTO` override — recomputes day + overall means and
audit-logs. `reject` keeps the LLM score and records the decision note.

### SCR-099 — Candidate History

```
GET /api/v1/assessment_ext/candidate_history?candidate_id=N
```

Returns the per-person history — one row per assessment the candidate has
taken (assessment, window date range, status pill, score band, action link
back into SCR-096). Header summary chip "Passed N of M" + the 3-KPI stat
row. PL sees only their own assessments; HR/CTO see everything.

### MOD-Score-Override

```
GET  /api/v1/assessment_ext/override_context?submission_id=N
GET  /api/v1/assessment_ext/override_preview?submission_id=N&new_score=N
POST /api/v1/assessment_ext/override                          body:
       {submission_id, new_score, reason?, note?, item_result?}
```

`override_context` returns the panel's prefill — LLM score + confidence
chip + rationale + the §6.4 policy (min/max/default/delta_threshold/reason
list) + the §6.5 self-case guard verdict
(`is_self_case`, `can_commit_directly`, `escalation_message`,
`primary_action_label` — either "Confirm override" or "Send to CTO").

`override_preview` is a live preview as the PL nudges the score — returns
the delta + delta band + the day/overall mean recompute the confirm card
shows.

`override` is the commit. The self-case guard branches automatically:

- **PL on a self-case** (their own direct report): creates a pending
  `etp.assessment.override.request` and returns
  `{outcome: "escalated_to_cto", row_badge: "Override pending CTO"}`.
- **Anyone else** (HR / CTO / PL on someone outside their team): commits
  the override on the submission directly and returns
  `{outcome: "committed", row_badge: "Overridden"}`.

Validation enforces the §6.4 rules — `new_score` 0-100, reason required
when `|delta| > override_delta_threshold` (default 10), note required when
reason is `other`.

---

## Status / score conventions (returned by the API)

Every status field comes paired with a `status_pill` dict
`{bg, text, dot, label}` matching the WORKFLOW §7.3 recipes — so the client
can render directly without re-deriving colour codes.

Every score is paired with a `*_band` field — one of `success` / `info` /
`warning` / `destructive` / `muted` — matching the SCR-096 §Design
alignment colour-grade against `pass_threshold` (default 70):

| Score | Band | Token | Hex |
|---|---|---|---|
| ≥80 | success | `successText` | #047857 |
| pass..79 | info | `infoText` | #1D4ED8 |
| 60..pass-1 | warning | `warnText` | #C2410C |
| <60 | destructive | `destText` | #B91C1C |
| None | muted | `textMuted` | #9CA3AF |

Task-type pills come as `{task_type, label, bg, text, dot}` matching the
verbatim §3.4.3 recipe in SCR-097.

---

## Wiring notes

- Depends on `etp_assessment` (the base assessment module) + `api_auth_gateway`.
- A pending override request can only exist once per submission while
  `state=pending` — re-POSTing `/override` for a self-case while a request
  is already open returns HTTP 409 with the existing `request_id`.
- The PL scope (`role=pl`) filters every list/detail endpoint by
  `assessment.create_uid == self.env.user` — HR Admin and CTO see all
  assessments. WORKFLOW §13.
- New ids: `ASM-####` / `QST-#####` / `SUB-######` / `REQ-#####`. The
  `code` fields on `etp.assessment` and `etp.assessment.question` are
  optional; controllers fall back to `f"ASM-{id:04d}"` / `f"QST-{id:05d}"`
  if unset, so the frontend always gets a mono id to render.
