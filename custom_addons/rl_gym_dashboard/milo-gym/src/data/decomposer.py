"""Decompose MILO-bench instances into per-PR sub-tasks."""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from src.core.schemas import TaskSpec

log = logging.getLogger(__name__)

_EVAL_SCRIPT_TEMPLATE = """#!/bin/bash
set -e
cd /repo

F2P_TOTAL=0
F2P_PASSED=0
{f2p_test_commands}

P2P_TOTAL=0
P2P_PASSED=0
{p2p_test_commands}

echo "F2P: $F2P_PASSED/$F2P_TOTAL"
echo "P2P: $P2P_PASSED/$P2P_TOTAL"
"""


@dataclass
class RawPR:
    repo: str
    pr_number: int
    base_commit: str
    merge_commit: str
    title: str
    body: str
    diff: str
    test_files: list[str]
    source_files: list[str]
    language: str


class MILODecomposer:
    def __init__(
        self,
        milo_data_dir: str | Path,
        languages: list[str] | None = None,
        docker_registry: str = "localhost:5000",
    ):
        self._data_dir = Path(milo_data_dir)
        self._languages = languages or ["python", "go"]
        self._registry = docker_registry

    def load_instances(self) -> list[dict]:
        instances: list[dict] = []
        for json_path in sorted(self._data_dir.glob("*.json")):
            try:
                data = json.loads(json_path.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    instances.extend(data)
                else:
                    instances.append(data)
            except (json.JSONDecodeError, OSError) as e:
                log.warning("Skipping %s: %s", json_path.name, e)
        log.info("Loaded %d instances from %s", len(instances), self._data_dir)
        return instances

    def filter_by_language(self, instances: list[dict]) -> list[dict]:
        return [
            inst for inst in instances
            if inst.get("language", "").lower() in self._languages
        ]

    def extract_prs(self, instance: dict) -> list[RawPR]:
        prs: list[RawPR] = []
        for pr_data in instance.get("prs", []):
            prs.append(RawPR(
                repo=instance.get("repo", ""),
                pr_number=pr_data.get("pr_number", 0),
                base_commit=pr_data.get("base_commit", ""),
                merge_commit=pr_data.get("merge_commit", ""),
                title=pr_data.get("title", ""),
                body=pr_data.get("body", ""),
                diff=pr_data.get("diff", ""),
                test_files=pr_data.get("test_files", []),
                source_files=pr_data.get("source_files", []),
                language=instance.get("language", "python"),
            ))
        return prs

    def pr_to_task_spec(
        self, pr: RawPR, instance_id: str, pr_index: int
    ) -> TaskSpec:
        task_id = f"{instance_id}__pr{pr_index}__{pr.pr_number}"
        docker_image = (
            f"{self._registry}/milo-{pr.repo.replace('/', '-')}:{pr.base_commit[:12]}"
        )
        eval_script = self.generate_evaluation_script_for_pr(pr)

        if pr.language not in ("python", "go"):
            raise ValueError(f"Unsupported language: {pr.language}")

        return TaskSpec(
            task_id=task_id,
            repo=pr.repo,
            language=pr.language,
            base_commit=pr.base_commit,
            problem_statement=f"{pr.title}\n\n{pr.body}",
            test_patch=self._extract_test_diff(pr),
            fix_patch=pr.diff,
            docker_image=docker_image,
            evaluation_script=eval_script,
            difficulty=self._estimate_difficulty(pr),
            timeout_seconds=1800,
        )

    def identify_dependencies(self, prs: list[RawPR]) -> dict[int, list[int]]:
        deps: dict[int, list[int]] = {}
        for i, pr in enumerate(prs):
            deps[i] = []
            current_sources = set(pr.source_files)
            for j in range(i):
                prior_sources = set(prs[j].source_files)
                if current_sources & prior_sources:
                    deps[i].append(j)
        return deps

    def build_docker_image(self, task: TaskSpec) -> str:
        dockerfile_content = (
            f"FROM python:3.10-slim\n"
            f"RUN apt-get update && apt-get install -y git && rm -rf /var/lib/apt/lists/*\n"
            f"RUN git clone https://github.com/{task.repo}.git /repo\n"
            f"WORKDIR /repo\n"
            f"RUN git checkout {task.base_commit}\n"
            f"RUN if [ -f requirements.txt ]; then pip install -r requirements.txt; fi\n"
        )

        build_dir = Path("/tmp/milo-docker") / task.task_id
        build_dir.mkdir(parents=True, exist_ok=True)
        dockerfile_path = build_dir / "Dockerfile"
        dockerfile_path.write_text(dockerfile_content)

        try:
            subprocess.run(
                ["docker", "build", "-t", task.docker_image, str(build_dir)],
                check=True,
                capture_output=True,
                text=True,
                timeout=600,
            )
            log.info("Built image %s", task.docker_image)
        except subprocess.CalledProcessError as e:
            log.error("Docker build failed for %s: %s", task.task_id, e.stderr)
            raise
        except subprocess.TimeoutExpired:
            log.error("Docker build timed out for %s", task.task_id)
            raise

        return task.docker_image

    def generate_evaluation_script(self, task: TaskSpec) -> str:
        return self._build_eval_script(task.test_patch, task.language)

    def generate_evaluation_script_for_pr(self, pr: RawPR) -> str:
        test_patch = self._extract_test_diff(pr)
        return self._build_eval_script(test_patch, pr.language)

    def decompose_all(self) -> list[TaskSpec]:
        """Full pipeline: load -> filter -> extract -> convert -> build images."""
        instances = self.load_instances()
        filtered = self.filter_by_language(instances)
        log.info(
            "Filtered to %d instances (languages: %s)",
            len(filtered), self._languages,
        )

        tasks: list[TaskSpec] = []
        for instance in filtered:
            instance_id = instance.get("instance_id", instance.get("id", "unknown"))
            prs = self.extract_prs(instance)
            for idx, pr in enumerate(prs):
                task = self.pr_to_task_spec(pr, instance_id, idx)
                tasks.append(task)

        log.info("Decomposed into %d tasks", len(tasks))
        return tasks

    def _extract_test_diff(self, pr: RawPR) -> str:
        lines: list[str] = []
        in_test_file = False
        for line in pr.diff.splitlines():
            if line.startswith("diff --git"):
                in_test_file = any(
                    tf in line for tf in pr.test_files
                )
            if in_test_file:
                lines.append(line)
        return "\n".join(lines)

    def _estimate_difficulty(self, pr: RawPR) -> Literal["easy", "medium", "hard"]:
        num_files = len(pr.source_files)
        diff_lines = len(pr.diff.splitlines())
        if num_files <= 1 and diff_lines < 50:
            return "easy"
        if num_files <= 3 and diff_lines < 200:
            return "medium"
        return "hard"

    def _build_eval_script(self, test_patch: str, language: str) -> str:
        if language == "python":
            f2p_cmds = (
                "for tf in $(grep '^+++ b/' /tmp/test_patch.diff "
                "| sed 's|^+++ b/||'); do\n"
                "  F2P_TOTAL=$((F2P_TOTAL + 1))\n"
                "  if python -m pytest \"$tf\" -x -q 2>/dev/null; then\n"
                "    F2P_PASSED=$((F2P_PASSED + 1))\n"
                "  fi\n"
                "done"
            )
            p2p_cmds = (
                "for tf in $(find . -name 'test_*.py' -not -path './.git/*' "
                "| head -20); do\n"
                "  P2P_TOTAL=$((P2P_TOTAL + 1))\n"
                "  if python -m pytest \"$tf\" -x -q 2>/dev/null; then\n"
                "    P2P_PASSED=$((P2P_PASSED + 1))\n"
                "  fi\n"
                "done"
            )
        elif language == "go":
            f2p_cmds = (
                "for tf in $(grep '^+++ b/' /tmp/test_patch.diff "
                "| sed 's|^+++ b/||' | xargs -I{} dirname {}); do\n"
                "  F2P_TOTAL=$((F2P_TOTAL + 1))\n"
                "  if go test ./$tf/... 2>/dev/null; then\n"
                "    F2P_PASSED=$((F2P_PASSED + 1))\n"
                "  fi\n"
                "done"
            )
            p2p_cmds = (
                "for pkg in $(go list ./... 2>/dev/null | head -20); do\n"
                "  P2P_TOTAL=$((P2P_TOTAL + 1))\n"
                "  if go test \"$pkg\" 2>/dev/null; then\n"
                "    P2P_PASSED=$((P2P_PASSED + 1))\n"
                "  fi\n"
                "done"
            )
        else:
            f2p_cmds = "F2P_TOTAL=1\nF2P_PASSED=0"
            p2p_cmds = "P2P_TOTAL=1\nP2P_PASSED=0"

        script = _EVAL_SCRIPT_TEMPLATE.format(
            f2p_test_commands=f2p_cmds,
            p2p_test_commands=p2p_cmds,
        )

        import base64

        encoded_patch = base64.b64encode(test_patch.encode()).decode()
        write_test_patch = (
            f"echo '{encoded_patch}' | base64 -d > /tmp/test_patch.diff\n"
            f"git apply /tmp/test_patch.diff\n"
        )

        return write_test_patch + script
