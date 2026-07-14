# Kensei Tracker — Code Review & Quality Audit

**Scope:** `custom_addons/kensei` — Tracker surface (5 models, 1 controller, 8 views, 4 dashboards, 8 migrations)
**Module version:** 19.0.1.9.0
**Date:** 13 July 2026
**Review type:** Pull-request review prior to production approval

---

## Verdict — Request Changes

The Tracker is **well-architected and unusually well-documented**, but it is **not production-ready**. Two access-control defects let a non-privileged user read and modify every tasker's allocations and silently re-assign tasks — both reachable over plain RPC. There is also **zero test coverage** for the entire Tracker surface.

The design is sound; the enforcement layer is not. Fix SEC-1 through SEC-4, add the pipeline tests, then merge.

---

## Executive Summary

The Kensei Tracker models a two-stage RL authoring pipeline where each stage is its own `kensei.tracker.allocation` record, chained by `parent_id`. That is a genuinely good decision — it gives every stage its own tasker, PL/QL, scores and audit trail, and it lets the existing pipeline replay verbatim instead of being special-cased. The status ladder in `_compute_status` is strictly nested, so clearing an earlier input correctly demotes the record rather than leaving stale delivery credit. The dashboard controllers are built on `_read_group` throughout, not `search([])`. Someone thought carefully about this.

The problem is that the security model was designed around the `kensei.*` group ladder, and then ACL rows were added for the `etp_user_roles.*` ladder **without corresponding record rules**. In Odoo, a record rule binds to a group; a user in *no* rule-bearing group is unrestricted. Since `etp_user_roles.group_tasker` implies only `base.group_user` and never any `kensei.*` group, every ETP role — and the whole ladder above it, up to CFO — currently has unscoped read/write/create on the allocation table.

Separately, the server-side guard that is supposed to be the real enforcement (UI `readonly` being "only cosmetic", as the code itself notes) has three bypasses: an unprotected field, a falsy-value hole, and a client-supplied context key.

Everything else is ordinary technical debt: an N+1 in the stage-mirror computes, a persona domain that will not survive 50k rows, a leaked `setInterval`, and a CSS prefix typo that silently disables the daily tracker's cell highlighting.

---

## Scorecard

| Dimension | Score |
|---|---|
| Architecture | 8 / 10 |
| Code Quality | 7 / 10 |
| Odoo 19 Best Practices | 8 / 10 |
| ORM Usage | 6 / 10 |
| Performance | 5 / 10 |
| **Security** | **2 / 10** |
| UI / UX | 6 / 10 |
| Maintainability | 7 / 10 |
| Scalability | 5 / 10 |
| Documentation | 9 / 10 |
| **Testing Readiness** | **1 / 10** |

**Overall: 5.8 / 10 — not shippable as-is.** Architecture and documentation are the strongest dimensions and would carry a merge on their own. Security and testing are what block it: both are structural gaps, not polish items.

---

## Critical & High Findings

Every item below was verified against source, not inferred.

### SEC-1 — CRITICAL — Every ETP role has unscoped read/write/create on all allocations

**File:** `security/ir.model.access.csv:25–27`, `security/kensei_tracker_security.xml`
**Category:** Privilege escalation / data exposure

Three ACL rows grant the ETP role ladder access to `kensei.tracker.allocation`:

```csv
access_kensei_tracker_allocation_etp_tasker,…,etp_user_roles.group_tasker,1,1,1,0
access_kensei_tracker_allocation_etp_hr,…,etp_user_roles.group_hr_admin,1,1,1,0
access_kensei_tracker_allocation_etp_it_admin,…,etp_user_roles.group_it_admin,1,1,1,0
```

But the only record rules on this model are bound to `group_kensei_tasker` / `_ql` / `_pl`. A record rule applies **only** to members of its group, and a user in no rule-bearing group is **unrestricted**.

Verified: `etp_user_roles.group_tasker` implies only `base.group_user` (`etp_user_roles/security/groups.xml:13`) — it never implies any `kensei.*` group, and `etp_user_roles` contains no reference to Kensei at all.

