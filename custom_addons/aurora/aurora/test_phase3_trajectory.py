import pytest

from aurora.tools.phase3_trajectory import main as phase3_main
from aurora.tools.util import AuroraPipelineError


_REPORTS = [
    "/tmp/phase2_report.jsonl",
    "/data/output/report.json",
    "s3://bucket/key/report.jsonl",
    "./local/report.jsonl",
    "/very/long/" + "sub/" * 50 + "report.jsonl",
    "",
    "/tmp/report with spaces.jsonl",
    "/tmp/résumé.jsonl",
]

_OUTPUT_DIRS = [
    "/tmp/phase3_output",
    "/data/trajectories",
    "s3://bucket/phase3",
    "./relative/output",
    "/opt/aurora/out",
    "",
    "/tmp/out with spaces",
]

_ORGS = ["test-org", "django", "facebook", "a", "My.Org", "org-123", ""]
_REPOS = ["test-repo", "django", "react", "b", "My_Repo", "repo.js", ""]
_LANGS = ["python", "javascript", "typescript", "java", "go", "rust", "c", ""]


@pytest.mark.parametrize("report", _REPORTS)
def test_raises_for_any_report(report):
    with pytest.raises(AuroraPipelineError, match="not yet implemented"):
        phase3_main(report, "/tmp/out", "org", "repo", "python")


@pytest.mark.parametrize("output_dir", _OUTPUT_DIRS)
def test_raises_for_any_output_dir(output_dir):
    with pytest.raises(AuroraPipelineError, match="not yet implemented"):
        phase3_main("/tmp/report.jsonl", output_dir, "org", "repo", "python")


@pytest.mark.parametrize("org", _ORGS)
def test_raises_for_any_org(org):
    with pytest.raises(AuroraPipelineError, match="not yet implemented"):
        phase3_main("/tmp/report.jsonl", "/tmp/out", org, "repo", "python")


@pytest.mark.parametrize("repo", _REPOS)
def test_raises_for_any_repo(repo):
    with pytest.raises(AuroraPipelineError, match="not yet implemented"):
        phase3_main("/tmp/report.jsonl", "/tmp/out", "org", repo, "python")


@pytest.mark.parametrize("lang", _LANGS)
def test_raises_for_any_lang(lang):
    with pytest.raises(AuroraPipelineError, match="not yet implemented"):
        phase3_main("/tmp/report.jsonl", "/tmp/out", "org", "repo", lang)


@pytest.mark.parametrize("use_callback", [True, False])
def test_raises_with_and_without_callback(use_callback):
    cb = (lambda msg: None) if use_callback else None
    with pytest.raises(AuroraPipelineError, match="not yet implemented"):
        phase3_main("/tmp/report.jsonl", "/tmp/out", "org", "repo", "python", log_callback=cb)


@pytest.mark.parametrize("report,output_dir", [
    ("/tmp/r.jsonl", "/tmp/out"),
    ("s3://b/r.jsonl", "s3://b/out"),
    ("./r.jsonl", "./out"),
    ("/r", "/o"),
    ("", ""),
])
def test_raises_for_report_output_combos(report, output_dir):
    with pytest.raises(AuroraPipelineError):
        phase3_main(report, output_dir, "org", "repo", "python")


@pytest.mark.parametrize("org,repo", [
    ("a", "b"),
    ("django", "django"),
    ("facebook", "react"),
    ("", ""),
    ("org-1", "repo.js"),
])
def test_raises_for_org_repo_combos(org, repo):
    with pytest.raises(AuroraPipelineError):
        phase3_main("/tmp/r.jsonl", "/tmp/out", org, repo, "python")


def test_error_is_aurora_pipeline_error():
    with pytest.raises(AuroraPipelineError) as exc_info:
        phase3_main("/tmp/r.jsonl", "/tmp/out", "org", "repo", "python")
    assert isinstance(exc_info.value, AuroraPipelineError)


def test_error_message_mentions_phase3():
    with pytest.raises(AuroraPipelineError) as exc_info:
        phase3_main("/tmp/r.jsonl", "/tmp/out", "org", "repo", "python")
    assert "Phase 3" in str(exc_info.value)


def test_error_message_mentions_trajectory():
    with pytest.raises(AuroraPipelineError) as exc_info:
        phase3_main("/tmp/r.jsonl", "/tmp/out", "org", "repo", "python")
    assert "Trajectory" in str(exc_info.value)


def test_error_is_exception_subclass():
    with pytest.raises(Exception):
        phase3_main("/tmp/r.jsonl", "/tmp/out", "org", "repo", "python")


def test_error_not_base_exception_directly():
    with pytest.raises(AuroraPipelineError) as exc_info:
        phase3_main("/tmp/r.jsonl", "/tmp/out", "org", "repo", "python")
    assert type(exc_info.value) is AuroraPipelineError
