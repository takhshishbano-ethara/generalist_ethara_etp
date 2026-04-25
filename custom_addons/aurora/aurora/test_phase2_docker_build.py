import importlib
import json
import subprocess
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from aurora.tools.phase2_docker_build import (
    _ensure_harness_importable,
    _resolve_org_dir,
    check_instance_registry,
    _import_all_repo_modules,
    _check_docker,
    _build_number_interval_map,
    _find_interval_for_pr,
    _RANGE_RE,
    main,
)
from aurora.tools.util import AuroraPipelineError


# ---------------------------------------------------------------------------
# _ensure_harness_importable
# ---------------------------------------------------------------------------

class TestEnsureHarnessImportable:

    def test_adds_root_to_sys_path(self, monkeypatch, tmp_dir):
        monkeypatch.setattr(
            "aurora.tools.phase2_docker_build.MULTI_SWE_BENCH_ROOT", tmp_dir
        )
        monkeypatch.setattr(
            "aurora.tools.phase2_docker_build._HARNESS_REPOS_ROOT",
            tmp_dir / "multi_swe_bench" / "harness" / "repos",
        )
        saved = sys.path[:]
        root_str = str(tmp_dir)
        if root_str in sys.path:
            sys.path.remove(root_str)
        for k in list(sys.modules):
            if k.startswith("multi_swe_bench"):
                del sys.modules[k]
        try:
            _ensure_harness_importable()
            assert root_str in sys.path
        finally:
            sys.path[:] = saved
            for k in list(sys.modules):
                if k.startswith("multi_swe_bench"):
                    del sys.modules[k]

    def test_does_not_duplicate_path(self, monkeypatch, tmp_dir):
        monkeypatch.setattr(
            "aurora.tools.phase2_docker_build.MULTI_SWE_BENCH_ROOT", tmp_dir
        )
        monkeypatch.setattr(
            "aurora.tools.phase2_docker_build._HARNESS_REPOS_ROOT",
            tmp_dir / "multi_swe_bench" / "harness" / "repos",
        )
        root_str = str(tmp_dir)
        saved = sys.path[:]
        for k in list(sys.modules):
            if k.startswith("multi_swe_bench"):
                del sys.modules[k]
        if root_str in sys.path:
            sys.path.remove(root_str)
        try:
            _ensure_harness_importable()
            _ensure_harness_importable()
            assert sys.path.count(root_str) == 1
        finally:
            sys.path[:] = saved
            for k in list(sys.modules):
                if k.startswith("multi_swe_bench"):
                    del sys.modules[k]

    def test_stub_modules_created(self, monkeypatch, tmp_dir):
        monkeypatch.setattr(
            "aurora.tools.phase2_docker_build.MULTI_SWE_BENCH_ROOT", tmp_dir
        )
        monkeypatch.setattr(
            "aurora.tools.phase2_docker_build._HARNESS_REPOS_ROOT",
            tmp_dir / "multi_swe_bench" / "harness" / "repos",
        )
        saved_path = sys.path[:]
        for k in list(sys.modules):
            if k.startswith("multi_swe_bench"):
                del sys.modules[k]
        try:
            _ensure_harness_importable()
            assert "multi_swe_bench" in sys.modules
            assert "multi_swe_bench.harness" in sys.modules
            assert "multi_swe_bench.harness.repos" in sys.modules
        finally:
            sys.path[:] = saved_path
            for k in list(sys.modules):
                if k.startswith("multi_swe_bench"):
                    del sys.modules[k]

    def test_idempotent_no_duplicate_stubs(self, monkeypatch, tmp_dir):
        monkeypatch.setattr(
            "aurora.tools.phase2_docker_build.MULTI_SWE_BENCH_ROOT", tmp_dir
        )
        monkeypatch.setattr(
            "aurora.tools.phase2_docker_build._HARNESS_REPOS_ROOT",
            tmp_dir / "multi_swe_bench" / "harness" / "repos",
        )
        saved_path = sys.path[:]
        for k in list(sys.modules):
            if k.startswith("multi_swe_bench"):
                del sys.modules[k]
        try:
            _ensure_harness_importable()
            stub1 = sys.modules["multi_swe_bench"]
            _ensure_harness_importable()
            assert sys.modules["multi_swe_bench"] is stub1
        finally:
            sys.path[:] = saved_path
            for k in list(sys.modules):
                if k.startswith("multi_swe_bench"):
                    del sys.modules[k]

    def test_scans_lang_dirs(self, monkeypatch, tmp_dir):
        repos_root = tmp_dir / "multi_swe_bench" / "harness" / "repos"
        lang_dir = repos_root / "python"
        org_dir = lang_dir / "encode"
        org_dir.mkdir(parents=True)
        monkeypatch.setattr(
            "aurora.tools.phase2_docker_build.MULTI_SWE_BENCH_ROOT", tmp_dir
        )
        monkeypatch.setattr(
            "aurora.tools.phase2_docker_build._HARNESS_REPOS_ROOT", repos_root,
        )
        saved = sys.path[:]
        for k in list(sys.modules):
            if k.startswith("multi_swe_bench"):
                del sys.modules[k]
        try:
            _ensure_harness_importable()
            assert "multi_swe_bench.harness.repos.python" in sys.modules
            assert "multi_swe_bench.harness.repos.python.encode" in sys.modules
        finally:
            sys.path[:] = saved
            for k in list(sys.modules):
                if k.startswith("multi_swe_bench"):
                    del sys.modules[k]

    def test_ignores_underscore_dirs(self, monkeypatch, tmp_dir):
        repos_root = tmp_dir / "multi_swe_bench" / "harness" / "repos"
        (repos_root / "__pycache__").mkdir(parents=True)
        (repos_root / "python").mkdir(parents=True)
        monkeypatch.setattr(
            "aurora.tools.phase2_docker_build.MULTI_SWE_BENCH_ROOT", tmp_dir
        )
        monkeypatch.setattr(
            "aurora.tools.phase2_docker_build._HARNESS_REPOS_ROOT", repos_root,
        )
        saved = sys.path[:]
        for k in list(sys.modules):
            if k.startswith("multi_swe_bench"):
                del sys.modules[k]
        try:
            _ensure_harness_importable()
            assert "multi_swe_bench.harness.repos.__pycache__" not in sys.modules
        finally:
            sys.path[:] = saved
            for k in list(sys.modules):
                if k.startswith("multi_swe_bench"):
                    del sys.modules[k]

    def test_no_repos_dir(self, monkeypatch, tmp_dir):
        monkeypatch.setattr(
            "aurora.tools.phase2_docker_build.MULTI_SWE_BENCH_ROOT", tmp_dir
        )
        monkeypatch.setattr(
            "aurora.tools.phase2_docker_build._HARNESS_REPOS_ROOT",
            tmp_dir / "multi_swe_bench" / "harness" / "repos",
        )
        saved = sys.path[:]
        for k in list(sys.modules):
            if k.startswith("multi_swe_bench"):
                del sys.modules[k]
        try:
            _ensure_harness_importable()
            assert "multi_swe_bench" in sys.modules
        finally:
            sys.path[:] = saved
            for k in list(sys.modules):
                if k.startswith("multi_swe_bench"):
                    del sys.modules[k]

    def test_multiple_lang_dirs(self, monkeypatch, tmp_dir):
        repos_root = tmp_dir / "multi_swe_bench" / "harness" / "repos"
        for lang in ("python", "javascript", "java"):
            (repos_root / lang).mkdir(parents=True)
        monkeypatch.setattr(
            "aurora.tools.phase2_docker_build.MULTI_SWE_BENCH_ROOT", tmp_dir
        )
        monkeypatch.setattr(
            "aurora.tools.phase2_docker_build._HARNESS_REPOS_ROOT", repos_root,
        )
        saved = sys.path[:]
        for k in list(sys.modules):
            if k.startswith("multi_swe_bench"):
                del sys.modules[k]
        try:
            _ensure_harness_importable()
            for lang in ("python", "javascript", "java"):
                assert f"multi_swe_bench.harness.repos.{lang}" in sys.modules
        finally:
            sys.path[:] = saved
            for k in list(sys.modules):
                if k.startswith("multi_swe_bench"):
                    del sys.modules[k]


