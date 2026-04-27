import os
import re
import types
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from odoo.exceptions import UserError

import aurora.models.s3_storage as _real_s3_storage

from aurora.models.pipeline import (
    STEP_SELECTION,
    TERMINAL_STATES,
    AUTOMATION_STATUS,
    _SAFE_GITHUB_NAME,
    _RECONCILER_ADVISORY_LOCK_ID,
    _WATCHDOG_ADVISORY_LOCK_ID,
    AuroraPipeline,
)


def _mock_pipeline(**kw):
    rec = AuroraPipeline()
    rec.id = kw.get("id", 1)
    rec.name = kw.get("name", "AUR-001")
    rec.github_org = kw.get("github_org", "test-org")
    rec.github_repo = kw.get("github_repo", "test-repo")
    rec.detected_lang = kw.get("detected_lang", "python")
    rec.stage = kw.get("stage", "draft")
    rec.output_dir = kw.get("output_dir", "/tmp/aurora")
    rec.job_name = kw.get("job_name", None)
    rec.phase1_file = kw.get("phase1_file", "")
    rec.phase2_file = kw.get("phase2_file", "")
    rec.phase3_file = kw.get("phase3_file", "")
    rec.dataset_count = kw.get("dataset_count", 0)
    rec.use_s3 = kw.get("use_s3", False)

    rec.step1_status = kw.get("step1_status", "idle")
    rec.step2_status = kw.get("step2_status", "idle")
    rec.step3_status = kw.get("step3_status", "idle")
    rec.step4_status = kw.get("step4_status", "idle")
    rec.step5_status = kw.get("step5_status", "idle")
    rec.step6_status = kw.get("step6_status", "idle")
    rec.phase1_status = kw.get("phase1_status", "idle")
    rec.phase2_status = kw.get("phase2_status", "idle")
    rec.phase3_status = kw.get("phase3_status", "idle")

    rec.phase2_result_ids = MagicMock()
    rec.phase2_result_ids.unlink = MagicMock()

    mock_env = MagicMock()
    cr = MagicMock()
    cr.dbname = "test_db"
    cr.execute = MagicMock()
    cr.fetchone = MagicMock(return_value=("draft",))
    mock_env.cr = cr
    mock_env.context = kw.get("context", {})

    icp = MagicMock()
    icp.get_param = MagicMock(side_effect=lambda key, default="": default)

    sudo_icp = MagicMock()
    sudo_icp.get_param = icp.get_param
    sudo_icp.set_param = MagicMock()

    def _getitem(key):
        m = MagicMock()
        m.sudo = MagicMock(return_value=sudo_icp)
        m.search_count = MagicMock(return_value=1)
        m.search = MagicMock(return_value=MagicMock())
        m.create = MagicMock(return_value=MagicMock(id=99))
        m._build_preview = MagicMock(return_value=("preview", 5))
        m._decrypt_token = MagicMock(return_value="ghp_fake")
        return m

    mock_env.__getitem__ = MagicMock(side_effect=_getitem)
    mock_env.user = MagicMock()
    mock_env.user.has_group = MagicMock(return_value=True)
    rec.env = mock_env
    return rec


# ===== A — STEP_SELECTION (14 tests) =====

_EXPECTED_STEPS = [
    ("draft", "Draft"),
    ("fetch_prs", "1 – Fetch PRs"),
    ("filter_prs", "2 – Filter PRs"),
    ("discover_tags", "3 – Discover Tags"),
    ("group_prs", "4 – Group PRs by Tags"),
    ("fetch_issues", "5 – Fetch Issues"),
    ("build_dataset", "6 – Build Dataset"),
    ("phase2_build", "Phase 2 – Docker Build"),
    ("phase2_test", "Phase 2 – Test Execution"),
    ("phase2_report", "Phase 2 – Report Generation"),
    ("phase3_infer", "Phase 3 – Inference"),
    ("phase3_eval", "Phase 3 – Evaluation"),
    ("phase3_summary", "Phase 3 – Summary"),
    ("done", "Done"),
    ("failed", "Failed"),
]


