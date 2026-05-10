from __future__ import annotations

from src.core.schemas import Trajectory, Turn
from src.rollout.patch_utils import (
    estimate_token_count,
    extract_patch,
    is_compact_filtered,
    is_empty_patch,
    patch_file_list,
    patch_stats,
)

SAMPLE_DIFF = (
    "--- a/src/utils.py\n+++ b/src/utils.py\n@@ -1,3 +1,3 @@\n-x = 1\n+x = 2\n"
)


def test_extract_patch_from_diff_fence():
    content = f"Here is the fix:\n```diff\n{SAMPLE_DIFF}```\nDone."
    assert extract_patch(content) == SAMPLE_DIFF.strip()


def test_extract_patch_from_submit_tag():
    content = f"<submit>\n{SAMPLE_DIFF}</submit>"
    assert extract_patch(content) == SAMPLE_DIFF.strip()


def test_extract_patch_from_raw_diff():
    content = f"Applying fix:\n{SAMPLE_DIFF}"
    result = extract_patch(content)
    assert "--- a/src/utils.py" in result
    assert "+x = 2" in result


def test_extract_patch_priority_submit_over_fence():
    submit_diff = "--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old\n+new\n"
    fence_diff = "--- a/b.py\n+++ b/b.py\n@@ -1 +1 @@\n-foo\n+bar\n"
    content = f"<submit>{submit_diff}</submit>\n```diff\n{fence_diff}```"
    result = extract_patch(content)
    assert "a.py" in result
    assert "b.py" not in result


def test_extract_patch_empty():
    assert extract_patch("No patch here, just text.") == ""
    assert extract_patch("```python\nprint('hi')\n```") == ""


def test_is_empty_patch_true():
    assert is_empty_patch("") is True
    assert is_empty_patch("   ") is True
    assert is_empty_patch("\n\t") is True


def test_is_empty_patch_false():
    assert is_empty_patch(SAMPLE_DIFF) is False


def test_is_compact_filtered_max_turns():
    traj = Trajectory(
        task_id="t1",
        turns=[Turn(role="user", content="x", token_count=5)] * 10,
        patch=SAMPLE_DIFF,
        reward=0.0,
        episode_length=10,
    )
    assert is_compact_filtered(traj, max_turns=10) is True


def test_is_compact_filtered_empty_patch():
    traj = Trajectory(
        task_id="t1",
        turns=[Turn(role="user", content="x", token_count=5)],
        patch="",
        reward=0.0,
        episode_length=1,
    )
    assert is_compact_filtered(traj, max_turns=50) is True


def test_is_compact_filtered_normal():
    traj = Trajectory(
        task_id="t1",
        turns=[Turn(role="user", content="x", token_count=5)],
        patch=SAMPLE_DIFF,
        reward=1.0,
        episode_length=1,
    )
    assert is_compact_filtered(traj, max_turns=50) is False


def test_estimate_token_count():
    text = "a" * 350
    count = estimate_token_count(text)
    assert 80 <= count <= 120


def test_patch_file_list():
    multi_file_patch = (
        "--- a/src/foo.py\n+++ b/src/foo.py\n@@ -1 +1 @@\n-a\n+b\n"
        "--- a/src/bar.py\n+++ b/src/bar.py\n@@ -1 +1 @@\n-c\n+d\n"
    )
    files = patch_file_list(multi_file_patch)
    assert files == ["src/bar.py", "src/foo.py"]


def test_patch_stats():
    stats = patch_stats(SAMPLE_DIFF)
    assert stats["lines_added"] == 1
    assert stats["lines_removed"] == 1
    assert stats["files_changed"] == 1
    assert stats["hunks"] == 1