**Why it matters:** a user holding only the ETP Tasker role reads, writes and creates *every* tasker's allocations — drive links, QC notes, rubric and pytest scores, PL sign-offs. Because the ETP ladder chains (`quality_lead → tasker`, `project_lead → quality_lead`, `tpm → …`, `cto → tpm`, `cfo → cto`), **every** ETP role inherits this. The record-rule layer is effectively absent for anyone who came in through the ETP ladder.

**Fix:** Delete rows 25–27 and have the ETP groups imply the matching `kensei.*` groups, so a single rule set governs both ladders. If the ACL rows must stay, every one of them needs a companion `ir.rule` carrying the same own-rows domain. Prefer the first — two parallel permission ladders over one table is the root cause, and it will re-break the next time a role is added.

---

### SEC-2 — CRITICAL — A tasker can re-assign any task to someone else via `tasker_email`

**File:** `models/kensei_tracker_allocation.py:672–679, 953–957`
**Category:** Broken access control

The guard protects the assignment fields — but not the email that *derives* them:

```python
_PRIVILEGED_VALUE_FIELDS = (
    'tasker_member_id', 'persona_id', 'assigned_pl_id', 'assigned_date',
    'stage_no', 'parent_id', 'total_stages',
)   # tasker_email and tasker_name are absent

def write(self, vals):
    self._guard_privileged_fields(vals)   # runs FIRST — sees only tasker_email
    self._check_locked(vals)
    self._sync_tasker([vals])             # …then rewrites tasker_member_id from it
```

The ordering is the bug. A tasker RPC-writes `{'tasker_email': 'someone.else@corp'}`; the guard inspects `vals`, sees no protected key and passes; `_sync_tasker` (line 462–465) then resolves that email and sets `vals['tasker_member_id']` for them. `tasker_user_id` recomputes, the record leaves the attacker's scope, and the task is silently re-assigned.

This defeats the exact control the docstring claims to enforce — *"Only a Project Lead / QL can re-assign a task"*.

**Fix:** Add `'tasker_email', 'tasker_name'` to `_PRIVILEGED_VALUE_FIELDS`, **and** move `_sync_tasker` above the guard so the guard always inspects the fully-resolved `vals`. Doing both closes the ordering hole permanently rather than patching one field name.

---

### SEC-3 — HIGH — The privileged-field guard is bypassed by any falsy value

**File:** `models/kensei_tracker_allocation.py:691–693`
**Category:** Broken access control

```python
blocked += [f for f in self._PRIVILEGED_VALUE_FIELDS if vals.get(f)]
```

The truthiness test means a protected field set to `0` or `False` is never blocked.

Two concrete exploits:

1. Writing `{'total_stages': 0}` passes the guard. `is_final_stage` is computed as `stage_no >= total_stages` → `1 >= 0` → **True**. A stage-1 tasker who completes their pipeline now lands on `deliverable` instead of `ready_next_stage` — self-certifying a task as delivered and skipping stage 2 entirely.
2. Writing `{'assigned_date': False}` clears the assignment date.

Note `required=True` does not save you: Odoo does not reject `0` for an Integer.

**Fix:** Test for key presence, not truthiness:

```python
blocked += [f for f in self._PRIVILEGED_VALUE_FIELDS if f in vals]
```

---

### SEC-4 — HIGH — The record lock is bypassable with a client-supplied context key

**File:** `models/kensei_tracker_allocation.py:871`
**Category:** Broken access control

```python
def _check_locked(self, vals):
    if self.env.su or self.env.context.get('kensei_reopen'):
        return
```

`action_reopen` correctly gates on QL/admin membership before setting this context. But `_check_locked` trusts the context key itself, and **context is attacker-controlled over RPC** — the web client sends it with every call.

Any tasker can issue a `write` with `context={'kensei_reopen': True}` and edit a frozen Deliverable record, overwriting scores and QC sign-offs that downstream stages and the Daily Tracker were derived from. The comment two lines above correctly reasons that view `readonly` is "trivially bypassed over RPC" — the same is true of a context flag.

**Fix:** Never use a bare context key as an authorization token. Either re-check the group inside `_check_locked`, or have `action_reopen` call a private `_write_unlocked()` that skips the check on the Python path only — so the escape hatch has no RPC surface at all.

---

### PERF-1 — HIGH — The stage-mirror computes issue up to 4 searches per record