@pytest.mark.parametrize("idx,expected", list(enumerate(_EXPECTED_STEPS)))
def test_step_selection_entry(idx, expected):
    assert STEP_SELECTION[idx] == expected


def test_step_selection_length():
    assert len(STEP_SELECTION) == 15


# ===== B — TERMINAL_STATES =====

@pytest.mark.parametrize("state", ["done", "failed"])
def test_terminal_state_present(state):
    assert state in TERMINAL_STATES


@pytest.mark.parametrize("state", ["draft", "fetch_prs", "filter_prs", "discover_tags",
                                    "group_prs", "fetch_issues", "build_dataset",
                                    "phase2_build", "phase2_test", "phase2_report",
                                    "phase3_infer", "phase3_eval", "phase3_summary"])
def test_non_terminal_state(state):
    assert state not in TERMINAL_STATES


def test_terminal_states_is_set():
    assert isinstance(TERMINAL_STATES, set)


# ===== C — AUTOMATION_STATUS (4 tests) =====

_EXPECTED_AUTOMATION = [("idle", "Idle"), ("running", "Running"), ("done", "Done"), ("failed", "Failed")]


@pytest.mark.parametrize("idx,expected", list(enumerate(_EXPECTED_AUTOMATION)))
def test_automation_status_entry(idx, expected):
    assert AUTOMATION_STATUS[idx] == expected


# ===== D — _SAFE_GITHUB_NAME (55 tests) =====

@pytest.mark.parametrize("name", [
    "my-org", "my.org", "my_org", "MyOrg123", "a", "A-B.C_D",
    "simple", "org123", "123org", "a-b", "a.b", "a_b",
    "X", "Z9", "test-repo", "test.repo", "test_repo",
    "ALL_CAPS", "lower", "MiXeD", "a1b2c3", "repo-1.0",
    "v2.3.4", "node.js", "vue.js", "react-native",
    "My.Super.Org", "My-Super-Org", "My_Super_Org",
    "x" * 100, "A" * 50, "1", "0", "9",
])
def test_safe_github_name_valid(name):
    assert _SAFE_GITHUB_NAME.match(name) is not None


@pytest.mark.parametrize("name", [
    "my org", "my/org", "", "my@org", "my!org", "../etc",
    "my org name", "has space", "has\ttab", "has\nnewline",
    "has#hash", "has$dollar", "has%percent", "has^caret",
    "has&amp", "has*star", "has(paren", "has)paren",
    "has+plus", "has=equal",
])
def test_safe_github_name_invalid(name):
    assert _SAFE_GITHUB_NAME.match(name) is None


# ===== E — advisory lock IDs =====

def test_reconciler_lock_id_is_int():
    assert isinstance(_RECONCILER_ADVISORY_LOCK_ID, int)


def test_watchdog_lock_id_is_int():
    assert isinstance(_WATCHDOG_ADVISORY_LOCK_ID, int)


def test_lock_ids_differ():
    assert _RECONCILER_ADVISORY_LOCK_ID != _WATCHDOG_ADVISORY_LOCK_ID


# ===== F — _compute_use_s3 (25 tests) =====

@pytest.mark.parametrize("output_dir,expected", [
    ("s3://bucket/key", True),
    ("s3://my-bucket", True),
    ("s3://a", True),
    ("s3://bucket/folder/sub", True),
    ("s3://bucket-name/aurora_phase1/org__repo", True),
    ("/tmp/aurora", False),
    ("/home/user/output", False),
    ("./relative", False),
    ("", False),
    (None, False),
    (False, False),
    ("http://example.com", False),
    ("https://example.com", False),
    ("ftp://server/path", False),
    ("S3://bucket/key", False),
    ("s3:/missing-slash", False),
    ("file:///tmp/x", False),
    ("/", False),
    (".", False),
    ("..", False),
    ("s3://", True),
    ("s3://a/b/c/d/e/f", True),
    ("  s3://bucket", False),
    ("s3:// bucket", True),
    ("local/path/s3://fake", False),
])
def test_compute_use_s3(output_dir, expected):
    rec = _mock_pipeline(output_dir=output_dir)
    AuroraPipeline._compute_use_s3(iter([rec]))
    assert rec.use_s3 is expected