# ---------------------------------------------------------------------------
# _resolve_org_dir
# ---------------------------------------------------------------------------

_ORG_EXACT_CASES = [
    ("encode", "encode"),
    ("pallets", "pallets"),
    ("django", "django"),
    ("psf", "psf"),
    ("encode", "encode"),
    ("numpy", "numpy"),
    ("pandas-dev", "pandas-dev"),
    ("scipy", "scipy"),
    ("matplotlib", "matplotlib"),
    ("sqlalchemy", "sqlalchemy"),
]

_ORG_CASE_INSENSITIVE = [
    ("Encode", "encode"),
    ("ENCODE", "encode"),
    ("Pallets", "pallets"),
    ("PALLETS", "pallets"),
    ("Django", "django"),
    ("DJANGO", "django"),
    ("PSF", "psf"),
    ("Psf", "psf"),
    ("NumPy", "numpy"),
    ("NUMPY", "numpy"),
    ("Pandas-Dev", "pandas-dev"),
    ("PANDAS-DEV", "pandas-dev"),
    ("SciPy", "scipy"),
    ("SCIPY", "scipy"),
    ("Matplotlib", "matplotlib"),
    ("MATPLOTLIB", "matplotlib"),
    ("SQLAlchemy", "sqlalchemy"),
    ("SQLALCHEMY", "sqlalchemy"),
    ("eNcOdE", "encode"),
    ("pAlLeTs", "pallets"),
    ("dJaNgO", "django"),
    ("nUmPy", "numpy"),
    ("sCiPy", "scipy"),
    ("mAtPlOtLiB", "matplotlib"),
    ("sQlAlChEmY", "sqlalchemy"),
]

_ORG_NO_MATCH = [
    "nonexistent",
    "foobar",
    "unknownorg",
    "x",
    "encode2",
    "encod",
    "palletsX",
]


class TestResolveOrgDir:

    @pytest.mark.parametrize("org,dir_name", _ORG_EXACT_CASES)
    def test_exact_match(self, tmp_dir, org, dir_name):
        lang_dir = tmp_dir / "python"
        (lang_dir / dir_name).mkdir(parents=True)
        result = _resolve_org_dir(lang_dir, org)
        assert result is not None
        assert result.name == dir_name

    @pytest.mark.parametrize("query_org,dir_name", _ORG_CASE_INSENSITIVE)
    def test_case_insensitive(self, tmp_dir, query_org, dir_name):
        lang_dir = tmp_dir / "python"
        (lang_dir / dir_name).mkdir(parents=True)
        result = _resolve_org_dir(lang_dir, query_org)
        assert result is not None
        assert result.name.lower() == dir_name.lower()

    @pytest.mark.parametrize("org", _ORG_NO_MATCH)
    def test_no_match(self, tmp_dir, org):
        lang_dir = tmp_dir / "python"
        (lang_dir / "encode").mkdir(parents=True)
        result = _resolve_org_dir(lang_dir, org)
        if org.lower() == "encode":
            assert result is not None
        else:
            assert result is None

    def test_empty_directory(self, tmp_dir):
        lang_dir = tmp_dir / "python"
        lang_dir.mkdir(parents=True)
        assert _resolve_org_dir(lang_dir, "encode") is None

    def test_file_not_dir_ignored(self, tmp_dir):
        lang_dir = tmp_dir / "python"
        lang_dir.mkdir(parents=True)
        (lang_dir / "encode").touch()
        assert _resolve_org_dir(lang_dir, "encode") is None

    def test_multiple_dirs_exact_priority(self, tmp_dir):
        lang_dir = tmp_dir / "python"
        (lang_dir / "myorg").mkdir(parents=True)
        (lang_dir / "other").mkdir(parents=True)
        result = _resolve_org_dir(lang_dir, "myorg")
        assert result is not None
        assert result.name.lower() == "myorg"


# ---------------------------------------------------------------------------
# check_instance_registry
# ---------------------------------------------------------------------------

_REGISTRY_MATCH_CASES = [
    ("encode", "starlette", "python", ["starlette.py"], True),
    ("encode", "starlette", "python", ["starlette_3055_to_2813.py"], True),
    ("encode", "starlette", "python", ["starlette_100_to_50.py", "starlette.py"], True),
    ("encode", "httpx", "python", ["httpx.py"], True),
    ("pallets", "flask", "python", ["flask.py"], True),
    ("pallets", "flask", "python", ["flask_500_to_200.py"], True),
    ("django", "django", "python", ["django.py"], True),
    ("encode", "starlette", "python", ["STARLETTE.py"], True),
    ("psf", "requests", "python", ["requests.py"], True),
    ("numpy", "numpy", "python", ["numpy.py"], True),
]

_REGISTRY_NO_MATCH_CASES = [
    ("encode", "starlette", "python", ["other_repo.py"], False),
    ("encode", "starlette", "python", ["__init__.py"], False),
    ("encode", "starlette", "python", ["starlette.txt"], False),
    ("encode", "starlette", "python", ["starlette.json"], False),
    ("encode", "starlette", "python", [], False),
    ("encode", "starlette", "python", ["httpx.py"], False),
    ("encode", "starlette", "python", ["star.py"], False),
    ("encode", "starlette", "python", ["README.md"], False),
]

_REGISTRY_LANG_MISSING = [
    ("encode", "starlette", "rust"),
    ("encode", "starlette", "go"),
    ("encode", "starlette", "haskell"),
    ("encode", "starlette", "nonexistent"),
]

_REGISTRY_ORG_MISSING = [
    ("nonexistent_org", "starlette", "python"),
    ("foobar", "starlette", "python"),
    ("x", "starlette", "python"),
]

_REGISTRY_EXTRA = [
    ("encode", "starlette", "python", ["Starlette.py"], True),
    ("encode", "starlette", "python", ["STARLETTE_100_TO_50.py"], True),
    ("encode", "http-x", "python", ["http_x.py"], True),
    ("encode", "http-x", "python", ["http_x_500_to_100.py"], True),
    ("pallets", "flask", "python", ["flask_1_to_0.py"], True),
    ("django", "django", "python", ["django_5000_to_4000.py"], True),
    ("encode", "starlette", "python", ["starlette_abc.py"], True),
    ("encode", "starlette", "python", ["xstarlette.py"], False),
    ("encode", "starlette", "python", ["__init__.py", "starlette.py"], True),
    ("encode", "starlette", "python", ["__init__.py"], False),
    ("encode", "starlette", "python", ["starlette.pyc"], False),
    ("scipy", "scipy", "python", ["scipy.py"], True),
    ("matplotlib", "matplotlib", "python", ["matplotlib_200_to_100.py"], True),
]


class TestCheckInstanceRegistry:

    @pytest.fixture(autouse=True)
    def _patch_harness(self, monkeypatch, tmp_dir):
        self._repos_root = tmp_dir / "repos"
        monkeypatch.setattr(
            "aurora.tools.phase2_docker_build._HARNESS_REPOS_ROOT", self._repos_root
        )
        monkeypatch.setattr(
            "aurora.tools.phase2_docker_build._ensure_harness_importable", lambda: None
        )

    def _setup_files(self, org, lang, filenames):
        org_dir = self._repos_root / lang / org
        org_dir.mkdir(parents=True, exist_ok=True)
        for fn in filenames:
            (org_dir / fn).touch()

    @pytest.mark.parametrize("org,repo,lang,files,expected", _REGISTRY_MATCH_CASES)
    def test_match(self, org, repo, lang, files, expected):
        self._setup_files(org, lang, files)
        assert check_instance_registry(org, repo, lang) == expected

    @pytest.mark.parametrize("org,repo,lang,files,expected", _REGISTRY_NO_MATCH_CASES)
    def test_no_match(self, org, repo, lang, files, expected):
        self._setup_files(org, lang, files)
        assert check_instance_registry(org, repo, lang) == expected

    @pytest.mark.parametrize("org,repo,lang", _REGISTRY_LANG_MISSING)
    def test_lang_missing(self, org, repo, lang):
        assert check_instance_registry(org, repo, lang) is False

    @pytest.mark.parametrize("org,repo,lang", _REGISTRY_ORG_MISSING)
    def test_org_missing(self, org, repo, lang):
        (self._repos_root / lang).mkdir(parents=True, exist_ok=True)
        assert check_instance_registry(org, repo, lang) is False

    @pytest.mark.parametrize("org,repo,lang,files,expected", _REGISTRY_EXTRA)
    def test_extra_cases(self, org, repo, lang, files, expected):
        self._setup_files(org, lang, files)
        assert check_instance_registry(org, repo, lang) == expected


