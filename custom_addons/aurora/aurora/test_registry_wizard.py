import re
from pathlib import Path

import pytest

from aurora.models.registry_wizard import (
    _to_class_name,
    _TEMPLATE,
    _HARNESS_REPOS_ROOT,
)


@pytest.mark.parametrize("input_str,expected", [
    ("starlette", "Starlette"),
    ("my-repo", "MyRepo"),
    ("my_repo", "MyRepo"),
    ("MyRepo", "Myrepo"),
    ("a-b-c", "ABC"),
    ("123repo", "123Repo"),
    ("repo-1.0", "Repo10"),
    ("", ""),
    ("a", "A"),
    ("A", "A"),
    ("ALL-CAPS", "AllCaps"),
    ("ALL_CAPS", "AllCaps"),
    ("mixed-Case-With-Numbers123", "MixedCaseWithNumbers123"),
    ("hello world", "HelloWorld"),
    ("hello--world", "HelloWorld"),
    ("hello__world", "HelloWorld"),
    ("hello-_world", "HelloWorld"),
    ("x", "X"),
    ("X", "X"),
    ("test-repo-name", "TestRepoName"),
    ("flask", "Flask"),
    ("scikit-learn", "ScikitLearn"),
    ("vue.js", "VueJs"),
    ("node.js", "NodeJs"),
    ("react-native", "ReactNative"),
    ("type-script", "TypeScript"),
    ("my--double--dash", "MyDoubleDash"),
    ("trailing-", "Trailing"),
    ("-leading", "Leading"),
    ("_leading", "Leading"),
    ("UPPER", "Upper"),
    ("lower", "Lower"),
    ("CamelCase", "Camelcase"),
    ("snake_case_name", "SnakeCaseName"),
    ("kebab-case-name", "KebabCaseName"),
    ("mix_of-both", "MixOfBoth"),
    ("123", "123"),
    ("1-2-3", "123"),
    ("repo!@#name", "RepoName"),
    ("a.b.c", "ABC"),
    ("special$chars%here", "SpecialCharsHere"),
])
def test_to_class_name(input_str, expected):
    assert _to_class_name(input_str) == expected


@pytest.mark.parametrize("input_str", [
    "starlette", "my-repo", "flask", "django", "react",
])
def test_to_class_name_no_special_chars(input_str):
    result = _to_class_name(input_str)
    assert re.match(r'^[a-zA-Z0-9]*$', result)


@pytest.mark.parametrize("input_str", [
    "starlette", "my-repo", "flask",
])
def test_to_class_name_starts_with_upper_or_digit(input_str):
    result = _to_class_name(input_str)
    if result:
        assert result[0].isupper() or result[0].isdigit()


def test_template_is_non_empty():
    assert isinstance(_TEMPLATE, str)
    assert len(_TEMPLATE) > 100


def test_template_has_class_name_placeholder():
    assert "{class_name}" in _TEMPLATE


def test_template_has_org_placeholder():
    assert "{org}" in _TEMPLATE


def test_template_has_repo_placeholder():
    assert "{repo}" in _TEMPLATE


def test_template_contains_image_base_class():
    assert "ImageBase(Image)" in _TEMPLATE


def test_template_contains_image_default_class():
    assert "ImageDefault(Image)" in _TEMPLATE


def test_template_contains_instance_register():
    assert "Instance.register" in _TEMPLATE


def test_harness_repos_root_is_path():
    assert isinstance(_HARNESS_REPOS_ROOT, Path)


def test_harness_repos_root_ends_with_repos():
    assert _HARNESS_REPOS_ROOT.name == "repos"


def test_harness_repos_root_parent_is_harness():
    assert _HARNESS_REPOS_ROOT.parent.name == "harness"