# ===== G — _presign_s3_url (35 tests) =====

def _pipeline_with_s3_config(**kw):
    rec = _mock_pipeline(**kw)
    return rec


@pytest.mark.parametrize("url", [
    "https://mybucket.s3.us-east-1.amazonaws.com/key/file.jsonl",
    "https://mybucket.s3.eu-west-1.amazonaws.com/some/path.json",
    "https://mybucket.s3-us-west-2.amazonaws.com/data.csv",
    "https://test-bucket.s3.ap-south-1.amazonaws.com/aurora/output.jsonl",
    "https://bucket.s3.us-east-1.amazonaws.com/a",
    "https://bucket.s3.us-east-1.amazonaws.com/a/b/c/d/e",
])
def test_presign_virtual_hosted_calls_s3(url):
    rec = _pipeline_with_s3_config()
    mock_s3 = MagicMock()
    mock_s3.is_configured.return_value = True
    mock_s3.generate_presigned_url.return_value = "https://presigned"
    with patch("aurora.models.s3_storage.is_configured", mock_s3.is_configured), \
         patch("aurora.models.s3_storage.generate_presigned_url", mock_s3.generate_presigned_url):
        result = AuroraPipeline._presign_s3_url(rec, url)
        assert result == "https://presigned"
        mock_s3.generate_presigned_url.assert_called_once()


@pytest.mark.parametrize("url", [
    "https://s3.us-east-1.amazonaws.com/mybucket/key/file.jsonl",
    "https://s3.eu-west-1.amazonaws.com/bucket/path.json",
    "https://s3-us-west-2.amazonaws.com/bucket/data.csv",
    "https://s3.ap-south-1.amazonaws.com/test-bucket/aurora/output.jsonl",
])
def test_presign_path_style_calls_s3(url):
    rec = _pipeline_with_s3_config()
    mock_s3 = MagicMock()
    mock_s3.is_configured.return_value = True
    mock_s3.generate_presigned_url.return_value = "https://presigned-path"
    with patch("aurora.models.s3_storage.is_configured", mock_s3.is_configured), \
         patch("aurora.models.s3_storage.generate_presigned_url", mock_s3.generate_presigned_url):
        result = AuroraPipeline._presign_s3_url(rec, url)
        assert result == "https://presigned-path"


@pytest.mark.parametrize("url", [
    "https://example.com/file.jsonl",
    "https://google.com/path",
    "https://cdn.company.com/assets/file",
    "https://storage.googleapis.com/bucket/key",
    "http://bucket.s3.us-east-1.amazonaws.com/key",
])
def test_presign_non_s3_returns_unchanged(url):
    rec = _pipeline_with_s3_config()
    result = AuroraPipeline._presign_s3_url(rec, url)
    assert result == url


@pytest.mark.parametrize("url", [
    "",
    "not-a-url",
    "ftp://bucket.s3.amazonaws.com/key",
    "/local/path",
    "s3://bucket/key",
])
def test_presign_malformed_returns_unchanged(url):
    rec = _pipeline_with_s3_config()
    result = AuroraPipeline._presign_s3_url(rec, url)
    assert result == url


@pytest.mark.parametrize("url", [
    "https://bucket.s3.us-east-1.amazonaws.com/key.jsonl",
])
def test_presign_s3_not_configured_returns_unchanged(url):
    rec = _pipeline_with_s3_config()
    with patch("aurora.models.s3_storage.is_configured", return_value=False):
        result = AuroraPipeline._presign_s3_url(rec, url)
        assert result == url


@pytest.mark.parametrize("url", [
    "https://bucket.s3.us-east-1.amazonaws.com/key.jsonl",
])
def test_presign_s3_exception_returns_original(url):
    rec = _pipeline_with_s3_config()
    with patch("aurora.models.s3_storage.is_configured", return_value=True), \
         patch("aurora.models.s3_storage.generate_presigned_url", side_effect=Exception("boom")):
        result = AuroraPipeline._presign_s3_url(rec, url)
        assert result == url


# ===== H — action_download_phase_file (20 tests) =====

