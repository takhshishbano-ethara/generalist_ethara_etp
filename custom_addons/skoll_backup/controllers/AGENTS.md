# Skoll Controllers — AGENTS.md

> 8 HTTP controllers providing the API surface for the Skoll frontend and external integrations.

## Controller Map

| File | Class | Routes | Purpose |
|------|-------|--------|---------|
| `llm_assisst_qc.py` | `SkollLLMAssistQC` | 6 | Bedrock LLM integration: prompt QC, trajectory QC, golden QC |
| `chat.py` | `SkollChatController` | 5 | Turn CRUD, response saving, trajectory export |
| `auto_hint.py` | `SkollAutoHint` | 4 | Automated hint evaluation loop |
| `costing.py` | `SkollCostingController` | 3 | Token cost tracking dashboard data |
| `export.py` | `SkollExportController` | 3 | Session/trajectory export endpoints |
| `skoll_controller.py` | `SkollController` | 2 | JSONL bulk data import |
| `browser_auth.py` | `BrowserAuthController` | 2 | Browser session authentication |
| `gog_auth.py` | `GogAuthController` | 3 | Google OAuth token management |

## Conventions

### Route Patterns

```python
@http.route('/skoll/<action>', type='json', auth='user', methods=['POST'])
```

- Prefix: `/skoll/` for all module routes, `/api/` for external-facing
- Auth: `auth='user'` (session-based) for frontend calls, `auth='none'` + manual token check for external
- Type: `type='json'` (Odoo JSON-RPC format) for internal, `type='http'` for file downloads/uploads
- Methods: Always explicit `methods=['POST']` or `methods=['GET']`

### Response Patterns

```python
# Success (JSON-RPC)
return {'status': 'success', 'data': {...}}

# Error (JSON-RPC)
return {'status': 'error', 'message': 'Human-readable error'}

# File download (HTTP)
return request.make_response(content, headers=[('Content-Type', 'application/json'), ...])
```

### Bedrock Integration (`llm_assisst_qc.py`)

Core method: `_call_bedrock_converse(messages, system_prompt, model_arn=None)`

- Uses `httpx` with AWS SigV4 signing (NOT boto3)
- Default model: `skoll.bedrock_inference_arn` config param
- Region: `skoll.bedrock_region` config param
- Returns structured response dict with `content[0].text`
- All Bedrock calls are synchronous within the controller (no threading)

### Controller Structure

```python
from odoo import http
from odoo.http import request

class SkollMyController(http.Controller):

    @http.route('/skoll/my_action', type='json', auth='user', methods=['POST'])
    def my_action(self, **kwargs):
        # 1. Extract params from kwargs
        # 2. Access models via request.env['skoll.skoll']
        # 3. Execute business logic (delegate to model methods when complex)
        # 4. Return response dict
        pass
```

## Key Endpoints

### Chat (`chat.py`)

| Route | Purpose |
|-------|---------|
| `/skoll/chat/create_turn` | Create new turn record for a sandbox |
| `/skoll/chat/save_response` | Save assistant response + tool calls to turn |
| `/skoll/chat/get_turns` | Fetch all turns for a sandbox |
| `/skoll/chat/export_session` | Export sandbox session as trajectory JSON |
| `/skoll/chat/delete_turns` | Delete turns from a sandbox (QL/Admin only) |

### QC (`llm_assisst_qc.py`)

| Route | Purpose |
|-------|---------|
| `/skoll/qc/prompt_check` | Run prompt through Bedrock for QC assessment |
| `/skoll/qc/trajectory_check` | Validate individual trajectory entry |
| `/skoll/qc/golden_check` | QC golden trajectory against criteria |
| `/skoll/qc/assist` | LLM-assisted writing/editing help |
| `/skoll/qc/batch_check` | Batch trajectory validation |

### Auto-Hint (`auto_hint.py`)

| Route | Purpose |
|-------|---------|
| `/skoll/auto_hint/evaluate` | Run hint evaluation on latest turn |
| `/skoll/auto_hint/generate` | Generate corrective hint for assistant |
| `/skoll/auto_hint/status` | Get current auto-hint loop state |
| `/skoll/auto_hint/stop` | Stop auto-hint loop for sandbox |

Logic: Evaluates assistant response → if unsatisfactory → generates hint → sends to sandbox → waits for new response → re-evaluates (up to 5 iterations).

### Export (`export.py`)

| Route | Purpose |
|-------|---------|
| `/skoll/export/trajectory` | Export trajectory in delivery schema format |
| `/skoll/export/session` | Export full session data (all turns + metadata) |
| `/skoll/export/bulk` | Bulk export multiple tasks |

## Anti-Patterns (DO NOT)

- **NEVER** put complex business logic in controllers — delegate to model methods
- **NEVER** use `request.env` without `.sudo()` awareness (respect record rules)
- **NEVER** return raw exceptions to the client — catch and return structured error
- **NEVER** use `requests` library — use `httpx` for external HTTP calls
- **NEVER** hardcode model ARNs or regions — read from `ir.config_parameter`
- **NEVER** do database writes in GET endpoints

## Adding a New Controller

1. Create `controllers/my_controller.py`
2. Import in `controllers/__init__.py`
3. Define class inheriting `http.Controller`
4. Add routes with explicit auth, type, methods
5. Add corresponding test file in `tests/test_controller_my_controller.py`
