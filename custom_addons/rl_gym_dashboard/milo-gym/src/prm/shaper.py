from __future__ import annotations

from dataclasses import dataclass

from src.core.config import PRMConfig


@dataclass
class PotentialShaper:
    alpha: float = 0.1
    gamma: float = 0.9
    outcome_gate: bool = True
    gate_mode: str = "add_on_success"
    sparse_non_terminal: bool = False  # If True, zero PRM shaping for non-last turns (reduces hacking)
    sparse_threshold: float = 0.0  # Only apply shaping if PRM score exceeds this (0.0 = disabled)

    @classmethod
    def from_config(cls, config: PRMConfig) -> PotentialShaper:
        return cls(
            alpha=config.shaping_alpha,
            gamma=config.gtpo_gamma,
            outcome_gate=config.outcome_gate,
            gate_mode=config.gate_mode,
        )

    def shape(self, prm_scores: list[float | None], outcome_reward: float) -> list[float]:
        """Compute shaped rewards using potential-based shaping (PBRS).

        None entries are non-scored turns (non-assistant) — they emit 0.0 delta
        and do NOT update the potential. Float entries (including 0.0) are scored
        turns that participate in the potential function.
        """
        n = len(prm_scores)
        if n == 0:
            return []

        shaped: list[float] = []
        prev_potential = 0.0

        # Find last scored index for sparse mode
        last_scored_idx: int | None = None
        for k in range(n - 1, -1, -1):
            if prm_scores[k] is not None:
                last_scored_idx = k
                break

        for k in range(n):
            score = prm_scores[k]
            if score is None:
                shaped.append(0.0)
                continue

            # Sparse mode: zero non-terminal PRM deltas to prevent reward hacking
            # Do NOT update prev_potential — preserve telescoping invariant
            if self.sparse_non_terminal and k != last_scored_idx:
                shaped.append(0.0)
                continue

            # Confidence filter: only apply shaping if PRM is confident
            # Do NOT update prev_potential — preserve telescoping invariant
            if self.sparse_threshold > 0.0 and score < self.sparse_threshold and k != last_scored_idx:
                shaped.append(0.0)
                continue

            # PBRS: F(s,s') = γ·Φ(s') - Φ(s)
            delta = self.alpha * (self.gamma * score - prev_potential)
            shaped.append(delta)
            prev_potential = score

        if self.outcome_gate:
            shaped = self._apply_gate(shaped, outcome_reward)
            if outcome_reward <= 0.0 and self.gate_mode in ("add_on_success", "hard_gate"):
                # Gate zeroed shaping rewards, but outcome must still be added to last turn
                last_scored_idx: int | None = None
                for k in range(n - 1, -1, -1):
                    if prm_scores[k] is not None:
                        last_scored_idx = k
                        break
                if last_scored_idx is not None:
                    shaped[last_scored_idx] += outcome_reward
                elif n > 0:
                    shaped[-1] += outcome_reward
                return shaped

        last_scored_idx: int | None = None
        for k in range(n - 1, -1, -1):
            if prm_scores[k] is not None:
                last_scored_idx = k
                break

        if last_scored_idx is not None:
            shaped[last_scored_idx] += outcome_reward
        elif n > 0:
            shaped[-1] += outcome_reward
        return shaped

    def _apply_gate(self, shaped: list[float], outcome: float) -> list[float]:
        if self.gate_mode == "add_on_success":
            if outcome <= 0.0:
                return [0.0] * len(shaped)
            return shaped
        elif self.gate_mode == "hard_gate":
            # Identical to add_on_success: zero ALL shaped rewards on failure.
            # Defense-in-depth — gate_mode="hard_gate" is never a no-op.
            if outcome <= 0.0:
                return [0.0] * len(shaped)
            return shaped
        elif self.gate_mode == "multiply":
            # WARNING: Only valid for binary outcomes {0,1}. Continuous outcomes
            # will introduce bias. Use add_on_success for non-binary rewards.
            return [s * outcome for s in shaped]
        return shaped

    def compute_shaped_return(self, step_rewards: list[float]) -> float:
        return sum(step_rewards)
