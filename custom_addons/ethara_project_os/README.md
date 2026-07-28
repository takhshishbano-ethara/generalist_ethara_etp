# Ethara Project OS

The Odoo 19 backend for the project side of the pod organisation — the production
build of the `ethara-project-os` prototype.

A PM stands up a project (knowledge folder → training → assessment → stagelist →
feedback form), allocates people to it, and Pod Leads run the daily roster while Pod
Members task against it. Every read and write is scoped by role, and every number on
every screen is a live aggregate — there is not a single counter column in the module.

---

## Where the frontend goes

**This module is the API. The frontend is a separate app that talks to it.**

**This module ships no Odoo UI for the four roles.** The Tasker's day, the pod
lead's roster board and the PM's kickoff screens are all served by `/api/project-os/*`
and rendered by a frontend of your choosing (React / Next / Vue / plain JS — the
prototype was vanilla JS and that worked fine).

The only Odoo screens here are the two things Odoo is genuinely better at than a bespoke
page, and neither is part of the product:

* **Settings → Project OS** — where the S3 bucket and the assessment API get configured.
* **Project OS → History** — a read-only window on the timeline, plus the Admin-only
  audit log, for explaining something after the fact.

Everything else was deliberately removed rather than maintained in parallel: a
half-maintained backend UI drifts from the frontend, exposes fields the frontend hides,
and is the most likely thing to break an upgrade. The module installs and passes its
full test suite with no views at all — that was verified, not assumed.

```
   your frontend                     this module
┌──────────────────┐            ┌──────────────────────┐
│  React / Next /  │  HTTPS     │  /api/v1/auth_token  │  ← login (api_auth_gateway)
│  Vue / vanilla   │ ─────────▶ │  /api/project-os/*   │  ← everything else
│                  │   JSON     │                      │
└──────────────────┘            └──────────────────────┘
        ▲                                  │
        └──── short-lived presigned ───────┘
              S3 links for files
```

### Talking to it

1. **Log in** — `POST /api/v1/auth_token` with `{"login", "password"}` (this endpoint
   comes from `api_auth_gateway`, which every service in this deployment already uses).
   It returns `access_token` and `refresh_token`.
2. **Send the token on every call** — header `access-token: <token>`.
   `Authorization: Bearer <token>` works too.

   ⚠ Use **dashes**, not `access_token`. Most WSGI front ends (nginx, gunicorn) silently
   drop headers containing underscores, which produces a 401 with no clue why. The API
   accepts `access-token`, `x-access-token` and `Authorization: Bearer`.
3. **Read the role from `GET /api/project-os/me`** and render the shell from it. The
   role is `tasker` | `pl` | `pm` | `admin`. Never trust it for authorisation — the server
   enforces scope on every call regardless of what the client believes.

Every response has the same shape, so one client-side wrapper handles all of them:

```json
{ "status_code": 200, "message": "ok", "errors": [], "data": { ... } }
```

```js
// the whole client, more or less
const api = async (path, opts = {}) => {
  const r = await fetch(BASE + '/api/project-os' + path, {
    ...opts,
    headers: {
      'access-token': localStorage.getItem('token'),
      ...(opts.body ? { 'Content-Type': 'application/json' } : {}),
      ...opts.headers,
    },
  });
  const body = await r.json();
  if (body.status_code >= 400) throw new Error(body.message);
  return body.data;
};
```

### CORS

Routes are registered with `cors='*'` and answer the `OPTIONS` preflight, so a frontend
served from its own origin works out of the box.

One consequence worth knowing before you start: a wildcard CORS origin **cannot** be
combined with cookie credentials. A cross-origin frontend must therefore authenticate
with the token header, not a session cookie. If you would rather use cookies, serve the
frontend from the same origin as Odoo (reverse-proxy `/api` and the app under one host)
and either works.

### Screens → endpoints