@pytest.mark.parametrize("cls,org,repo", [
    ("Starlette", "encode", "starlette"),
    ("Flask", "pallets", "flask"),
    ("Django", "django", "django"),
    ("ReactNative", "facebook", "react-native"),
    ("ScikitLearn", "scikit-learn", "scikit-learn"),
    ("Vue", "vuejs", "vue"),
    ("Express", "expressjs", "express"),
    ("FastApi", "tiangolo", "fastapi"),
])
def test_template_formatting(cls, org, repo):
    result = _TEMPLATE.format(class_name=cls, org=org, repo=repo)
    assert f"class {cls}ImageBase(Image):" in result
    assert f"class {cls}ImageDefault(Image):" in result
    assert f"class {cls}(Instance):" in result
    assert f'@Instance.register("{org}", "{repo}")' in result


@pytest.mark.parametrize("cls,org,repo", [
    ("Starlette", "encode", "starlette"),
    ("MyRepo", "org", "repo"),
])
def test_template_format_contains_imports(cls, org, repo):
    result = _TEMPLATE.format(class_name=cls, org=org, repo=repo)
    assert "from multi_swe_bench.harness.image import" in result
    assert "from multi_swe_bench.harness.instance import" in result
    assert "from multi_swe_bench.harness.pull_request import" in result


@pytest.mark.parametrize("cls,org,repo", [
    ("Starlette", "encode", "starlette"),
])
def test_template_format_contains_methods(cls, org, repo):
    result = _TEMPLATE.format(class_name=cls, org=org, repo=repo)
    for method in ["dependency", "image_tag", "workdir", "files", "dockerfile",
                   "run", "test_patch_run", "fix_patch_run", "parse_log"]:
        assert f"def {method}" in result


from unittest.mock import MagicMock, patch
from odoo.exceptions import UserError


def _make_wizard(org, repo, lang, content, tmp_dir):
    wiz = MagicMock()
    wiz.org = org
    wiz.repo = repo
    wiz.lang = lang
    wiz.registry_content = content
    wiz.pipeline_id = MagicMock()
    wiz.ensure_one = MagicMock()
    return wiz


def test_action_save_empty_content_raises():
    from aurora.models.registry_wizard import AuroraRegistryWizard
    wiz = AuroraRegistryWizard()
    wiz.registry_content = ""
    wiz.org = "test"
    wiz.repo = "repo"
    wiz.lang = "python"
    with pytest.raises(UserError, match="empty"):
        wiz.action_save_registry()


def test_action_save_whitespace_only_raises():
    from aurora.models.registry_wizard import AuroraRegistryWizard
    wiz = AuroraRegistryWizard()
    wiz.registry_content = "   \n  "
    wiz.org = "test"
    wiz.repo = "repo"
    wiz.lang = "python"
    with pytest.raises(UserError, match="empty"):
        wiz.action_save_registry()


def test_action_save_none_content_raises():
    from aurora.models.registry_wizard import AuroraRegistryWizard
    wiz = AuroraRegistryWizard()
    wiz.registry_content = None
    wiz.org = "test"
    wiz.repo = "repo"
    wiz.lang = "python"
    with pytest.raises(UserError, match="empty"):
        wiz.action_save_registry()


@pytest.mark.parametrize("org,repo,lang", [
    ("pallets", "flask", "python"),
    ("facebook", "react", "javascript"),
    ("microsoft", "typescript", "typescript"),
    ("golang", "go", "golang"),
    ("rust-lang", "rust", "rust"),
])
def test_action_save_creates_files(tmp_dir, org, repo, lang):
    from aurora.models.registry_wizard import AuroraRegistryWizard
    wiz = AuroraRegistryWizard()
    wiz.registry_content = "# test content"
    wiz.org = org
    wiz.repo = repo
    wiz.lang = lang
    wiz.pipeline_id = MagicMock()

    with patch("aurora.models.registry_wizard._HARNESS_REPOS_ROOT", tmp_dir):
        result = wiz.action_save_registry()

    repo_safe = repo.replace("-", "_").lower()
    target = tmp_dir / lang / org / f"{repo_safe}.py"
    assert target.exists()
    assert target.read_text() == "# test content"
    assert result["type"] == "ir.actions.client"


