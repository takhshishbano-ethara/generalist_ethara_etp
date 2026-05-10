"""Pydantic data models used across the MILO-RL pipeline."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator


class Turn(BaseModel):
    """Single turn in a multi-turn trajectory."""

    role: Literal["user", "assistant", "tool", "system"]
    content: str
    tool_call_id: str | None = None
    timestamp: float | None = None
    token_count: int = 0
    prm_score: float | None = None


class Trajectory(BaseModel):
    """Complete multi-turn interaction for one task attempt."""

    task_id: str
    turns: list[Turn]
    raw_response: str = ""
    patch: str = ""
    reward: float = 0.0
    mask: bool = True
    hit_max_turns: bool = False
    hit_max_context: bool = False
    timed_out: bool = False
    episode_length: int = 0
    wall_clock_seconds: float = 0.0
    curriculum_phase: int = 1
    training_step: int = 0
    error: str | None = None
    step_rewards: list[float] = Field(default_factory=list)
    shaped_return: float = 0.0

    @property
    def is_success(self) -> bool:
        return self.reward > 0.0

    @property
    def total_tokens(self) -> int:
        return sum(t.token_count for t in self.turns)


class TaskSpec(BaseModel):
    """Specification for a single training/eval task."""

    task_id: str
    instance_id: str = ""
    repo: str
    language: Literal["python", "go"]
    base_commit: str
    problem_statement: str
    test_patch: str
    fix_patch: str
    docker_image: str = ""
    evaluation_script: str = ""
    difficulty: Literal["easy", "medium", "hard"] = "medium"
    difficulty_score: float = Field(default=0.5, ge=0.0, le=1.0)
    max_turns: int = 50
    timeout_seconds: int = 1800
    dependencies: list[str] = Field(default_factory=list)

    @field_validator("task_id")
    @classmethod
    def validate_task_id(cls, v: str) -> str:
        if not v.strip():
            msg = "task_id cannot be empty"
            raise ValueError(msg)
        return v


class RewardResult(BaseModel):
    """Output of reward computation for one trajectory."""

    reward: float
    mask: bool = True
    f2p_passed: int = 0
    f2p_total: int = 0
    p2p_passed: int = 0
    p2p_total: int = 0
    timed_out: bool = False
    error: str | None = None

    @property
    def f2p_pass(self) -> bool:
        return self.f2p_total > 0 and self.f2p_passed == self.f2p_total

    @property
    def p2p_pass(self) -> bool:
        return self.p2p_total == 0 or self.p2p_passed == self.p2p_total


class EvalResult(BaseModel):
    """Evaluation result for a single task."""

    task_id: str
    passed: bool
    trajectories_attempted: int
    best_trajectory_idx: int = 0
    pass_at_1: float = 0.0
    pass_at_n: float = 0.0
    details: dict = Field(default_factory=dict)


class TrainingMetrics(BaseModel):
    """Metrics recorded at each training step."""

    step: int
    success_rate: float
    mask_rate: float
    avg_episode_length: float
    reward_variance: float
    grad_norm: float
    learning_rate: float
    unique_tasks_solved: int
    curriculum_phase: int
    total_rollouts: int
    eval_pass_at_1: float | None = None
    timestamp: float = 0.0


class CheckpointMeta(BaseModel):
    """Metadata for a saved checkpoint."""

    step: int
    eval_pass_at_1: float
    curriculum_phase: int
    path: str
    timestamp: float
    is_best: bool = False