# ---------------------------------------------------------------------------
# _import_all_repo_modules
# ---------------------------------------------------------------------------

_IMPORT_CASES = [
    ("encode", "starlette", "python", ["starlette.py"], 1),
    ("encode", "starlette", "python", ["starlette.py", "starlette_100_to_50.py"], 2),
    ("encode", "starlette", "python", ["starlette.py", "starlette_200_to_100.py", "starlette_500_to_300.py"], 3),
    ("pallets", "flask", "python", ["flask.py"], 1),
    ("django", "django", "python", ["django.py", "django_5000_to_4000.py"], 2),
    ("encode", "http-x", "python", ["http_x.py"], 1),
    ("encode", "http-x", "python", ["http_x.py", "http_x_300_to_200.py"], 2),
]

_IMPORT_NO_FILES = [
    ("encode", "starlette", "python", ["other_repo.py"]),
    ("encode", "starlette", "python", ["__init__.py"]),
    ("encode", "starlette", "python", []),
    ("encode", "starlette", "python", ["httpx.py"]),
    ("pallets", "flask", "python", ["django.py"]),
]


class TestImportAllRepoModules:

    @pytest.fixture(autouse=True)
    def _patch(self, monkeypatch, tmp_dir):
        self._repos_root = tmp_dir / "repos"
        monkeypatch.setattr(
            "aurora.tools.phase2_docker_build._HARNESS_REPOS_ROOT", self._repos_root
        )
        monkeypatch.setattr(
            "aurora.tools.phase2_docker_build._ensure_harness_importable", lambda: None
        )

    def _setup(self, org, lang, files):
        d = self._repos_root / lang / org
        d.mkdir(parents=True, exist_ok=True)
        for f in files:
            (d / f).touch()

    @pytest.mark.parametrize("org,repo,lang,files,expected_count", _IMPORT_CASES)
    def test_loads_modules(self, monkeypatch, org, repo, lang, files, expected_count):
        self._setup(org, lang, files)
        loaded = []
        monkeypatch.setattr(
            "importlib.import_module",
            lambda name: loaded.append(name) or MagicMock(),
        )
        _import_all_repo_modules(org, repo, lang)
        assert len(loaded) == expected_count

    @pytest.mark.parametrize("org,repo,lang,files", _IMPORT_NO_FILES)
    def test_no_files_raises(self, monkeypatch, org, repo, lang, files):
        self._setup(org, lang, files)
        monkeypatch.setattr("importlib.import_module", lambda name: MagicMock())
        with pytest.raises(AuroraPipelineError, match="No harness modules found"):
            _import_all_repo_modules(org, repo, lang)

    def test_org_missing_raises(self):
        (self._repos_root / "python").mkdir(parents=True, exist_ok=True)
        with pytest.raises(AuroraPipelineError, match="Org directory not found"):
            _import_all_repo_modules("missing_org", "repo", "python")

    def test_import_failure_raises(self, monkeypatch):
        self._setup("encode", "python", ["starlette.py"])
        monkeypatch.setattr(
            "importlib.import_module",
            MagicMock(side_effect=RuntimeError("broken")),
        )
        with pytest.raises(AuroraPipelineError, match="Failed to import"):
            _import_all_repo_modules("encode", "starlette", "python")

    @pytest.mark.parametrize("exc_type", [ImportError, ModuleNotFoundError, RuntimeError, TypeError])
    def test_import_various_errors(self, monkeypatch, exc_type):
        self._setup("encode", "python", ["starlette.py"])
        monkeypatch.setattr(
            "importlib.import_module",
            MagicMock(side_effect=exc_type("err")),
        )
        with pytest.raises(AuroraPipelineError):
            _import_all_repo_modules("encode", "starlette", "python")


# ---------------------------------------------------------------------------
# _check_docker
# ---------------------------------------------------------------------------

_DOCKER_FAIL_CODES = [1, 2, 3, 4, 5, 10, 42, 100, 125, 126, 127, 128, 137, 143, 255]


class TestCheckDocker:

    def test_docker_running(self, monkeypatch):
        mock_result = MagicMock(returncode=0)
        monkeypatch.setattr("subprocess.run", MagicMock(return_value=mock_result))
        _check_docker()

    @pytest.mark.parametrize("rc", _DOCKER_FAIL_CODES)
    def test_docker_not_running(self, monkeypatch, rc):
        mock_result = MagicMock(returncode=rc)
        monkeypatch.setattr("subprocess.run", MagicMock(return_value=mock_result))
        with pytest.raises(AuroraPipelineError, match="Docker daemon is not running"):
            _check_docker()

    def test_docker_not_installed(self, monkeypatch):
        monkeypatch.setattr(
            "subprocess.run", MagicMock(side_effect=FileNotFoundError)
        )
        with pytest.raises(AuroraPipelineError, match="Docker is not installed"):
            _check_docker()

    def test_docker_timeout(self, monkeypatch):
        monkeypatch.setattr(
            "subprocess.run",
            MagicMock(side_effect=subprocess.TimeoutExpired(cmd="docker info", timeout=10)),
        )
        with pytest.raises(AuroraPipelineError, match="timed out"):
            _check_docker()


# ---------------------------------------------------------------------------
# _build_number_interval_map
# ---------------------------------------------------------------------------

_INTERVAL_VALID_FILES = [
    ("encode", "starlette", "python", ["starlette_3055_to_2813.py"], [("starlette_3055_to_2813", 2813, 3055)]),
    ("encode", "starlette", "python", ["starlette_100_to_50.py"], [("starlette_100_to_50", 50, 100)]),
    ("encode", "starlette", "python", ["starlette_50_to_100.py"], [("starlette_50_to_100", 50, 100)]),
    ("pallets", "flask", "python", ["flask_500_to_200.py"], [("flask_500_to_200", 200, 500)]),
    ("django", "django", "python", ["django_5000_to_4000.py"], [("django_5000_to_4000", 4000, 5000)]),
    ("encode", "starlette", "python", [
        "starlette_3055_to_2813.py",
        "starlette_2812_to_2000.py",
    ], [
        ("starlette_2812_to_2000", 2000, 2812),
        ("starlette_3055_to_2813", 2813, 3055),
    ]),
    ("encode", "starlette", "python", [
        "starlette_500_to_300.py",
        "starlette_200_to_100.py",
        "starlette_1000_to_600.py",
    ], [
        ("starlette_200_to_100", 100, 200),
        ("starlette_500_to_300", 300, 500),
        ("starlette_1000_to_600", 600, 1000),
    ]),
    ("encode", "http-x", "python", ["http_x_300_to_200.py"], [("http_x_300_to_200", 200, 300)]),
    ("encode", "starlette", "python", ["starlette_1_to_0.py"], [("starlette_1_to_0", 0, 1)]),
    ("encode", "starlette", "python", ["starlette_99999_to_10000.py"], [("starlette_99999_to_10000", 10000, 99999)]),
]