| Screen | Calls |
|---|---|
| login | `POST /api/v1/auth_token` |
| app shell | `GET /me` |
| Tasker — main | `GET /me`, `GET /me/onboarding` |
| Tasker — onboarding | `GET /me/onboarding`, `POST /me/onboarding/sop`, `POST /me/onboarding/training`, `GET+POST /me/assessment` |
| Tasker — stagelist / feedback | `GET /me/form?form_type=…`, `POST /entries` |
| Tasker — knowledge | `GET /projects/<id>/folders`, `GET /documents/<id>/download` |
| pod lead — my pod | `GET /roster`, `PATCH /roster/<employee_id>` |
| pod lead — review | `GET /counts`, `GET /submissions`, `GET /submissions/<id>` |
| PM — projects | `GET/POST /projects`, `GET /projects/<id>` |
| PM — kickoff | `GET/POST /projects/<id>/folders`, `POST /folders/<id>/documents`, `POST /projects/<id>/training`, `POST /projects/<id>/assessment`, `PUT /templates/<id>`, `POST /templates/<id>/publish`, `POST /projects/<id>/activate` |
| PM — allocation | `GET /allocations`, `POST /allocations/bulk`, `POST /allocations/<id>/release` |
| PM — analytics | `GET /analytics/overview`, `GET /counts` |
| anyone — history | `GET /employees/<id>/history`, `GET /projects/<id>/history` |

---

## What is in the module

```
models/          20 models — the whole domain
controllers/     55 REST endpoints under /api/project-os
  utils.py         auth, role gating, request parsing, uniform responses
  projects.py      projects, folders, documents, training, assessment, forms
  work.py          onboarding, filling, review, roster, allocation
  history.py       employee history, project history, analytics
security/        4 groups, 39 record rules, 51 ACLs
data/            sequences, crons, the allocation email, config defaults
demo/            the demo organisation (demo installs only)
migrations/      security-record unfreeze for 1.0.0 → 1.0.1
tests/           96 tests
views/           settings + a read-only history window. No UI for the four roles —
                 that is the frontend's job (see above)
hooks.py         pre/post-init DDL the ORM cannot express
```

---

## The four roles

| Role | Group | Sees | Does |
|---|---|---|---|
| **Tasker** — Tasker | `group_epo_tasker` | themselves | completes onboarding, fills the stagelist and the feedback form |
| **PL** — Pod Lead | `group_epo_pod_lead` | their pod | owns the daily roster, approves leave, reviews their pod's submissions |
| **PM** — General Program Management | `group_epo_pm` | org-wide | creates projects, fills knowledge/training/assessment, builds and publishes forms, allocates people, waives onboarding |
| **Admin** | `group_epo_admin` | everything | voids submissions, unlocks payroll-closed roster days, reads the audit log |

The groups are a ladder — each implies the one below, so a PM can do anything a Pod
Lead can. The *record rules* then narrow what each level may read.

Access is granted through **`epo.role.assignment`**, not by editing groups by hand.
A grant is effective-dated, so "who was Pod Lead when this was reviewed?" has an answer
a mutable group membership could never give. Writing a grant syncs `res.users.group_ids`
automatically; revoking is a date, and the record of having held the role survives.

---

## Model map

```
epo.pod                  seating / supervision group (a grouping, never the allocation unit)
epo.role.assignment      effective-dated role grants  → drives res.users groups
hr.employee (extended)   the person: pod, seat, engagement, current role, project history
epo.roster.day           ONE ROW PER PERSON PER DAY — "what are they doing today"

ethara.project (extended) THE project registry, shared with ethara_project. This module
                         adds the delivery side to it: is_project_os, os_state, the
                         go-live gate and the unique project code. There is no second
                         project table — `state` stays the commercial lifecycle the
                         budget subsystem drives, `os_state` is setup/active/archived.
epo.folder                 the filing cabinet: Knowledge/* and Management/* (tree)
epo.document               files (S3) and links inside those folders
epo.training               live or recorded sessions
epo.assessment.link        the LINK to a paper in etp.assessment (or a remote system).
                           Project OS authors no questions: one bank per deployment.
epo.assessment.result      the attempt, local and authoritative — pulled from
                           etp.assessment.evaluator, matched applicant -> employee

epo.form.template          one engine, two form_types: stagelist + feedback
epo.form.section
epo.form.field
epo.form.entry           the submission ledger — APPEND ONLY
epo.form.value             one answer per field

epo.allocation           "who is on this project, since when, at what %"
epo.allocation.phase       time inside that: onboarding / training / assessment / tasking …
epo.onboarding           the SOP → Training → Assessment gate, per person per project

epo.timeline.event       ONE append-only history: read by employee OR by project
epo.audit.log            admin overrides only — void, unlock, waive, retake, override
epo.demo                 builds the demo organisation (demo installs only)
```

### Why allocation and roster are two models

They answer two different questions and merging them loses one:

