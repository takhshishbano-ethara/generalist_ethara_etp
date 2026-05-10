"""Stage 1: Rejection-sampling fine-tuning warmup."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path

from src.core.config import MiloConfig, RFTConfig
from src.core.schemas import TaskSpec, Trajectory, Turn
from src.data.dataset import MiloDataset
from src.rollout.docker_executor import DockerExecutor
from src.rollout.patch_utils import extract_patch

log = logging.getLogger(__name__)

TEACHER_SYSTEM_PROMPT = (
    "You are a senior software engineer solving a bug in a codebase. "
    "You will receive a problem statement describing a failing test or issue. "
    "Analyze the problem and produce a fix as a unified diff patch. "
    "Wrap your final patch in <submit>...</submit> tags. "
    "The patch should be a valid unified diff that can be applied with `git apply`."
)


class RFTWarmup:
    """Rejection-sampling fine-tuning: generate, filter, SFT on passing trajectories."""

    def __init__(self, config: MiloConfig, executor: DockerExecutor):
        self._config = config
        self._rft_config = config.rft
        self._executor = executor
        self._output_dir = Path(config.output_dir) / config.run_id / "rft"
        self._output_dir.mkdir(parents=True, exist_ok=True)

    def run(self, dataset: MiloDataset) -> str:
        run_start = time.time()
        log.info("Starting RFT warmup pipeline")

        easy_dataset = dataset.filter_by_difficulty(["easy"])
        tasks = easy_dataset.tasks
        log.info("Filtered to %d easy tasks for RFT", len(tasks))

        if not tasks:
            log.error("No easy tasks found — cannot run RFT")
            return str(self._output_dir / "checkpoint")

        log.info("Step 1/6: Generating trajectories (%d per task)", self._rft_config.n_per_task)
        gen_start = time.time()
        trajectories = self.generate_trajectories(tasks)
        log.info(
            "Generated %d trajectories in %.1fs",
            len(trajectories), time.time() - gen_start,
        )

        log.info("Step 2/6: Grading trajectories via Docker")
        grade_start = time.time()
        task_map = {t.task_id: t for t in tasks}
        graded = self.grade_trajectories(trajectories, task_map)
        log.info("Graded %d trajectories in %.1fs", len(graded), time.time() - grade_start)

        log.info("Step 3/6: Filtering to passing trajectories")
        passing = self.filter_passing(graded)
        total_attempts = len(graded)
        log.info(
            "Passing: %d/%d (%.1f%%)",
            len(passing), total_attempts,
            100.0 * len(passing) / max(1, total_attempts),
        )

        self._save_intermediate(graded, "all_graded.jsonl")
        self._save_intermediate(passing, "passing_only.jsonl")

        log.info("Step 4/6: Formatting SFT data")
        sft_data = self.format_sft_data(passing)
        log.info("Formatted %d SFT examples", len(sft_data))

        log.info("Step 5/6: Running SFT training")
        checkpoint_path = self.run_sft(sft_data)
        log.info("SFT complete: checkpoint at %s", checkpoint_path)

        log.info("Step 6/6: Gate check")
        eval_tasks = tasks[:min(50, len(tasks))]
        pass_rate = self.gate_check(checkpoint_path, eval_tasks)

        total_elapsed = time.time() - run_start
        log.info(
            "RFT pipeline complete: pass@1=%.4f, gate_threshold=%.4f, elapsed=%.1fs",
            pass_rate, self._rft_config.gate_threshold, total_elapsed,
        )

        if pass_rate < self._rft_config.gate_threshold:
            log.warning(
                "Gate check FAILED: pass@1=%.4f < threshold=%.4f. "
                "Consider using teacher model fallback.",
                pass_rate, self._rft_config.gate_threshold,
            )

        return checkpoint_path

    def generate_trajectories(self, tasks: list[TaskSpec]) -> list[Trajectory]:
        n_per_task = self._rft_config.n_per_task
        trajectories: list[Trajectory] = []

        from src.prm.bedrock_scorer import BedrockTeacherClient

        teacher = BedrockTeacherClient(
            model_arn=os.environ.get("BEDROCK_MODEL_ARN", ""),
            region=os.environ.get("BEDROCK_REGION", "ap-south-1"),
            temperature=self._rft_config.temperature,
            max_tokens=self._rft_config.max_tokens,
        )

        async def _generate_for_task(task: TaskSpec) -> list[Trajectory]:
            responses = await teacher.generate_batch(
                problem_statement=task.problem_statement,
                n=n_per_task,
                system_prompt=TEACHER_SYSTEM_PROMPT,
                max_concurrent=8,
            )
            task_trajs: list[Trajectory] = []
            for resp in responses:
                patch = extract_patch(resp)
                traj = Trajectory(
                    task_id=task.task_id,
                    turns=[
                        Turn(
                            role="user",
                            content=task.problem_statement,
                            timestamp=time.time(),
                        ),
                        Turn(
                            role="assistant",
                            content=resp,
                            timestamp=time.time(),
                        ),
                    ],
                    patch=patch,
                    reward=0.0,
                    mask=True,
                    episode_length=2,
                    curriculum_phase=0,
                    training_step=0,
                )
                traj.raw_response = resp
                task_trajs.append(traj)
            return task_trajs

        async def _run_all():
            semaphore = asyncio.Semaphore(4)

            async def _bounded(task: TaskSpec):
                async with semaphore:
                    return await _generate_for_task(task)

            results = await asyncio.gather(*[_bounded(t) for t in tasks])
            return [traj for batch in results for traj in batch]

        try:
            loop: asyncio.AbstractEventLoop | None = None
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                pass

            if loop and loop.is_running():
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor(1) as pool:
                    trajectories = pool.submit(asyncio.run, _run_all()).result()
            else:
                trajectories = asyncio.run(_run_all())
        except Exception as e:
            log.error("Bedrock trajectory generation failed: %s", e)
            trajectories = []

        log.info(
            "Generated %d trajectories from Bedrock Claude for %d tasks",
            len(trajectories), len(tasks),
        )
        return trajectories

    def grade_trajectories(
        self, trajectories: list[Trajectory], tasks: dict[str, TaskSpec]
    ) -> list[Trajectory]:
        items: list[tuple[TaskSpec, str]] = []
        valid_indices: list[int] = []

        for i, traj in enumerate(trajectories):
            task = tasks.get(traj.task_id)
            if task is None:
                continue
            patch = traj.patch if traj.patch else ""
            if not patch.strip():
                continue
            items.append((task, patch))
            valid_indices.append(i)

        if not items:
            log.warning("No valid patches to grade — all trajectories get reward=0.0")
            return trajectories

        try:
            loop: asyncio.AbstractEventLoop | None = None
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                pass

            if loop and loop.is_running():
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor(1) as pool:
                    results = pool.submit(
                        asyncio.run, self._executor.run_batch(items)
                    ).result()
            else:
                results = asyncio.run(self._executor.run_batch(items))
        except Exception as e:
            log.error("Docker grading failed: %s", e)
            return trajectories

        for batch_idx, docker_result in zip(valid_indices, results):
            if docker_result.success:
                trajectories[batch_idx].reward = 1.0
            else:
                trajectories[batch_idx].reward = 0.0
            if docker_result.error:
                trajectories[batch_idx].error = docker_result.error

        return trajectories

    def filter_passing(self, trajectories: list[Trajectory]) -> list[Trajectory]:
        return [t for t in trajectories if t.reward == 1.0]

    def format_sft_data(self, trajectories: list[Trajectory]) -> list[dict]:
        sft_examples: list[dict] = []

        for traj in trajectories:
            if not traj.turns:
                continue

            messages: list[dict[str, str]] = []
            for turn in traj.turns:
                messages.append({"role": turn.role, "content": turn.content})

            sft_examples.append({"messages": messages, "task_id": traj.task_id})

        return sft_examples

    def run_sft(self, sft_data: list[dict]) -> str:
        sft_data_path = self._output_dir / "sft_data.jsonl"
        with sft_data_path.open("w") as f:
            for example in sft_data:
                f.write(json.dumps(example) + "\n")

        log.info("SFT data written to %s (%d examples)", sft_data_path, len(sft_data))

        checkpoint_path = str(self._output_dir / "checkpoint")
        Path(checkpoint_path).mkdir(parents=True, exist_ok=True)

        try:
            from transformers import (
                AutoModelForCausalLM,
                AutoTokenizer,
                TrainingArguments,
                Trainer as HFTrainer,
            )
            from peft import LoraConfig, get_peft_model, TaskType
            from datasets import Dataset
            import torch

            tokenizer = AutoTokenizer.from_pretrained(self._config.model_path)
            if tokenizer.pad_token is None:
                tokenizer.pad_token = tokenizer.eos_token

            model = AutoModelForCausalLM.from_pretrained(
                self._config.model_path,
                torch_dtype=torch.bfloat16,
                device_map="auto",
            )

            lora_config = LoraConfig(
                task_type=TaskType.CAUSAL_LM,
                r=self._config.lora.rank,
                lora_alpha=self._config.lora.alpha,
                lora_dropout=self._config.lora.dropout,
                target_modules=self._config.lora.target_modules.split(","),
            )
            model = get_peft_model(model, lora_config)
            model.print_trainable_parameters()

            def tokenize_messages(examples):
                texts = []
                for msgs in examples["messages"]:
                    text = tokenizer.apply_chat_template(msgs, tokenize=False)
                    texts.append(text)
                encodings = tokenizer(
                    texts, truncation=True, max_length=4096, padding="max_length"
                )
                encodings["labels"] = encodings["input_ids"].copy()
                return encodings

            dataset = Dataset.from_list(sft_data)
            tokenized = dataset.map(tokenize_messages, batched=True, remove_columns=dataset.column_names)

            training_args = TrainingArguments(
                output_dir=checkpoint_path,
                num_train_epochs=self._rft_config.sft_epochs,
                per_device_train_batch_size=self._rft_config.sft_batch_size,
                learning_rate=self._rft_config.sft_lr,
                warmup_ratio=0.1,
                logging_steps=10,
                save_strategy="epoch",
                bf16=True,
                gradient_accumulation_steps=4,
                report_to="none",
            )

            trainer = HFTrainer(
                model=model,
                args=training_args,
                train_dataset=tokenized,
            )
            trainer.train()
            model.save_pretrained(checkpoint_path)
            tokenizer.save_pretrained(checkpoint_path)
            log.info("SFT training complete, checkpoint saved to %s", checkpoint_path)

        except ImportError as e:
            log.warning(
                "SFT dependencies not available (%s). Writing config only (no training).", e
            )
            training_config = {
                "model_path": self._config.model_path,
                "sft_data_path": str(sft_data_path),
                "learning_rate": self._rft_config.sft_lr,
                "epochs": self._rft_config.sft_epochs,
                "batch_size": self._rft_config.sft_batch_size,
                "lora_rank": self._config.lora.rank,
                "lora_alpha": self._config.lora.alpha,
                "num_examples": len(sft_data),
            }
            config_path = self._output_dir / "sft_training_config.json"
            with config_path.open("w") as f:
                json.dump(training_config, f, indent=2)

        return checkpoint_path

    def gate_check(self, checkpoint_path: str, eval_tasks: list[TaskSpec]) -> float:
        log.info(
            "Gate check: evaluating student checkpoint %s on %d tasks",
            checkpoint_path, len(eval_tasks),
        )

        trajectories = self._generate_from_student(checkpoint_path, eval_tasks)
        task_map = {t.task_id: t for t in eval_tasks}
        graded = self.grade_trajectories(trajectories, task_map)

        pass_rate = self._estimate_pass_rate(graded)

        log.info("Gate check result: pass@1=%.4f (threshold=%.4f)", pass_rate,
                 self._rft_config.gate_threshold)

        if pass_rate < self._rft_config.gate_threshold:
            log.warning(
                "Gate check below threshold: %.4f < %.4f",
                pass_rate, self._rft_config.gate_threshold,
            )

        return pass_rate

    def _generate_from_student(
        self, checkpoint_path: str, tasks: list[TaskSpec]
    ) -> list[Trajectory]:
        try:
            from transformers import AutoModelForCausalLM, AutoTokenizer
            from peft import PeftModel
            import torch

            tokenizer = AutoTokenizer.from_pretrained(checkpoint_path)
            base_model = AutoModelForCausalLM.from_pretrained(
                self._config.model_path,
                torch_dtype=torch.bfloat16,
                device_map="auto",
            )
            model = PeftModel.from_pretrained(base_model, checkpoint_path)
            model.eval()

            trajectories: list[Trajectory] = []
            for task in tasks:
                inputs = tokenizer(
                    task.problem_statement, return_tensors="pt", truncation=True, max_length=4096
                ).to(model.device)
                with torch.no_grad():
                    output_ids = model.generate(
                        **inputs,
                        max_new_tokens=self._rft_config.max_tokens,
                        temperature=0.0,
                        do_sample=False,
                    )
                response = tokenizer.decode(output_ids[0][inputs["input_ids"].shape[1]:], skip_special_tokens=True)
                patch = extract_patch(response)
                traj = Trajectory(
                    task_id=task.task_id,
                    turns=[
                        Turn(role="user", content=task.problem_statement, timestamp=time.time()),
                        Turn(role="assistant", content=response, timestamp=time.time()),
                    ],
                    patch=patch,
                    reward=0.0,
                    mask=True,
                    episode_length=2,
                    curriculum_phase=0,
                    training_step=0,
                )
                traj.raw_response = response
                trajectories.append(traj)
            return trajectories

        except ImportError as e:
            log.warning("Cannot load student for gate_check (%s), falling back to teacher", e)
            return self.generate_trajectories(tasks)

    def _estimate_pass_rate(
        self, trajectories: list[Trajectory]
    ) -> float:
        """Compute pass@1 as fraction of unique tasks with at least one passing trajectory."""
        if not trajectories:
            return 0.0
        task_passed: dict[str, bool] = {}
        for t in trajectories:
            if t.task_id not in task_passed:
                task_passed[t.task_id] = False
            if t.reward == 1.0:
                task_passed[t.task_id] = True
        if not task_passed:
            return 0.0
        return sum(1 for v in task_passed.values() if v) / len(task_passed)

    def _save_intermediate(self, trajectories: list[Trajectory], filename: str) -> None:
        path = self._output_dir / filename
        try:
            with path.open("w") as f:
                for traj in trajectories:
                    f.write(traj.model_dump_json() + "\n")
            log.debug("Saved %d trajectories to %s", len(trajectories), path)
        except Exception as e:
            log.error("Failed to save intermediate results to %s: %s", path, e)