@pytest.mark.parametrize("org,repo,lang", [
    ("encode", "starlette", "python"),
    ("django", "django", "python"),
])
def test_action_save_creates_init_files(tmp_dir, org, repo, lang):
    from aurora.models.registry_wizard import AuroraRegistryWizard
    wiz = AuroraRegistryWizard()
    wiz.registry_content = "# content"
    wiz.org = org
    wiz.repo = repo
    wiz.lang = lang
    wiz.pipeline_id = MagicMock()

    with patch("aurora.models.registry_wizard._HARNESS_REPOS_ROOT", tmp_dir):
        wiz.action_save_registry()

    org_init = tmp_dir / lang / org / "__init__.py"
    lang_init = tmp_dir / lang / "__init__.py"
    assert org_init.exists()
    assert lang_init.exists()
    repo_safe = repo.replace("-", "_").lower()
    assert f"from .{repo_safe} import *" in org_init.read_text()
    assert f"from .{org} import *" in lang_init.read_text()


def test_action_save_appends_to_existing_init(tmp_dir):
    from aurora.models.registry_wizard import AuroraRegistryWizard
    lang, org = "python", "myorg"
    org_dir = tmp_dir / lang / org
    org_dir.mkdir(parents=True)
    (org_dir / "__init__.py").write_text("from .existing import *\n")
    (tmp_dir / lang / "__init__.py").write_text("from .other_org import *\n")

    wiz = AuroraRegistryWizard()
    wiz.registry_content = "# new"
    wiz.org = org
    wiz.repo = "new-repo"
    wiz.lang = lang
    wiz.pipeline_id = MagicMock()

    with patch("aurora.models.registry_wizard._HARNESS_REPOS_ROOT", tmp_dir):
        wiz.action_save_registry()

    org_init_text = (org_dir / "__init__.py").read_text()
    assert "from .existing import *" in org_init_text
    assert "from .new_repo import *" in org_init_text


def test_action_save_no_duplicate_init_entry(tmp_dir):
    from aurora.models.registry_wizard import AuroraRegistryWizard
    lang, org, repo = "python", "myorg", "myrepo"
    org_dir = tmp_dir / lang / org
    org_dir.mkdir(parents=True)
    (org_dir / "__init__.py").write_text("from .myrepo import *\n")
    (tmp_dir / lang / "__init__.py").write_text("from .myorg import *\n")

    wiz = AuroraRegistryWizard()
    wiz.registry_content = "# content"
    wiz.org = org
    wiz.repo = repo
    wiz.lang = lang
    wiz.pipeline_id = MagicMock()

    with patch("aurora.models.registry_wizard._HARNESS_REPOS_ROOT", tmp_dir):
        wiz.action_save_registry()

    org_init_text = (org_dir / "__init__.py").read_text()
    assert org_init_text.count("from .myrepo import *") == 1


def test_action_save_notification_message(tmp_dir):
    from aurora.models.registry_wizard import AuroraRegistryWizard
    wiz = AuroraRegistryWizard()
    wiz.registry_content = "# code"
    wiz.org = "org"
    wiz.repo = "repo"
    wiz.lang = "python"
    wiz.pipeline_id = MagicMock()

    with patch("aurora.models.registry_wizard._HARNESS_REPOS_ROOT", tmp_dir):
        result = wiz.action_save_registry()

    assert result["params"]["type"] == "success"
    assert "python/org/repo.py" in result["params"]["message"]


def test_action_save_sets_phase2_flag(tmp_dir):
    from aurora.models.registry_wizard import AuroraRegistryWizard
    wiz = AuroraRegistryWizard()
    wiz.registry_content = "# code"
    wiz.org = "org"
    wiz.repo = "repo"
    wiz.lang = "python"
    wiz.pipeline_id = MagicMock()

    with patch("aurora.models.registry_wizard._HARNESS_REPOS_ROOT", tmp_dir):
        wiz.action_save_registry()

    wiz.pipeline_id.write.assert_called_once_with({"phase2_has_registry": True})
