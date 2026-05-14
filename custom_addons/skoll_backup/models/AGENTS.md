# Skoll Models — AGENTS.md

> 6 Odoo models totaling 6000+ lines. Core business logic lives here.

## Model Map

| Model | File | Lines | Purpose |
|-------|------|-------|---------|
| `skoll.skoll` | `skoll.py` | ~2000 | Task records, trajectory management, golden generation |
| `skoll.turn` | `skoll.py` | ~200 | Per-turn chat data (prompt, response, tokens, QC) |
| `skoll.taxonomy` | `skoll.py` | ~20 | HEART taxonomy tags |
| `skoll.sandbox` | `skoll_sandbox.py` | ~1380 | Sandbox lifecycle, trajectory export, auto-hint |
| `skoll.sandbox.k8s` | `skoll_sandbox_k8s.py` | ~1390 | K8s deployer (AbstractModel mixin) |
| `skoll.persona` | `skoll_persona.py` | ~80 | Persona profiles (soul/memory/agents markdown) |
| `skoll.domain` | `skoll_domain.py` | ~16 | Legacy hierarchical domain taxonomy |
| `res.config.settings` | `res_config_settings.py` | ~125 | Settings page for `skoll.*` config params |

## skoll.py — Task + Turn + Taxonomy

### `skoll.skoll` (Task)

**Key fields:**
- `task_id` — Auto-generated unique ID
- `persona_id` → `skoll.persona`
- `task_type` — Selection: swe/qa/conversational/research
- `difficulty` — Selection: easy/medium/hard
- `sandbox_ids` — One2many → `skoll.sandbox`
- Trajectory text fields: `trajectory_claude`, `trajectory_glm`, `trajectory_1pa`–`trajectory_1pd`, `trajectory_golden`
- QC: `qc_status`, token counters, `auto_process_status`
- Prompts: `system_prompt`, `seed_prompt`, `initial_prompt`, `trajectory_modifier`, `safety_critical`

**Key methods:**
- `action_generate_golden_trajectory()` — Submits to `_GOLDEN_POOL` ThreadPoolExecutor (2 workers). Reads `golden_prompt.md`, calls Bedrock, saves result, sends bus notification `skoll/golden_ready`.
- `action_generate_task_description()` — Submits to `_TASKDESC_POOL` (2 workers). Reads `task_description_prompt.md`, calls Bedrock.
- `action_export_session()` — Aggregates turn data from sandbox into trajectory JSON.
- `action_upload_to_s3()` / `action_mass_upload_to_s3()` — S3 trajectory upload.
- `action_publish_to_rabbitmq()` — Publishes task to `skoll_auto_process` queue.

**ThreadPoolExecutors (module-level):**
```python
_GOLDEN_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="skoll-golden")
_TASKDESC_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="skoll-taskdesc")
```
Each uses `threading.Lock()` for thread safety on shared state.

### `skoll.turn`

- `sandbox_id` → `skoll.sandbox`
- `turn_number` — Integer sequence within sandbox
- `prompt`, `response` — Text fields
- `tool_calls` / `raw_events` / `trajectory_messages` — JSON fields
- Token counters: `input_tokens`, `output_tokens`, `cache_creation_tokens`, `cache_read_tokens`
- QC: `qc_score`, `qc_feedback`, `qc_status`
- Auto-hint: `hint_text`, `hint_feedback`, `auto_hint_iteration`, `auto_hint_model`

### `skoll.taxonomy`

Simple `_name = 'skoll.taxonomy'` with a single `name` field. Used for HEART tags.

## skoll_sandbox.py — Sandbox Lifecycle

### `skoll.sandbox`

**Key fields:**
- `skoll_id` → `skoll.skoll` (required)
- `model_type` — Selection: claude/glm/1pa/1pb/1pc/1pd
- SQL constraint: `UNIQUE(skoll_id, model_type)`
- Docker fields: `status` (draft/starting/running/stopping/stopped/error), `port`, `token`, `url`, `workdir`
- `session_status` — Selection: idle/active/completed/error
- `turn_ids` — One2many → `skoll.turn`
- Auto-hint state: `auto_hint_active`, `auto_hint_iteration`, `auto_hint_max_iterations`

