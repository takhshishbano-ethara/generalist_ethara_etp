# Skoll Tests — AGENTS.md

> 24 test files using Odoo's unittest framework with a shared base class.

## Test Framework

- **Framework**: Odoo `TransactionCase` (unittest-based, each test rolled back)
- **Runner**: `python src/odoo-bin --test-enable --test-tags=skoll --stop-after-init -u skoll -d <db>`
- **Mocking**: stdlib `unittest.mock` (`patch`, `MagicMock`, `PropertyMock`)
- **No pytest** — all tests run through Odoo's test loader

## Test Map

| File | Tests | Covers |
|------|-------|--------|
| `common.py` | — | Base class `SkollTestCase` with factories and fixtures |
| `test_skoll_model.py` | Task CRUD, field defaults, trajectory | `skoll.skoll` model |
| `test_skoll_sandbox.py` | Sandbox lifecycle, start/stop | `skoll.sandbox` model |
| `test_skoll_sandbox_k8s.py` | K8s resource creation | `skoll.sandbox.k8s` mixin |
| `test_skoll_sandbox_lifecycle.py` | Full lifecycle sequences | Sandbox state machine |
| `test_sandbox_internals.py` | Internal sandbox helpers | Private methods |
| `test_sandbox_data.py` | Data export/import | Trajectory serialization |
| `test_controller_chat.py` | Chat CRUD endpoints | `chat.py` controller |
| `test_controller_qc.py` | QC endpoint responses | `llm_assisst_qc.py` |
| `test_controller_auto_hint.py` | Hint loop behavior | `auto_hint.py` |
| `test_controller_costing.py` | Cost data endpoints | `costing.py` |
| `test_controller_export.py` | Export endpoints | `export.py` |
| `test_controller_browser_auth.py` | Browser auth flow | `browser_auth.py` |
| `test_controller_gog_auth.py` | Google OAuth flow | `gog_auth.py` |
| `test_consumer.py` | RabbitMQ consumer pipeline | `consumer.py` |
| `test_ws_client.py` | WebSocket client | `ws_client.py` |
| `test_rabbitmq_service.py` | RabbitMQ publish helpers | `services/rabbitmq_service.py` |
| `test_background_workers.py` | ThreadPool behavior | Background executors |

## Base Class: `SkollTestCase`

Located in `common.py`. All test classes inherit from it.

```python
from odoo.tests.common import TransactionCase, tagged

class SkollTestCase(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Creates: cls.persona, cls.task, cls.sandbox, cls.employee
        # Sets up config params for test mode

    def _create_task(self, **overrides):
        """Factory: create skoll.skoll record with sensible defaults."""

    def _create_sandbox(self, task=None, model_type='claude', **overrides):
        """Factory: create skoll.sandbox record."""

    def _create_turn(self, sandbox=None, **overrides):
        """Factory: create skoll.turn record."""

    @staticmethod
    def _make_trajectory_json(entries=None):
        """Build trajectory JSON matching Delivery_Schema.json."""

    @staticmethod
    def _make_session_entry(turn_number=1, **overrides):
        """Build a single session entry for trajectory."""

    @staticmethod
    def _make_tool_calls_json():
        """Build sample tool_calls JSON structure."""
```

## Conventions

### Test Class Pattern

```python
from odoo.tests.common import tagged
from .common import SkollTestCase

@tagged('skoll', 'skoll_sandbox', 'post_install', '-at_install')
class TestSandboxLifecycle(SkollTestCase):

    def test_start_sandbox_local(self):
        """Sandbox starts in local mode with Docker Compose."""
        sandbox = self._create_sandbox()
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            sandbox.action_start_sandbox()
        self.assertEqual(sandbox.status, 'running')
```

### Tagging Rules

Every test class MUST have:
- `'skoll'` — module-level tag (required for `--test-tags=skoll`)
- `'skoll_{feature}'` — feature-specific tag (for granular runs)
- `'post_install'` — run after module install
- `'-at_install'` — do NOT run during install

### Mocking External Services

```python
# Bedrock
with patch.object(SkollLLMAssistQC, '_call_bedrock_converse', return_value={'content': [{'text': '...'}]}):

# K8s
with patch('kubernetes.client.CoreV1Api') as mock_k8s:

# Docker
with patch('subprocess.run') as mock_run:

# RabbitMQ
with patch('pika.BlockingConnection') as mock_pika:

# S3
with patch('boto3.client') as mock_s3:

# httpx
with patch('httpx.Client.post') as mock_post:
```

### Test Registration

Every test file MUST be imported in `tests/__init__.py`:
```python
from . import test_skoll_model
from . import test_skoll_sandbox
# ... etc
```

**If you forget this import, the test will not be discovered.**

## Running Tests

```bash
# All skoll tests
python src/odoo-bin --test-enable --test-tags=skoll --stop-after-init -u skoll -d mydb

# Specific feature
python src/odoo-bin --test-enable --test-tags=skoll_sandbox --stop-after-init -u skoll -d mydb

# With verbose output
python src/odoo-bin --test-enable --test-tags=skoll --stop-after-init -u skoll -d mydb --log-level=test
```

## Adding a New Test

1. Create `tests/test_{feature}.py`
2. Import `SkollTestCase` from `common.py`
3. Add `@tagged('skoll', 'skoll_{feature}', 'post_install', '-at_install')`
4. Inherit `SkollTestCase`, use factory methods
5. **Register in `tests/__init__.py`** — this step is critical
6. Mock all external services (Bedrock, K8s, Docker, S3, RabbitMQ)