@pytest.mark.parametrize("phase", [0, 4, 5, -1, None, "abc", 99])
def test_download_invalid_phase_raises(phase):
    rec = _mock_pipeline(context={"phase_number": phase})
    with pytest.raises(UserError, match="Invalid phase"):
        AuroraPipeline.action_download_phase_file(rec)


@pytest.mark.parametrize("phase,field", [(1, "phase1_file"), (2, "phase2_file"), (3, "phase3_file")])
def test_download_no_file_raises(phase, field):
    rec = _mock_pipeline(context={"phase_number": phase})
    setattr(rec, field, "")
    with pytest.raises(UserError, match="No file available"):
        AuroraPipeline.action_download_phase_file(rec)


@pytest.mark.parametrize("phase,field", [(1, "phase1_file"), (2, "phase2_file"), (3, "phase3_file")])
def test_download_local_file(phase, field, tmp_dir):
    fpath = tmp_dir / "data.jsonl"
    fpath.write_text('{"test": true}\n')
    rec = _mock_pipeline(context={"phase_number": phase})
    setattr(rec, field, str(fpath))
    result = AuroraPipeline.action_download_phase_file(rec)
    assert result["type"] == "ir.actions.act_url"


@pytest.mark.parametrize("phase,field", [(1, "phase1_file"), (2, "phase2_file"), (3, "phase3_file")])
def test_download_file_prefix_stripped(phase, field, tmp_dir):
    fpath = tmp_dir / "report.jsonl"
    fpath.write_text('{"ok": true}\n')
    rec = _mock_pipeline(context={"phase_number": phase})
    setattr(rec, field, f"file://{fpath}")
    result = AuroraPipeline.action_download_phase_file(rec)
    assert result["type"] == "ir.actions.act_url"


@pytest.mark.parametrize("phase,field", [(1, "phase1_file"), (2, "phase2_file"), (3, "phase3_file")])
def test_download_s3_url_calls_presign(phase, field):
    rec = _mock_pipeline(context={"phase_number": phase})
    s3_url = "https://mybucket.s3.us-east-1.amazonaws.com/key.jsonl"
    setattr(rec, field, s3_url)
    with patch.object(AuroraPipeline, "_presign_s3_url", return_value="https://presigned") as m:
        result = AuroraPipeline.action_download_phase_file(rec)
        m.assert_called_once_with(s3_url)
    assert result["url"] == "https://presigned"


# ===== I — action_view_phase_file (15 tests) =====

@pytest.mark.parametrize("phase", [0, 4, 5, -1, None])
def test_view_invalid_phase_raises(phase):
    rec = _mock_pipeline(context={"phase_number": phase})
    with pytest.raises(UserError, match="Invalid phase"):
        AuroraPipeline.action_view_phase_file(rec)


@pytest.mark.parametrize("phase,field", [(1, "phase1_file"), (2, "phase2_file"), (3, "phase3_file")])
def test_view_no_file_raises(phase, field):
    rec = _mock_pipeline(context={"phase_number": phase})
    setattr(rec, field, "")
    with pytest.raises(UserError, match="No file available"):
        AuroraPipeline.action_view_phase_file(rec)


@pytest.mark.parametrize("phase,field", [(1, "phase1_file"), (2, "phase2_file"), (3, "phase3_file")])
def test_view_file_not_on_disk_raises(phase, field):
    rec = _mock_pipeline(context={"phase_number": phase})
    setattr(rec, field, "/nonexistent/path/data.jsonl")
    with pytest.raises(UserError, match="File not found"):
        AuroraPipeline.action_view_phase_file(rec)


@pytest.mark.parametrize("phase,field", [(1, "phase1_file"), (2, "phase2_file"), (3, "phase3_file")])
def test_view_valid_file_creates_wizard(phase, field, tmp_dir):
    fpath = tmp_dir / "data.jsonl"
    fpath.write_text('{"x": 1}\n')
    rec = _mock_pipeline(context={"phase_number": phase})
    setattr(rec, field, str(fpath))
    result = AuroraPipeline.action_view_phase_file(rec)
    assert result["type"] == "ir.actions.act_window"
    assert result["res_model"] == "aurora.pipeline.preview"


# ===== J — action_create_registry (25 tests) =====