* `epo.allocation` — **membership**: on this project from this date to that date. It
  outlives the project, the pod and the person's departure, because a delivery report a
  year from now still has to say who worked on what.
* `epo.roster.day` — **today**: someone allocated for three months is, on any given day,
  tasking or in training or on leave or blocked.

`epo.allocation.phase` sits underneath the allocation and records contiguous stretches
of each phase with a day count. It is maintained **from the roster** — a lead sets a
status each morning and the phase log accumulates. That is what makes these answerable
without anyone keeping a second record by hand:

* how long did this person spend in onboarding vs training vs tasking, on each project?
* how many days from joining to first productive day (`days_to_productive`)?
* how much of this project went into getting people ready?

---

## How a project is created

```
POST /api/project-os/projects   { "name": "Multimango Batch 7" }
   │
   ├─ code generated        EXT-2026-0007        ← unique, never typed by accident
   ├─ state                 setup                ← not allocatable yet
   └─ folders built         the full skeleton, immediately
```

### The code

Every project has a unique code, because the code is the handle everyone *outside* this
system uses — it goes on the delivery report, in the channel name, on the invoice line.

* **Generated, not typed.** `EXT-2026-0007` for client work, `INT-2026-0003` for
  internal, from two separate `ir.sequence` series. Two GPMs creating projects on the
  same morning cannot collide.
* **The year is in it**, because "MM-0042" tells you nothing three years later.
* **A code can still be supplied** when a client mandates one. It is uppercased and
  whitespace-normalised (`"  mm 2041 "` → `MM-2041`), then checked against a
  case-insensitive unique index.
* **A project cannot lose its code** — `write({'code': ''})` is refused.

### The folders

Created with the project, identically every time, so a PM never faces an empty screen
and a Tasker always knows where to look:

```
EXT-2026-0007/
├── Knowledge/                 ← what a Tasker reads before tasking
│   ├── SOP/                   ← mandatory: no SOP, no go-live
│   ├── Common Errors/
│   ├── Task Videos/
│   └── Other/
└── Management/                ← what the PM keeps about the engagement
    └── Client Documents/      ← contracts, briefs, sign-offs — nests freely below
```

The two sides behave differently on purpose:

| | Knowledge | Management |
|---|---|---|
| shape | **fixed** — four folders, on every project | **free** — nest to any depth |
| why | so "read the SOP" means the same thing everywhere, and the go-live gate has a stable question to ask | one engagement has a single MSA, the next has forty files across six phases |
| new subfolders | refused (put extra material in *Other*) | allowed |
| who can read it | anyone allocated to the project | **PM and Admin only** |

Management is hidden by `ir.rule`, not by the API — a Pod Lead calling
`GET /projects/<id>/folders` does not see a Management branch at all, and fetching the
folder by id returns 403. Client contracts are not a Tasker's business, and the
honest way to say so is to leave them out of the tree rather than show a folder that
errors when opened.

System folders (`Knowledge`, `SOP`, `Management`, `Client Documents`, …) cannot be
renamed, moved or deleted. Deleting *any* folder that still holds documents is refused —
a folder delete must never quietly take files with it.

### Where the files live

**The folders are an S3 layout.** A document is either an object in the bucket or an
external link — there is no third case, and no second storage path:

| | |
|---|---|
| **S3** | every uploaded file, without exception |
| **link** | nothing is stored |
| no bucket configured | the upload is **refused** with a message saying so |

There is deliberately no fallback. Storing some documents in Odoo and some in S3 would
put two files in the same folder in two different places, and "where is this file?"
would stop having one answer. The trade-off is real and accepted: a PM cannot upload
an SOP until the bucket is set up. Links need no storage at all, so a project can still
be documented while that is being sorted out.

The S3 key mirrors the visible path, so an operator browsing the bucket sees the same
shape as a PM browsing the UI:

```
project-os/ext-2026-0007/knowledge/sop/9f2c1ab77e04-induction-v3.pdf
project-os/ext-2026-0007/management/client-documents/phase-1/3b81de09aa12-signed-sow.pdf
```

The leaf carries a random prefix so re-uploading `contract.pdf` never silently
overwrites last month's `contract.pdf` — both objects exist and both show in the folder.

**Nothing is ever served from a public URL.** `GET /documents/<id>/download` checks the
caller may read the document and then mints a presigned link that expires (default 300
seconds, configurable). This is deliberately *not* `s3.connector.upload_to_s3()`, which
sets `ACL='public-read'` — correct for product images, wrong for a client MSA.

