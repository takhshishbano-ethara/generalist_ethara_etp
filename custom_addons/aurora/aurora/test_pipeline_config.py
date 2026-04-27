import pytest

from aurora.models.pipeline_config import (
    GITHUB_LANG_MAP,
    LANG_DETECTION_MODE,
    LANGUAGE_SELECTION,
    AuroraSettings,
)


@pytest.mark.parametrize("code,label", LANGUAGE_SELECTION)
def test_language_selection_entry(code, label):
    assert isinstance(code, str)
    assert isinstance(label, str)
    assert code == label


def test_language_selection_count():
    assert len(LANGUAGE_SELECTION) == 15


@pytest.mark.parametrize("lang", [
    "python", "java", "javascript", "typescript", "cpp",
    "c", "csharp", "golang", "rust", "ruby",
    "php", "kotlin", "scala", "swift", "html",
])
def test_language_present(lang):
    codes = [c for c, _ in LANGUAGE_SELECTION]
    assert lang in codes


def test_language_selection_is_list():
    assert isinstance(LANGUAGE_SELECTION, list)


def test_language_selection_all_tuples():
    for item in LANGUAGE_SELECTION:
        assert isinstance(item, tuple)
        assert len(item) == 2


def test_language_selection_no_duplicates():
    codes = [c for c, _ in LANGUAGE_SELECTION]
    assert len(codes) == len(set(codes))


@pytest.mark.parametrize("code,label", LANG_DETECTION_MODE)
def test_lang_detection_mode_entry(code, label):
    assert isinstance(code, str)
    assert isinstance(label, str)


def test_lang_detection_mode_count():
    assert len(LANG_DETECTION_MODE) == 2


def test_lang_detection_manual_present():
    codes = [c for c, _ in LANG_DETECTION_MODE]
    assert "manual" in codes


def test_lang_detection_automatic_present():
    codes = [c for c, _ in LANG_DETECTION_MODE]
    assert "automatic" in codes


def test_lang_detection_mode_labels():
    labels = {c: l for c, l in LANG_DETECTION_MODE}
    assert labels["manual"] == "Manual"
    assert labels["automatic"] == "Automatic"


@pytest.mark.parametrize("github_name,harness_name", [
    ("Python", "python"),
    ("Java", "java"),
    ("JavaScript", "javascript"),
    ("TypeScript", "typescript"),
    ("C++", "cpp"),
    ("C", "c"),
    ("C#", "csharp"),
    ("Go", "golang"),
    ("Rust", "rust"),
    ("Ruby", "ruby"),
    ("PHP", "php"),
    ("Kotlin", "kotlin"),
    ("Scala", "scala"),
    ("Swift", "swift"),
    ("HTML", "html"),
])
def test_github_lang_map(github_name, harness_name):
    assert GITHUB_LANG_MAP[github_name] == harness_name


def test_github_lang_map_count():
    assert len(GITHUB_LANG_MAP) == 15


def test_github_lang_map_is_dict():
    assert isinstance(GITHUB_LANG_MAP, dict)


def test_github_lang_map_keys_title_case():
    for key in GITHUB_LANG_MAP:
        assert key[0].isupper() or key in ("C++", "C#")


def test_github_lang_map_values_lowercase():
    for val in GITHUB_LANG_MAP.values():
        assert val == val.lower()


def test_github_lang_map_values_match_language_selection():
    lang_codes = {c for c, _ in LANGUAGE_SELECTION}
    for val in GITHUB_LANG_MAP.values():
        assert val in lang_codes


def test_github_lang_map_all_languages_mapped():
    lang_codes = {c for c, _ in LANGUAGE_SELECTION}
    mapped = set(GITHUB_LANG_MAP.values())
    assert mapped == lang_codes


@pytest.mark.parametrize("github_name", list(GITHUB_LANG_MAP.keys()))
def test_github_lang_map_key_is_string(github_name):
    assert isinstance(github_name, str)


@pytest.mark.parametrize("harness_name", list(GITHUB_LANG_MAP.values()))
def test_github_lang_map_value_is_string(harness_name):
    assert isinstance(harness_name, str)


def test_encrypted_field_map_has_s3_access_key():
    assert "aurora_s3_access_key" in AuroraSettings._ENCRYPTED_FIELD_MAP


def test_encrypted_field_map_has_s3_secret_key():
    assert "aurora_s3_secret_key" in AuroraSettings._ENCRYPTED_FIELD_MAP


def test_encrypted_field_map_count():
    assert len(AuroraSettings._ENCRYPTED_FIELD_MAP) == 2


def test_encrypted_field_map_param_keys():
    assert AuroraSettings._ENCRYPTED_FIELD_MAP["aurora_s3_access_key"] == "aurora.s3_access_key"
    assert AuroraSettings._ENCRYPTED_FIELD_MAP["aurora_s3_secret_key"] == "aurora.s3_secret_key"


def test_settings_inherits_res_config():
    assert AuroraSettings._inherit == "res.config.settings"


@pytest.mark.parametrize("field_name", [
    "aurora_output_dir",
    "aurora_cache_dir",
    "aurora_delay_on_error",
    "aurora_retry_attempts",
    "aurora_max_tags",
    "aurora_window_days",
    "aurora_lang_detection_mode",
    "aurora_lang",
    "aurora_s3_bucket",
    "aurora_s3_access_key",
    "aurora_s3_secret_key",
    "aurora_s3_region",
    "aurora_s3_folder",
    "aurora_max_active_tasks",
    "aurora_k8s_namespace",
    "aurora_k8s_image",
    "aurora_k8s_service_account",
    "aurora_k8s_node_pool",
    "aurora_k8s_kueue_queue",
    "aurora_k8s_efs_pvc",
    "aurora_k8s_cpu_request",
    "aurora_k8s_memory_request",
    "aurora_k8s_memory_limit",
    "aurora_k8s_deadline_seconds",
    "aurora_k8s_worker_script",
    "aurora_k8s_odoo_conf",
    "aurora_k8s_configmap",
    "aurora_k8s_secret",
])
def test_settings_field_exists(field_name):
    assert hasattr(AuroraSettings, field_name)


@pytest.mark.parametrize("field_name,expected_type", [
    ("aurora_output_dir", "Char"),
    ("aurora_cache_dir", "Char"),
    ("aurora_delay_on_error", "Integer"),
    ("aurora_retry_attempts", "Integer"),
    ("aurora_max_tags", "Integer"),
    ("aurora_window_days", "Integer"),
    ("aurora_lang_detection_mode", "Selection"),
    ("aurora_lang", "Selection"),
    ("aurora_s3_bucket", "Char"),
    ("aurora_s3_access_key", "Char"),
    ("aurora_s3_secret_key", "Char"),
    ("aurora_s3_region", "Char"),
    ("aurora_s3_folder", "Char"),
    ("aurora_max_active_tasks", "Integer"),
    ("aurora_k8s_namespace", "Char"),
    ("aurora_k8s_image", "Char"),
    ("aurora_k8s_deadline_seconds", "Integer"),
])
def test_settings_field_type(field_name, expected_type):
    field = getattr(AuroraSettings, field_name)
    assert type(field).__name__ == f"_{expected_type}"
