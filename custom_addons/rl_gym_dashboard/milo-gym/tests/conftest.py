from __future__ import annotations

import pytest

from src.core.config import ECRConfig
from src.core.schemas import TaskSpec, Trajectory, Turn


@pytest.fixture
def sample_task_spec() -> TaskSpec:
    return TaskSpec(
        task_id="test__repo__pr_1",
        repo="test-org/test-repo",
        language="python",
        base_commit="abc123",
        problem_statement="Fix the bug in utils.py",
        test_patch=(
            "--- a/tests/test_utils.py\n+++ b/tests/test_utils.py\n"
            "@@ -1,3 +1,5 @@\n+def test_new():\n+    assert True\n"
        ),
        fix_patch=(
            "--- a/src/utils.py\n+++ b/src/utils.py\n"
            "@@ -1,3 +1,3 @@\n-x = 1\n+x = 2\n"
        ),
        docker_image="test-repo:abc123",
        evaluation_script="#!/bin/bash\necho 'F2P: 1/1'\necho 'P2P: 5/5'",
        difficulty="easy",
        difficulty_score=0.8,
    )


@pytest.fixture
def ecr_config() -> ECRConfig:
    return ECRConfig(
        enabled=True,
        account_id="426628337772",
        region="ap-south-1",
        repository="rfp-coding-q1-tag",
    )


@pytest.fixture
def ecr_task_spec() -> TaskSpec:
    return TaskSpec(
        task_id="numpy__numpy-12345",
        instance_id="numpy__numpy-12345",
        repo="numpy/numpy",
        language="python",
        base_commit="abc123",
        problem_statement="Fix the bug",
        test_patch="...",
        fix_patch="--- a/f.py\n+++ b/f.py\n@@ -1 +1 @@\n-x\n+y\n",
    )


@pytest.fixture
def sample_trajectory(sample_task_spec: TaskSpec) -> Trajectory:
    return Trajectory(
        task_id=sample_task_spec.task_id,
        turns=[
            Turn(role="user", content="Fix the bug", token_count=10),
            Turn(
                role="assistant",
                content=(
                    "```diff\n--- a/src/utils.py\n+++ b/src/utils.py\n"
                    "@@ -1,3 +1,3 @@\n-x = 1\n+x = 2\n```"
                ),
                token_count=50,
            ),
        ],
        patch="--- a/src/utils.py\n+++ b/src/utils.py\n@@ -1,3 +1,3 @@\n-x = 1\n+x = 2\n",
        reward=1.0,
        episode_length=2,
    )


@pytest.fixture
def failed_trajectory(sample_task_spec: TaskSpec) -> Trajectory:
    return Trajectory(
        task_id=sample_task_spec.task_id,
        turns=[
            Turn(role="user", content="Fix the bug", token_count=10),
            Turn(role="assistant", content="I don't know", token_count=5),
        ],
        patch="",
        reward=0.0,
        episode_length=2,
        hit_max_turns=True,
    )
