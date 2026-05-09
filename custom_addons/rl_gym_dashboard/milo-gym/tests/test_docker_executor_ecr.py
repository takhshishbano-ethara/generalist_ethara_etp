from __future__ import annotations

import pytest

from src.core.config import ECRConfig
from src.core.schemas import TaskSpec
from src.rollout.docker_executor import DockerExecutor


class TestDockerExecutorECR:
    @pytest.fixture
    def ecr_config(self) -> ECRConfig:
        return ECRConfig(
            enabled=True,
            account_id="426628337772",
            region="ap-south-1",
            repository="rfp-coding-q1-tag",
            patch_path="/home/fix.patch",
        )

    @pytest.fixture
    def ecr_task(self) -> TaskSpec:
        return TaskSpec(
            task_id="numpy__numpy-12345",
            instance_id="numpy__numpy-12345",
            repo="numpy/numpy",
            language="python",
            base_commit="abc123",
            problem_statement="Fix bug",
            test_patch="...",
            fix_patch="...",
        )

    @pytest.fixture
    def legacy_task(self) -> TaskSpec:
        return TaskSpec(
            task_id="test_task",
            repo="test/repo",
            language="python",
            base_commit="abc",
            problem_statement="Fix",
            test_patch="...",
            fix_patch="...",
            docker_image="my-image:v1",
            evaluation_script="echo 'F2P: 1/1'",
        )

    def test_resolve_image_from_instance_id(self, ecr_config, ecr_task):
        executor = DockerExecutor.__new__(DockerExecutor)
        executor._ecr_config = ecr_config
        executor._ecr_auth = None
        executor._ecr_images = None
        result = executor._resolve_image(ecr_task)
        assert "numpy_m_numpy:pr-12345" in result
        assert "426628337772.dkr.ecr.ap-south-1.amazonaws.com" in result

    def test_resolve_image_direct_docker_image_wins(self, ecr_config, legacy_task):
        executor = DockerExecutor.__new__(DockerExecutor)
        executor._ecr_config = ecr_config
        executor._ecr_auth = None
        executor._ecr_images = None
        result = executor._resolve_image(legacy_task)
        assert result == "my-image:v1"

    def test_resolve_image_no_ecr_no_image_raises(self, ecr_task):
        executor = DockerExecutor.__new__(DockerExecutor)
        executor._ecr_config = None
        with pytest.raises(ValueError, match="no docker_image"):
            executor._resolve_image(ecr_task)

    def test_resolve_eval_script_ecr_default(self, ecr_config, ecr_task):
        executor = DockerExecutor.__new__(DockerExecutor)
        executor._ecr_config = ecr_config
        result = executor._resolve_eval_script(ecr_task)
        assert "/home/fix-run.sh" in result

    def test_resolve_eval_script_task_override(self, ecr_config):
        task = TaskSpec(
            task_id="t1",
            instance_id="org__repo-1",
            repo="org/repo",
            language="python",
            base_commit="x",
            problem_statement="Fix",
            test_patch="...",
            fix_patch="...",
            evaluation_script="custom_eval.sh",
        )
        executor = DockerExecutor.__new__(DockerExecutor)
        executor._ecr_config = ecr_config
        assert executor._resolve_eval_script(task) == "custom_eval.sh"

    def test_resolve_patch_path_ecr(self, ecr_config):
        executor = DockerExecutor.__new__(DockerExecutor)
        executor._ecr_config = ecr_config
        assert executor._resolve_patch_path() == "/home/fix.patch"

    def test_resolve_patch_path_legacy(self):
        executor = DockerExecutor.__new__(DockerExecutor)
        executor._ecr_config = None
        assert executor._resolve_patch_path() == "/tmp/patch.diff"

    def test_backward_compat_no_ecr(self, legacy_task):
        executor = DockerExecutor.__new__(DockerExecutor)
        executor._ecr_config = None
        assert executor._resolve_image(legacy_task) == "my-image:v1"
        assert executor._resolve_eval_script(legacy_task) == "echo 'F2P: 1/1'"
        assert executor._resolve_patch_path() == "/tmp/patch.diff"