**File:** `models/kensei_tracker_allocation.py:743–819`
**Category:** N+1 queries

`_sibling_stages()` calls `ensure_one()` and runs a `search` per record. It is then called from `_compute_stage_targets` (once) **and** again from `_compute_stage_mirrors` (once) — neither caches — while `_compute_stage_chain` runs a third independent search. All three are non-stored computes on a model whose form exposes ~40 mirror fields.

The searches are deliberately un-sudo'd so record rules scope them, which is the right call for correctness. But the per-record loop is not: each rule-filtered search re-runs the rule's domain. On any batch read — an export, a `read_group` that touches these, a list view that adds one mirror column — this is a linear query storm.

**Fix:** Batch by task — one `search([('task_id', 'in', self.mapped('task_id'))])` at the top of the compute, bucket the result into a `{(task_id, stage_no): rec}` dict, then assign in the loop. That collapses 4N searches into 1, keeps the rule scoping intact, and lets all three computes share it.

---

### PERF-2 — HIGH — The unassigned-persona domain will not survive 50k personas

**File:** `models/kensei_tracker_bulk_allocation.py:154–168`
**Category:** Scalability

```python
allocated_ids = [p.id for p, in Alloc._read_group(
    [('persona_id', '!=', False)], ['persona_id']) if p]
return [('id', 'not in', allocated_ids)] if allocated_ids else []
```

This materialises every allocated persona id into a Python list and ships it back to Postgres as a literal `NOT IN (…)`. At the stated target of 50,000 personas and 10,000+ tasks that is a five-figure IN-list on every wizard open — and `_compute_unassigned_available` re-runs it as a `search_count` on each `source_mode` change.

**Fix:** Add a one2many `allocation_ids` inverse on `kensei.persona` and let the ORM push the anti-join into SQL: `[('allocation_ids', '=', False)]`. Constant-size domain, one query, index-backed.

---

### FE-1 — HIGH — A polling `setInterval` is never cleared on unmount

**File:** `static/src/task_dashboard/task_dashboard.js:418–447` (vs `onWillUnmount` at 74–85)
**Category:** Memory leak

```js
_pollDescriptionThenTriggerQc(recordId, fieldName, entryIndex) {
    const poll = setInterval(async () => {          // handle is a local const
        await this.props.record.load();
        …
    }, 5000);
}
```

`onWillUnmount` clears `_pollTimer` and `_testWeightsPollTimer` — but this handle only ever exists in a local variable, so nothing can cancel it. If the status never leaves `pending`, the interval calls `record.load()` on a destroyed record every 5 seconds, forever. It is started once per model in `_handleBatchStatusChanged` and again in `_pollBatchStatus`, so several stack up.

**Fix:** Push each handle onto `this._descPollTimers`, clear them all in `onWillUnmount`, and bound the poll with a max-attempt counter so it terminates on its own.

---

### FE-2 — HIGH — Daily Tracker cell classes don't match its stylesheet

**File:** `static/src/tracker_daily/tracker_daily.js:145–147` vs `tracker_daily.scss:115–118`
**Category:** Dead styling / silent visual bug

```js
// JS emits o_ktd_*                        /* …but the SCSS defines o_kd_* */
let cls = "o_ktd_cell";                    .o_kd_weekend { … }
if (col.weekend) cls += " o_ktd_weekend";  .o_kd_cell { … }
if (v > 0) cls += " o_ktd_has";            .o_kd_cell.o_kd_has { … }
```

A prefix typo — `o_ktd_` belongs to the *other* dashboard's stylesheet. Weekend shading and the green "has completions" highlight never appear in the table body. The `<th>` uses the correct `o_kd_weekend`, so headers *are* shaded while their columns are not — exactly the kind of half-working visual that gets shipped unnoticed.

**Fix:** Rename the three strings in `cellClass` to `o_kd_*`. Two-minute fix, and a good argument for the SCSS scoping cleanup (DUP-1, CSS-2).

---

### TEST-1 — HIGH — The entire Tracker has zero tests

**File:** `tests/` — 23 test modules, none referencing the Tracker
**Category:** No coverage

The module ships a substantial suite (sandbox, personas, domains, chat, QC, RabbitMQ, consumers, integration). `grep -rl "tracker" tests/` returns **nothing**. Not one test covers `kensei.tracker.allocation`, the team roster, the bulk-allocation wizard, the stage hand-off, the team import, or `controllers/tracker.py`.

