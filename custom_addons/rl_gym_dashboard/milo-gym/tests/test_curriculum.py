from __future__ import annotations

import pytest

from src.training.curriculum import ScalingInterRLSampler
from src.core.config import CurriculumConfig, CurriculumPhaseConfig


@pytest.fixture
def curriculum_config() -> CurriculumConfig:
    return CurriculumConfig(
        phases=[
            CurriculumPhaseConfig(
                phase_id=1, max_turns=10, step_start=0, step_end=50,
                difficulty_filter=["easy"], expected_success_rate=0.3,
            ),
            CurriculumPhaseConfig(
                phase_id=2, max_turns=20, step_start=50, step_end=150,
                difficulty_filter=["easy", "medium"], expected_success_rate=0.2,
            ),
            CurriculumPhaseConfig(
                phase_id=3, max_turns=35, step_start=150, step_end=300,
                difficulty_filter=["easy", "medium", "hard"],
                expected_success_rate=0.15,
            ),
        ],
        advance_threshold=0.7,
        advance_window=5,
    )


@pytest.fixture
def task_difficulties() -> dict[int, str]:
    return {
        0: "easy", 1: "easy", 2: "easy",
        3: "medium", 4: "medium",
        5: "hard", 6: "hard",
    }


@pytest.fixture
def sampler(
    curriculum_config: CurriculumConfig, task_difficulties: dict[int, str]
) -> ScalingInterRLSampler:
    return ScalingInterRLSampler(
        task_difficulties=task_difficulties,
        curriculum_config=curriculum_config,
    )


class TestScalingInterRLSampler:
    def test_initial_phase(self, sampler: ScalingInterRLSampler):
        assert sampler.current_phase == 0
        assert sampler.max_turns == 10

    def test_sample_batch_respects_difficulty(
        self, sampler: ScalingInterRLSampler, task_difficulties: dict[int, str]
    ):
        batch = sampler.sample_batch(20)
        for idx in batch:
            assert task_difficulties[idx] == "easy"

    def test_phase_advancement_by_step(self, sampler: ScalingInterRLSampler):
        for _ in range(51):
            sampler.update(success_rate=0.3, reward_variance=0.1)
        assert sampler.current_phase == 1

    def test_phase_advancement_early(
        self, curriculum_config: CurriculumConfig, task_difficulties: dict[int, str]
    ):
        sampler = ScalingInterRLSampler(
            task_difficulties=task_difficulties,
            curriculum_config=curriculum_config,
        )
        for _ in range(5):
            sampler.update(success_rate=0.8, reward_variance=0.1)
        assert sampler.current_phase == 1

    def test_max_turns_increases(self, sampler: ScalingInterRLSampler):
        phase1_turns = sampler.max_turns
        for _ in range(51):
            sampler.update(success_rate=0.3, reward_variance=0.1)
        phase2_turns = sampler.max_turns
        assert phase2_turns > phase1_turns

    def test_state_serialization(self, sampler: ScalingInterRLSampler):
        for _ in range(10):
            sampler.update(success_rate=0.5, reward_variance=0.1)

        state = sampler.get_state_dict()
        new_sampler = ScalingInterRLSampler(
            task_difficulties={0: "easy", 1: "medium"},
            curriculum_config=sampler._config,
        )
        new_sampler.load_state_dict(state)

        assert new_sampler.current_phase == sampler.current_phase
        assert new_sampler.step_count == sampler.step_count