**Key methods:**
- `action_start_sandbox()` — Dispatches to local (Docker Compose) or K8s based on `skoll.deployment_mode`.
- `action_stop_sandbox()` — Graceful shutdown with cleanup.
- `_start_sandbox_local()` — `docker compose up` via subprocess.
- `_start_sandbox_k8s()` — Delegates to `skoll.sandbox.k8s` mixin.
- `_export_trajectory_from_jsonl()` — Reads JSONL from sandbox filesystem.
- `_export_trajectory_from_ws()` — Fetches via WebSocket.
- `_export_trajectory_from_turns()` — Builds from `skoll.turn` records.
- `_query_litellm_spend()` — Queries LiteLLM `/spend/logs` for token costs.
- `_cron_reconcile_sandbox_status()` — Runs every 1 min, syncs Docker/K8s state.

**Background pool:**
```python
_SANDBOX_POOL = ThreadPoolExecutor(max_workers=3, thread_name_prefix="skoll-sandbox")
```

## skoll_sandbox_k8s.py — Kubernetes Deployer

### `skoll.sandbox.k8s` (AbstractModel)

Mixed into `skoll.sandbox` — provides K8s deployment methods.

**Creates these K8s resources per sandbox:**
1. **Secret** — Sandbox token, LiteLLM master key
2. **Persona ConfigMap** — SOUL.md, MEMORY.md, AGENTS.md from persona
3. **GOG Secret** — Google OAuth credentials (if configured)
4. **OpenClaw Config ConfigMap** — Runtime config for OpenClaw agent
5. **LiteLLM ConfigMap** — Model routing config
6. **Deployment** — 5 init containers + 4 containers:
   - Init: wait-for-postgres, db-setup, persona-copy, openclaw-config, litellm-config
   - Containers: openclaw (:18789), litellm (:4000), postgres (:5432), session-backup (sidecar)
7. **Service** — ClusterIP exposing ports 18789, 4000
8. **WS Router** — nginx ConfigMap + Ingress for WebSocket routing

**Naming convention:** `skoll-sandbox-{sandbox_id}` for all K8s resources.

**Namespace:** `skoll` (configurable via `skoll.k8s_namespace`).

**Node selector:** `kubernetes.io/arch: amd64` + `ethara.ai/node-pool: general-purpose`.

## skoll_persona.py

- `name` — Char, auto-lowercased via `@api.onchange`
- `soul_md`, `memory_md`, `agents_md` — Text fields (markdown)
- `litellm_config_yaml` — Text (YAML override for LiteLLM)
- `docker_compose_yaml` — Text (Docker Compose override)

## res_config_settings.py

Extends `res.config.settings` to expose 20+ `skoll.*` config parameters in Settings UI. All fields use `config_parameter='skoll.*'` attribute for auto-persistence.

## Patterns to Follow

- **New model**: Add to `models/__init__.py`, create `skoll_{name}.py`, inherit `models.Model`
- **New field**: Add to model class, bump version in `__manifest__.py`, add migration if needed
- **Background work**: Use existing `ThreadPoolExecutor` pools or create new one at module level with explicit `max_workers`
- **Config params**: Add field to `res_config_settings.py` with `config_parameter` attribute, add to settings view XML
- **Bus notifications**: `self.env['bus.bus']._sendone(self.env.user.partner_id, 'skoll/event_name', payload)`

## Gotchas

- `skoll.sandbox.k8s` is an **AbstractModel** — it has no database table. It's mixed into `skoll.sandbox`.
- Thread pools use `self.env.cr.dbname` to get a new cursor in background threads — the original cursor is NOT safe across threads.
- `_cron_reconcile_sandbox_status` runs every 1 minute — be mindful of performance impact when adding logic.
- `skoll.domain` is legacy — 16 lines, hierarchical. Likely to be removed.