Storage is configured under **Settings → Project OS → Document storage**: pick the
`s3.connector` record to use and the download-link lifetime.

---

## The staffing bar

A project can demand a higher score than the assessment paper's own pass mark — the
paper passes at 60, this client's work only takes people who scored 80 somewhere.

```
project.min_assessment_score = 80      set by the PM, 0 = no bar

GET /projects/<id>/candidates
  ✓ Mira    best score 88   eligible
    Noor    best score 40   scored 40, needs 80
    Ravi    no score yet    no assessment score yet
    Sam     best score 91   already on this project
```

The list returns **everybody**, not just the eligible — a staffing screen that silently
omits people cannot tell the PM the difference between "nobody qualifies" and "the
filter is broken". `considered_count` and `eligible_count` come back alongside it.

The bar is checked when somebody is **put on the project**, not later once they have
already started reading the SOP. It is compared against their best graded score across
*every* project, since the project's own assessment is taken during onboarding — i.e.
after allocation.

**Below the bar is still possible, deliberately.** Somebody has to be first on a
project, and a new joiner has no score at all:

```http
POST /api/project-os/allocations
{ "project_id": 7, "employee_id": 22,
  "override_reason": "First cohort — nobody has sat this paper yet." }
```

The reason is mandatory and lands in the audit log as `allocation_below_minimum`.

Both the bar and the person's score are **snapshotted onto the allocation**
(`min_score_applied`, `score_at_allocation`), so raising the bar next month never
retroactively unseats somebody who is already doing the work.

### The joining email

Being allocated sends the Tasker an email, because that is the moment they need to
know something and nobody has told them: the onboarding gate has just appeared in front
of them and until they clear it the stagelist stays locked. It names the project and
code, **their pod lead**, the pod, their role on it, the PM who owns it, and the exact
steps left — skipping training or assessment if the project has none.

A missing work email, or a mail server having a bad afternoon, is logged and swallowed:
it must not roll back the staffing decision.

---

## The go-live gate

A project cannot go active without **an SOP** and **a published stagelist**. This is a
database constraint, not a check in one API handler:

```sql
CHECK (state <> 'active' OR (has_sop AND has_stagelist))
```

Publishing a stagelist on a project that already has an SOP takes it live automatically.
Deleting the last SOP of a live project is refused, because it would silently un-gate
everybody already tasking.

Allocation requires a live project. Submission requires an allocation *and* a completed
onboarding. A stage with **no content auto-passes** — a project with no mandatory
training and no assessment gates on the SOP click alone, so the gate never blocks on
something the PM never set up.

---

## The assessment is federated

Assessments are **authored in another system** and pulled over an API. The design is
honest about what that means:

* You cannot foreign-key across a system boundary — integrity to the remote paper is
  eventual, never enforced.
* **The local side owns the result; the remote side owns the paper.** The gate reads
  `epo.assessment.result` and never calls out synchronously, so a remote outage cannot
  block somebody who has already been graded.
* **The answer key is never mirrored.** Anything that looks like one (`answer_key`,
  `correct`, `correct_index`, `solution`, …) is stripped on arrival by
  `strip_answer_key()` before the snapshot is stored.

Configure it in **Settings → Project OS**:

| Parameter | Meaning |
|---|---|
| `epo.assessment.base_url` | root of the assessment system |
| `epo.assessment.token` | bearer / `access_token` header value |
| `epo.assessment.system` | source name, recorded on every link for traceability |

Expected remote contract:

```
GET  {base}/api/assessments/{external_id}
     → {"title", "version", "pass_score", "questions": [{"id", "question", "options"}]}

POST {base}/api/assessments/{external_id}/attempts
     body → {"candidate_ref", "candidate_email", "answers"}
     → {"attempt_id", "score", "correct", "total"}
```

If grading fails (remote down), the attempt is stored as `submitted` and the
`_cron_grade_pending` job grades it later — a candidate never loses their work to
someone else's downtime.

---

## REST API

Base path `/api/project-os`. Auth is either an `access_token` header (via
`api_auth_gateway`) or an authenticated Odoo session, so one endpoint serves a mobile
client and the web SPA. Every response is
`{status_code, message, errors, data}`.

### Identity
| Method | Path | Min role |
|---|---|---|
| GET | `/me` | Tasker |

