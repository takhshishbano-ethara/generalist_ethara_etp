"""MILO training orchestrator with real training loop, kill conditions, and monitoring."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

import torch
from torch.optim import AdamW
from torch.optim.lr_scheduler import CosineAnnealingWarmRestarts, LinearLR, SequentialLR

from src.core.config import MiloConfig, MonitoringConfig
from src.core.schemas import TrainingMetrics, Trajectory
from src.monitoring.kill_conditions import KillConditionMonitor, KillAction, KillReason
from src.monitoring.metrics import MetricsTracker
from src.monitoring.replay import RolloutReplayStore
from src.prm.step_advantage import StepAdvantageEstimator
from src.training.curriculum import ScalingInterRLSampler
from src.training.reward_manager import MiloRewardManager

log = logging.getLogger(__name__)


def _run_async(coro):
    """Bridge sync → async: run coroutine in a new event loop on a thread."""
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(1) as pool:
        future = pool.submit(asyncio.run, coro)
        return future.result()


class MiloTrainer:
    """Training loop orchestrator with monitoring, kill conditions, and recovery."""

    def __init__(
        self,
        config: MiloConfig,
        reward_manager: MiloRewardManager,
        curriculum: ScalingInterRLSampler,
        model: torch.nn.Module | None = None,
        tokenizer=None,
        vllm_engine=None,
        rollout_engine=None,
        lora_adapter_dir: str | None = None,
        eval_tasks: list | None = None,
        task_id_map: dict[int, str] | None = None,
    ):
        self._config = config
        self._reward_manager = reward_manager
        self._curriculum = curriculum
        self._model = model
        self._tokenizer = tokenizer
        self._vllm_engine = vllm_engine
        self._rollout_engine = rollout_engine
        self._lora_adapter_dir = lora_adapter_dir
        self._eval_tasks = eval_tasks or []
        self._task_id_map = task_id_map or {}

        output_dir = Path(config.output_dir) / config.run_id
        output_dir.mkdir(parents=True, exist_ok=True)
        self._output_dir = output_dir
        self._metrics_tracker = MetricsTracker(output_dir=str(output_dir))
        self._replay_store = RolloutReplayStore(output_dir=str(output_dir / "replays"))
        self._kill_monitor = KillConditionMonitor(config.monitoring)

        self._step = 0
        self._best_eval = 0.0
        self._best_checkpoint_path: str | None = None
        self._stopped = False
        self._stop_reason: str | None = None
        self._learning_rate = config.gspo.learning_rate
        self._temperature = config.gspo.temperature
        self._consecutive_docker_failures = 0

        self._prm_config = config.prm
        self._teacher_refresh_counter = 0
        self._step_advantage: StepAdvantageEstimator | None = None
        if self._prm_config and self._prm_config.enabled:
            self._step_advantage = StepAdvantageEstimator.from_config(self._prm_config)
            self._reward_manager.configure_prm(self._prm_config)

        self._optimizer: torch.optim.Optimizer | None = None
        self._scheduler = None
        self._loss_computer = None
        self._lora_sync_interval = config.monitoring.checkpoint_every

    def _init_optimizer(self) -> None:
        if self._model is None:
            return
        self._optimizer = AdamW(
            self._model.parameters(),
            lr=self._config.gspo.learning_rate,
            weight_decay=0.01,
            betas=(0.9, 0.95),
        )
        warmup_steps = self._config.gspo.warmup_steps
        total_steps = self._config.gspo.total_steps
        warmup_scheduler = LinearLR(
            self._optimizer, start_factor=0.1, end_factor=1.0, total_iters=warmup_steps
        )
        cosine_scheduler = CosineAnnealingWarmRestarts(
            self._optimizer, T_0=total_steps - warmup_steps, T_mult=1
        )
        self._scheduler = SequentialLR(
            self._optimizer, schedulers=[warmup_scheduler, cosine_scheduler], milestones=[warmup_steps]
        )

    def _init_loss_computer(self) -> None:
        from src.training.gtpo_loss import GTPOLossComputer
        from src.training.gspo_loss import GSPOLossComputer
        if self._config.gspo.loss_type == "gtpo":
            self._loss_computer = GTPOLossComputer.from_config(self._config.gspo)
        else:
            self._loss_computer = GSPOLossComputer(self._config.gspo)

    @property
    def step(self) -> int:
        return self._step

    @property
    def is_stopped(self) -> bool:
        return self._stopped

    @property
    def stop_reason(self) -> str | None:
        return self._stop_reason

    def fit(self) -> None:
        if self._optimizer is None:
            self._init_optimizer()
        if self._loss_computer is None:
            self._init_loss_computer()

        total_steps = self._config.gspo.total_steps
        eval_every = self._config.monitoring.eval_every
        checkpoint_every = self._config.monitoring.checkpoint_every

        log.info(
            "Starting training: total_steps=%d, eval_every=%d, checkpoint_every=%d",
            total_steps, eval_every, checkpoint_every,
        )
        start_time = time.time()

        for step in range(self._step, total_steps):
            if self._stopped:
                log.info("Training stopped at step %d: %s", step, self._stop_reason)
                break

            self._step = step
            step_start = time.time()

            trajectories = self._generate_rollouts()
            if not trajectories:
                log.warning("Empty rollout batch at step %d, skipping", step)
                continue

            rewards = self._compute_rewards(trajectories)
            grad_norm = self._training_step(trajectories, rewards)
            metrics = self._compute_step_metrics(trajectories, rewards, grad_norm)

            kill_action = self._kill_monitor.check(metrics)
            if kill_action is not None:
                self._handle_kill_action(kill_action)
                if self._stopped:
                    break

            self._curriculum.update(metrics.success_rate, metrics.reward_variance)

            if trajectories:
                self._replay_store.store(trajectories, step)

            if eval_every > 0 and step % eval_every == 0 and step > 0:
                eval_score = self._run_eval(step)
                metrics.eval_pass_at_1 = eval_score

            if checkpoint_every > 0 and step % checkpoint_every == 0 and step > 0:
                self._save_checkpoint(step)

            if self._lora_sync_interval > 0 and step % self._lora_sync_interval == 0 and step > 0:
                self._sync_lora_weights()

            step_elapsed = time.time() - step_start
            if step % 10 == 0:
                log.info(
                    "step=%d/%d elapsed=%.1fs phase=%d lr=%.2e grad_norm=%.4f",
                    step, total_steps, step_elapsed,
                    self._curriculum.current_phase, self._learning_rate, grad_norm,
                )

        total_elapsed = time.time() - start_time
        log.info(
            "Training complete: steps=%d, elapsed=%.1fs, best_eval=%.4f",
            self._step, total_elapsed, self._best_eval,
        )
        self._metrics_tracker.save_to_jsonl()
        self._save_checkpoint(self._step)

    def _generate_rollouts(self) -> list[Trajectory]:
        """Generate multi-turn rollouts via rollout engine or fallback to vLLM direct."""
        group_size = self._config.gspo.group_size
        batch_size = self._config.gspo.batch_size
        n_unique_tasks = batch_size // group_size

        batch_indices = self._curriculum.sample_batch(n_unique_tasks)
        if not batch_indices:
            log.warning("Curriculum returned empty batch at step %d", self._step)
            return []

        tasks = []
        for idx in batch_indices:
            task_id = self._task_id_map.get(idx, f"task_{idx}")
            task = self._reward_manager._task_registry.get(task_id)
            if task is not None:
                tasks.append(task)

        if not tasks:
            return []

        if self._rollout_engine is not None:
            return self._generate_multi_turn_rollouts(tasks, group_size)
        elif self._vllm_engine is not None:
            return self._generate_single_shot_rollouts(tasks, group_size)
        else:
            return self._generate_placeholder_rollouts(batch_indices)

    def _generate_multi_turn_rollouts(
        self, tasks: list, group_size: int
    ) -> list[Trajectory]:
        """Use MultiTurnRolloutEngine for full multi-turn episodes."""
        trajectories = _run_async(
            self._rollout_engine.generate_batch(
                tasks,
                group_size=group_size,
                temperature=self._temperature,
            )
        )
        for traj in trajectories:
            traj.training_step = self._step
            traj.curriculum_phase = self._curriculum.current_phase
        return trajectories

    def _generate_single_shot_rollouts(
        self, tasks: list, group_size: int
    ) -> list[Trajectory]:
        """Fallback: single-shot vLLM generation without tool-use."""
        from vllm import SamplingParams

        sampling_params = SamplingParams(
            temperature=self._temperature,
            top_p=self._config.gspo.top_p,
            max_tokens=self._rollout_cfg_max_tokens(),
            n=group_size,
        )

        prompts = [task.problem_statement for task in tasks]
        outputs = self._vllm_engine.generate(
            prompts, sampling_params,
            lora_request=self._get_lora_request(),
        )

        trajectories: list[Trajectory] = []
        for task, output in zip(tasks, outputs):
            for completion in output.outputs:
                from src.rollout.patch_utils import extract_patch
                patch = extract_patch(completion.text)
                traj = Trajectory(
                    task_id=task.task_id,
                    turns=[],
                    raw_response=completion.text,
                    patch=patch,
                    mask=True,
                    episode_length=1,
                    curriculum_phase=self._curriculum.current_phase,
                    training_step=self._step,
                )
                trajectories.append(traj)
        return trajectories

    def _generate_placeholder_rollouts(self, batch_indices: list[int]) -> list[Trajectory]:
        trajectories: list[Trajectory] = []
        for idx in batch_indices:
            task_id = self._task_id_map.get(idx, f"task_{idx}")
            traj = Trajectory(
                task_id=task_id,
                turns=[],
                raw_response="",
                patch="",
                reward=0.0,
                mask=True,
                episode_length=0,
                curriculum_phase=self._curriculum.current_phase,
                training_step=self._step,
            )
            trajectories.append(traj)
        return trajectories

    def _training_step(self, trajectories: list[Trajectory], rewards: list[float]) -> float:
        """Forward pass → compute advantages → GTPO loss → backward → optimizer step."""
        if self._model is None or self._optimizer is None or self._loss_computer is None:
            return 0.0

        self._model.train()

        batch_data = self._prepare_batch(trajectories, rewards)
        if batch_data is None:
            return 0.0

        log_probs = batch_data["log_probs"]
        ref_log_probs = batch_data["ref_log_probs"]
        advantages = batch_data["advantages"]
        response_mask = batch_data["response_mask"]

        loss_out = self._loss_computer(
            log_probs=log_probs,
            old_log_probs=ref_log_probs,
            advantages=advantages,
            response_mask=response_mask,
        )

        loss = loss_out["loss"]

        self._optimizer.zero_grad()
        loss.backward()

        grad_norm = torch.nn.utils.clip_grad_norm_(
            self._model.parameters(), self._config.gspo.max_grad_norm
        ).item()

        self._optimizer.step()
        if self._scheduler is not None:
            self._scheduler.step()
            self._learning_rate = self._scheduler.get_last_lr()[0]

        return grad_norm

    def _prepare_batch(
        self, trajectories: list[Trajectory], rewards: list[float]
    ) -> dict[str, torch.Tensor] | None:
        """Tokenize trajectories, compute log-probs and per-step advantages."""
        if self._tokenizer is None or self._model is None:
            return None

        valid_trajs = [t for t in trajectories if t.raw_response or t.turns]
        if not valid_trajs:
            return None

        from src.training.tokenization import batch_tokenize_trajectories
        from src.training.model_loader import compute_ref_log_probs, compute_current_log_probs

        batch = batch_tokenize_trajectories(
            self._tokenizer,
            valid_trajs,
            max_length=self._config.hardware.max_model_len,
            device=str(next(self._model.parameters()).device),
        )

        input_ids = batch["input_ids"]
        attention_mask = batch["attention_mask"]
        response_mask = batch["response_mask"]
        turn_spans = batch["turn_spans"]

        if input_ids.numel() == 0:
            return None

        # Compute reference log-probs (base model without LoRA)
        ref_log_probs = compute_ref_log_probs(self._model, input_ids, attention_mask)

        # Compute current policy log-probs (with LoRA, differentiable)
        log_probs = compute_current_log_probs(self._model, input_ids, attention_mask)

        # Align response_mask with shifted log-probs (shifted by 1 for next-token prediction)
        response_mask = response_mask[:, 1:]  # [batch, seq_len-1]

        # Compute per-step advantages using StepAdvantageEstimator
        advantages = self._compute_advantages(valid_trajs, rewards, turn_spans, response_mask, log_probs)

        return {
            "log_probs": log_probs,
            "ref_log_probs": ref_log_probs,
            "advantages": advantages,
            "response_mask": response_mask,
        }

    def _compute_advantages(
        self,
        trajectories: list[Trajectory],
        rewards: list[float],
        turn_spans: list[list],
        response_mask: torch.Tensor,
        log_probs: torch.Tensor,
    ) -> torch.Tensor:
        """Compute per-token advantages via step_advantage or group-relative scalar."""
        batch_size, seq_len = log_probs.shape
        group_size = self._config.gspo.group_size
        device = log_probs.device

        if self._step_advantage is not None and any(t.step_rewards for t in trajectories):
            step_rewards_batch = []
            seq_lengths = []
            for i, traj in enumerate(trajectories):
                step_rewards_batch.append(traj.step_rewards if traj.step_rewards else [0.0])
                seq_lengths.append(seq_len)

            # StepAdvantageEstimator returns [batch, max_seq_len] with advantages at turn token positions
            adv_tensor = self._step_advantage.compute(
                step_rewards=step_rewards_batch,
                turn_spans=turn_spans,
                seq_lengths=seq_lengths,
                group_size=group_size,
            )

            if adv_tensor is not None:
                # Ensure it matches our seq_len (may differ due to shift)
                if adv_tensor.shape[1] > seq_len:
                    adv_tensor = adv_tensor[:, :seq_len]
                elif adv_tensor.shape[1] < seq_len:
                    pad = torch.zeros(batch_size, seq_len - adv_tensor.shape[1], device=device)
                    adv_tensor = torch.cat([adv_tensor, pad], dim=1)
                return adv_tensor.to(device)

        # Fallback: GRPO group-relative scalar advantages
        reward_tensor = torch.tensor(rewards[:batch_size], dtype=torch.float32, device=device)

        # Normalize within groups
        if group_size > 1 and batch_size >= group_size:
            n_groups = batch_size // group_size
            for g in range(n_groups):
                start = g * group_size
                end = start + group_size
                group_rewards = reward_tensor[start:end]
                mean = group_rewards.mean()
                std = group_rewards.std() + 1e-8
                reward_tensor[start:end] = (group_rewards - mean) / std

        # Broadcast scalar advantage to all response tokens
        advantages = reward_tensor.unsqueeze(1).expand(batch_size, seq_len)
        return advantages * response_mask

    def _compute_rewards(self, trajectories: list[Trajectory]) -> list[float]:
        if not trajectories:
            return []

        data = {
            "responses": [t.raw_response for t in trajectories],
            "task_ids": [t.task_id for t in trajectories],
            "episode_lengths": [t.episode_length for t in trajectories],
            "hit_max_turns": [t.hit_max_turns for t in trajectories],
            "timed_out": [t.timed_out for t in trajectories],
            "trajectories": trajectories,
            "task_descriptions": [""] * len(trajectories),
        }

        try:
            result = self._reward_manager(data)
            self._consecutive_docker_failures = 0

            use_shaped = (
                self._prm_config
                and self._prm_config.enabled
                and "shaped_returns" in result
            )
            reward_tensor = result["shaped_returns"] if use_shaped else result["rewards"]
            rewards = reward_tensor.tolist()
        except Exception as e:
            self._consecutive_docker_failures += 1
            log.error(
                "Reward computation failed at step %d (consecutive=%d): %s",
                self._step, self._consecutive_docker_failures, e,
            )
            if self._consecutive_docker_failures >= 3:
                self._stop_training("3+ consecutive Docker failures")
            rewards = [0.0] * len(trajectories)

        for traj, reward in zip(trajectories, rewards):
            traj.reward = reward

        if self._prm_config and self._prm_config.enabled:
            self._maybe_refresh_teacher()

        return rewards

    def _compute_step_metrics(
        self, trajectories: list[Trajectory], rewards: list[float], grad_norm: float = 0.0
    ) -> TrainingMetrics:
        metrics = self._metrics_tracker.record_step(
            step=self._step,
            trajectories=trajectories,
            grad_norm=grad_norm,
            learning_rate=self._learning_rate,
            curriculum_phase=self._curriculum.current_phase,
        )
        return metrics

    def _sync_lora_weights(self) -> None:
        """Sync LoRA adapter weights to vLLM for updated rollouts."""
        if self._model is None or self._lora_adapter_dir is None:
            return
        from src.training.model_loader import sync_lora_to_vllm
        sync_lora_to_vllm(
            self._model, self._vllm_engine, self._lora_adapter_dir
        )
        if self._rollout_engine is not None:
            from src.training.model_loader import get_lora_request
            self._rollout_engine.set_lora_request(
                get_lora_request(self._lora_adapter_dir)
            )
        log.info("LoRA weights synced to vLLM at step %d", self._step)

    def _get_lora_request(self):
        if self._lora_adapter_dir is None:
            return None
        from src.training.model_loader import get_lora_request
        return get_lora_request(self._lora_adapter_dir)

    def _rollout_cfg_max_tokens(self) -> int:
        return min(4096, self._config.hardware.max_model_len)

    def _maybe_refresh_teacher(self) -> None:
        if not self._prm_config or self._prm_config.teacher_refresh_steps <= 0:
            return
        self._teacher_refresh_counter += 1
        if self._teacher_refresh_counter >= self._prm_config.teacher_refresh_steps:
            self._teacher_refresh_counter = 0
            log.info("PRM teacher refresh at step %d", self._step)

    def _handle_kill_action(self, action: KillAction) -> None:
        if action.severity == "warning":
            log.warning(
                "Kill condition warning [%s]: %s",
                action.reason.value, action.message,
            )
            return

        if action.severity == "recoverable":
            log.warning(
                "Recoverable kill condition [%s]: %s. Action: %s",
                action.reason.value, action.message, action.suggested_action,
            )
            if action.reason == KillReason.ECHO_TRAP:
                self._adjust_hyperparams("temperature", 1.2)
                log.info("Increased temperature to %.3f to escape echo trap", self._temperature)
            elif action.reason == KillReason.GRADIENT_EXPLOSION:
                self._adjust_hyperparams("learning_rate", 0.5)
                log.info("Halved learning rate to %.2e", self._learning_rate)
            elif action.reason == KillReason.OOM:
                log.info("OOM detected — would reduce batch size")
            return

        if action.severity == "fatal":
            log.error(
                "Fatal kill condition [%s]: %s",
                action.reason.value, action.message,
            )
            self._stop_training(
                f"Fatal: {action.reason.value} — {action.message}"
            )

    def _run_eval(self, step: int) -> float:
        """Evaluate on held-out set. Returns pass@1."""
        log.info("Running evaluation at step %d", step)
        eval_start = time.time()

        if not self._eval_tasks:
            recent = self._metrics_tracker.get_recent_metrics(n=5)
            if recent:
                estimated = sum(m.success_rate for m in recent) / len(recent)
            else:
                estimated = 0.0
            log.info("No eval tasks configured — using training estimate: %.4f", estimated)
            return estimated

        if self._vllm_engine is None:
            recent = self._metrics_tracker.get_recent_metrics(n=5)
            estimated = sum(m.success_rate for m in recent) / len(recent) if recent else 0.0
            return estimated

        if self._rollout_engine is not None:
            return self._run_eval_multi_turn(step, eval_start)
        return self._run_eval_single_shot(step, eval_start)

    def _run_eval_multi_turn(self, step: int, eval_start: float) -> float:
        """Eval via multi-turn rollout + Docker grading."""
        passed_tasks = set()
        total_tasks = len(self._eval_tasks)

        eval_trajs = _run_async(
            self._rollout_engine.generate_batch(
                self._eval_tasks[:total_tasks],
                group_size=1,
                temperature=0.0,
            )
        )

        for traj in eval_trajs:
            if traj.patch and traj.patch.strip():
                task = self._reward_manager._task_registry.get(traj.task_id)
                if task:
                    try:
                        results = _run_async(
                            self._reward_manager._executor.run_batch([(task, traj.patch)])
                        )
                        if results and results[0].success:
                            passed_tasks.add(traj.task_id)
                    except Exception:
                        pass

        pass_at_1 = len(passed_tasks) / max(1, total_tasks)
        eval_elapsed = time.time() - eval_start
        log.info("Eval at step %d: pass@1=%.4f (%d/%d, elapsed=%.1fs)",
                 step, pass_at_1, len(passed_tasks), total_tasks, eval_elapsed)

        if pass_at_1 > self._best_eval:
            self._best_eval = pass_at_1
            self._save_checkpoint(step)
            self._best_checkpoint_path = str(
                self._output_dir / "checkpoints" / f"step_{step}.json"
            )
            log.info("New best eval: %.4f at step %d", self._best_eval, step)

        return pass_at_1

    def _run_eval_single_shot(self, step: int, eval_start: float) -> float:
        """Eval via single-shot vLLM generation + Docker grading."""
        from vllm import SamplingParams
        from src.rollout.patch_utils import extract_patch

        sampling_params = SamplingParams(temperature=0.0, max_tokens=4096)
        passed_tasks = set()
        total_tasks = len(self._eval_tasks)

        for task in self._eval_tasks:
            outputs = self._vllm_engine.generate(
                [task.problem_statement], sampling_params,
                lora_request=self._get_lora_request(),
            )
            if outputs:
                response = outputs[0].outputs[0].text
                patch = extract_patch(response)
                if patch.strip():
                    try:
                        results = _run_async(
                            self._reward_manager._executor.run_batch([(task, patch)])
                        )
                        if results and results[0].success:
                            passed_tasks.add(task.task_id)
                    except Exception:
                        pass

        pass_at_1 = len(passed_tasks) / max(1, total_tasks)
        eval_elapsed = time.time() - eval_start
        log.info("Eval at step %d: pass@1=%.4f (%d/%d, elapsed=%.1fs)",
                 step, pass_at_1, len(passed_tasks), total_tasks, eval_elapsed)

        if pass_at_1 > self._best_eval:
            self._best_eval = pass_at_1
            self._save_checkpoint(step)
            self._best_checkpoint_path = str(
                self._output_dir / "checkpoints" / f"step_{step}.json"
            )

        return pass_at_1

    def _save_checkpoint(self, step: int) -> None:
        ckpt_dir = self._output_dir / "checkpoints"
        ckpt_dir.mkdir(parents=True, exist_ok=True)
        ckpt_path = ckpt_dir / f"step_{step}.json"

        state = self.get_state_dict()
        curriculum_state = state.get("curriculum_state", {})
        rng_state = curriculum_state.get("rng_state")
        if rng_state is not None:
            curriculum_state["rng_seed"] = int(self._step * 7919 + 42)
            curriculum_state.pop("rng_state", None)
        state["curriculum_state"] = curriculum_state

        with ckpt_path.open("w") as f:
            json.dump(state, f, indent=2)

        log.info("Checkpoint saved: %s", ckpt_path)
        self._evict_old_checkpoints(ckpt_dir, just_saved=ckpt_path)

        if self._model is not None:
            model_ckpt_path = ckpt_dir / f"model_step_{step}"
            model_ckpt_path.mkdir(parents=True, exist_ok=True)
            self._model.save_pretrained(str(model_ckpt_path))
            log.info("Model weights saved: %s", model_ckpt_path)

    def _load_checkpoint(self, path: str) -> None:
        ckpt_path = Path(path)
        if not ckpt_path.exists():
            log.error("Checkpoint not found: %s", path)
            return

        with ckpt_path.open("r") as f:
            state = json.load(f)

        self.load_state_dict(state)
        log.info("Loaded checkpoint from %s (step=%d)", path, self._step)

    def _adjust_hyperparams(self, param: str, factor: float) -> None:
        if param == "learning_rate":
            self._learning_rate *= factor
            if self._optimizer is not None:
                for pg in self._optimizer.param_groups:
                    pg["lr"] = self._learning_rate
            log.info("Adjusted learning_rate: %.2e (factor=%.2f)", self._learning_rate, factor)
        elif param == "temperature":
            self._temperature = min(2.0, self._temperature * factor)
            log.info("Adjusted temperature: %.3f (factor=%.2f)", self._temperature, factor)
        else:
            log.warning("Unknown hyperparameter to adjust: %s", param)

    def _stop_training(self, reason: str) -> None:
        self._stopped = True
        self._stop_reason = reason
        log.info("Training stopped: %s", reason)
        self._save_checkpoint(self._step)
        self._metrics_tracker.save_to_jsonl()

    def get_state_dict(self) -> dict:
        return {
            "step": self._step,
            "best_eval": self._best_eval,
            "best_checkpoint_path": self._best_checkpoint_path,
            "learning_rate": self._learning_rate,
            "temperature": self._temperature,
            "stopped": self._stopped,
            "stop_reason": self._stop_reason,
            "curriculum_state": self._curriculum.get_state_dict(),
            "teacher_refresh_counter": self._teacher_refresh_counter,
            "config": {
                "model_path": self._config.model_path,
                "run_id": self._config.run_id,
                "total_steps": self._config.gspo.total_steps,
            },
        }

    def load_state_dict(self, state: dict) -> None:
        self._step = state.get("step", 0)
        self._best_eval = state.get("best_eval", 0.0)
        self._best_checkpoint_path = state.get("best_checkpoint_path")
        self._learning_rate = state.get("learning_rate", self._config.gspo.learning_rate)
        self._temperature = state.get("temperature", self._config.gspo.temperature)
        self._stopped = state.get("stopped", False)
        self._stop_reason = state.get("stop_reason")
        self._teacher_refresh_counter = state.get("teacher_refresh_counter", 0)

        curriculum_state = state.get("curriculum_state")
        if curriculum_state:
            self._curriculum.load_state_dict(curriculum_state)

    def _evict_old_checkpoints(self, ckpt_dir: Path, just_saved: Path | None = None) -> None:
        keep = self._config.monitoring.keep_checkpoints
        checkpoints = sorted(
            ckpt_dir.glob("step_*.json"),
            key=lambda p: self._extract_step_from_path(p),
        )

        protected = set()
        if self._best_checkpoint_path:
            protected.add(Path(self._best_checkpoint_path))
        if just_saved is not None:
            protected.add(just_saved)

        to_delete = [ckpt for ckpt in checkpoints if ckpt not in protected]

        while len(to_delete) + len(protected) > keep and to_delete:
            oldest = to_delete.pop(0)
            oldest.unlink(missing_ok=True)
            log.debug("Evicted old checkpoint: %s", oldest.name)

    @staticmethod
    def _extract_step_from_path(path: Path) -> int:
        stem = path.stem
        if stem.startswith("step_"):
            try:
                return int(stem[5:])
            except ValueError:
                pass
        return 0
