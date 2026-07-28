# Ethara Project OS — End-to-End Workflow

From a login arriving with no identity, through role derivation and two independent
authorization layers, to a project being created, staffed, worked and archived.

Three modules cooperate:

| Module | Owns |
|---|---|
| `api_auth_gateway` | tokens, `api.role`, the endpoint-grant gate for the platform API |
| `pod_roles` | the four organisational roles as `api.role` records, and the app menu tree |
| `ethara_project_os` | the four Odoo groups, the project lifecycle, and its own 52-route API |

**Vocabulary.** Four levels, weakest to strongest — `tasker → pl → pm → admin`. `PM` is
**Programme Manager**, the level *above* Pod Lead. Before `19.0.1.5.0` this module called
that level `gpm` and used `pm` for the bottom level; anything still using the old words is
reading the ladder upside down. The names now match `pod_roles` exactly, which is the point.

---

## Part 1 — Where a role comes from

Project OS owns **no role table**. It owns four Odoo groups (because ACLs and record rules
can only be written against groups) and derives membership from `res.users.user_role`,
which points at an `api.role` record.

```mermaid
flowchart TD
    subgraph REG["REGISTRY · pod_roles"]
        R1["api.role: Admin<br/>user_type = admin"]
        R2["api.role: PM<br/>user_type = PM"]
        R3["api.role: PL<br/>user_type = PL"]
        R4["api.role: Tasker<br/>user_type = Tasker"]
    end

    U["res.users.user_role"] --> MAP
    R1 --> U
    R2 --> U
    R3 --> U
    R4 --> U

    subgraph MAP["MAPPING · epo_role_map.py"]
        M1["ROLE_XML_IDS<br/>match by xml-id"]
        M2["ROLE_USER_TYPES<br/>match by user_type,<br/>exact string, case-insensitive"]
        M1 --> LVL["level_for_api_role()<br/>strongest match wins"]
        M2 --> LVL
    end

    LVL --> SYNC["res.users._epo_sync_groups()"]
    GRANT["epo.role.assignment<br/>effective-dated grant"] -.->|"only if the<br/>registry has no answer"| SYNC

    SYNC --> G1["group_epo_tasker"]
    SYNC --> G2["group_epo_pod_lead"]
    SYNC --> G3["group_epo_pm"]
    SYNC --> G4["group_epo_admin"]

    G1 --> LADDER["implied_ids ladder<br/>tasker ⊂ pl ⊂ pm ⊂ admin"]
    G2 --> LADDER
    G3 --> LADDER
    G4 --> LADDER
```

### The mapping, as shipped

| `pod_roles` record | `user_type` | Project OS level | Odoo group |
|---|---|---|---|
| `role_admin` | `admin` | `admin` | `group_epo_admin` |
| `role_pm` | `PM` | `pm` | `group_epo_pm` |
| `role_pl` | `PL` | `pl` | `group_epo_pod_lead` |
| `role_tasker` | `Tasker` | `tasker` | `group_epo_tasker` |

Two routes into the map, because a role can arrive either way:

1. **xml-id** — shipped in a data file, explicit and versionable.
2. **`user_type`** — a record somebody created in Settings, which has no xml-id at all.
   Without this route, UI-created roles would never map.

`user_type` matching is **exact string, case-insensitive — never substring**. This is
load-bearing: `TPM` contains `PM`, and a substring rule would silently map every TPM onto
the programme-management level. `role_tpm_technical` is deliberately unmapped — TPM is a
different job, and guessing would hand out project creation and allocation rights.

### Two safety properties

- **A missing role resolves to nothing, it does not raise.** `resolve_role_ids` skips
  records it cannot find, so a half-populated registry degrades instead of crashing.
- **`epo.role.assignment` is the fallback.** While `user_role` yields no level, groups come
  from the effective-dated grant. The moment a mapped `api.role` is set, that wins.

```mermaid
flowchart LR
    A["user.write({'user_role': ...})"] --> B["ResUsers.write override"]
    B --> C{"level_for_api_role<br/>returns a level?"}
    C -->|yes| D["wanted = that one group<br/>(the ladder implies the rest)"]
    C -->|no| E["wanted = groups from<br/>current epo.role.assignment rows"]
    D --> F["strip every Project OS group<br/>not in wanted, add the one that is"]
    E --> F
    F --> G["user.sudo().write({'group_ids': ...})"]
```