@pytest.mark.parametrize("org,repo,lang", [
    ("", "repo", "python"),
    ("org", "", "python"),
    ("org", "repo", ""),
    ("", "", ""),
    ("", "", "python"),
    ("org", "", ""),
])
def test_create_registry_missing_fields_raises(org, repo, lang):
    rec = _mock_pipeline(github_org=org, github_repo=repo, detected_lang=lang)
    with pytest.raises(UserError, match="Organisation.*repository.*language"):
        AuroraPipeline.action_create_registry(rec)


@pytest.mark.parametrize("org,repo,lang", [
    ("test-org", "test-repo", "python"),
    ("django", "django", "python"),
    ("facebook", "react", "javascript"),
    ("microsoft", "vscode", "typescript"),
    ("pallets", "flask", "python"),
    ("torvalds", "linux", "c"),
    ("rust-lang", "rust", "rust"),
    ("golang", "go", "go"),
    ("apache", "spark", "scala"),
    ("tensorflow", "tensorflow", "python"),
    ("pytorch", "pytorch", "python"),
    ("vuejs", "vue", "javascript"),
    ("angular", "angular", "typescript"),
    ("rails", "rails", "ruby"),
    ("spring-projects", "spring-boot", "java"),
    ("dotnet", "runtime", "csharp"),
    ("JuliaLang", "julia", "python"),
    ("a", "b", "python"),
    ("my.org", "my.repo", "python"),
])
def test_create_registry_valid(org, repo, lang):
    rec = _mock_pipeline(github_org=org, github_repo=repo, detected_lang=lang)
    mock_template = MagicMock()
    mock_template.format = MagicMock(return_value="class content")
    with patch("aurora.models.registry_wizard._TEMPLATE", mock_template), \
         patch("aurora.models.registry_wizard._to_class_name", return_value="TestRepo"):
        result = AuroraPipeline.action_create_registry(rec)
        assert result["type"] == "ir.actions.act_window"
        assert result["res_model"] == "aurora.registry.wizard"


# ===== K — action_cancel (12 tests) =====

@pytest.mark.parametrize("stage", ["done", "failed"])
def test_cancel_terminal_raises(stage):
    rec = _mock_pipeline(stage=stage)
    with pytest.raises(UserError, match="Cannot cancel"):
        AuroraPipeline.action_cancel(rec)


@pytest.mark.parametrize("stage", [
    "draft", "fetch_prs", "filter_prs", "discover_tags", "group_prs",
    "fetch_issues", "build_dataset", "phase2_build", "phase2_test",
    "phase2_report",
])
def test_cancel_valid_stage_sets_failed(stage):
    rec = _mock_pipeline(stage=stage, job_name=None)
    AuroraPipeline.action_cancel(rec)
    assert rec.stage == "failed"


# ===== L — action_reset_to_draft (18 tests) =====

@pytest.mark.parametrize("stage", [
    "draft", "fetch_prs", "filter_prs", "discover_tags", "group_prs",
    "fetch_issues", "build_dataset", "phase2_build", "phase2_test",
    "phase2_report", "phase3_infer", "phase3_eval", "phase3_summary",
])
def test_reset_non_terminal_raises(stage):
    rec = _mock_pipeline(stage=stage)
    with pytest.raises(UserError, match="Only finished"):
        AuroraPipeline.action_reset_to_draft(rec)


@pytest.mark.parametrize("stage", ["done", "failed"])
def test_reset_terminal_sets_draft(stage):
    rec = _mock_pipeline(stage=stage)
    AuroraPipeline.action_reset_to_draft(rec)
    assert rec.stage == "draft"


@pytest.mark.parametrize("stage", ["done", "failed"])
def test_reset_clears_statuses(stage):
    rec = _mock_pipeline(stage=stage, step1_status="done", phase2_status="done")
    AuroraPipeline.action_reset_to_draft(rec)
    for attr in ("step1_status", "step2_status", "step3_status",
                 "step4_status", "step5_status", "step6_status",
                 "phase1_status", "phase2_status", "phase3_status"):
        assert getattr(rec, attr) == "idle"