_INTERVAL_IGNORED_FILES = [
    ("encode", "starlette", "python", ["starlette.py"], []),
    ("encode", "starlette", "python", ["starlette_abc_to_def.py"], []),
    ("encode", "starlette", "python", ["other_repo_100_to_50.py"], []),
    ("encode", "starlette", "python", ["__init__.py"], []),
    ("encode", "starlette", "python", ["starlette.txt"], []),
    ("encode", "starlette", "python", ["starlette.json"], []),
    ("encode", "starlette", "python", ["httpx_100_to_50.py"], []),
    ("encode", "starlette", "python", ["starlette_to_100.py"], []),
    ("encode", "starlette", "python", ["starlette_100_to.py"], []),
    ("encode", "starlette", "python", ["xstarlette_100_to_50.py"], []),
]

_INTERVAL_MIXED_FILES = [
    ("encode", "starlette", "python",
     ["starlette.py", "starlette_3055_to_2813.py", "__init__.py", "httpx.py", "starlette_100_to_50.py"],
     [("starlette_100_to_50", 50, 100), ("starlette_3055_to_2813", 2813, 3055)]),
    ("encode", "starlette", "python",
     ["starlette.py", "other_100_to_50.py", "starlette_abc_to_def.py"],
     []),
    ("pallets", "flask", "python",
     ["flask.py", "flask_500_to_200.py", "flask_100_to_50.py", "flask.txt"],
     [("flask_100_to_50", 50, 100), ("flask_500_to_200", 200, 500)]),
]

_INTERVAL_EXTRA = [
    ("encode", "starlette", "python", ["starlette_0_to_0.py"], [("starlette_0_to_0", 0, 0)]),
    ("encode", "starlette", "python", ["starlette_10_to_10.py"], [("starlette_10_to_10", 10, 10)]),
    ("encode", "starlette", "python", ["starlette_2_to_1.py", "starlette_4_to_3.py", "starlette_6_to_5.py"],
     [("starlette_2_to_1", 1, 2), ("starlette_4_to_3", 3, 4), ("starlette_6_to_5", 5, 6)]),
]


class TestBuildNumberIntervalMap:

    @pytest.fixture(autouse=True)
    def _patch(self, monkeypatch, tmp_dir):
        self._repos_root = tmp_dir / "repos"
        monkeypatch.setattr(
            "aurora.tools.phase2_docker_build._HARNESS_REPOS_ROOT", self._repos_root
        )

    def _setup(self, org, lang, files):
        d = self._repos_root / lang / org
        d.mkdir(parents=True, exist_ok=True)
        for f in files:
            (d / f).touch()

    @pytest.mark.parametrize("org,repo,lang,files,expected", _INTERVAL_VALID_FILES)
    def test_valid_ranges(self, org, repo, lang, files, expected):
        self._setup(org, lang, files)
        result = _build_number_interval_map(org, repo, lang)
        assert result == expected

    @pytest.mark.parametrize("org,repo,lang,files,expected", _INTERVAL_IGNORED_FILES)
    def test_ignored_files(self, org, repo, lang, files, expected):
        self._setup(org, lang, files)
        assert _build_number_interval_map(org, repo, lang) == expected

    @pytest.mark.parametrize("org,repo,lang,files,expected", _INTERVAL_MIXED_FILES)
    def test_mixed(self, org, repo, lang, files, expected):
        self._setup(org, lang, files)
        assert _build_number_interval_map(org, repo, lang) == expected

    @pytest.mark.parametrize("org,repo,lang,files,expected", _INTERVAL_EXTRA)
    def test_extra(self, org, repo, lang, files, expected):
        self._setup(org, lang, files)
        assert _build_number_interval_map(org, repo, lang) == expected

    def test_org_missing_returns_empty(self):
        (self._repos_root / "python").mkdir(parents=True, exist_ok=True)
        assert _build_number_interval_map("nonexistent", "starlette", "python") == []

    def test_lang_missing_returns_empty(self):
        (self._repos_root / "nonexistent").mkdir(parents=True, exist_ok=True)
        assert _build_number_interval_map("encode", "starlette", "nonexistent") == []

    def test_empty_dir(self):
        d = self._repos_root / "python" / "encode"
        d.mkdir(parents=True)
        assert _build_number_interval_map("encode", "starlette", "python") == []


# ---------------------------------------------------------------------------
# _find_interval_for_pr
# ---------------------------------------------------------------------------

_SINGLE_RANGE = [("starlette_3055_to_2813", 2813, 3055)]
_MULTI_RANGES = [
    ("starlette_100_to_50", 50, 100),
    ("starlette_200_to_150", 150, 200),
    ("starlette_500_to_300", 300, 500),
]

_PR_IN_RANGE = [
    (2813, _SINGLE_RANGE, "starlette_3055_to_2813"),
    (3055, _SINGLE_RANGE, "starlette_3055_to_2813"),
    (2900, _SINGLE_RANGE, "starlette_3055_to_2813"),
    (3000, _SINGLE_RANGE, "starlette_3055_to_2813"),
    (50, _MULTI_RANGES, "starlette_100_to_50"),
    (100, _MULTI_RANGES, "starlette_100_to_50"),
    (75, _MULTI_RANGES, "starlette_100_to_50"),
    (150, _MULTI_RANGES, "starlette_200_to_150"),
    (200, _MULTI_RANGES, "starlette_200_to_150"),
    (175, _MULTI_RANGES, "starlette_200_to_150"),
    (300, _MULTI_RANGES, "starlette_500_to_300"),
    (500, _MULTI_RANGES, "starlette_500_to_300"),
    (400, _MULTI_RANGES, "starlette_500_to_300"),
    (350, _MULTI_RANGES, "starlette_500_to_300"),
    (450, _MULTI_RANGES, "starlette_500_to_300"),
    (51, _MULTI_RANGES, "starlette_100_to_50"),
    (99, _MULTI_RANGES, "starlette_100_to_50"),
    (151, _MULTI_RANGES, "starlette_200_to_150"),
    (199, _MULTI_RANGES, "starlette_200_to_150"),
    (301, _MULTI_RANGES, "starlette_500_to_300"),
    (499, _MULTI_RANGES, "starlette_500_to_300"),
]

_PR_NOT_IN_RANGE = [
    (0, _SINGLE_RANGE, ""),
    (1, _SINGLE_RANGE, ""),
    (2812, _SINGLE_RANGE, ""),
    (3056, _SINGLE_RANGE, ""),
    (9999, _SINGLE_RANGE, ""),
    (49, _MULTI_RANGES, ""),
    (101, _MULTI_RANGES, ""),
    (149, _MULTI_RANGES, ""),
    (201, _MULTI_RANGES, ""),
    (299, _MULTI_RANGES, ""),
    (501, _MULTI_RANGES, ""),
    (1000, _MULTI_RANGES, ""),
    (0, _MULTI_RANGES, ""),
    (10000, _MULTI_RANGES, ""),
    (2, _SINGLE_RANGE, ""),
    (100, _SINGLE_RANGE, ""),
    (500, _SINGLE_RANGE, ""),
    (1000, _SINGLE_RANGE, ""),
    (5000, _SINGLE_RANGE, ""),
    (999999, _SINGLE_RANGE, ""),
    (25, _MULTI_RANGES, ""),
    (110, _MULTI_RANGES, ""),
    (250, _MULTI_RANGES, ""),
    (600, _MULTI_RANGES, ""),
]

_PR_EMPTY = [
    (0, [], ""),
    (1, [], ""),
    (100, [], ""),
    (999, [], ""),
    (2813, [], ""),
    (50, [], ""),
    (9999, [], ""),
    (500000, [], ""),
]

_PR_BOUNDARY = [
    (2813, _SINGLE_RANGE, "starlette_3055_to_2813"),
    (3055, _SINGLE_RANGE, "starlette_3055_to_2813"),
    (50, _MULTI_RANGES, "starlette_100_to_50"),
    (100, _MULTI_RANGES, "starlette_100_to_50"),
    (150, _MULTI_RANGES, "starlette_200_to_150"),
    (200, _MULTI_RANGES, "starlette_200_to_150"),
    (300, _MULTI_RANGES, "starlette_500_to_300"),
    (500, _MULTI_RANGES, "starlette_500_to_300"),
]

