from __future__ import annotations

import pytest

torch = pytest.importorskip("torch")

from unittest.mock import AsyncMock, MagicMock, patch

from src.core.config import GSPOConfig, PRMConfig, NemoRLConfig
from src.prm.step_advantage import TurnSpan


class TestMiloGTPOLoss:
    @pytest.fixture
    def loss_fn(self):
        from src.nemo_integration.loss import MiloGTPOLoss
        config = GSPOConfig(clip_low=3e-4, clip_high=4e-4, beta_kl=0.0)
        return MiloGTPOLoss(config)

    def test_call_returns_loss_and_metrics(self, loss_fn):
        B, S = 4, 32
        data = {
            "advantages": torch.randn(B, S),
            "prev_logprobs": torch.zeros(B, S),
            "token_mask": torch.ones(B, S),
            "sample_mask": torch.ones(B),
        }
        next_token_logprobs = torch.zeros(B, S - 1)

        loss, metrics = loss_fn(
            data,
            global_valid_seqs=B,
            global_valid_toks=B * (S - 1),
            next_token_logprobs=next_token_logprobs,
        )

        assert loss.dim() == 0
        assert torch.isfinite(loss)
        assert "clip_frac" in metrics
        assert "approx_kl" in metrics

    def test_respects_token_mask(self, loss_fn):
        B, S = 2, 16
        data = {
            "advantages": torch.ones(B, S),
            "prev_logprobs": torch.zeros(B, S),
            "token_mask": torch.zeros(B, S),
            "sample_mask": torch.ones(B),
        }
        next_token_logprobs = torch.randn(B, S - 1)

        loss, _ = loss_fn(
            data,
            global_valid_seqs=B,
            global_valid_toks=1,
            next_token_logprobs=next_token_logprobs,
        )

        assert loss.item() == 0.0


class TestMiloAdvantageEstimator:
    @pytest.fixture
    def estimator(self):
        from src.nemo_integration.advantage import MiloAdvantageEstimator
        config = PRMConfig(advantage_mode="step_wise", gtpo_gamma=0.9)
        return MiloAdvantageEstimator(config, group_size=2)

    def test_compute_advantage_with_step_rewards(self, estimator):
        B, S = 4, 64
        prompt_ids = torch.zeros(B, 10, dtype=torch.long)
        rewards = torch.tensor([1.0, 0.0, 1.0, 0.5])
        mask = torch.ones(B, S)

        extra_env_info = [
            {
                "step_rewards": [0.3, 0.7, 1.0],
                "turn_spans": [
                    {"turn_idx": 0, "start_token": 10, "end_token": 20},
                    {"turn_idx": 1, "start_token": 20, "end_token": 35},
                    {"turn_idx": 2, "start_token": 35, "end_token": 50},
                ],
            },
            {
                "step_rewards": [0.1, 0.2, 0.0],
                "turn_spans": [
                    {"turn_idx": 0, "start_token": 10, "end_token": 20},
                    {"turn_idx": 1, "start_token": 20, "end_token": 35},
                    {"turn_idx": 2, "start_token": 35, "end_token": 50},
                ],
            },
            {
                "step_rewards": [0.9, 0.8, 0.95],
                "turn_spans": [
                    {"turn_idx": 0, "start_token": 10, "end_token": 20},
                    {"turn_idx": 1, "start_token": 20, "end_token": 35},
                    {"turn_idx": 2, "start_token": 35, "end_token": 50},
                ],
            },
            {
                "step_rewards": [0.5, 0.4, 0.3],
                "turn_spans": [
                    {"turn_idx": 0, "start_token": 10, "end_token": 20},
                    {"turn_idx": 1, "start_token": 20, "end_token": 35},
                    {"turn_idx": 2, "start_token": 35, "end_token": 50},
                ],
            },
        ]

        repeated_batch = {"extra_env_info": extra_env_info}

        advantages = estimator.compute_advantage(
            prompt_ids, rewards, mask, repeated_batch=repeated_batch
        )

        assert advantages.shape == (B, S)
        assert not torch.all(advantages == 0)

    def test_rloo_fallback_when_no_step_rewards(self, estimator):
        from src.nemo_integration.advantage import MiloAdvantageEstimator
        config = PRMConfig(advantage_mode="rloo")
        rloo_estimator = MiloAdvantageEstimator(config, group_size=2)

        B, S = 4, 32
        prompt_ids = torch.zeros(B, 10, dtype=torch.long)
        rewards = torch.tensor([1.0, 0.0, 1.0, 0.5])
        mask = torch.ones(B, S)

        extra_env_info = [{} for _ in range(B)]
        repeated_batch = {"extra_env_info": extra_env_info}

        advantages = rloo_estimator.compute_advantage(
            prompt_ids, rewards, mask, repeated_batch=repeated_batch
        )

        assert advantages.shape == (B, S)


