import json
import sys
import tempfile
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest


def _create_psycopg2_stub():
    """Stub psycopg2 so aurora can import without the C extension installed."""
    psycopg2_mod = types.ModuleType("psycopg2")
    psycopg2_mod.__path__ = []
    psycopg2_mod.__package__ = "psycopg2"
    psycopg2_mod.connect = MagicMock()
    psycopg2_mod.OperationalError = type("OperationalError", (Exception,), {})
    psycopg2_mod.DatabaseError = type("DatabaseError", (Exception,), {})
    psycopg2_mod.InterfaceError = type("InterfaceError", (Exception,), {})
    psycopg2_mod.Error = type("Error", (Exception,), {})
    extras = types.ModuleType("psycopg2.extras")
    extras.RealDictCursor = MagicMock()
    extensions = types.ModuleType("psycopg2.extensions")
    errors = types.ModuleType("psycopg2.errors")
    errors.SerializationFailure = type("SerializationFailure", (Exception,), {})
    psycopg2_mod.extras = extras
    psycopg2_mod.extensions = extensions
    psycopg2_mod.errors = errors
    sys.modules["psycopg2"] = psycopg2_mod
    sys.modules["psycopg2.extras"] = extras
    sys.modules["psycopg2.extensions"] = extensions
    sys.modules["psycopg2.errors"] = errors


def _create_kubernetes_stub():
    """Stub kubernetes so K8S_AVAILABLE=True and k8s_client/k8s_config are patchable."""
    k8s = types.ModuleType("kubernetes")
    k8s.__path__ = []
    k8s_client_mod = MagicMock()
    k8s_config_mod = MagicMock()
    k8s_config_mod.ConfigException = type("ConfigException", (Exception,), {})
    k8s.client = k8s_client_mod
    k8s.config = k8s_config_mod
    sys.modules["kubernetes"] = k8s
    sys.modules["kubernetes.client"] = k8s_client_mod
    sys.modules["kubernetes.config"] = k8s_config_mod

    rest_mod = types.ModuleType("kubernetes.client.rest")
    rest_mod.ApiException = type("ApiException", (Exception,), {
        "__init__": lambda self, status=None, reason=None: (
            setattr(self, "status", status) or setattr(self, "reason", reason)
        ),
    })
    k8s_client_mod.rest = rest_mod
    sys.modules["kubernetes.client.rest"] = rest_mod


_create_psycopg2_stub()
_create_kubernetes_stub()


def _create_boto3_stub():
    boto3_mod = types.ModuleType("boto3")
    boto3_mod.__path__ = []
    boto3_mod.__package__ = "boto3"
    boto3_mod.client = MagicMock()
    s3_mod = types.ModuleType("boto3.s3")
    s3_mod.__path__ = []
    transfer_mod = types.ModuleType("boto3.s3.transfer")
    transfer_mod.TransferConfig = MagicMock()
    boto3_mod.s3 = s3_mod
    s3_mod.transfer = transfer_mod
    sys.modules["boto3"] = boto3_mod
    sys.modules["boto3.s3"] = s3_mod
    sys.modules["boto3.s3.transfer"] = transfer_mod

    botocore_mod = types.ModuleType("botocore")
    botocore_mod.__path__ = []
    botocore_mod.__package__ = "botocore"
    botocore_config = types.ModuleType("botocore.config")
    botocore_config.Config = MagicMock()
    botocore_mod.config = botocore_config
    sys.modules["botocore"] = botocore_mod
    sys.modules["botocore.config"] = botocore_config


_create_boto3_stub()


def _create_docker_stub():
    """Stub docker so tests don't try to reach /var/run/docker.sock."""
    docker_mod = types.ModuleType("docker")
    docker_mod.__path__ = []
    docker_mod.__package__ = "docker"
    docker_mod.from_env = MagicMock()
    docker_mod.DockerClient = MagicMock()
    errors_mod = types.ModuleType("docker.errors")
    errors_mod.DockerException = type("DockerException", (Exception,), {})
    errors_mod.NotFound = type("NotFound", (Exception,), {})
    errors_mod.APIError = type("APIError", (Exception,), {})
    errors_mod.ImageNotFound = type("ImageNotFound", (Exception,), {})
    errors_mod.BuildError = type("BuildError", (Exception,), {})
    errors_mod.ContainerError = type("ContainerError", (Exception,), {})
    docker_mod.errors = errors_mod
    sys.modules["docker"] = docker_mod
    sys.modules["docker.errors"] = errors_mod


_create_docker_stub()


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

    odoo_mod = _make("odoo")
    _make("odoo.tools")
    _make("odoo.modules")
    _make("odoo.modules.registry", {
        "Registry": MagicMock(),
    })
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
        "constrains": lambda *a: lambda fn: fn,
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

    class _Controller:
        pass

    class _Route:
        def __init__(self, *a, **kw):
            pass
        def __call__(self, fn):
            return fn

    http_mod = _make("odoo.http", {
        "Controller": _Controller,
        "route": _Route,
        "request": MagicMock(),
        "Response": MagicMock,
    })
    odoo_mod.http = http_mod

    for name, mod in stubs.items():
        sys.modules[name] = mod

    odoo_mod.api = stubs["odoo.api"]
    odoo_mod.fields = stubs["odoo.fields"]
    odoo_mod.models = stubs["odoo.models"]
    odoo_mod.exceptions = stubs["odoo.exceptions"]
    odoo_mod.tools = stubs["odoo.tools"]
    odoo_mod.SUPERUSER_ID = 1

    stubs["odoo.tools"].config = MagicMock()
    stubs["odoo.tools"].config.__getitem__ = lambda self, k: {
        "db_host": "localhost", "db_port": "5432",
        "db_user": "odoo", "db_password": "pwd",
        "data_dir": "/tmp", "server_wide_modules": "base,web",
    }.get(k, "")
    stubs["odoo.tools"].config.get = lambda k, d=None: {
        "db_host": "localhost", "db_port": "5432",
        "db_user": "odoo", "db_password": "pwd",
        "data_dir": "/tmp", "server_wide_modules": "base,web",
    }.get(k, d)

    return stubs


_create_odoo_stubs()

# Add custom_addons to sys.path so `odoo.addons.aurora` resolves correctly.
# This conftest lives at custom_addons/conftest.py.
_custom_addons = str(Path(__file__).resolve().parent.parent.parent)
if _custom_addons not in sys.path:
    sys.path.insert(0, _custom_addons)

# Register odoo.addons.aurora package path for namespace resolution.
sys.modules["odoo.addons"].__path__.append(_custom_addons)


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