_PR_OVERLAP = [
    (75, [("r1", 50, 100), ("r2", 60, 90)], "r1"),
    (80, [("r1", 50, 100), ("r2", 60, 120)], "r1"),
    (110, [("r1", 50, 100), ("r2", 60, 120)], "r2"),
    (60, [("r1", 50, 100), ("r2", 60, 90)], "r1"),
    (90, [("r1", 50, 100), ("r2", 60, 90)], "r1"),
    (65, [("r1", 50, 80), ("r2", 70, 100)], "r1"),
    (85, [("r1", 50, 80), ("r2", 70, 100)], "r2"),
    (75, [("r1", 50, 80), ("r2", 70, 100)], "r1"),
]


class TestFindIntervalForPr:

    @pytest.mark.parametrize("pr,ranges,expected", _PR_IN_RANGE)
    def test_pr_in_range(self, pr, ranges, expected):
        assert _find_interval_for_pr(pr, ranges) == expected

    @pytest.mark.parametrize("pr,ranges,expected", _PR_NOT_IN_RANGE)
    def test_pr_not_in_range(self, pr, ranges, expected):
        assert _find_interval_for_pr(pr, ranges) == expected

    @pytest.mark.parametrize("pr,ranges,expected", _PR_EMPTY)
    def test_empty_ranges(self, pr, ranges, expected):
        assert _find_interval_for_pr(pr, ranges) == expected

    @pytest.mark.parametrize("pr,ranges,expected", _PR_BOUNDARY)
    def test_boundary_values(self, pr, ranges, expected):
        assert _find_interval_for_pr(pr, ranges) == expected

    @pytest.mark.parametrize("pr,ranges,expected", _PR_OVERLAP)
    def test_overlapping_ranges(self, pr, ranges, expected):
        assert _find_interval_for_pr(pr, ranges) == expected


# ---------------------------------------------------------------------------
# _RANGE_RE
# ---------------------------------------------------------------------------

_REGEX_MATCH_CASES = [
    ("starlette_3055_to_2813", "starlette", "3055", "2813"),
    ("flask_500_to_200", "flask", "500", "200"),
    ("django_5000_to_4000", "django", "5000", "4000"),
    ("http_x_300_to_200", "http_x", "300", "200"),
    ("my_repo_1_to_0", "my_repo", "1", "0"),
    ("a_99999_to_10000", "a", "99999", "10000"),
    ("repo_0_to_0", "repo", "0", "0"),
    ("x_1_to_2", "x", "1", "2"),
    ("abc_def_100_to_50", "abc_def", "100", "50"),
    ("some_long_name_500_to_100", "some_long_name", "500", "100"),
]

_REGEX_NO_MATCH_CASES = [
    "starlette",
    "starlette.py",
    "starlette_abc_to_def",
    "starlette_to_100",
    "starlette_100_to",
    "_100_to_50",
    "",
    "starlette_100_200",
    "starlette_100to50",
    "100_to_50",
    "starlette_100_to_abc",
    "starlette_abc_to_100",
]


class TestRangeRegex:

    @pytest.mark.parametrize("stem,g1,g2,g3", _REGEX_MATCH_CASES)
    def test_matches(self, stem, g1, g2, g3):
        m = _RANGE_RE.match(stem)
        assert m is not None
        assert m.group(1) == g1
        assert m.group(2) == g2
        assert m.group(3) == g3

    @pytest.mark.parametrize("stem", _REGEX_NO_MATCH_CASES)
    def test_no_match(self, stem):
        assert _RANGE_RE.match(stem) is None


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------

_MAIN_MAX_WORKERS = [1, 2, 4, 8, 16]
_MAIN_FORCE_BUILD = [True, False]


def _make_mock_instance(inst_id, report_exists=True, report_data=None, valid=True):
    inst = MagicMock()
    inst.pr.id = inst_id
    inst.pr.org = "encode"
    inst.pr.repo = "starlette"
    dep = MagicMock()
    dep.workdir.return_value = f"workdir_{inst_id}"
    inst.dependency.return_value = dep
    inst._report_exists = report_exists
    inst._report_data = report_data or {
        "valid": valid,
        "f2p_tests": {"test_a": {}} if valid else {},
        "p2p_tests": {},
        "s2p_tests": {},
        "n2p_tests": {},
        "fixed_tests": {},
        "error_msg": "" if valid else "build failed",
    }
    return inst


