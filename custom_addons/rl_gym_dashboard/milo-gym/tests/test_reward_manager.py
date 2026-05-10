from __future__ import annotations

import pytest
torch = pytest.importorskip("torch")
from unittest.mock import AsyncMock, MagicMock, patch

from src.training.reward_manager import MiloRewardManager, milo_compute_score
from src.rollout.docker_executor import DockerResult


class TestMiloComputeScore:
    def test_with_docker_result_success(self):
        result = DockerResult(f2p_passed=3, f2p_total=3, p2p_passed=5, p2p_total=5)
        score = milo_compute_score(
            data_source="milo",
            solution_str="some patch",
            ground_truth="",
            extra_info={"docker_result": result},
        )
        assert score == 1.0

    def test_with_docker_result_failure(self):
        result = DockerResult(f2p_passed=0, f2p_total=3, p2p_passed=5, p2p_total=5)
        score = milo_compute_score(
            data_source="milo",
            solution_str="some patch",
            ground_truth="",
            extra_info={"docker_result": result},
        )
        assert score == 0.0

    def test_no_docker_result(self):
        score = milo_compute_score(
            data_source="milo",
            solution_str="some patch",
            ground_truth="",
            extra_info={},
        )
        assert score == 0.0

    def test_with_dict_result(self):
        score = milo_compute_score(
            data_source="milo",
            solution_str="patch",
            ground_truth="",
            extra_info={"docker_result": {"f2p_pass": True, "p2p_pass": True}},
        )
        assert score == 1.0


class TestMiloRewardManager:
    @pytest.fixture
    def mock_tokenizer(self):
        return MagicMock()

    @pytest.fixture
    def mock_executor(self):
        executor = MagicMock()
        executor.run_batch = AsyncMock(return_value=[])
        return executor

    def test_empty_patches_filtered(self, mock_tokenizer, mock_executor):
        manager = MiloRewardManager(
            tokenizer=mock_tokenizer,
            executor=mock_executor,
            compact_filtering=True,
        )
        data = {
            "responses": ["", "   ", ""],
            "task_ids": ["t1", "t2", "t3"],
            "hit_max_turns": [False, False, False],
            "timed_out": [False, False, False],
        }
        rewards = manager(data)
        assert torch.all(rewards == 0.0)

    def test_build_mask_hit_max_turns(self, mock_tokenizer, mock_executor):
        manager = MiloRewardManager(
            tokenizer=mock_tokenizer,
            executor=mock_executor,
            compact_filtering=True,
        )
        patches = ["some patch content", "another patch"]
        data = {
            "hit_max_turns": [True, False],
            "timed_out": [False, False],
        }
        mask = manager._build_mask(data, patches)
        assert mask[0].item() == 0.0
        assert mask[1].item() == 1.0


class TestPRMIntegration:
    @pytest.fixture
    def mock_tokenizer(self):
        return MagicMock()

    @pytest.fixture
    def manager_with_prm(self, mock_tokenizer):
        from src.core.config import PRMConfig
        from src.core.schemas import Trajectory, Turn

        manager = MiloRewardManager(
            tokenizer=mock_tokenizer,
            executor=None,
            compact_filtering=False,
        )
        prm_config = PRMConfig(
            enabled=True,
            mode="llm_judge",
            judge_endpoint="http://test:8001/v1/chat/completions",
            judge_votes=1,
            shaping_alpha=0.1,
            gtpo_gamma=1.0,
            outcome_gate=False,
            gate_mode="add_on_success",
        )
        manager.configure_prm(prm_config)
        return manager

    def test_configure_prm_enables_scorer(self, manager_with_prm):
        from src.prm.scorer import LLMJudgeScorer
        assert manager_with_prm._prm_scorer is not None
        assert isinstance(manager_with_prm._prm_scorer, LLMJudgeScorer)
        assert manager_with_prm._shaper is not None

    def test_configure_prm_disable(self, mock_tokenizer):
        from src.core.config import PRMConfig
        manager = MiloRewardManager(tokenizer=mock_tokenizer, executor=None)
        manager.configure_prm(PRMConfig(enabled=False))
        assert manager._prm_scorer is None
        assert manager._shaper is None

    def test_prm_scoring_populates_trajectory_fields(self, manager_with_prm):
        from src.core.schemas import Trajectory, Turn

        turns = [
            Turn(role="user", content="fix the bug"),
            Turn(role="assistant", content="reading file..."),
            Turn(role="tool", content="file contents here"),
            Turn(role="assistant", content="applying fix"),
        ]
        traj = Trajectory(
            task_id="test_task",
            turns=turns,
            raw_response="<submit>--- a/foo.py\n+++ b/foo.py\n@@ -1 +1 @@\n-old\n+new\n</submit>",
            reward=0.0,
        )

        mock_score_turn = AsyncMock(side_effect=[0.4, 0.8])
        manager_with_prm._prm_scorer.score_turn = mock_score_turn

        data = {
            "responses": [traj.raw_response],
            "task_ids": ["test_task"],
            "hit_max_turns": [False],
            "timed_out": [False],
            "trajectories": [traj],
            "task_descriptions": ["Fix the bug in foo.py"],
        }
        rewards = manager_with_prm(data)

        assert len(traj.step_rewards) == 4
        assert traj.step_rewards[0] == 0.0
        assert traj.step_rewards[2] == 0.0
        assert abs(traj.step_rewards[1] - 0.04) < 1e-9
        assert abs(traj.step_rewards[3] - 0.04) < 1e-9
        assert abs(traj.shaped_return - sum(traj.step_rewards)) < 1e-9
        assert turns[0].prm_score is None
        assert turns[1].prm_score == 0.4
        assert turns[2].prm_score is None
        assert turns[3].prm_score == 0.8

    def test_prm_scoring_handles_empty_trajectories(self, manager_with_prm):
        data = {
            "responses": ["no patch"],
            "task_ids": ["t1"],
            "hit_max_turns": [False],
            "timed_out": [False],
            "trajectories": [],
            "task_descriptions": [],
        }
        rewards = manager_with_prm(data)
        assert rewards.shape == (1,)
