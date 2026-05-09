"""ScalingInter-RL curriculum sampler with 4-phase progressive difficulty."""

from __future__ import annotations

import logging
from collections import deque

import numpy as np

from src.core.config import CurriculumConfig, CurriculumPhaseConfig

log = logging.getLogger(__name__)


class ScalingInterRLSampler:
    """4-phase curriculum: progressively increases max_turns and difficulty."""

    def __init__(
        self,
        task_difficulties: dict[int, str],
        curriculum_config: CurriculumConfig | None = None,
    ):
        self._task_difficulties = task_difficulties
        self._config = curriculum_config or CurriculumConfig()
        self._current_phase_idx = 0
        self._step_count = 0
        self._success_history: deque[float] = deque(
            maxlen=self._config.advance_window
        )
        self._rng = np.random.default_rng(42)
        self._variance_adjustment: list[str] = []

    @property
    def current_phase(self) -> int:
        return self._current_phase_idx

    @property
    def current_phase_config(self) -> CurriculumPhaseConfig:
        return self._config.phases[self._current_phase_idx]

    @property
    def max_turns(self) -> int:
        return self.current_phase_config.max_turns

    @property
    def step_count(self) -> int:
        return self._step_count

    def sample_batch(self, batch_size: int) -> list[int]:
        eligible = self._get_eligible_indices()
        if not eligible:
            all_indices = list(self._task_difficulties.keys())
            if not all_indices:
                return []
            chosen = self._rng.choice(
                all_indices, size=min(batch_size, len(all_indices)), replace=True
            )
            return [int(x) for x in chosen.tolist()]

        weighted = self._apply_sampling_weights(eligible)
        if not weighted:
            weighted = eligible

        indices = np.array(weighted)
        chosen = self._rng.choice(
            indices, size=min(batch_size, len(indices)), replace=True
        )
        return [int(x) for x in chosen.tolist()]

    def update(self, success_rate: float, reward_variance: float) -> None:
        self._step_count += 1
        self._success_history.append(success_rate)

        if self.should_advance_phase():
            self._advance_phase()

        self._adjust_for_variance(reward_variance)

    def should_advance_phase(self) -> bool:
        if self._current_phase_idx >= len(self._config.phases) - 1:
            return False

        phase = self.current_phase_config
        if self._step_count > phase.step_end:
            return True

        if len(self._success_history) >= self._config.advance_window:
            return all(
                s >= self._config.advance_threshold
                for s in self._success_history
            )

        return False

    def _advance_phase(self) -> None:
        old_phase = self._current_phase_idx
        self._current_phase_idx = min(
            self._current_phase_idx + 1, len(self._config.phases) - 1
        )
        if self._current_phase_idx != old_phase:
            new_config = self.current_phase_config
            log.info(
                "Curriculum advanced: phase %d -> %d (max_turns=%d, difficulty=%s) "
                "at step %d",
                old_phase,
                self._current_phase_idx,
                new_config.max_turns,
                new_config.difficulty_filter,
                self._step_count,
            )
            self._success_history.clear()
            self._variance_adjustment.clear()

    def _adjust_for_variance(self, variance: float) -> None:
        self._variance_adjustment.clear()

        if variance < self._config.variance_target_low:
            # Low variance = stuck model. Add HARDER tasks for reward diversity.
            current_filter = set(self.current_phase_config.difficulty_filter)
            if "hard" not in current_filter:
                self._variance_adjustment.append("hard")
            if self._variance_adjustment:
                log.debug(
                    "Low variance (%.4f): temporarily including %s tasks",
                    variance,
                    self._variance_adjustment,
                )
        elif variance > self._config.variance_target_high:
            # High variance = unstable. Add EASIER tasks to stabilize.
            current_filter = set(self.current_phase_config.difficulty_filter)
            if "easy" not in current_filter:
                self._variance_adjustment.append("easy")
            if "medium" not in current_filter and "easy" in current_filter:
                self._variance_adjustment.append("medium")
            if self._variance_adjustment:
                log.debug(
                    "High variance (%.4f): temporarily including %s tasks",
                    variance,
                    self._variance_adjustment,
                )

    def _get_eligible_indices(self) -> list[int]:
        allowed = set(self.current_phase_config.difficulty_filter)
        allowed.update(self._variance_adjustment)

        eligible = [
            idx
            for idx, diff in self._task_difficulties.items()
            if diff in allowed
        ]
        return eligible

    def _apply_sampling_weights(self, indices: list[int]) -> list[int]:
        hard_bias = self.current_phase_config.hard_bias
        if hard_bias <= 1.0:
            return indices

        weighted: list[int] = []
        for idx in indices:
            difficulty = self._task_difficulties.get(idx, "medium")
            if difficulty == "hard":
                repeat = int(hard_bias)
                weighted.extend([idx] * repeat)
            else:
                weighted.append(idx)

        return weighted

    def get_state_dict(self) -> dict:
        return {
            "current_phase_idx": self._current_phase_idx,
            "step_count": self._step_count,
            "success_history": list(self._success_history),
            "variance_adjustment": self._variance_adjustment,
            "rng_state": self._rng.bit_generator.state,
        }

    def load_state_dict(self, state: dict) -> None:
        self._current_phase_idx = state.get("current_phase_idx", 0)
        self._step_count = state.get("step_count", 0)

        history = state.get("success_history", [])
        self._success_history.clear()
        for val in history:
            self._success_history.append(val)

        self._variance_adjustment = state.get("variance_adjustment", [])

        rng_state = state.get("rng_state")
        if rng_state is not None:
            self._rng.bit_generator.state = rng_state
        else:
            rng_seed = state.get("rng_seed")
            if rng_seed is not None:
                self._rng = np.random.default_rng(rng_seed)
