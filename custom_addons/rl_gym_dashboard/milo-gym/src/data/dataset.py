"""Dataset loading, stratified splitting, and parquet conversion."""
from __future__ import annotations

import json
import random
from collections import Counter
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from src.core.schemas import TaskSpec


class MiloDataset:
    def __init__(self, tasks: list[TaskSpec] | None = None):
        self._tasks: list[TaskSpec] = tasks or []

    @classmethod
    def from_jsonl(cls, path: str | Path) -> MiloDataset:
        path = Path(path)
        tasks: list[TaskSpec] = []
        with path.open("r") as f:
            for line in f:
                line = line.strip()
                if line:
                    tasks.append(TaskSpec.model_validate_json(line))
        return cls(tasks)

    @classmethod
    def from_parquet(cls, path: str | Path) -> MiloDataset:
        path = Path(path)
        table = pq.read_table(path)
        tasks: list[TaskSpec] = []
        for i in range(table.num_rows):
            extra_info = table.column("extra_info")[i].as_py()
            data = json.loads(extra_info) if isinstance(extra_info, str) else extra_info
            tasks.append(TaskSpec.model_validate(data))
        return cls(tasks)

    def split_train_eval(
        self,
        eval_size: int = 200,
        seed: int = 42,
        stratify_by: list[str] | None = None,
    ) -> tuple[MiloDataset, MiloDataset]:
        rng = random.Random(seed)

        if not stratify_by:
            shuffled = list(self._tasks)
            rng.shuffle(shuffled)
            eval_size = min(eval_size, len(shuffled))
            return MiloDataset(shuffled[eval_size:]), MiloDataset(shuffled[:eval_size])

        buckets: dict[tuple, list[TaskSpec]] = {}
        for task in self._tasks:
            key = tuple(getattr(task, attr) for attr in stratify_by)
            buckets.setdefault(key, []).append(task)

        train_tasks: list[TaskSpec] = []
        eval_tasks: list[TaskSpec] = []
        total = len(self._tasks)

        for key, bucket in buckets.items():
            rng.shuffle(bucket)
            bucket_eval_size = max(1, round(len(bucket) / total * eval_size))
            bucket_eval_size = min(bucket_eval_size, len(bucket))
            eval_tasks.extend(bucket[:bucket_eval_size])
            train_tasks.extend(bucket[bucket_eval_size:])

        return MiloDataset(train_tasks), MiloDataset(eval_tasks)

    def filter_by_difficulty(self, difficulties: list[str]) -> MiloDataset:
        filtered = [t for t in self._tasks if t.difficulty in difficulties]
        return MiloDataset(filtered)

    def filter_by_language(self, languages: list[str]) -> MiloDataset:
        filtered = [t for t in self._tasks if t.language in languages]
        return MiloDataset(filtered)

    def sample_batch(
        self,
        batch_size: int,
        difficulty_weights: dict[str, float] | None = None,
        seed: int | None = None,
    ) -> list[TaskSpec]:
        rng = random.Random(seed)

        if not difficulty_weights:
            return rng.sample(self._tasks, min(batch_size, len(self._tasks)))

        by_difficulty: dict[str, list[TaskSpec]] = {}
        for task in self._tasks:
            by_difficulty.setdefault(task.difficulty, []).append(task)

        total_weight = sum(difficulty_weights.values())
        sampled: list[TaskSpec] = []

        for diff, weight in difficulty_weights.items():
            pool = by_difficulty.get(diff, [])
            if not pool:
                continue
            n = max(1, round(batch_size * weight / total_weight))
            n = min(n, len(pool))
            sampled.extend(rng.sample(pool, n))

        rng.shuffle(sampled)
        return sampled[:batch_size]

    def to_verl_parquet(self, output_path: str | Path) -> Path:
        """Convert to verl-compatible Parquet: uid, data_source, prompt, ground_truth, extra_info."""
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        uids: list[str] = []
        data_sources: list[str] = []
        prompts: list[str] = []
        ground_truths: list[str] = []
        extra_infos: list[str] = []

        for task in self._tasks:
            uids.append(task.task_id)
            data_sources.append("milo-rl")
            prompts.append(task.problem_statement)
            ground_truths.append(task.fix_patch)
            extra_infos.append(task.model_dump_json())

        table = pa.table({
            "uid": pa.array(uids, type=pa.string()),
            "data_source": pa.array(data_sources, type=pa.string()),
            "prompt": pa.array(prompts, type=pa.string()),
            "ground_truth": pa.array(ground_truths, type=pa.string()),
            "extra_info": pa.array(extra_infos, type=pa.string()),
        })
        pq.write_table(table, output_path)
        return output_path

    def to_jsonl(self, output_path: str | Path) -> Path:
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("w") as f:
            for task in self._tasks:
                f.write(task.model_dump_json() + "\n")
        return output_path

    @property
    def tasks(self) -> list[TaskSpec]:
        return self._tasks

    def __len__(self) -> int:
        return len(self._tasks)

    def difficulty_distribution(self) -> dict[str, int]:
        return dict(Counter(t.difficulty for t in self._tasks))

    def language_distribution(self) -> dict[str, int]:
        return dict(Counter(t.language for t in self._tasks))