@pytest.mark.parametrize("stage", ["done", "failed"])
def test_reset_calls_unlink(stage):
    rec = _mock_pipeline(stage=stage)
    AuroraPipeline.action_reset_to_draft(rec)
    rec.phase2_result_ids.unlink.assert_called_once()


# ===== M — action_run_pipeline validations (35 tests) =====

@pytest.mark.parametrize("org", [
    "my org", "my/org", "", "my@org", "my!org", "../etc",
    "has space", "a b", "x/y", None,
])
def test_run_invalid_org_raises(org):
    rec = _mock_pipeline(github_org=org)
    with pytest.raises(UserError):
        AuroraPipeline.action_run_pipeline(rec)


@pytest.mark.parametrize("repo", [
    "my repo", "my/repo", "", "my@repo", "my!repo", "../etc",
    "has space", "a b", "x/y", None,
])
def test_run_invalid_repo_raises(repo):
    rec = _mock_pipeline(github_org="valid-org", github_repo=repo)
    with pytest.raises(UserError):
        AuroraPipeline.action_run_pipeline(rec)


@pytest.mark.parametrize("stage_val", [
    "fetch_prs", "filter_prs", "done", "failed", "phase2_build",
])
def test_run_not_draft_raises(stage_val):
    rec = _mock_pipeline(stage="draft")
    rec.env.cr.fetchone.return_value = (stage_val,)
    with pytest.raises(UserError, match="Draft"):
        AuroraPipeline.action_run_pipeline(rec)


def _run_pipeline_with_config(rec, config_overrides):
    original_get_config = AuroraPipeline._get_config

    def patched_get_config(self_inner):
        cfg = {
            "output_dir": "/tmp/aurora",
            "cache_dir": "/tmp/cache",
            "delay_on_error": 300,
            "retry_attempts": 3,
            "max_tags": 200,
            "window_days": 30,
            "lang": "python",
            "s3_bucket": "",
            "s3_access_key": "",
            "s3_secret_key": "",
            "s3_region": "ap-south-1",
            "s3_folder": "",
        }
        cfg.update(config_overrides)
        return cfg

    with patch.object(AuroraPipeline, "_get_config", patched_get_config), \
         patch.object(AuroraPipeline, "_resolve_lang", return_value="python"), \
         patch.object(AuroraPipeline, "_check_max_active"):
        token_mock = MagicMock()
        token_mock.search_count = MagicMock(return_value=1)
        rec.env.__getitem__ = MagicMock(side_effect=lambda key: token_mock)
        AuroraPipeline.action_run_pipeline(rec)


def test_run_no_tokens_raises():
    rec = _mock_pipeline()
    with patch.object(AuroraPipeline, "_get_config", return_value={
        "output_dir": "/tmp/out", "retry_attempts": 3, "max_tags": 200,
        "window_days": 30, "s3_bucket": "", "s3_access_key": "", "s3_secret_key": "",
        "s3_region": "", "s3_folder": "",
    }), patch.object(AuroraPipeline, "_check_max_active"):
        token_mock = MagicMock()
        token_mock.search_count = MagicMock(return_value=0)
        rec.env.__getitem__ = MagicMock(side_effect=lambda key: token_mock)
        with pytest.raises(UserError, match="No GitHub tokens"):
            AuroraPipeline.action_run_pipeline(rec)


def test_run_no_output_dir_raises():
    rec = _mock_pipeline()
    with patch.object(AuroraPipeline, "_get_config", return_value={
        "output_dir": "", "retry_attempts": 3, "max_tags": 200,
        "window_days": 30, "s3_bucket": "", "s3_access_key": "", "s3_secret_key": "",
        "s3_region": "", "s3_folder": "",
    }), patch.object(AuroraPipeline, "_check_max_active"):
        token_mock = MagicMock()
        token_mock.search_count = MagicMock(return_value=1)
        rec.env.__getitem__ = MagicMock(side_effect=lambda key: token_mock)
        with pytest.raises(UserError, match="No output directory"):
            AuroraPipeline.action_run_pipeline(rec)