This is what makes the security findings above dangerous rather than merely embarrassing: `_compute_status` is a nested ladder with two divergent stage pipelines, and `_guard_privileged_fields` is the only thing standing between a tasker and the delivery credit. Both are exactly the kind of logic that must be pinned by tests, and neither is.

**Recommended minimum bar before merge:**

- **Status ladder** — each rung of `_compute_status` for stage 1 and stage 2; demotion when an earlier input is cleared; `failed` overriding the ladder from any gate.
- **Access control** — a tasker cannot set each `_PRIVILEGED_VALUE_FIELDS` entry, *including* `0`/`False`; cannot write `tasker_email`; cannot bypass the lock via the `kensei_reopen` context; cannot see another tasker's rows — **assert this for an ETP-only user**, which is the case SEC-1 misses.
- **Hand-off** — only from `ready_next_stage`; never twice; `total_stages` carried forward.
- **Wizards** — round-robin respects per-tasker caps; already-allocated personas skipped; CSV with no header, bad encoding, unknown status.

---

## Medium & Low Findings

| ID | Sev | Location | Finding |
|---|---|---|---|
| MIG-1 | Med | `migrations/19.0.1.6.0/post-migration.py:22` | Unconditional `UPDATE … SET baseline_ready_status='done' WHERE pl_verified_status='done'`. Not idempotent — a re-run promotes rows that were legitimately reverted or failed. Guard with `AND baseline_ready_status='in_progress'`. |
| DB-1 | Med | `kensei_tracker_allocation.py:323` | `persona_id` lacks `index=True` despite being a search-view group-by and `_read_group`'d on every bulk-allocation preview. Every other grouped field on the model is indexed. |
| PERF-3 | Med | `kensei_tracker_allocation.py:486–493` | `_compute_pl_employee` resolves `hr.employee` one record at a time with a `sudo()` per row — N+1 on any bulk create. Prefetch employees in one search keyed by user id. |
| PERF-4 | Med | `kensei_tracker_allocation.py:940–960` | `_stamp_completion` runs after `super().write()` and assigns `date_final` per record, firing a second recursive `write` per row (which re-runs all three guards). Batch the stamp into one `write` over the filtered subset. |
| FE-3 | Med | `dashboard_base.js:28–50` | No request sequencing on `_fetch`. Two rapid filter clicks race; the slower, older response resolves last and overwrites fresh data. Add a monotonic sequence token and drop stale responses. |
| FE-4 | Med | `progress_table.js:31–38` | `state.page` is clamped on read but never on write — after a filter shrinks the row set the pager renders "5 / 3" and prev does nothing for two clicks. Clamp on write, or reset to page 1 on load. |
| FE-5 | Med | `task_dashboard.js:126` | `batch_size` is read from `record.data` but never declared in the form arch, so it is always `undefined` and the pod count silently hard-wires to 8. Declare it via `fieldDependencies` on the widget. |
| A11Y-1 | Med | `tracker_dashboard.xml:27,46,63`; `progress_table.xml:30` | Every drill-down card and table row is a clickable `<div>`/`<tr>` with no `tabindex`, `role` or key handler — keyboard and screen-reader users cannot drill down at all. The segmented controls nearby are done correctly (`<button>` + `aria-pressed`); copy that pattern. |
| CSS-1 | Med | `task_dashboard.scss:7–37` | 24 `--kensei-*` custom properties declared on `:root` — injected into every backend page for every user, Kensei access or not. Scope under `.o_kensei_dashboard`. |
| CSS-2 | Med | `tracker_dashboard.scss`, `tracker_daily.scss` | ~34 hardcoded hex values and zero dark-mode handling — these dashboards stay white-on-white islands in Odoo dark mode. `task_dashboard.scss` already wires `--o-view-background-color` correctly; follow it. |
| DUP-1 | Med | `tracker_daily/*` | The Daily Tracker ignores `KenseiDashboardBase` and `ProgressTable` and re-implements loading, pagination and a parallel `o_kd_*` design system duplicating `o_ktd_*`. The one copy-paste island among four otherwise well-shared dashboards — and the direct cause of FE-2. |
| SEC-5 | Low | `kensei_tracker_security.xml:11` | The tasker rule's `('create_uid','=',user.id)` clause is a latent escape hatch: harmless while `perm_create=0`, but combined with the ETP ACL (`create=1`) it lets a user create an allocation naming any tasker and keep visibility. Remove once SEC-1 lands. |
| SMELL-1 | Low | `kensei_tracker_allocation.py:671, 743` | Dead code: `_PRIVILEGED_STAGE_STATES = {}` is an empty dict driving a no-op loop, and `_sibling_stages` carries an `@api.depends` decorator despite being a plain helper, not a compute. |
| SMELL-2 | Low | `kensei_tracker_bulk_allocation.py:396`; `team_import.py:380` | HTML reports built by string concatenation into `fields.Html(sanitize=False)`. User data *is* correctly `escape()`d today, so not currently an XSS — but one careless interpolation away. Render via a QWeb template. |
| API-1 | Low | `controllers/tracker.py:319` | Access denial returns `{"error": "access_denied"}` with HTTP 200 instead of raising `AccessError`. Every caller must remember to check — and `tracker_dashboard.js` already collapses *all* error payloads into one misleading "not allowed" message. |
| API-2 | Low | `controllers/tracker.py:279` | `int(page)` without the `_to_int` guard used elsewhere in the same file — a non-numeric `page` throws a 500. Also `export=True` bypasses pagination entirely and is unbounded. |