### Projects and content
| Method | Path | Min role |
|---|---|---|
| GET | `/projects` | Tasker (scoped: live projects they are on) |
| GET | `/projects/<id>` | Tasker |
| POST | `/projects` | PM |
| PATCH | `/projects/<id>` | PM |
| POST | `/projects/<id>/activate` | PM |
| POST | `/projects/<id>/archive` | PM |
| GET | `/projects/<id>/folders?with_documents=1` | Tasker (Management hidden below PM) |
| POST | `/projects/<id>/folders` | PM |
| GET | `/folders/<id>` | Tasker (403 on Management below PM) |
| PATCH | `/folders/<id>` | PM (system folders refused) |
| DELETE | `/folders/<id>` | PM (must be empty and non-system) |
| GET | `/folders/<id>/documents` | Tasker |
| POST | `/folders/<id>/documents` | PM (multipart, base64 or URL) |
| GET | `/documents/<id>/download` | Tasker — mints a short-lived link |
| DELETE | `/documents/<id>` | PM |
| GET | `/projects/<id>/knowledge` | Tasker — flat, grouped by folder, for onboarding |
| GET/POST | `/projects/<id>/training` | Tasker read / PM write |
| DELETE | `/training/<id>` | PM |
| POST | `/projects/<id>/assessment` | PM (link an external paper) |
| POST | `/assessments/<id>/sync` | PM |
| GET | `/projects/<id>/assessment/results` | PL (scoped) |
| POST | `/assessment-results/<id>/retake` | PM (reason required) |

### Forms
| Method | Path | Min role |
|---|---|---|
| GET | `/projects/<id>/templates` | Tasker |
| GET | `/templates/<id>` | Tasker |
| POST | `/templates` | PM |
| PUT | `/templates/<id>` | PM (**draft only** — 409 on a published form) |
| POST | `/templates/<id>/publish` | PM |
| POST | `/templates/<id>/new-version` | PM |

### The Tasker's day
| Method | Path | Min role |
|---|---|---|
| GET | `/me/onboarding` | Tasker |
| POST | `/me/onboarding/sop` | Tasker |
| POST | `/me/onboarding/training` | Tasker |
| GET | `/me/assessment` | Tasker (questions, never the key) |
| POST | `/me/assessment` | Tasker |
| GET | `/me/form?form_type=stagelist\|feedback` | Tasker |
| POST | `/entries` | Tasker |

### Roster, allocation, review
| Method | Path | Min role |
|---|---|---|
| GET | `/roster?date=&pod_id=` | PL |
| PATCH | `/roster/<employee_id>` | PL |
| POST | `/roster/<roster_id>/unlock` | Admin (reason required) |
| GET | `/onboarding?project_id=&pending_only=` | PL |
| POST | `/onboarding/<id>/waive` | PM (reason required) |
| GET | `/allocations?project_id=&open_only=` | PL |
| GET | `/projects/<id>/candidates` | PM — who clears the bar, and why the rest do not |
| POST | `/allocations` | PM (`override_reason` to go below the bar) |
| POST | `/allocations/bulk` | PM |
| POST | `/allocations/<id>/release` | PM |
| GET | `/submissions` | Tasker (scoped) |
| GET | `/submissions/<id>` | Tasker (scoped) |
| POST | `/submissions/<id>/void` | Admin (reason required) |
| GET | `/counts?form_type=` | Tasker (scoped) |

### History and analytics
| Method | Path | Min role |
|---|---|---|
| GET | `/employees/<id>/history` | Tasker (scoped) — a person's full history |
| GET | `/projects/<id>/history` | Tasker (scoped) — a project's full history |
| GET | `/timeline?employee_id=&project_id=&category=` | PL |
| GET | `/analytics/overview?date_from=&date_to=` | PL |

`POST /entries` accepts an `idempotency_key`; a retried request returns the original
entry instead of a duplicate that would inflate every count.

### What each role can actually read

Scope is enforced by `ir.rule`, so it holds for the REST API, the backend UI, XML-RPC
and reports alike. Verified by asserting what the ORM returns for a real pod-member
user, not by reading the controllers:

| | Tasker | pod lead | PM / Admin |
|---|---|---|---|
| their own submissions | ✓ | ✓ | ✓ |
| their pod's submissions | — | ✓ | ✓ |
| Knowledge of **projects they are on** | ✓ | pod's projects | all |
| Knowledge of other projects | — | — | ✓ |
| **Management** (client documents) | — | — | ✓ |
| form templates | own projects | pod's projects | all |
| role grants | own only | pod | all |
| audit log | — | — | Admin only |