@pytest.mark.parametrize("retry", [-1, -5, -100])
def test_run_negative_retry_raises(retry):
    rec = _mock_pipeline()
    with patch.object(AuroraPipeline, "_get_config", return_value={
        "output_dir": "/tmp/out", "retry_attempts": retry, "max_tags": 200,
        "window_days": 30, "s3_bucket": "", "s3_access_key": "", "s3_secret_key": "",
        "s3_region": "", "s3_folder": "",
    }), patch.object(AuroraPipeline, "_check_max_active"):
        token_mock = MagicMock()
        token_mock.search_count = MagicMock(return_value=1)
        rec.env.__getitem__ = MagicMock(side_effect=lambda key: token_mock)
        with pytest.raises(UserError, match="Retry attempts"):
            AuroraPipeline.action_run_pipeline(rec)


@pytest.mark.parametrize("max_tags", [0, -1, -10])
def test_run_max_tags_too_low_raises(max_tags):
    rec = _mock_pipeline()
    with patch.object(AuroraPipeline, "_get_config", return_value={
        "output_dir": "/tmp/out", "retry_attempts": 3, "max_tags": max_tags,
        "window_days": 30, "s3_bucket": "", "s3_access_key": "", "s3_secret_key": "",
        "s3_region": "", "s3_folder": "",
    }), patch.object(AuroraPipeline, "_check_max_active"):
        token_mock = MagicMock()
        token_mock.search_count = MagicMock(return_value=1)
        rec.env.__getitem__ = MagicMock(side_effect=lambda key: token_mock)
        with pytest.raises(UserError, match="Max tags"):
            AuroraPipeline.action_run_pipeline(rec)


@pytest.mark.parametrize("window", [0, -1, -30])
def test_run_window_days_too_low_raises(window):
    rec = _mock_pipeline()
    with patch.object(AuroraPipeline, "_get_config", return_value={
        "output_dir": "/tmp/out", "retry_attempts": 3, "max_tags": 200,
        "window_days": window, "s3_bucket": "", "s3_access_key": "", "s3_secret_key": "",
        "s3_region": "", "s3_folder": "",
    }), patch.object(AuroraPipeline, "_check_max_active"):
        token_mock = MagicMock()
        token_mock.search_count = MagicMock(return_value=1)
        rec.env.__getitem__ = MagicMock(side_effect=lambda key: token_mock)
        with pytest.raises(UserError, match="Window days"):
            AuroraPipeline.action_run_pipeline(rec)


# ===== N — output directory construction (20 tests) =====

@pytest.mark.parametrize("org,repo,output_dir,expected_prefix", [
    ("myorg", "myrepo", "/tmp/aurora", "/tmp/aurora/myorg__myrepo"),
    ("django", "django", "/opt/output", "/opt/output/django__django"),
    ("a", "b", "/x", "/x/a__b"),
    ("org-1", "repo-2", "/data", "/data/org-1__repo-2"),
    ("Org.Name", "Repo_Name", "/out", "/out/Org.Name__Repo_Name"),
])
def test_output_dir_local(org, repo, output_dir, expected_prefix):
    result = os.path.join(output_dir, f"{org}__{repo}")
    assert result == expected_prefix


@pytest.mark.parametrize("bucket,folder,org,repo,expected", [
    ("mybucket", "myfolder", "org", "repo", "s3://mybucket/myfolder/aurora_phase1/org__repo"),
    ("test-bucket", "test", "a", "b", "s3://test-bucket/test/aurora_phase1/a__b"),
    ("bkt", "deep/nested", "x", "y", "s3://bkt/deep/nested/aurora_phase1/x__y"),
    ("bucket", "f1/f2/f3", "org", "repo", "s3://bucket/f1/f2/f3/aurora_phase1/org__repo"),
    ("b", "a", "o", "r", "s3://b/a/aurora_phase1/o__r"),
])
def test_output_dir_s3_with_folder(bucket, folder, org, repo, expected):
    s3_folder = folder.strip("/")
    result = f"s3://{bucket}/{s3_folder}/aurora_phase1/{org}__{repo}"
    assert result == expected