---

## Strengths

This is not a weak module. Naming the strengths matters, because the refactor must not destroy them.

- **The stage-chain model is the right abstraction.** Making each stage its own allocation record — rather than bolting stage-2 columns onto one row — gives every stage its own tasker, leads, scores and chatter, and lets the pipeline replay unchanged. The `_STAGE1_MIRROR` / `_STAGE2_MIRROR` tuples deriving the field list, the `@api.depends` and the compute from one source is a genuinely good defence against drift.
- **`_compute_status` is correctly nested, not flat.** The comment explains that a flat series of `if`s let the last rung win regardless of the ones below it; the nested ladder means clearing an early input demotes the record and withdraws delivery credit. A subtle bug someone found and fixed properly.
- **The dashboards are built on `_read_group`, not `search([])`.** Aggregation is pushed into SQL throughout `controllers/tracker.py`. The bulk allocator's round-robin is likewise O(n) via a `deque` rather than the naive O(n·m) scan.
- **The team roster stores no duplicate user data.** `kensei.tracker.team.member` is a thin link to `res.users` with everything else related or sudo-computed — one source of truth, and adding a member can never create a user.
- **Odoo 19 idioms are used correctly.** `models.Constraint` instead of `_sql_constraints`; `<list>` not `<tree>`; Python-expression `invisible=`/`readonly=` with zero surviving `attrs=` or `states=`; `<chatter/>`; `res.groups.privilege`. Across every view file there is not one deprecated construct.
- **No XSS anywhere in the frontend.** Zero `t-raw`, `markup()` or `innerHTML` across all four dashboards — including the `<pre>` blocks rendering raw LLM and pytest output, which is precisely where you would expect to find one.
- **The 19.0.1.9.0 pre-migration is exemplary.** It correctly reasons that a group-less `ir.rule` becomes *global* and a NULL-group ACL means *everybody*, so it deletes rather than orphans — and guards every table with an existence check.
- **The comments explain *why*, not *what*.** Unusually good. Several document a previous bug and why the current shape avoids it. That is the reason this review could be precise.

---

## Refactoring Roadmap

Ordered by dependency, not by severity — some cheap fixes must land before the expensive ones are safe.

### Phase 1 — Blocker: Close the access-control holes

SEC-1 through SEC-4, in that order. SEC-1 first because it is the widest and its fix (collapsing the two group ladders into one) changes what the others are defending. Then the guard fixes — presence-not-truthiness, `tasker_email` in the protected set, `_sync_tasker` before the guard, and the `kensei_reopen` context flag replaced with a Python-only path. Small diffs; high consequence.

### Phase 2 — Blocker: Pin the pipeline with tests