> **Note the direction of failure.** Because any registry answer is authoritative, giving a
> PM-by-grant a `user_role` of PL *demotes* them and ignores the grant. Fail-safe (access is
> lost, never gained) but silent.

---

## Part 2 — Authentication and authorization

There are **two independent systems**. Conflating them is the single easiest mistake to
make here.

```mermaid
flowchart TD
    CLIENT["client<br/>(Flutter app / SPA / curl)"]

    CLIENT -->|"access_token header<br/>→ /api/v1/... /api/v2/..."| GW
    CLIENT -->|"access_token header or session<br/>→ /api/project-os/..."| POS

    subgraph GW["A · PLATFORM API — api_auth_gateway"]
        GW1["@validate_token"] --> GW2{"token valid<br/>and not expired?"}
        GW2 -->|no| GW401["401"]
        GW2 -->|yes| GW3{"user has a<br/>user_role?"}
        GW3 -->|no| GW403A["403 · No role assigned"]
        GW3 -->|yes| GW4["role.endpoint_ids<br/>filtered by _endpoint_allowed()"]
        GW4 --> GW5{"any grant<br/>matches this path?"}
        GW5 -->|no| GW403B["403 · Role not allowed"]
        GW5 -->|yes| GWOK["handler runs<br/>680 endpoints"]
    end

    subgraph POS["B · PROJECT OS API — ethara_project_os"]
        P1["route(...) → api_route(min_role=...)<br/>auth='public', own check inside"] --> P2{"authenticated?"}
        P2 -->|no| P401["401 · Not signed in"]
        P2 -->|yes| P3["role = user._epo_role()<br/>from GROUP MEMBERSHIP"]
        P3 --> P4{"role present?"}
        P4 -->|no| P403A["403 · No Project OS role"]
        P4 -->|yes| P5{"ROLE_RANK[role] >=<br/>ROLE_RANK[min_role]?"}
        P5 -->|no| P403B["403 · Needs a higher role"]
        P5 -->|yes| P6{"linked to an<br/>hr.employee?<br/>(admin exempt)"}
        P6 -->|no| P403C["403 · No employee record"]
        P6 -->|yes| POK["handler runs with ctx<br/>52 routes"]
    end
```

### A · Platform API — grant-based

`validate_token` resolves the token to a user, reads `user_role`, and checks the request
path against that role's `api.role.endpoint` grants. This gates all **680** registered
endpoints — taskforge (112), etp_projects (77), ethara_project (61), etp_assessment_ext
(46), hrms, wiki and ~130 more prefixes.

The convention for granting is narrow and per-module: the module that owns an endpoint
declares who may call it, one `(role, endpoint, method)` triple at a time.

```xml
<record id="role_ep_candidate_me_get" model="api.role.endpoint">
    <field name="role_id" ref="api_auth_gateway.role_candidate_technical"/>
    <field name="api_end_point_id" ref="endpoint_api_v1_candidates_me"/>
    <field name="method">GET</field>
</record>
```

### B · Project OS API — rank-based

Project OS registers 52 routes under `/api/project-os/` with `auth='public'` and does its
own check inside, so one route serves both a token client and a session client. **None of
its endpoints are in `api.endpoint`**, so the grant system does not apply to it at all.

Authorization is a rank floor per route:

```python
ROLE_RANK = {'tasker': 0, 'pl': 1, 'pm': 2, 'admin': 3}
if ROLE_RANK[role] < ROLE_RANK[min_role]:
    return respond(status=403)
```

| `min_role` | routes | what they are |
|---|---:|---|
| `tasker` | 2 | own onboarding, own form |
| `pl` | 7 | pod roster, pod submissions |
| `pm` | 21 | create/activate projects, knowledge, templates, allocation, analytics |
| `admin` | 2 | void a submission, unlock a payroll-locked roster day |

`_epo_role()` reads **group membership**, not the employee record — so a service account
with the admin group and no employee row still works. Membership itself comes from Part 1,
so the two always agree.

Record rules then narrow *which rows* each level sees, independently of route access:

| Level | Scope |
|---|---|
| `tasker` | themselves |
| `pl` | themselves + their pod + direct reports |
| `pm` | org-wide |
| `admin` | org-wide, plus audit and correction paths |

---

## Part 3 — Project setup

