"""Synthetic data augmentation strategies for MILO-RL tasks."""
from __future__ import annotations

import ast
import asyncio
import copy
import logging
import random
import re
from abc import ABC, abstractmethod

from src.core.schemas import TaskSpec

log = logging.getLogger(__name__)


class AugmentationStrategy(ABC):
    @abstractmethod
    def augment(self, task: TaskSpec) -> list[TaskSpec]: ...

    @abstractmethod
    def expected_yield(self) -> float: ...


class CommitReversionAugmenter(AugmentationStrategy):
    """Partially revert gold_patch (remove 1-2 hunks) to create 'complete this fix' tasks."""

    def __init__(self, min_hunks_to_keep: int = 1, max_hunks_to_remove: int = 2):
        self._min_hunks_to_keep = min_hunks_to_keep
        self._max_hunks_to_remove = max_hunks_to_remove

    def augment(self, task: TaskSpec) -> list[TaskSpec]:
        hunks = self._parse_hunks(task.fix_patch)
        if len(hunks) <= self._min_hunks_to_keep:
            return []

        max_removable = min(self._max_hunks_to_remove, len(hunks) - self._min_hunks_to_keep)
        results: list[TaskSpec] = []

        for n_remove in range(1, max_removable + 1):
            indices_to_remove = random.sample(range(len(hunks)), n_remove)
            removed = [hunks[i] for i in indices_to_remove]
            partial_patch = self._remove_hunks(hunks, set(indices_to_remove))
            if not partial_patch.strip():
                continue

            new_problem = self._generate_problem_statement(task, removed)
            augmented = task.model_copy(
                update={
                    "task_id": f"{task.task_id}__revert_{n_remove}",
                    "problem_statement": new_problem,
                    "fix_patch": partial_patch,
                }
            )
            results.append(augmented)

        return results

    def expected_yield(self) -> float:
        return 1.0

    def _parse_hunks(self, patch: str) -> list[str]:
        parts = re.split(r"(^@@[^\n]*@@[^\n]*\n)", patch, flags=re.MULTILINE)
        hunks: list[str] = []
        i = 1
        while i < len(parts) - 1:
            header = parts[i]
            body = parts[i + 1] if i + 1 < len(parts) else ""
            hunks.append(header + body)
            i += 2
        return hunks

    def _remove_hunks(self, hunks: list[str], indices_to_remove: set[int]) -> str:
        remaining = [h for i, h in enumerate(hunks) if i not in indices_to_remove]
        return "".join(remaining)

    def _generate_problem_statement(self, task: TaskSpec, removed_hunks: list[str]) -> str:
        kept_summary = "Some changes have already been applied."
        removed_summary = "\n".join(
            line for hunk in removed_hunks
            for line in hunk.split("\n")[:3]
        )
        return (
            f"Complete the following partial fix: {task.problem_statement}\n\n"
            f"{kept_summary}\n"
            f"Complete the remaining fix for these areas:\n{removed_summary}"
        )


class ASTMutationAugmenter(AugmentationStrategy):
    """Procedural AST mutations: swap operands, flip conditionals, remove error handling."""

    _COMPARISON_FLIPS: dict[type, type] = {
        ast.Lt: ast.Gt,
        ast.Gt: ast.Lt,
        ast.LtE: ast.GtE,
        ast.GtE: ast.LtE,
        ast.Eq: ast.NotEq,
        ast.NotEq: ast.Eq,
    }

    def __init__(self, mutations_per_task: int = 3, seed: int = 42):
        self._mutations_per_task = mutations_per_task
        self._seed = seed

    def augment(self, task: TaskSpec) -> list[TaskSpec]:
        rng = random.Random(self._seed + hash(task.task_id))
        source = self._extract_source_from_patch(task.fix_patch)
        if not source:
            return []

        mutation_fns = [self._mutate_comparison, self._mutate_boolean, self._remove_error_handling]
        results: list[TaskSpec] = []

        for i in range(self._mutations_per_task):
            fn = rng.choice(mutation_fns)
            mutated = self._apply_mutation(source, fn)
            if mutated is None:
                continue

            fix_patch = self._make_diff(mutated, source)
            augmented = task.model_copy(
                update={
                    "task_id": f"{task.task_id}__ast_mut_{i}",
                    "problem_statement": (
                        f"A bug was introduced in the code. {task.problem_statement}"
                    ),
                    "fix_patch": fix_patch,
                }
            )
            results.append(augmented)

        return results

    def expected_yield(self) -> float:
        return 0.75

    def _extract_source_from_patch(self, patch: str) -> str:
        added_lines: list[str] = []
        for line in patch.split("\n"):
            if line.startswith("+") and not line.startswith("+++"):
                added_lines.append(line[1:])
        return "\n".join(added_lines)

    def _mutate_comparison(self, tree: ast.AST) -> ast.AST | None:
        class ComparisonFlipper(ast.NodeTransformer):
            flipped = False

            def visit_Compare(self, node: ast.Compare) -> ast.Compare:
                if not self.flipped and node.ops:
                    op_type = type(node.ops[0])
                    if op_type in ASTMutationAugmenter._COMPARISON_FLIPS:
                        node.ops[0] = ASTMutationAugmenter._COMPARISON_FLIPS[op_type]()
                        self.flipped = True
                return node

        transformer = ComparisonFlipper()
        new_tree = transformer.visit(copy.deepcopy(tree))
        return new_tree if transformer.flipped else None

    def _mutate_boolean(self, tree: ast.AST) -> ast.AST | None:
        class BooleanFlipper(ast.NodeTransformer):
            flipped = False

            def visit_BoolOp(self, node: ast.BoolOp) -> ast.BoolOp:
                if not self.flipped:
                    if isinstance(node.op, ast.And):
                        node.op = ast.Or()
                    else:
                        node.op = ast.And()
                    self.flipped = True
                return node

            def visit_Constant(self, node: ast.Constant) -> ast.Constant:
                if not self.flipped and isinstance(node.value, bool):
                    node.value = not node.value
                    self.flipped = True
                return node

        transformer = BooleanFlipper()
        new_tree = transformer.visit(copy.deepcopy(tree))
        return new_tree if transformer.flipped else None

    def _remove_error_handling(self, tree: ast.AST) -> ast.AST | None:
        class ErrorHandlerRemover(ast.NodeTransformer):
            removed = False

            def visit_Try(self, node: ast.Try) -> ast.AST:
                if not self.removed:
                    self.removed = True
                    return ast.copy_location(
                        ast.Module(body=node.body, type_ignores=[]), node
                    ) if isinstance(node, ast.Module) else node.body[0] if node.body else node
                return node

        transformer = ErrorHandlerRemover()
        new_tree = transformer.visit(copy.deepcopy(tree))
        return new_tree if transformer.removed else None

    @staticmethod
    def _make_diff(before: str, after: str) -> str:
        import difflib
        before_lines = before.splitlines(keepends=True)
        after_lines = after.splitlines(keepends=True)
        diff = difflib.unified_diff(before_lines, after_lines, fromfile="a/file.py", tofile="b/file.py")
        return "".join(diff)

    def _apply_mutation(self, source: str, mutation_fn) -> str | None:
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return None

        mutated_tree = mutation_fn(tree)
        if mutated_tree is None:
            return None

        try:
            ast.fix_missing_locations(mutated_tree)
            result = ast.unparse(mutated_tree)
            compile(result, "<augmented>", "exec")
            return result
        except (SyntaxError, ValueError, TypeError):
            return None