Knowledge is deliberately *not* world-readable: a client's SOP and task videos describe
how that client's work is done. The allocation is what grants access to it.

> **Note on upgrades.** These rules load with `noupdate` off, so a corrected domain
> reaches existing databases. Version 1.0.0 shipped them frozen, and
> `migrations/19.0.1.0.1/pre-migration.py` unfreezes them — without it a security fix
> would land in the source and change nothing in production.


### Error handling

Every endpoint answers in the same envelope, and the status code means what it says:

| | |
|---|---|
| `401` | no token, or one that has expired |
| `403` | authenticated but not allowed — wrong role, or a record outside your scope |
| `404` | the id does not exist |
| `409` | the request conflicts with the record's state (editing a published form) |
| `400` | the request itself is wrong — a missing id, `limit=abc`, a link with no URL |
| `500` | a genuine server fault, and nothing else |

Malformed input never reaches the ORM as a raw `int()` call: required parameters are
coerced through helpers that name the offender (`project_id must be a number, got
'abc'`), pagination is bounded, and a catch-all turns any stray `ValueError`/`KeyError`
into a 400 rather than a 500. That distinction matters operationally — a 500 should mean
somebody needs paging, not that a client sent a typo.

Failures that are nobody's fault degrade instead of exploding: a mail server that is
down logs and lets the allocation stand, one person released overnight does not cost the
rest of the organisation their roster, and one ineligible name in a bulk allocation
returns eleven allocations and a note rather than an error and nothing.

---

## What the database refuses

Enforced in Postgres, not in application code, so nothing can route around it:

| | |
|---|---|
| project active without an SOP or a stagelist | `CHECK` |
| two roster rows for one person on one day | `UNIQUE` |
| "unable to task" with no reason | `CHECK` |
| on leave and on a project the same day | `CHECK` |
| same role granted twice over overlapping dates | `EXCLUDE USING gist` |
| same person allocated twice to one project, overlapping | `EXCLUDE USING gist` |
| two phases of one allocation overlapping | `EXCLUDE USING gist` |
| two published forms of the same type on one project | partial `UNIQUE` |
| a dropdown with no options | `@api.constrains` |
| a required section header (unsubmittable form) | `CHECK` |
| two answers for one field | `UNIQUE` |
| a duplicate submission from a retried POST | partial `UNIQUE` |
| more than one draft per person per form | partial `UNIQUE` |
| two assessment attempts open at once | partial `UNIQUE` |
| a graded attempt with no timestamp | `CHECK` |
| a void / waiver / unlock / retake with no reason | `CHECK` |

The three `EXCLUDE` constraints need `btree_gist`, created by the module's
`pre_init_hook`. A Python check reads, decides, then writes — two concurrent requests
both pass it. These do not.

Deletion is refused where history matters: `epo.form.field` is `ondelete='restrict'`
from `epo.form.value`, so a field with answers cannot be deleted even if the
immutability trigger were somehow bypassed. That single word is the difference between
this and the prototype, where saving an edited form deleted every answer ever submitted.

---

## Demo data

Odoo 19 installs **without** demo data by default, so a production install is empty.
Ask for it explicitly:

```bash
odoo-bin -d <db> -i ethara_project_os --with-demo    # a worked example
odoo-bin -d <db> -i ethara_project_os                # nothing but the module
```

You get a small but complete organisation: two pods with leads, one PM, six pod
members, and three projects that between them show every state the system has —

| | |
|---|---|
| **Multimango Batch 7** | live and fully staffed. Three weeks of roster history behind it, so the phase log, the day counts and the timeline all have real content. One person has already been released, one is still in onboarding. 33 submissions. |
| **Atlas Annotation** | live with a minimum score of 80, so the candidate list has people on both sides of the bar — including one allocated below it with a recorded reason. |
| **Internal Tooling Revamp** | deliberately stuck in setup, showing its go-live blockers. |

Everyone logs in with password `demo1234` — `gita@demo.ethara` (PM),
`piyush@demo.ethara` (PL), `mira@demo.ethara` (Tasker).

It is built by **calling the real business logic**, not by inserting rows: projects go
through the go-live gate, people through the onboarding gate, the roster drives the
phase log, submissions through the admission guard. So if any of it were broken the
demo would fail at install time, and the numbers on the demo screens are the numbers
the system actually computes. Building it is what surfaced three of the bugs this
module no longer has.

