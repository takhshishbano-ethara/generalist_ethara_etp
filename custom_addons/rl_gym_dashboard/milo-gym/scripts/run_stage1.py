"""Stage 1: Rejection Fine-Tuning warmup."""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.core.config import load_milo_config
from src.data.dataset import MiloDataset
from src.rollout.docker_executor import DockerExecutor
from src.training.rft_warmup import RFTWarmup

log = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(description="Stage 1: RFT Warmup")
    parser.add_argument("--config", type=str, default="configs/stage1_rft.yaml")
    parser.add_argument("--data-dir", type=str, default="data/tasks/")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s"
    )

    config = load_milo_config(args.config)
    dataset = MiloDataset.from_jsonl(Path(args.data_dir) / "train.jsonl")
    executor = DockerExecutor(timeout=config.hardware.docker_timeout)

    log.info(f"Starting RFT warmup with {len(dataset)} training tasks")
    warmup = RFTWarmup(config, executor)
    checkpoint_path = warmup.run(dataset)

    log.info(f"RFT complete. Checkpoint: {checkpoint_path}")
    gate_pass = warmup.gate_check(
        checkpoint_path, dataset.filter_by_difficulty(["easy"]).tasks[:50]
    )
    if gate_pass >= config.rft.gate_threshold:
        log.info(f"Gate PASSED: {gate_pass:.3f} >= {config.rft.gate_threshold}")
    else:
        log.warning(f"Gate FAILED: {gate_pass:.3f} < {config.rft.gate_threshold}")
        log.warning("Consider using teacher distillation or adjusting difficulty threshold")

    import json
    handoff = {
        "stage": 1,
        "checkpoint_path": checkpoint_path,
        "gate_pass_rate": gate_pass,
        "gate_passed": gate_pass >= config.rft.gate_threshold,
        "model_path": config.model_path,
    }
    handoff_path = Path(config.output_dir) / "stage1_handoff.json"
    handoff_path.parent.mkdir(parents=True, exist_ok=True)
    with handoff_path.open("w") as f:
        json.dump(handoff, f, indent=2)
    log.info(f"Stage 1 handoff written to {handoff_path}")


if __name__ == "__main__":
    main()