```mermaid
stateDiagram-v2
    [*] --> setup: create project.project<br/>is_project_os = true<br/>code = EPR/YYYY/0001
    setup --> setup: fill knowledge folder<br/>build stagelist<br/>set training + assessment URL
    setup --> active: action_activate()<br/>GATE has_sop AND has_stagelist
    active --> archived: action_archive()<br/>active = false
    setup --> archived: abandoned before launch
    archived --> [*]
```

A project is an **extension of Odoo's native `project.project`** (`_inherit`), not a
separate model. `ethara_state` is deliberately namespaced so the native `stage_id` stays
free for kanban columns.

### The go-live gate

```mermaid
flowchart LR
    D["epo.document<br/>category = sop<br/>in the knowledge folder"] --> HS["has_sop"]
    T["epo.form.template<br/>form_type = stagelist<br/>state = published"] --> HT["has_stagelist"]
    HS --> GATE{"both true?"}
    HT --> GATE
    GATE -->|yes| A["ethara_state = active<br/>allocation permitted"]
    GATE -->|no| B["stays in setup<br/>blockers listed on the form"]
```

Enforced at the database level, not just in Python:

```sql
CHECK (ethara_state <> 'active'
       OR (COALESCE(has_sop, false) AND COALESCE(has_stagelist, false)))
```

`COALESCE` is not decoration — both columns are nullable and **a CHECK passes on NULL**, so
without it a project could be stored `active` with no SOP and no stagelist. Closed in
`19.0.1.3.1`.

### The filing cabinet

Every project gets the same skeleton on creation, so nobody faces an empty screen:

```
Knowledge/                     ← visible to anyone allocated to the project
├── SOP/                       ← mandatory; drives has_sop
├── Common Errors/
├── Task Videos/
└── Other/
Management/                    ← PM and Admin only
└── Client Documents/
```

Documents are stored in S3 via `s3.connector` (`s3_key` + presigned URLs, never public) or
as a link. `read_bytes()` degrades cleanly when no bucket is configured.

### Forms are versioned, not edited

```mermaid
stateDiagram-v2
    state "draft (v+1)" as draft2
    state "published (v+1)" as published2

    [*] --> draft: create template
    draft --> published: action_publish()<br/>needs >= 1 answerable field
    draft --> [*]: unlink (allowed)
    published --> draft2: action_new_version()<br/>clones to a new draft
    draft2 --> published2: action_publish()
    published --> archived: superseded on publish<br/>of the new version
    archived --> [*]: kept — historical entries<br/>still point here
```

A published template is immutable — its fields and sections refuse create, write and
unlink. Publishing a new version archives the old one; historical `epo.form.entry` rows keep
pointing at the archived version so their answers stay readable. One published template per
`(project, form_type)`, enforced by a partial unique index.

---

## Part 4 — Onboarding

```mermaid
flowchart TD
    START["epo.onboarding row<br/>UNIQUE (employee_id, project_id)"] --> SOP
    SOP["read the SOP<br/>sop_done = true"] --> TR
    TR["complete training<br/>training_done = true"] --> AS
    AS["sit the external assessment<br/>ethara.assessment.url<br/>Google Form / TestGorilla"] --> V
    V["PL/PM/Admin reads the result<br/>and ticks it<br/>assessment_passed = true<br/>+ verifier + timestamp + score"] --> U

    U{"unlocked =<br/>sop_done AND training_done<br/>AND assessment_passed"}
    U -->|true| OK["may submit the stagelist"]
    U -->|false| NO["submission refused,<br/>blockers named"]

    W["action_waive()<br/>PM or Admin, reason required"] -.->|"skips the gate,<br/>mail.thread audit"| OK
```

Odoo does **not** grade. It stores only the URL and the pass mark; a human reads the result
in the external application and records it. That makes the tick the one place a person
asserts something the system cannot verify, so it is stamped and audited:

| Constraint | Forbids |
|---|---|
| `_verdict_stamped` | `assessment_passed` true with no verifier |
| `_score_bounds` | a score outside 0–100 |
| `_sop_stamped` / `_training_stamped` | a done flag with no timestamp |
| `_waiver_audited` | a waiver with no reason or no waiver |
| `_uniq` | two onboarding rows for one (employee, project) |

### Pushing SOP + training to the assessment app

```mermaid
sequenceDiagram
    participant PM
    participant POS as ethara_project_os
    participant APP as etp_assessment
    PM->>POS: action_send_to_assessment()
    POS->>POS: collect knowledge docs + training
    Note over POS: files read BEFORE anything<br/>is created over there
    POS->>APP: create etp.assessment.prompt
    POS->>APP: create prompt.resource per file
    Note over APP: extraction, skill selection<br/>and question generation<br/>all happen here, driven by a person
    POS-->>PM: prompt id + counts stamped on the project
```