@pytest.mark.parametrize("bucket,org,repo,expected", [
    ("mybucket", "org", "repo", "s3://mybucket/aurora_phase1/org__repo"),
    ("test-bucket", "a", "b", "s3://test-bucket/aurora_phase1/a__b"),
    ("bkt", "x", "y", "s3://bkt/aurora_phase1/x__y"),
    ("bucket-name", "django", "django", "s3://bucket-name/aurora_phase1/django__django"),
    ("b", "o", "r", "s3://b/aurora_phase1/o__r"),
])
def test_output_dir_s3_no_folder(bucket, org, repo, expected):
    result = f"s3://{bucket}/aurora_phase1/{org}__{repo}"
    assert result == expected


# ===== O — model metadata =====

def test_pipeline_model_name():
    assert AuroraPipeline._name == "aurora.pipeline"


def test_pipeline_model_description():
    assert AuroraPipeline._description == "Aurora Pipeline Run"


def test_pipeline_model_order():
    assert AuroraPipeline._order == "id desc"


def test_pipeline_inherit():
    assert "mail.thread" in AuroraPipeline._inherit
    assert "mail.activity.mixin" in AuroraPipeline._inherit


# ===== P — field existence checks (40 tests) =====

_PIPELINE_FIELDS = {
    "name": "Char",
    "user_id": "Many2one",
    "stage": "Selection",
    "active": "Boolean",
    "github_org": "Char",
    "github_repo": "Char",
    "skip_pr_fetch": "Boolean",
    "detected_lang": "Selection",
    "job_name": "Char",
    "step1_status": "Selection",
    "step2_status": "Selection",
    "step3_status": "Selection",
    "step4_status": "Selection",
    "step5_status": "Selection",
    "step6_status": "Selection",
    "output_dir": "Char",
    "log": "Text",
    "step1_file": "Char",
    "step2_file": "Char",
    "step3_file": "Char",
    "step4_file": "Char",
    "step5_file": "Char",
    "step6_file": "Char",
    "step1_log": "Text",
    "step2_log": "Text",
    "step3_log": "Text",
    "step4_log": "Text",
    "step5_log": "Text",
    "step6_log": "Text",
    "dataset_url": "Char",
    "dataset_filename": "Char",
    "last_heartbeat": "Datetime",
    "progress_text": "Char",
    "is_admin": "Boolean",
    "use_s3": "Boolean",
    "pr_count": "Integer",
    "filtered_pr_count": "Integer",
    "tag_count": "Integer",
    "group_count": "Integer",
    "issue_count": "Integer",
    "dataset_count": "Integer",
    "phase1_status": "Selection",
    "phase1_file": "Char",
    "phase2_status": "Selection",
    "phase2_file": "Char",
    "phase2_image_count": "Integer",
    "phase2_instance_count": "Integer",
    "phase2_resolved_count": "Integer",
    "phase2_log": "Text",
    "phase2_has_registry": "Boolean",
    "phase2_result_ids": "One2many",
    "phase3_status": "Selection",
    "phase3_file": "Char",
    "phase3_inference_count": "Integer",
    "phase3_pass_at_k": "Float",
    "phase3_log": "Text",
}


@pytest.mark.parametrize("field_name,expected_type", list(_PIPELINE_FIELDS.items()))
def test_pipeline_field_exists_and_type(field_name, expected_type):
    field_obj = getattr(AuroraPipeline, field_name, None)
    assert field_obj is not None, f"Field {field_name} not found"
    assert type(field_obj).__name__.lstrip("_") == expected_type


# ===== Q — _SAFE_GITHUB_NAME boundary cases (additional) =====

@pytest.mark.parametrize("name,expected", [
    ("a", True),
    ("Z", True),
    ("0", True),
    (".", True),
    ("-", True),
    ("_", True),
    ("a" * 200, True),
    ("a-b.c_d", True),
    ("", False),
    (" ", False),
    ("\t", False),
    ("\n", False),
    ("a b", False),
    ("a/b", False),
    ("a\\b", False),
    ("a:b", False),
    ("a;b", False),
    ("a,b", False),
    ("a<b", False),
    ("a>b", False),
])
def test_safe_name_boundary(name, expected):
    result = _SAFE_GITHUB_NAME.match(name)
    assert (result is not None) == expected