The suite from TEST-1. Write these *immediately after* Phase 1, not before — so each security test is authored against the fixed behaviour and would catch the regression if it returned. Include the ETP-only-user visibility test explicitly: that is the case the current design forgot, and only a test will stop it being forgotten again.

### Phase 3 — Before scale: Fix the query behaviour

PERF-1 (batch the stage-mirror searches), PERF-2 (replace the `NOT IN` list with a one2many anti-join), PERF-3, PERF-4, and the `persona_id` index. None of these are visible at demo scale; all of them bite at the stated 10k tasks / 50k personas. Do them together — they share the same test fixtures.

### Phase 4 — Quality: Frontend correctness, then consolidation

The bugs first — FE-1 (leaked interval), FE-2 (the CSS prefix typo), FE-3 (RPC races), FE-4, FE-5. Then fold `tracker_daily` onto `KenseiDashboardBase` / `ProgressTable`, which retires the duplicate `o_kd_*` system that caused FE-2 in the first place. Accessibility (A11Y-1) and the SCSS scoping / dark-mode work (CSS-1, CSS-2) land naturally with that consolidation.

### Phase 5 — Hygiene: Debt and polish

MIG-1's idempotency guard, the dead code in SMELL-1, the QWeb-ify of the HTML report builders, the controller's error-response shape, and the ~400 lines of dead CSS in `task_dashboard.scss`. Safe to defer; do not let it accumulate further.

---

## Production Readiness

**Is the Kensei Tracker production-ready?**
**No.** Two critical access-control defects (SEC-1, SEC-2) expose and allow modification of every tasker's allocation data over plain RPC, and there is no test coverage to prevent the fix regressing. Nothing about the *architecture* blocks production — the enforcement layer and the safety net do.

**Would you approve this pull request?**
**Request changes.** I would approve after Phase 1 and Phase 2 — the security fixes are small, well-localised diffs, and the design they are protecting is already correct. This is not a rewrite; it is roughly a day of work plus the test suite.

**What architectural improvements do you recommend?**
One structural change: **collapse the two parallel permission ladders.** Having both `kensei.*` and `etp_user_roles.*` groups grant access to the same table, with record rules written for only one of them, is the root cause of SEC-1 and will re-break every time a role is added. Make the ETP groups imply the Kensei groups and delete the duplicate ACL rows. Beyond that, extract the stage-mirror machinery into a small mixin — 40 mirror fields declared by hand is the single biggest source of noise in the model.

**What performance optimisations are required?**
Required before the stated scale targets: batch the stage-mirror searches (PERF-1, currently up to 4 searches per record) and replace the unassigned-persona `NOT IN` list with a proper anti-join (PERF-2, currently a five-figure IN-list at 50k personas). Recommended: index `persona_id`, batch `_compute_pl_employee`, and stop `_stamp_completion` firing a recursive per-row write. The dashboards themselves are already correctly built on grouped SQL and are not the bottleneck.

**Are there security concerns?**
**Yes — four, two of them critical.** Unscoped ETP-role access to all allocations; task re-assignment via the unguarded `tasker_email`; the falsy-value hole in the privileged-field guard; and a record lock bypassable with a client-supplied context key. The common thread: the guards were written correctly but their *enforcement boundary* is wrong — each trusts something the client controls (a group ladder that does not imply, a field that derives another, a truthiness test, a context flag).

**What technical debt exists?**
Moderate and mostly contained. The Daily Tracker is a copy-paste island duplicating a whole design system; `task_dashboard.scss` carries ~400 lines of dead CSS in its 1,633; the HTML report builders concatenate strings into unsanitised `fields.Html`; and there is scattered dead code. None of it is load-bearing — it is the ordinary residue of fast iteration, and Phases 4–5 clear it.

**Should new features be built now, or should this be refactored first?**
**Refactor first — but only Phases 1–3.** Shipping features onto a table that every ETP role can read and write compounds the exposure, and shipping them onto an untested status ladder means the next change silently breaks delivery credit. Phases 1–3 are bounded and unambiguous. Phases 4–5 are quality work that can safely run in parallel with new feature development.

---

## Scope Note

This audit covers the Tracker surface only. `models/kensei.py` (3,326 lines) and `models/kensei_sandbox.py` (5,027 lines) are the module's real bulk and were examined only where the Tracker reaches into them. A review of those is a separate pass.