Project OS **calls** the assessment app and never modifies it. Link documents cannot be
attached (the far side needs a file), so their URLs go across as notes and the summary says
how many went each way rather than pretending everything was sent.

---

## Part 5 — Staffing

```mermaid
flowchart TD
    BAR["PM sets min_assessment_score<br/>on the project"] --> CAND
    CAND["candidates()<br/>name · seat · score · current project · leave"]
    CAND --> FILTER{"score >=<br/>the bar?"}
    FILTER -->|no| HIDE["hidden<br/>(include_below_bar reveals them)"]
    FILTER -->|yes| SHOW["shown, sorted by<br/>eligible, not on leave, score desc, name"]
    SHOW --> LEAVE["approved hr.leave in the<br/>next 14 days is flagged"]
    LEAVE --> ALLOC["epo.allocation<br/>employee + project + pct"]
    ALLOC --> OVR{"below the bar?"}
    OVR -->|yes| REASON["override_reason REQUIRED"]
    OVR -->|no| DONE["allocated"]
    REASON --> DONE
    DONE --> MAIL["email to the Tasker:<br/>project, pod lead, steps left"]
```

Allocation invariants, all enforced in the database:

| Constraint | Forbids |
|---|---|
| `epo_allocation_no_self_overlap` | the same person on the same project twice over overlapping dates (Postgres `EXCLUDE`, needs `btree_gist`) |
| `epo_allocation_pct_bounds` | a percentage of 0 or less, or above 100 |
| `epo_allocation_range_sane` | `date_to` before `date_from` |
| `epo_allocation_release_audited` | closing an allocation without recording who released the person |

Plus a Python check: combined open allocations may not exceed the configured ceiling
(`epo.allocation.max_pct`, default 100, clamped to 1–1000 so a misconfiguration cannot lock
the whole organisation out of being staffed). Allocating to a project still in `setup` is
refused.

---

## Part 6 — Daily tasking

```mermaid
flowchart TD
    subgraph MORNING["each working day"]
        ROSTER["epo.roster.day<br/>UNIQUE (employee, date)"] --> STATUS
        STATUS{"tasking_status"}
        STATUS -->|tasking| TASK["project_id required"]
        STATUS -->|onboarding / training / assessment| PREP["no project needed"]
        STATUS -->|leave| LV["project must be EMPTY"]
        STATUS -->|unable_to_task| ISSUE["issue text REQUIRED"]
        STATUS -->|bench| BENCH["unassigned"]
    end

    TASK --> ATT["hr.attendance check-in<br/>→ present = true"]
    ATT --> FILL["fill the published stagelist<br/>epo.form.entry (draft)"]
    FILL --> SUB{"onboarding unlocked<br/>for this project?"}
    SUB -->|no| REJ["refused, blockers named"]
    SUB -->|yes| SUBMITTED["action_submit()<br/>→ submitted, frozen"]
    SUBMITTED --> REVIEW["PL reviews their pod"]
    REVIEW --> ANALYTICS["PM reads org-wide analytics<br/>aggregates over the ledger"]
```

```mermaid
stateDiagram-v2
    [*] --> draft: create entry
    draft --> submitted: action_submit()<br/>gated on onboarding<br/>+ required fields present
    submitted --> void: action_void()<br/>ADMIN only, reason required
    void --> [*]
    submitted --> [*]: frozen ledger row
```

The ledger is append-only by design. A submitted entry refuses changes to its
`business_date`, `employee_id` and `state`, and refuses deletion. One entry per
`(template, employee, business_date)`. An idempotency key makes a retried POST return the
original entry rather than a duplicate — the difference between a flaky network and a
corrupted count.

A roster day inside a payroll-locked period can only be changed by an Admin
(`action_unlock`).

---

## Part 7 — Leave, running in parallel

```mermaid
sequenceDiagram
    participant T as Tasker
    participant PL as Pod Lead / PM
    participant HR as hr.leave
    participant R as epo.roster.day
    T->>HR: request leave (state=confirm)
    PL->>HR: action_validate() → state=validate
    HR->>R: upsert each covered day<br/>tasking_status = leave, project cleared
    Note over R: DB constraint forbids<br/>leave + a project on the same day
    R-->>T: cannot submit a stagelist<br/>(no active tasking allocation)
```

