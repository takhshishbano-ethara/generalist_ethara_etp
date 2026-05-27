# Code Review — `mm_tasker`

**Module:** `mm_tasker` (v19.0.5.0.0)
**Reviewed:** 2026-05-19
**Scope:** Full module — models, security, views, scripts, data, dispatcher.

Overall: solid module, well-structured, defensible workflow. Below is what I'd want fixed before treating this as production-grade. Severities are mine.

---

## Blocker

### B1. SHA-256 fallback for output files is broken — `models/agent_dispatcher.py:87-89`

```python
'sha256': f.get('sha256') or hashlib.sha256(
    (f.get('file') or b'').encode() if isinstance(f.get('file'), str) else (f.get('file') or b'')
).hexdigest(),
```

Two real bugs:

1. **Crash on empty-string file.** If `f['file'] == ''`, `isinstance('', str)` is True, so the branch runs `('' or b'').encode()`. `'' or b''` evaluates to `b''` (empty str is falsy), then `b''.encode()` → **`AttributeError: 'bytes' object has no attribute 'encode'`**. Any output file the backend sends with `file=''` and no `sha256` will explode the whole run.
2. **Wrong hash when the backend sends base64.** When `file` is a non-empty base64 string, this calls `'AAAB...'.encode()` → UTF-8 bytes of the *base64 text*, not the decoded binary. The stored sha256 won't match what any downstream consumer computes from the actual file content.

**Fix** (decode base64 once, hash bytes):

```python
raw = f.get('file') or b''
if isinstance(raw, str):
    try:
        raw_bytes = base64.b64decode(raw)
    except Exception:
        raw_bytes = raw.encode('utf-8')
else:
    raw_bytes = raw
sha = f.get('sha256') or hashlib.sha256(raw_bytes).hexdigest()
```

---

## High

### H1. QC group can edit any task field — `security/mm_tasker_security.xml:56-65` + `security/ir.model.access.csv`

`mm_task_qc_rule` grants `perm_write=True` on every task; ACL gives QC `1,1,0,0` (read+write). There is no field-level restriction (`groups="..."` on the *editable* fields, or a write-time check in `write()`). A QC user can edit `final_prompt`, replace `media_ids`, change `rubric_ids` — silently destroying the tasker's submission while it's under review.

The form view hides the QC verdict from taskers, but **doesn't reciprocally lock the authoring fields from QC**.

**Options:**
- Add `groups="mm_tasker.group_mm_manager"` to the editable authoring fields, so QC sees but can't edit them.
- Or override `write()` to reject changes to `default_prompt/human_prompt/final_prompt/media_ids/rubric_ids/rubrics_file` when the user is QC-but-not-manager and state is `dispatched`/`evaluated`.

### H2. `action_back_to_draft` unconditionally unlinks runs in any non-terminal state

`run_ids.unlink()` runs even when a run is `running` or `judging`. With a live backend this loses an in-flight execution the user has already paid tokens for, and the backend may still POST results to a now-orphaned run. At minimum, refuse to unlock if any run is `running`/`judging`, or surface a confirm.

---

## Medium

### M1. Rubric edits after dispatch silently desynchronize scores

`action_submit_rubrics` allows editing in `dispatched`/`evaluated`. `_build_rubric_upsert_commands` preserves score rows when rubric numbers don't change, but:
- New rubrics created post-dispatch have **no scores** on existing runs — UI looks like a partial grade.
- Rubric `points` / `importance` changes do **not** invalidate `awarded` / `passed` on existing runs (those fields are stored, not recomputed).

Either auto-`_compute_aggregate` on all runs after rubric upsert, or flag affected runs as "needs regrade".

### M2. Wizard marks task `dispatched` even when every run errored — `models/mm_tasker_run_wizard.py` `action_dispatch`

`dispatch_runs` swallows per-run exceptions and writes `state='error'` on each. The wizard then unconditionally sets `task.state = 'dispatched'`. If 0/N runs succeeded the task still advances, which is misleading. Either check `runs.filtered(lambda r: r.state != 'error')` before promoting state, or post a warning message.

### M3. `action_run_judge` advances state to `evaluated` even if no runs were judged

Filter `runs.state != 'error'` can yield empty. The method still writes `state='evaluated'` and posts a success message. Should be a no-op (or `UserError`) when nothing was judged.

---

## Low

### L1. `_param_truthy` default is `'true'` — production safety net missing

Out-of-the-box the module mocks everything. A misconfigured deploy that forgets to flip `mm_tasker.test_mode` will happily emit `[MOCK ...]` responses as if they were real. Consider logging a `_logger.warning` once per process when `test_mode` is true AND a live `backend_url` is configured (the configuration suggests the operator intended real mode).

### L2. Run-related `ir.rule` records omit explicit `perm_*` — `security/mm_tasker_security.xml:145-200`

Tasker/QC/manager rules on `mm.tasker.run`, `.run.output`, `.run.score` rely on Odoo's default (`True` for all four). Works today, but the task/rubric/media rules above them are explicit. Be consistent — the next reader can't tell whether the omission was intentional.

### L3. `_compute_active_models_display` depends on `state`

```python
active_models_display = fields.Char(compute='_compute_active_models_display', ...)
# @api.depends('state')
```

The displayed value comes from `ir.config_parameter`, not from any field. Depending on `state` is a hack to force re-render on state change — but config edits never refresh the display. Either compute on the fly in the view (`<field ... readonly="1"/>` with a related/SQL view) or accept that this is a presentation-only field and document it.

### L4. Subprocess timeout granularity

`_run_qc` uses a single 30s timeout for all three QC scripts. `scripts/qc_media.py` does base64-decode-aware size checks per attachment; a task with 30+ large attachments could legitimately tip past 30s. Either let the timeout scale with `len(media)`, or expose per-section overrides.

### L5. `_compute_file_size` swallows all exceptions — `models/mm_tasker_media.py`

`bare except Exception` hides backend storage issues. Use `except (binascii.Error, TypeError, ValueError)` so genuine bugs surface in logs.

---

## Nits

- `models/mm_tasker_run.py:194` — superfluous outer parens on `(rec.score_ids.filtered(...).unlink())`.
- `models/mm_tasker_task.py` — field `system_prompt` is referenced via `getattr(task, 'system_prompt', None)` in the dispatcher but never declared on the model. If you intend to support it, add the field; otherwise drop the `getattr` and pass `None` explicitly.
- `views/mm_tasker_menus.xml` references `mm_tasker,static/description/icon.png` — confirm the icon exists or the menu will render the generic placeholder.

---

## Summary

One real **blocker** (SHA-256 fallback crashes on empty string and produces wrong hashes for base64 strings) and one **high-severity** security gap (QC can rewrite a tasker's submission). Everything else is workflow polish — the architecture, dispatcher design, and rubric upsert semantics are good.

### Priority Order

| # | Severity | Item | Effort |
|---|---|---|---|
| 1 | Blocker | B1 — SHA-256 fix | ~10 lines |
| 2 | High | H1 — QC field-level lockdown | view + write() override |
| 3 | High | H2 — guard `action_back_to_draft` against in-flight runs | small |
| 4 | Medium | M1 — auto-aggregate / flag stale runs on rubric edit | medium |
| 5 | Medium | M2 — wizard state promotion guard | small |
| 6 | Medium | M3 — `action_run_judge` empty-batch guard | small |
| 7 | Low | L1–L5 — polish, logging, types | small each |
| 8 | Nits | as listed | trivial |
