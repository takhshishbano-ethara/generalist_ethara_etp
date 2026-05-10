"""MILO → NeMo-RL integration layer.

This package provides adapters that bridge MILO's custom components
(GTPO loss, PRM-based step advantages, Docker sandbox environment,
curriculum sampler) into NeMo-RL's GRPO training loop.

Components:
    MiloGTPOLoss: LossFunction protocol implementation wrapping GTPOLossComputer
    MiloAdvantageEstimator: Token-level advantage estimator wrapping StepAdvantageEstimator
    MiloCurriculumDataloader: StatefulDataLoader wrapping ScalingInterRLSampler
    MiloDockerEnvironment: EnvironmentInterface wrapping DockerSandboxTool
"""

from __future__ import annotations

__all__ = [
    "MiloGTPOLoss",
    "MiloAdvantageEstimator",
    "MiloCurriculumDataloader",
    "MiloDockerEnvironment",
]


def __getattr__(name: str):
    """Lazy imports to avoid loading heavy deps at package import time."""
    if name == "MiloGTPOLoss":
        from src.nemo_integration.loss import MiloGTPOLoss
        return MiloGTPOLoss
    elif name == "MiloAdvantageEstimator":
        from src.nemo_integration.advantage import MiloAdvantageEstimator
        return MiloAdvantageEstimator
    elif name == "MiloCurriculumDataloader":
        from src.nemo_integration.dataloader import MiloCurriculumDataloader
        return MiloCurriculumDataloader
    elif name == "MiloDockerEnvironment":
        from src.nemo_integration.environment import MiloDockerEnvironment
        return MiloDockerEnvironment
    raise AttributeError(f"module 'src.nemo_integration' has no attribute {name!r}")