Leave and tasking are mutually exclusive at the database level
(`epo_roster_day_leave_unprojected`), so the two subsystems cannot disagree.

---

## Part 8 — Winding down

```mermaid
flowchart LR
    A["work complete"] --> B["close open allocations<br/>release_reason required"]
    B --> C["action_archive()<br/>ethara_state = archived<br/>active = false"]
    C --> D["knowledge folder retained<br/>submissions retained<br/>timeline retained"]
    D --> E["history endpoints still answer:<br/>who worked here, what they filed,<br/>who was PL at the time"]
```

Nothing is deleted. `epo.role.assignment` is effective-dated, so *"who was Pod Lead when
this was reviewed?"* has an answer that mutable group membership could never give.

---

## The whole thing, one diagram

```mermaid
flowchart TD
    subgraph ID["IDENTITY"]
        A1["pod_roles → api.role"] --> A2["res.users.user_role"]
        A2 --> A3["epo_role_map"]
        A3 --> A4["group_epo_tasker / pod_lead / pm / admin"]
        A5["epo.role.assignment"] -.->|fallback| A4
    end

    subgraph AUTH["AUTHORIZATION"]
        A4 --> B1["Project OS API<br/>min_role rank floor<br/>52 routes"]
        A2 --> B2["Platform API<br/>endpoint grants<br/>680 routes"]
        A4 --> B3["record rules<br/>which rows, not which routes"]
    end

    subgraph SETUP["SETUP · PM"]
        B1 --> C1["create project"]
        C1 --> C2["folder skeleton"]
        C2 --> C3["SOP → has_sop"]
        C3 --> C4["publish stagelist → has_stagelist"]
        C4 --> C5["training + assessment URL"]
        C5 --> C6["GATE → active"]
    end

    subgraph ON["ONBOARDING · Tasker + verifier"]
        C6 --> D1["onboarding row"]
        D1 --> D2["sop_done"]
        D2 --> D3["training_done"]
        D3 --> D4["external assessment"]
        D4 --> D5["verified → assessment_passed"]
        D5 --> D6["unlocked"]
    end

    subgraph STAFF["STAFFING · PM"]
        C6 --> E1["set the score bar"]
        E1 --> E2["candidates() shortlist"]
        E2 --> E3["epo.allocation"]
        E3 --> E4["welcome email"]
    end

    subgraph DAY["DAILY OPS"]
        E3 --> F1["roster day"]
        D6 --> F2["submit stagelist"]
        F1 --> F2
        F2 --> F3["PL review"]
        F3 --> F4["PM analytics"]
    end

    subgraph LV["LEAVE · parallel"]
        G1["hr.leave validated"] --> G2["roster day = leave"]
        G2 -.->|blocks| F2
    end

    F4 --> H1["archive · nothing deleted"]
```

---

## Data model

```mermaid
erDiagram
    RES_USERS ||--o| API_ROLE : "user_role"
    RES_USERS ||--o| HR_EMPLOYEE : "employee_id"
    HR_EMPLOYEE ||--o{ EPO_ROLE_ASSIGNMENT : "effective-dated grants"
    HR_EMPLOYEE }o--o| EPO_POD : "epo_pod_id"
    PROJECT_PROJECT ||--o{ EPO_FOLDER : "skeleton"
    EPO_FOLDER ||--o{ EPO_DOCUMENT : "contents"
    PROJECT_PROJECT ||--o{ EPO_TRAINING : ""
    PROJECT_PROJECT ||--o{ ETHARA_ASSESSMENT : "external URL"
    PROJECT_PROJECT ||--o{ EPO_FORM_TEMPLATE : "versioned"
    EPO_FORM_TEMPLATE ||--o{ EPO_FORM_SECTION : ""
    EPO_FORM_TEMPLATE ||--o{ EPO_FORM_FIELD : ""
    EPO_FORM_TEMPLATE ||--o{ EPO_FORM_ENTRY : "submissions"
    EPO_FORM_ENTRY ||--o{ EPO_FORM_VALUE : "answers"
    PROJECT_PROJECT ||--o{ EPO_ONBOARDING : "per employee"
    PROJECT_PROJECT ||--o{ EPO_ALLOCATION : "who is on it"
    HR_EMPLOYEE ||--o{ EPO_ALLOCATION : ""
    HR_EMPLOYEE ||--o{ EPO_ROSTER_DAY : "one per day"
    PROJECT_PROJECT ||--o{ EPO_TIMELINE_EVENT : "audit"
    PROJECT_PROJECT }o--|| HR_EMPLOYEE : "pm_id owner"
```