class TestMain:

    @pytest.fixture(autouse=True)
    def _patch_all(self, monkeypatch, tmp_dir):
        self._tmp = tmp_dir
        self._repos_root = tmp_dir / "repos"
        monkeypatch.setattr(
            "aurora.tools.phase2_docker_build._ensure_harness_importable", lambda: None,
        )
        monkeypatch.setattr(
            "aurora.tools.phase2_docker_build._check_docker", lambda: None,
        )
        monkeypatch.setattr(
            "aurora.tools.phase2_docker_build._HARNESS_REPOS_ROOT", self._repos_root,
        )
        self._check_reg = MagicMock(return_value=True)
        monkeypatch.setattr(
            "aurora.tools.phase2_docker_build.check_instance_registry",
            self._check_reg,
        )
        self._import_mods = MagicMock()
        monkeypatch.setattr(
            "aurora.tools.phase2_docker_build._import_all_repo_modules",
            self._import_mods,
        )
        self._translate = MagicMock(return_value=3)
        monkeypatch.setattr(
            "aurora.tools.phase2_docker_build._translate_phase1_jsonl",
            self._translate,
        )

    def _make_jsonl(self, records=None):
        p = self._tmp / "phase1.jsonl"
        with open(p, "w") as f:
            for r in (records or [{"number": 100}]):
                f.write(json.dumps(r) + "\n")
        return str(p)

    def test_phase1_not_found_raises(self):
        with pytest.raises(AuroraPipelineError, match="Phase 1 JSONL not found"):
            main("/nonexistent/path.jsonl", str(self._tmp / "out"), "encode", "starlette", "python")

    def test_no_registry_raises(self):
        self._check_reg.return_value = False
        jsonl = self._make_jsonl()
        with pytest.raises(AuroraPipelineError, match="No instance registry found"):
            main(jsonl, str(self._tmp / "out"), "encode", "starlette", "python")

    def test_zero_translation_raises(self, monkeypatch):
        self._translate.return_value = 0
        jsonl = self._make_jsonl()
        mock_bd = MagicMock()
        monkeypatch.setattr("importlib.import_module", lambda name: mock_bd)
        with pytest.raises(AuroraPipelineError, match="No Phase 1 records matched"):
            main(jsonl, str(self._tmp / "out"), "encode", "starlette", "python")

    def test_no_instances_raises(self, monkeypatch):
        jsonl = self._make_jsonl()
        mock_cli = MagicMock()
        mock_cli.instances = []
        mock_bd = MagicMock()
        mock_bd.CliArgs.return_value = mock_cli
        monkeypatch.setattr("importlib.import_module", lambda name: mock_bd)
        with pytest.raises(AuroraPipelineError, match="No valid instances"):
            main(jsonl, str(self._tmp / "out"), "encode", "starlette", "python")

    @pytest.mark.parametrize("max_workers", _MAIN_MAX_WORKERS)
    def test_success_path_max_workers(self, monkeypatch, max_workers):
        jsonl = self._make_jsonl()
        out_dir = str(self._tmp / "out")
        inst = _make_mock_instance("inst1", valid=True)
        mock_cli = MagicMock()
        mock_cli.instances = [inst]
        mock_bd = MagicMock()
        mock_bd.CliArgs.return_value = mock_cli
        monkeypatch.setattr("importlib.import_module", lambda name: mock_bd)
        workdir = Path(out_dir) / "phase2_workdir"
        idir = workdir / "encode" / "starlette" / "instances" / "workdir_inst1"
        idir.mkdir(parents=True, exist_ok=True)
        with open(idir / "report.json", "w") as f:
            json.dump(inst._report_data, f)
        result = main(jsonl, out_dir, "encode", "starlette", "python", max_workers=max_workers)
        assert result["instance_count"] == 1
        assert result["resolved_count"] == 1

    @pytest.mark.parametrize("force_build", _MAIN_FORCE_BUILD)
    def test_success_path_force_build(self, monkeypatch, force_build):
        jsonl = self._make_jsonl()
        out_dir = str(self._tmp / "out")
        inst = _make_mock_instance("inst1", valid=True)
        mock_cli = MagicMock()
        mock_cli.instances = [inst]
        mock_bd = MagicMock()
        mock_bd.CliArgs.return_value = mock_cli
        monkeypatch.setattr("importlib.import_module", lambda name: mock_bd)
        workdir = Path(out_dir) / "phase2_workdir"
        idir = workdir / "encode" / "starlette" / "instances" / "workdir_inst1"
        idir.mkdir(parents=True, exist_ok=True)
        with open(idir / "report.json", "w") as f:
            json.dump(inst._report_data, f)
        result = main(jsonl, out_dir, "encode", "starlette", "python", force_build=force_build)
        assert result["instance_count"] == 1

    def test_log_callback_called(self, monkeypatch):
        jsonl = self._make_jsonl()
        out_dir = str(self._tmp / "out")
        inst = _make_mock_instance("inst1")
        mock_cli = MagicMock()
        mock_cli.instances = [inst]
        mock_bd = MagicMock()
        mock_bd.CliArgs.return_value = mock_cli
        monkeypatch.setattr("importlib.import_module", lambda name: mock_bd)
        workdir = Path(out_dir) / "phase2_workdir"
        idir = workdir / "encode" / "starlette" / "instances" / "workdir_inst1"
        idir.mkdir(parents=True, exist_ok=True)
        with open(idir / "report.json", "w") as f:
            json.dump(inst._report_data, f)
        cb = MagicMock()
        main(jsonl, out_dir, "encode", "starlette", "python", log_callback=cb)
        assert cb.call_count >= 3

    def test_log_callback_none(self, monkeypatch):
        jsonl = self._make_jsonl()
        out_dir = str(self._tmp / "out")
        inst = _make_mock_instance("inst1")
        mock_cli = MagicMock()
        mock_cli.instances = [inst]
        mock_bd = MagicMock()
        mock_bd.CliArgs.return_value = mock_cli
        monkeypatch.setattr("importlib.import_module", lambda name: mock_bd)
        workdir = Path(out_dir) / "phase2_workdir"
        idir = workdir / "encode" / "starlette" / "instances" / "workdir_inst1"
        idir.mkdir(parents=True, exist_ok=True)
        with open(idir / "report.json", "w") as f:
            json.dump(inst._report_data, f)
        result = main(jsonl, out_dir, "encode", "starlette", "python", log_callback=None)
        assert "results" in result

    def test_report_file_written(self, monkeypatch):
        jsonl = self._make_jsonl()
        out_dir = str(self._tmp / "out")
        inst = _make_mock_instance("inst1")
        mock_cli = MagicMock()
        mock_cli.instances = [inst]
        mock_bd = MagicMock()
        mock_bd.CliArgs.return_value = mock_cli
        monkeypatch.setattr("importlib.import_module", lambda name: mock_bd)
        workdir = Path(out_dir) / "phase2_workdir"
        idir = workdir / "encode" / "starlette" / "instances" / "workdir_inst1"
        idir.mkdir(parents=True, exist_ok=True)
        with open(idir / "report.json", "w") as f:
            json.dump(inst._report_data, f)
        result = main(jsonl, out_dir, "encode", "starlette", "python")
        assert Path(result["report_file"]).exists()

    def test_image_count_equals_instances(self, monkeypatch):
        jsonl = self._make_jsonl()
        out_dir = str(self._tmp / "out")
        instances = [_make_mock_instance(f"inst{i}") for i in range(3)]
        mock_cli = MagicMock()
        mock_cli.instances = instances
        mock_bd = MagicMock()
        mock_bd.CliArgs.return_value = mock_cli
        monkeypatch.setattr("importlib.import_module", lambda name: mock_bd)
        workdir = Path(out_dir) / "phase2_workdir"
        for inst in instances:
            idir = workdir / "encode" / "starlette" / "instances" / f"workdir_{inst.pr.id}"
            idir.mkdir(parents=True, exist_ok=True)
            with open(idir / "report.json", "w") as f:
                json.dump(inst._report_data, f)
        result = main(jsonl, out_dir, "encode", "starlette", "python")
        assert result["image_count"] == 3

    def test_run_mode_image_called(self, monkeypatch):
        jsonl = self._make_jsonl()
        out_dir = str(self._tmp / "out")
        inst = _make_mock_instance("inst1")
        mock_cli = MagicMock()
        mock_cli.instances = [inst]
        mock_bd = MagicMock()
        mock_bd.CliArgs.return_value = mock_cli
        monkeypatch.setattr("importlib.import_module", lambda name: mock_bd)
        workdir = Path(out_dir) / "phase2_workdir"
        idir = workdir / "encode" / "starlette" / "instances" / "workdir_inst1"
        idir.mkdir(parents=True, exist_ok=True)
        with open(idir / "report.json", "w") as f:
            json.dump(inst._report_data, f)
        main(jsonl, out_dir, "encode", "starlette", "python")
        mock_cli.run_mode_image.assert_called_once()
        mock_cli.run_mode_instance_only.assert_called_once()


# ---------------------------------------------------------------------------
# Report parsing in main()
# ---------------------------------------------------------------------------

_REPORT_VALID_DATA = [
    ({"valid": True, "f2p_tests": {"t1": {}}, "p2p_tests": {}, "s2p_tests": {}, "n2p_tests": {}, "fixed_tests": {}, "error_msg": ""}, True, ["t1"]),
    ({"valid": True, "f2p_tests": {"a": {}, "b": {}}, "p2p_tests": {"c": {}}, "s2p_tests": {}, "n2p_tests": {}, "fixed_tests": {}, "error_msg": ""}, True, ["a", "b"]),
    ({"valid": False, "f2p_tests": {}, "p2p_tests": {}, "s2p_tests": {}, "n2p_tests": {}, "fixed_tests": {}, "error_msg": "fail"}, False, []),
    ({"valid": True, "f2p_tests": {}, "p2p_tests": {}, "s2p_tests": {}, "n2p_tests": {}, "fixed_tests": {"t1": {}}, "error_msg": ""}, True, []),
    ({"valid": True, "f2p_tests": {"x": {}}, "p2p_tests": {"y": {}}, "s2p_tests": {"z": {}}, "n2p_tests": {"w": {}}, "fixed_tests": {"v": {}}, "error_msg": ""}, True, ["x"]),
]

_REPORT_INVALID_DATA = [
    "not json",
    "{invalid}",
    "",
    "null",
]

_REPORT_VARIATIONS = [
    ({"valid": True, "f2p_tests": {f"t{i}": {} for i in range(10)}, "p2p_tests": {}, "s2p_tests": {}, "n2p_tests": {}, "fixed_tests": {}, "error_msg": ""}, True, 10),
    ({"valid": True, "f2p_tests": {}, "p2p_tests": {f"p{i}": {} for i in range(5)}, "s2p_tests": {}, "n2p_tests": {}, "fixed_tests": {}, "error_msg": ""}, True, 0),
    ({"valid": False, "f2p_tests": {}, "p2p_tests": {}, "s2p_tests": {}, "n2p_tests": {}, "fixed_tests": {}, "error_msg": "exit code 1"}, False, 0),
    ({"valid": False, "f2p_tests": {}, "p2p_tests": {}, "s2p_tests": {}, "n2p_tests": {}, "fixed_tests": {}, "error_msg": "timeout"}, False, 0),
    ({"valid": True, "f2p_tests": {"single": {}}, "p2p_tests": {}, "s2p_tests": {}, "n2p_tests": {}, "fixed_tests": {}, "error_msg": ""}, True, 1),
    ({"valid": True, "f2p_tests": {}, "p2p_tests": {}, "s2p_tests": {f"s{i}": {} for i in range(3)}, "n2p_tests": {}, "fixed_tests": {}, "error_msg": ""}, True, 0),
    ({"valid": True, "f2p_tests": {}, "p2p_tests": {}, "s2p_tests": {}, "n2p_tests": {f"n{i}": {} for i in range(7)}, "fixed_tests": {}, "error_msg": ""}, True, 0),
    ({"valid": True, "f2p_tests": {}, "p2p_tests": {}, "s2p_tests": {}, "n2p_tests": {}, "fixed_tests": {f"f{i}": {} for i in range(4)}, "error_msg": ""}, True, 0),
]