class TestMiloCurriculumDataloader:
    @pytest.fixture
    def dataloader(self):
        from src.nemo_integration.dataloader import MiloCurriculumDataloader
        from src.training.curriculum import ScalingInterRLSampler
        from src.core.config import CurriculumConfig, CurriculumPhaseConfig
        from src.core.schemas import TaskSpec

        phases = [
            CurriculumPhaseConfig(phase_id=1, step_start=0, step_end=100,
                                  difficulty_filter=["easy"], max_turns=10)
        ]
        config = CurriculumConfig(phases=phases)

        task_difficulties = {0: "easy", 1: "easy"}
        sampler = ScalingInterRLSampler(task_difficulties, config)

        task_registry = {
            0: TaskSpec(
                task_id="task_001",
                instance_id="test/repo__issue-1",
                problem_statement="Fix the bug in foo.py",
                repo="test/repo",
                language="python",
                base_commit="abc123",
                test_patch="--- a/test.py\n+++ b/test.py\n",
                fix_patch="--- a/foo.py\n+++ b/foo.py\n",
                difficulty="easy",
                difficulty_score=0.2,
            ),
            1: TaskSpec(
                task_id="task_002",
                instance_id="test/repo__issue-2",
                problem_statement="Add feature bar",
                repo="test/repo",
                language="python",
                base_commit="def456",
                test_patch="--- a/test.py\n+++ b/test.py\n",
                fix_patch="--- a/bar.py\n+++ b/bar.py\n",
                difficulty="easy",
                difficulty_score=0.1,
            ),
        }

        tokenizer = MagicMock()
        tokenizer.apply_chat_template = MagicMock(return_value="System: Hello\nUser: Fix bug")
        tokenizer.return_value = {"input_ids": torch.tensor([[1, 2, 3, 4, 5]])}
        tokenizer.pad_token_id = 0

        return MiloCurriculumDataloader(
            sampler=sampler,
            task_registry=task_registry,
            tokenizer=tokenizer,
            num_prompts_per_step=2,
        )

    def test_iter_yields_batches(self, dataloader):
        iterator = iter(dataloader)
        batch = next(iterator)
        assert "message_log" in batch
        assert "extra_env_info" in batch
        assert "task_name" in batch
        assert "length" in batch
        assert "stop_strings" in batch
        assert "idx" in batch

    def test_state_dict_round_trip(self, dataloader):
        state = dataloader.state_dict()
        assert isinstance(state, dict)
        dataloader.load_state_dict(state)

    def test_update_metrics(self, dataloader):
        dataloader.update_metrics(success_rate=0.5, reward_variance=0.1)


class TestStepAdvantageComputeFromBatch:
    def test_compute_from_batch_delegates(self):
        from src.prm.step_advantage import StepAdvantageEstimator, TurnSpan

        estimator = StepAdvantageEstimator(mode="step_wise", gamma=0.9)

        B, S = 4, 64
        mask = torch.ones(B, S)

        extra_env_info = [
            {
                "step_rewards": [0.5, 0.8],
                "turn_spans": [
                    {"turn_idx": 0, "start_token": 5, "end_token": 25},
                    {"turn_idx": 1, "start_token": 25, "end_token": 50},
                ],
            }
            for _ in range(B)
        ]

        result = estimator.compute_from_batch(extra_env_info, mask, group_size=2)
        assert result is not None
        assert result.shape == (B, S)

    def test_compute_from_batch_handles_turnspan_objects(self):
        from src.prm.step_advantage import StepAdvantageEstimator, TurnSpan

        estimator = StepAdvantageEstimator(mode="step_wise", gamma=0.9)

        B, S = 2, 32
        mask = torch.ones(B, S)

        extra_env_info = [
            {
                "step_rewards": [0.3],
                "turn_spans": [TurnSpan(turn_idx=0, start_token=5, end_token=20)],
            }
            for _ in range(B)
        ]

        result = estimator.compute_from_batch(extra_env_info, mask, group_size=2)
        assert result is not None
        assert result.shape == (B, S)


class TestDockerToolStopStrings:
    def test_get_tool_stop_strings(self):
        from src.rollout.docker_tool import DockerSandboxTool

        executor = MagicMock()
        tool = DockerSandboxTool(executor=executor, task_registry={})
        stop_strings = tool.get_tool_stop_strings()
        assert "</tool_call>" in stop_strings


class TestNemoRLConfig:
    def test_nemo_rl_config_in_milo_config(self):
        from src.core.config import MiloConfig, NemoRLConfig

        config = MiloConfig()
        assert isinstance(config.nemo_rl, NemoRLConfig)
        assert config.nemo_rl.backend == "fsdp2"
        assert config.nemo_rl.tensor_parallel_size == 2
        assert config.nemo_rl.max_rollout_turns == 50

    def test_nemo_rl_config_from_yaml(self):
        from src.core.config import NemoRLConfig

        config = NemoRLConfig(backend="megatron", num_nodes=4, gpus_per_node=8)
        assert config.backend == "megatron"
        assert config.num_nodes == 4