Key uniqueness rules:

| Model | Unique on |
|---|---|
| `project.project` | `lower(code)` where `code` is set |
| `epo.pod` | `lower(code)` among active pods |
| `epo.onboarding` | `(employee_id, project_id)` |
| `epo.roster.day` | `(employee_id, date)` |
| `epo.form.template` | `(project_id, form_type)` where published; `(project, form_type, version)` |
| `epo.form.entry` | `(employee_id, template_id)` **where state = draft** — one open draft at a time; `(employee_id, idempotency_key)` where a key was supplied |
| `epo.form.value` | `(entry_id, field_id)` |
| `epo.folder` | `lower(name)` within a parent; `slug` per project for system folders |

> **There is no per-day uniqueness on `epo.form.entry`.** Nothing stops a second
> *submitted* entry for the same `(template, employee, business_date)` — verified by
> replaying one entry's own answers as a new submission. The only protection is the
> idempotency key, and only when the client sends one. See *Known gaps*.

---

## Configuration

| `ir.config_parameter` key | Default | Effect |
|---|---|---|
| `epo.allocation.max_pct` | 100 | combined allocation ceiling, clamped 1–1000 |
| `epo.roster.carry_forward` | on | copy yesterday's roster forward |
| `etp_assessment.vertex_project_id` / `_location` / `_model` | — | required by the assessment app for question generation |
| `s3.connector` settings | — | bucket for documents; without it everything must be a link |

---

## Known gaps

Open findings from the strict review, listed so nobody mistakes this document for a clean
bill of health.

| Severity | Where | Issue |
|---|---|---|
| Critical | `pod_roles` + `api_auth_gateway` | every role is granted all 680 endpoints; the grant's `method` field is never enforced (`_endpoint_allowed` reads it only in a log line), so a Tasker's "GET" grant permits any verb |
| Critical | `project_project.action_send_to_assessment` | no `has_group` check in the body — the view's `groups=` is UI-only, so a read-only member can push project documents into another module. The other five gated buttons all check in code |
| High | `epo_form_entry._check_entry_open` | `@api.constrains('entry_id')` never fires on a `value_text` write, and `unlink()` runs no constraints — so answers on a *frozen* submission can be rewritten or deleted with `state` and `submitted_at` untouched |
| High | `epo_form_template.write` | guards only `form_type` and `project_id`, so `state` is writable; and `_recompute_gate()` is called only from `action_publish`. Un-publish then delete leaves a project `active` with `has_stagelist = true` and no stagelist at all |
| High | `epo.form.entry` | no uniqueness on `(template, employee, business_date)` — a second *submitted* entry for the same day is accepted, so any count aggregated over the ledger can be inflated by replaying a submission. `_one_draft` only stops two open drafts; the idempotency key only helps when the client supplies one |
| Medium | `action_send_to_assessment` | a second send orphans the first prompt; archived projects send; an SOP is not actually required |
| Medium | duplicate `api.role` records | `pod_roles` and `api_auth_gateway` both ship `Admin`/`PL`/`Tasker` with identical `name`, but 680 vs 0 endpoint grants — the picker shows two indistinguishable entries with opposite consequences |
| Low | `epo.roster.carry_forward` | `'no'`, `'off'`, `''`, `'garbage'` all read as **on**; only `'False'/'false'/'0'/'None'` disable it |
| Low | `ethara.assessment.url` | bare `http://` with no host passes validation |
| Low | `scope_pod_id` | displayed but never used for scoping |

### Breaking change in 19.0.1.5.0

The role rename reaches the API. These now emit the new vocabulary:

```
controllers/projects.py:117   'role': ctx.role          ← /me
controllers/work.py:326       'role': employee.epo_role
controllers/history.py:137    'role': r.role
```

A Programme Manager is now `"role": "pm"`. A stale client already knows `"pm"` — as *pod
member* — so it will not crash; it will treat the most privileged non-admin role as the
least privileged one. **Client releases must be coordinated with this upgrade.**

---

*Verified against `ethara_project_os` 19.0.1.5.0: 96/96 tests passing, full migration chain
1.0.1 → 1.5.0 applied on a populated database with group membership, project owners and
stored role values preserved.*