_REPORT_PARTIAL_KEYS = [
    ({"valid": True}, True),
    ({"valid": False}, False),
    ({}, False),
    ({"valid": True, "f2p_tests": {}}, True),
    ({"valid": True, "extra_field": "ignored"}, True),
]


class TestMainReportParsing:

    @pytest.fixture(autouse=True)
    def _patch_all(self, monkeypatch, tmp_dir):
        self._tmp = tmp_dir
        monkeypatch.setattr(
            "aurora.tools.phase2_docker_build._ensure_harness_importable", lambda: None,
        )
        monkeypatch.setattr(
            "aurora.tools.phase2_docker_build._check_docker", lambda: None,
        )
        monkeypatch.setattr(
            "aurora.tools.phase2_docker_build._HARNESS_REPOS_ROOT",
            tmp_dir / "repos",
        )
        monkeypatch.setattr(
            "aurora.tools.phase2_docker_build.check_instance_registry",
            MagicMock(return_value=True),
        )
        monkeypatch.setattr(
            "aurora.tools.phase2_docker_build._import_all_repo_modules",
            MagicMock(),
        )
        monkeypatch.setattr(
            "aurora.tools.phase2_docker_build._translate_phase1_jsonl",
            MagicMock(return_value=1),
        )

    def _run_with_report(self, monkeypatch, report_content, *, write_report=True, as_string=False):
        jsonl = self._tmp / "phase1.jsonl"
        jsonl.write_text(json.dumps({"number": 1}) + "\n")
        out_dir = str(self._tmp / "out")
        inst = _make_mock_instance("inst1")
        mock_cli = MagicMock()
        mock_cli.instances = [inst]
        mock_bd = MagicMock()
        mock_bd.CliArgs.return_value = mock_cli
        monkeypatch.setattr("importlib.import_module", lambda name: mock_bd)
        workdir = Path(out_dir) / "phase2_workdir"
        idir = workdir / "encode" / "starlette" / "instances" / "workdir_inst1"
        idir.mkdir(parents=True, exist_ok=True)
        if write_report:
            rp = idir / "report.json"
            if as_string:
                rp.write_text(report_content)
            else:
                with open(rp, "w") as f:
                    json.dump(report_content, f)
        return main(str(jsonl), out_dir, "encode", "starlette", "python")

    @pytest.mark.parametrize("data,expected_valid,expected_f2p", _REPORT_VALID_DATA)
    def test_valid_report(self, monkeypatch, data, expected_valid, expected_f2p):
        result = self._run_with_report(monkeypatch, data)
        r = result["results"][0]
        assert r["valid"] == expected_valid
        assert r.get("f2p", []) == expected_f2p

    @pytest.mark.parametrize("bad_content", _REPORT_INVALID_DATA)
    def test_invalid_report_content(self, monkeypatch, bad_content):
        result = self._run_with_report(monkeypatch, bad_content, as_string=True)
        r = result["results"][0]
        assert r["valid"] is False

    def test_report_missing(self, monkeypatch):
        result = self._run_with_report(monkeypatch, {}, write_report=False)
        r = result["results"][0]
        assert r["valid"] is False
        assert "error" in r

    @pytest.mark.parametrize("data,expected_valid,f2p_count", _REPORT_VARIATIONS)
    def test_report_variations(self, monkeypatch, data, expected_valid, f2p_count):
        result = self._run_with_report(monkeypatch, data)
        r = result["results"][0]
        assert r["valid"] == expected_valid
        assert len(r.get("f2p", [])) == f2p_count

    @pytest.mark.parametrize("data,expected_valid", _REPORT_PARTIAL_KEYS)
    def test_partial_keys(self, monkeypatch, data, expected_valid):
        result = self._run_with_report(monkeypatch, data)
        r = result["results"][0]
        assert r["valid"] == expected_valid

    def test_multiple_instances_mixed(self, monkeypatch):
        jsonl = self._tmp / "phase1.jsonl"
        jsonl.write_text(json.dumps({"number": 1}) + "\n")
        out_dir = str(self._tmp / "out")
        inst1 = _make_mock_instance("i1", valid=True)
        inst2 = _make_mock_instance("i2", valid=False)
        inst3 = _make_mock_instance("i3", valid=True)
        mock_cli = MagicMock()
        mock_cli.instances = [inst1, inst2, inst3]
        mock_bd = MagicMock()
        mock_bd.CliArgs.return_value = mock_cli
        monkeypatch.setattr("importlib.import_module", lambda name: mock_bd)
        workdir = Path(out_dir) / "phase2_workdir"
        for inst in [inst1, inst2, inst3]:
            idir = workdir / "encode" / "starlette" / "instances" / f"workdir_{inst.pr.id}"
            idir.mkdir(parents=True, exist_ok=True)
            with open(idir / "report.json", "w") as f:
                json.dump(inst._report_data, f)
        result = main(str(jsonl), out_dir, "encode", "starlette", "python")
        assert result["resolved_count"] == 2
        assert result["instance_count"] == 3

    @pytest.mark.parametrize("count", [1, 2, 5, 10])
    def test_n_instances(self, monkeypatch, count):
        jsonl = self._tmp / "phase1.jsonl"
        jsonl.write_text(json.dumps({"number": 1}) + "\n")
        out_dir = str(self._tmp / "out")
        instances = [_make_mock_instance(f"i{i}", valid=True) for i in range(count)]
        mock_cli = MagicMock()
        mock_cli.instances = instances
        mock_bd = MagicMock()
        mock_bd.CliArgs.return_value = mock_cli
        monkeypatch.setattr("importlib.import_module", lambda name: mock_bd)
        workdir = Path(out_dir) / "phase2_workdir"
        for inst in instances:
            idir = workdir / "encode" / "starlette" / "instances" / f"workdir_{inst.pr.id}"
            idir.mkdir(parents=True, exist_ok=True)
            with open(idir / "report.json", "w") as f:
                json.dump(inst._report_data, f)
        result = main(str(jsonl), out_dir, "encode", "starlette", "python")
        assert result["instance_count"] == count
        assert result["resolved_count"] == count

    @pytest.mark.parametrize("err_msg", [
        "", "exit code 1", "timeout", "OOM killed", "network error",
        "permission denied", "disk full", "segfault", "build failed",
        "test compile error", "dependency not found",
        "image pull failed", "container crashed", "apt install failed",
        "pip install failed", "npm install failed", "cmake error",
        "linker error", "missing header", "core dumped",
    ])
    def test_report_error_messages(self, monkeypatch, err_msg):
        data = {
            "valid": False, "f2p_tests": {}, "p2p_tests": {},
            "s2p_tests": {}, "n2p_tests": {}, "fixed_tests": {},
            "error_msg": err_msg,
        }
        result = self._run_with_report(monkeypatch, data)
        r = result["results"][0]
        assert r["error_msg"] == err_msg

    @pytest.mark.parametrize("n_f2p,n_p2p,n_s2p,n_n2p,n_fixed", [
        (0, 0, 0, 0, 0),
        (1, 0, 0, 0, 0),
        (0, 1, 0, 0, 0),
        (0, 0, 1, 0, 0),
        (0, 0, 0, 1, 0),
        (0, 0, 0, 0, 1),
        (3, 2, 1, 0, 1),
        (10, 5, 3, 2, 4),
        (1, 1, 1, 1, 1),
        (5, 0, 0, 0, 0),
        (0, 5, 0, 0, 0),
        (0, 0, 5, 0, 0),
        (0, 0, 0, 5, 0),
        (0, 0, 0, 0, 5),
        (20, 10, 5, 3, 8),
        (2, 3, 4, 5, 6),
    ])
    def test_report_test_counts(self, monkeypatch, n_f2p, n_p2p, n_s2p, n_n2p, n_fixed):
        data = {
            "valid": True,
            "f2p_tests": {f"f{i}": {} for i in range(n_f2p)},
            "p2p_tests": {f"p{i}": {} for i in range(n_p2p)},
            "s2p_tests": {f"s{i}": {} for i in range(n_s2p)},
            "n2p_tests": {f"n{i}": {} for i in range(n_n2p)},
            "fixed_tests": {f"x{i}": {} for i in range(n_fixed)},
            "error_msg": "",
        }
        result = self._run_with_report(monkeypatch, data)
        r = result["results"][0]
        assert len(r["f2p"]) == n_f2p
        assert len(r["p2p"]) == n_p2p
        assert len(r["s2p"]) == n_s2p
        assert len(r["n2p"]) == n_n2p
        assert len(r["fixed_tests"]) == n_fixed