class LLMBugInjector(AugmentationStrategy):
    """Use LLM to inject subtle bugs. Expensive, high quality. Only for hard tasks."""

    def __init__(
        self,
        llm_endpoint: str,
        model: str = "gpt-4o",
        only_hard: bool = True,
        max_retries: int = 3,
    ):
        self._llm_endpoint = llm_endpoint
        self._model = model
        self._only_hard = only_hard
        self._max_retries = max_retries

    def augment(self, task: TaskSpec) -> list[TaskSpec]:
        if self._only_hard and task.difficulty != "hard":
            return []

        target_fn = self._select_target_function(task.fix_patch)
        if target_fn is None:
            return []

        prompt = self._generate_bug_prompt(target_fn, task.problem_statement)

        for _ in range(self._max_retries):
            bugged_code = self._call_llm(prompt)
            if bugged_code and self._validate_bug(target_fn, bugged_code):
                augmented = task.model_copy(
                    update={
                        "task_id": f"{task.task_id}__llm_bug",
                        "problem_statement": (
                            f"A subtle bug exists in the code. {task.problem_statement}"
                        ),
                    }
                )
                return [augmented]

        return []

    def expected_yield(self) -> float:
        return 0.5

    def _select_target_function(self, fix_patch: str) -> str | None:
        added_lines: list[str] = []
        for line in fix_patch.split("\n"):
            if line.startswith("+") and not line.startswith("+++"):
                added_lines.append(line[1:])

        source = "\n".join(added_lines)
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return None

        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                try:
                    return ast.unparse(node)
                except (ValueError, TypeError):
                    return None
        return None

    def _generate_bug_prompt(self, function_source: str, context: str) -> str:
        return (
            "Inject a single subtle bug into the following function. "
            "The bug should be realistic (off-by-one, wrong variable, missing edge case). "
            "Return ONLY the modified function code, no explanation.\n\n"
            f"Context: {context}\n\n"
            f"```python\n{function_source}\n```"
        )

    def _call_llm(self, prompt: str) -> str:
        try:
            import aiohttp

            async def _post() -> str:
                payload = {
                    "model": self._model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.7,
                }
                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        self._llm_endpoint,
                        json=payload,
                        timeout=aiohttp.ClientTimeout(total=60),
                    ) as resp:
                        if resp.status != 200:
                            return ""
                        data = await resp.json()
                        return data.get("choices", [{}])[0].get("message", {}).get(
                            "content", ""
                        )

            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None

            if loop and loop.is_running():
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor(1) as pool:
                    return pool.submit(asyncio.run, _post()).result()
            else:
                return asyncio.run(_post())
        except Exception:
            return ""

    def _validate_bug(self, original: str, bugged: str) -> bool:
        if original.strip() == bugged.strip():
            return False
        try:
            compile(bugged, "<bugged>", "exec")
            return True
        except SyntaxError:
            return False


def run_augmentation_pipeline(
    tasks: list[TaskSpec], strategies: list[AugmentationStrategy]
) -> list[TaskSpec]:
    """Run all strategies on all tasks. Returns augmented (unvalidated) tasks."""
    augmented: list[TaskSpec] = []
    for task in tasks:
        for strategy in strategies:
            try:
                results = strategy.augment(task)
                augmented.extend(results)
            except Exception as e:
                log.warning("Augmentation failed for %s with %s: %s", task.task_id, type(strategy).__name__, e)
                continue
    return augmented
