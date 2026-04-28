import json
import sys
import tempfile
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest


def _create_odoo_stubs():
    stubs = {}

    def _make(name, attrs=None):
        mod = types.ModuleType(name)
        mod.__path__ = []
        mod.__package__ = name
        if attrs:
            for k, v in attrs.items():
                setattr(mod, k, v)
        stubs[name] = mod
        return mod

    class UserError(Exception):
        pass

    class ValidationError(Exception):
        pass

    class AccessError(Exception):
        pass

    _make("odoo")
    _make("odoo.tools")
    _make("odoo.modules")
    _make("odoo.modules.registry")
    _make("odoo.exceptions", {
        "UserError": UserError,
        "ValidationError": ValidationError,
        "AccessError": AccessError,
    })

    def _depends(*args, **kwargs):
        def decorator(fn):
            fn._depends = args
            return fn
        return decorator

    def _depends_context(*args, **kwargs):
        def decorator(fn):
            return fn
        return decorator

    def _model_create_multi(fn):
        fn._model_create_multi = True
        return fn

    def _onchange(*args):
        def decorator(fn):
            return fn
        return decorator

    _make("odoo.api", {
        "depends": _depends,
        "depends_context": _depends_context,
        "model_create_multi": _model_create_multi,
        "onchange": _onchange,
        "model": lambda fn: fn,
        "Environment": MagicMock(),
        "SUPERUSER_ID": 1,
    })

    class _FieldBase:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs
            self.string = kwargs.get("string", "")

    class _Char(_FieldBase): pass
    class _Text(_FieldBase): pass
    class _Integer(_FieldBase): pass
    class _Float(_FieldBase): pass
    class _Boolean(_FieldBase): pass
    class _Date(_FieldBase): pass

    class _Datetime(_FieldBase):
        @staticmethod
        def now():
            from datetime import datetime, timezone
            return datetime.now(tz=timezone.utc)

        @staticmethod
        def subtract(dt, **kwargs):
            from datetime import timedelta
            return dt - timedelta(**kwargs)

    class _Selection(_FieldBase): pass
    class _Many2one(_FieldBase): pass
    class _One2many(_FieldBase): pass
    class _Many2many(_FieldBase): pass
    class _Binary(_FieldBase): pass

    _make("odoo.fields", {
        "Char": _Char,
        "Text": _Text,
        "Integer": _Integer,
        "Float": _Float,
        "Boolean": _Boolean,
        "Date": type("Date", (), {"today": staticmethod(lambda: "2025-04-24")}),
        "Datetime": _Datetime,
        "Selection": _Selection,
        "Many2one": _Many2one,
        "One2many": _One2many,
        "Many2many": _Many2many,
        "Binary": _Binary,
    })

    class _Constraint:
        def __init__(self, *a, **kw):
            pass

    class _BaseModel:
        _name = ""
        _description = ""
        _inherit = []
        _order = ""
        env = MagicMock()

        def ensure_one(self):
            pass

        def write(self, vals):
            for k, v in vals.items():
                setattr(self, k, v)

        def browse(self, ids):
            return self

        def search(self, *a, **kw):
            return self

        def search_count(self, *a, **kw):
            return 0

        def exists(self):
            return True

        def create(self, vals):
            return MagicMock()

        def unlink(self):
            pass

        def message_post(self, **kw):
            pass

        def mapped(self, field):
            return MagicMock(ids=[])

    class _Model(_BaseModel):
        pass

    class _TransientModel(_BaseModel):
        pass

    _make("odoo.models", {
        "Model": _Model,
        "TransientModel": _TransientModel,
        "Constraint": _Constraint,
        "BaseModel": _BaseModel,
    })

    _make("odoo.addons")
    _make("odoo.addons.mail")
    _make("odoo.addons.mail.models")

    for name, mod in stubs.items():
        sys.modules[name] = mod

    return stubs


_create_odoo_stubs()

_custom_addons = str(Path(__file__).resolve().parent / "custom_addons")
if _custom_addons not in sys.path:
    sys.path.insert(0, _custom_addons)


@pytest.fixture
def tmp_dir():
    with tempfile.TemporaryDirectory(prefix="aurora_test_") as d:
        yield Path(d)


@pytest.fixture
def sample_jsonl_file(tmp_dir):
    def _make(records, filename="data.jsonl"):
        p = tmp_dir / filename
        with open(p, "w") as f:
            for rec in records:
                f.write(json.dumps(rec) + "\n")
        return str(p)
    return _make


@pytest.fixture
def empty_file(tmp_dir):
    def _make(filename="empty.jsonl"):
        p = tmp_dir / filename
        p.touch()
        return str(p)
    return _make


@pytest.fixture
def mock_cursor():
    cr = MagicMock()
    cr.execute = MagicMock()
    cr.fetchone = MagicMock(return_value=(1,))
    cr.fetchall = MagicMock(return_value=[])
    cr.commit = MagicMock()
    cr.rollback = MagicMock()
    cr.close = MagicMock()
    cr.dbname = "test_db"
    return cr


@pytest.fixture
def mock_registry(mock_cursor):
    registry = MagicMock()
    registry.cursor.return_value = mock_cursor
    return registry


@pytest.fixture
def mock_env():
    env = MagicMock()
    icp = MagicMock()
    icp.get_param = MagicMock(side_effect=lambda key, default="": default)
    icp.set_param = MagicMock()
    env.__getitem__ = MagicMock(return_value=MagicMock(sudo=MagicMock(return_value=icp)))
    env.user = MagicMock()
    env.user.has_group = MagicMock(return_value=True)
    env.cr = MagicMock()
    env.cr.dbname = "test_db"
    return env


@pytest.fixture
def s3_config_valid():
    return {
        "bucket": "test-bucket",
        "access_key": "AKIA_TEST",
        "secret_key": "secret123",
        "region": "us-east-1",
        "folder": "test_folder",
    }


@pytest.fixture
def s3_config_empty():
    return {
        "bucket": "",
        "access_key": "",
        "secret_key": "",
        "region": "ap-south-1",
        "folder": "",
    }


@pytest.fixture
def fernet_key():
    from cryptography.fernet import Fernet
    return Fernet.generate_key()


@pytest.fixture
def pipeline_config():
    return {
        "org": "test-org",
        "repo": "test-repo",
        "output_dir": "/tmp/aurora_test",
        "skip_pr_fetch": False,
        "lang": "python",
        "delay_on_error": 300,
        "retry_attempts": 3,
        "max_tags": 200,
        "window_days": 30,
        "cache_dir": "/tmp/cache",
        "s3_bucket": "",
        "s3_access_key": "",
        "s3_secret_key": "",
        "s3_region": "ap-south-1",
        "s3_folder": "",
        "uid": 1,
    }


@pytest.fixture
def phase2_report_data():
    return {
        "valid": True,
        "f2p_tests": {"test_a": {"status": "pass"}, "test_b": {"status": "pass"}},
        "p2p_tests": {"test_c": {"status": "pass"}},
        "s2p_tests": {},
        "n2p_tests": {},
        "fixed_tests": {"test_a": {"status": "pass"}},
        "error_msg": "",
    }


@pytest.fixture
def phase2_report_invalid():
    return {
        "valid": False,
        "f2p_tests": {},
        "p2p_tests": {},
        "s2p_tests": {},
        "n2p_tests": {},
        "fixed_tests": {},
        "error_msg": "Docker build failed: exit code 1",
    }