_INTERVAL_SINGLE_PR_VALUES = [
    (2813, "starlette_3055_to_2813", 2813, 3055, True),
    (3055, "starlette_3055_to_2813", 2813, 3055, True),
    (2814, "starlette_3055_to_2813", 2813, 3055, True),
    (3054, "starlette_3055_to_2813", 2813, 3055, True),
    (2900, "starlette_3055_to_2813", 2813, 3055, True),
    (2812, "starlette_3055_to_2813", 2813, 3055, False),
    (3056, "starlette_3055_to_2813", 2813, 3055, False),
    (0, "starlette_3055_to_2813", 2813, 3055, False),
    (10000, "starlette_3055_to_2813", 2813, 3055, False),
    (1, "starlette_3055_to_2813", 2813, 3055, False),
]


class TestFindIntervalSingleRange:

    @pytest.mark.parametrize("pr,name,lo,hi,expected_found", _INTERVAL_SINGLE_PR_VALUES)
    def test_single_range_check(self, pr, name, lo, hi, expected_found):
        ranges = [(name, lo, hi)]
        result = _find_interval_for_pr(pr, ranges)
        if expected_found:
            assert result == name
        else:
            assert result == ""


_REGISTRY_HYPHEN_REPOS = [
    ("encode", "http-x", "python", ["http_x.py"], True),
    ("encode", "http-x", "python", ["http_x_500_to_100.py"], True),
    ("encode", "my-repo-name", "python", ["my_repo_name.py"], True),
    ("encode", "some-lib", "python", ["some_lib.py"], True),
    ("encode", "a-b-c", "python", ["a_b_c.py"], True),
    ("encode", "http-x", "python", ["httpx.py"], False),
    ("encode", "http-x", "python", ["http-x.py"], False),
]


class TestCheckInstanceRegistryHyphenRepos:

    @pytest.fixture(autouse=True)
    def _patch_harness(self, monkeypatch, tmp_dir):
        self._repos_root = tmp_dir / "repos"
        monkeypatch.setattr(
            "aurora.tools.phase2_docker_build._HARNESS_REPOS_ROOT", self._repos_root
        )
        monkeypatch.setattr(
            "aurora.tools.phase2_docker_build._ensure_harness_importable", lambda: None
        )

    @pytest.mark.parametrize("org,repo,lang,files,expected", _REGISTRY_HYPHEN_REPOS)
    def test_hyphen_repos(self, org, repo, lang, files, expected):
        d = self._repos_root / lang / org
        d.mkdir(parents=True, exist_ok=True)
        for f in files:
            (d / f).touch()
        assert check_instance_registry(org, repo, lang) == expected


_INTERVAL_SORTED_CHECK = [
    (
        ["starlette_500_to_300.py", "starlette_200_to_100.py", "starlette_1000_to_600.py"],
        [(100, 200), (300, 500), (600, 1000)],
    ),
    (
        ["starlette_3055_to_2813.py", "starlette_100_to_50.py"],
        [(50, 100), (2813, 3055)],
    ),
    (
        ["starlette_10_to_1.py", "starlette_30_to_20.py", "starlette_50_to_40.py"],
        [(1, 10), (20, 30), (40, 50)],
    ),
]


class TestBuildNumberIntervalMapSorting:

    @pytest.fixture(autouse=True)
    def _patch(self, monkeypatch, tmp_dir):
        self._repos_root = tmp_dir / "repos"
        monkeypatch.setattr(
            "aurora.tools.phase2_docker_build._HARNESS_REPOS_ROOT", self._repos_root
        )

    @pytest.mark.parametrize("files,expected_ranges", _INTERVAL_SORTED_CHECK)
    def test_sorted_by_min(self, files, expected_ranges):
        d = self._repos_root / "python" / "encode"
        d.mkdir(parents=True, exist_ok=True)
        for f in files:
            (d / f).touch()
        result = _build_number_interval_map("encode", "starlette", "python")
        actual_ranges = [(lo, hi) for _, lo, hi in result]
        assert actual_ranges == expected_ranges


_MAIN_ORG_REPO_LANG = [
    ("encode", "starlette", "python"),
    ("pallets", "flask", "python"),
    ("django", "django", "python"),
    ("encode", "httpx", "python"),
    ("psf", "requests", "python"),
    ("numpy", "numpy", "python"),
    ("scipy", "scipy", "python"),
    ("matplotlib", "matplotlib", "python"),
]


class TestMainOrgRepoLang:

    @pytest.fixture(autouse=True)
    def _patch_all(self, monkeypatch, tmp_dir):
        self._tmp = tmp_dir
        monkeypatch.setattr(
            "aurora.tools.phase2_docker_build._ensure_harness_importable", lambda: None,
        )
        monkeypatch.setattr(
            "aurora.tools.phase2_docker_build._check_docker", lambda: None,
        )
        monkeypatch.setattr(
            "aurora.tools.phase2_docker_build._HARNESS_REPOS_ROOT",
            tmp_dir / "repos",
        )
        monkeypatch.setattr(
            "aurora.tools.phase2_docker_build.check_instance_registry",
            MagicMock(return_value=True),
        )
        monkeypatch.setattr(
            "aurora.tools.phase2_docker_build._import_all_repo_modules",
            MagicMock(),
        )
        monkeypatch.setattr(
            "aurora.tools.phase2_docker_build._translate_phase1_jsonl",
            MagicMock(return_value=1),
        )

    @pytest.mark.parametrize("org,repo,lang", _MAIN_ORG_REPO_LANG)
    def test_various_orgs(self, monkeypatch, org, repo, lang):
        jsonl = self._tmp / "phase1.jsonl"
        jsonl.write_text(json.dumps({"number": 1}) + "\n")
        out_dir = str(self._tmp / "out")
        inst = _make_mock_instance("i1")
        inst.pr.org = org
        inst.pr.repo = repo
        mock_cli = MagicMock()
        mock_cli.instances = [inst]
        mock_bd = MagicMock()
        mock_bd.CliArgs.return_value = mock_cli
        monkeypatch.setattr("importlib.import_module", lambda name: mock_bd)
        workdir = Path(out_dir) / "phase2_workdir"
        idir = workdir / org / repo / "instances" / "workdir_i1"
        idir.mkdir(parents=True, exist_ok=True)
        with open(idir / "report.json", "w") as f:
            json.dump(inst._report_data, f)
        result = main(str(jsonl), out_dir, org, repo, lang)
        assert result["instance_count"] == 1
