from __future__ import annotations

from typing import TYPE_CHECKING

from .curriculum import ScalingInterRLSampler

if TYPE_CHECKING:
    from .model_loader import TrainingStack, load_training_stack
    from .reward_manager import MiloRewardManager
    from .rft_warmup import RFTWarmup
    from .tokenization import TokenizedTrajectory, TurnSpan
    from .trainer import MiloTrainer


def __getattr__(name: str):
    """Lazy imports for modules requiring torch/vllm."""
    if name == "MiloRewardManager":
        from .reward_manager import MiloRewardManager
        return MiloRewardManager
    if name == "MiloTrainer":
        from .trainer import MiloTrainer
        return MiloTrainer
    if name == "RFTWarmup":
        from .rft_warmup import RFTWarmup
        return RFTWarmup
    if name == "load_training_stack":
        from .model_loader import load_training_stack
        return load_training_stack
    if name == "TrainingStack":
        from .model_loader import TrainingStack
        return TrainingStack
    raise AttributeError(f"module 'src.training' has no attribute {name!r}")


__all__ = [
    "MiloRewardManager",
    "MiloTrainer",
    "RFTWarmup",
    "ScalingInterRLSampler",
    "TrainingStack",
    "load_training_stack",
]