**Removing it** is deleting `demo/` and the `"demo"` key in `__manifest__.py` — nothing
else in the module references it. Demo records are also findable by hand: projects are
coded `DEMO-…`, people have `@demo.ethara` emails.

---

## Install

```bash
odoo-bin -d mydb -i ethara_project_os --stop-after-init              # empty
odoo-bin -d mydb -i ethara_project_os --with-demo --stop-after-init  # worked example
```

Depends on `hr`, `hr_holidays`, `hr_attendance`, `mail`, `api_auth_gateway` and
`s3_connector`.

The install runs two hooks: **pre-init** creates the `btree_gist` extension, which has
to exist before the ORM applies the three EXCLUDE constraints, and **post-init** adds
the covering indexes and two reporting views the ORM cannot express.

### Upgrading

```bash
odoo-bin -d <db> -u ethara_project_os --stop-after-init
```

`migrations/19.0.1.0.1/pre-migration.py` unfreezes the security records shipped by
1.0.0, which were written with `noupdate` and so would have ignored every later fix.
Record rules now load updatable on purpose — a corrected domain has to be able to reach
a database that is already running.

### Scheduled jobs

| Job | Cadence | Why |
|---|---|---|
| carry the roster forward | nightly 00:20 | without it the board is empty every morning |
| stamp approved leave onto the roster | nightly 00:40 | leave approved weeks ago still lands on the right day |
| grade pending assessments | every 30 min | picks up attempts the source could not grade in the moment |
| refresh assessment snapshots | every 6 h | keeps the local copy of the paper current |
| lock roster days past the payroll window | nightly 02:00 | stops accidental retro-edits |

### How to test it

Three levels, cheapest first.

**1 · The test suite** — proves each rule in isolation. No demo data needed.

```bash
odoo-bin -d epo_try -u ethara_project_os --test-enable \
         --test-tags /ethara_project_os --stop-after-init --http-port=8171
```

Look for `0 failed, 0 error(s) of 96 tests`. A non-zero exit code means a failure.

> **`--http-port` is not optional if anything else is already running.** Odoo binds the
> port even with `--stop-after-init`, and even with `--no-http`; if 8069 is taken it
> exits with `Address already in use` before it reaches a single test. Pick any free
> port. And run it without piping to `grep` the first time — a filter on the result line
> hides exactly that error and leaves you staring at a blank screen.

**2 · The dry run** — walks one realistic organisation end to end and prints what it
finds, so you can read the numbers rather than trust a green tick.

```bash
odoo-bin -d demo_db -i ethara_project_os --with-demo --stop-after-init --http-port=8171
odoo-bin shell -d demo_db --no-http < custom_addons/ethara_project_os/tools/dry_run.py
```

42 checks across ten areas — roles, the go-live gate, the folder cabinet, time per
phase, the onboarding gate, the staffing bar, the ledger, per-role scoping, both history
readings, and the nightly jobs actually running. It rolls back, so it changes nothing.

**3 · The API** — every endpoint, as all three roles, against a running server.

```bash
odoo-bin -d demo_db                       # leave it running
./custom_addons/ethara_project_os/tools/api_smoke.sh http://localhost:8069
```

It ends with five deliberate failures (403 403 400 400 401). If those come back 200,
something is wrong with the scoping.

### What the tests cover

96 tests. They are grouped by the thing that would go wrong, not by the class under
test:

| Group | What it holds down |
|---|---|
| roles | grants drive the login level, the ladder holds, revoking is a date |
| kickoff gate | no SOP or stagelist means no go-live; the last SOP cannot be removed |
| form engine | published forms are immutable; a new version arrives carrying the whole form; publishing v2 supersedes v1 |
| submission guard | allocation, onboarding, required fields, idempotency, append-only |
| allocation & phases | overlap, capacity, the staffing bar and its override, phases stay inside their window, backfilling a past day does not disturb the present |
| roster | one row per person per day, reasons required, no far-future rows, the nightly carry-forward survives a released person |
| onboarding | a stage with no content auto-passes; **passing the assessment opens the gate**; waivers are audited |
| storage | uploads need a bucket; links do not; the folder skeleton is fixed |
| isolation | cross-project reads, Management visibility, per-role scope |
| API | every malformed input is a 400 that names the parameter |

Add `--with-demo` to the same command to run them against the demo organisation.
