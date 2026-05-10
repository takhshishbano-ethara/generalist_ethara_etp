"""Partial credit for failed trajectories via code embedding similarity.

Implements the GTPO paper's self-supervised reward shaping:
  r_acc_failed = (alpha / |P|) * Σ_{p∈P} sim(code_i, code_p)

Where:
  - P = set of trajectories that PASS within the same group
  - sim() = cosine similarity via Amazon Titan Text Embeddings V2
  - alpha = 0.5 (upper bound on partial credit)
  - code_i = concatenated code blocks from all turns of trajectory i

Also provides heuristic-based partial credit when embeddings unavailable:
  - Patch applies cleanly: +0.1
  - Partial tests pass (some f2p): +proportional credit
  - Code structure valid (AST-parseable): +0.05

Reference: arXiv:2511.14846, Section 3.4 (Self-Supervised Reward Shaping)
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

import torch

log = logging.getLogger(__name__)


@dataclass
class PartialCreditConfig:
    """Configuration for partial credit computation."""

    enabled: bool = True
    alpha: float = 0.5  # upper bound on similarity-based credit (paper: 0.5)
    use_embeddings: bool = True  # use Titan embeddings; fallback to heuristic if False
    heuristic_patch_applies: float = 0.1  # credit if patch applies cleanly
    heuristic_partial_tests: float = 0.3  # max credit from partial test pass
    heuristic_valid_code: float = 0.05  # credit for AST-valid code
    embedding_model: str = "amazon.titan-embed-text-v2:0"
    embedding_region: str = "us-east-1"


CODE_BLOCK_PATTERN = re.compile(
    r"```(?:python|diff|patch|bash|go)?\s*\n(.*?)```",
    re.DOTALL,
)


@dataclass
class PartialCreditComputer:
    """Computes partial credit for failed trajectories within a GRPO group.

    For each group of trajectories (same prompt, different rollouts):
    1. Identify PASS trajectories (P set)
    2. For each FAIL trajectory, compute similarity to P
    3. Assign r_acc = (alpha / |P|) * mean_sim(fail_code, pass_codes)
    """

    config: PartialCreditConfig = field(default_factory=PartialCreditConfig)
    _embeddings_cache: dict[str, list[float]] = field(default_factory=dict, repr=False)

    def compute_group_partial_credit(
        self,
        group_codes: list[str],
        group_outcomes: list[bool],
        group_partial_test_ratios: list[float] | None = None,
    ) -> list[float]:
        """Compute partial credit for a single GRPO group.

        Args:
            group_codes: Extracted code for each trajectory in the group.
            group_outcomes: True if trajectory passed, False if failed.
            group_partial_test_ratios: Optional ratio of tests passed (0-1) per trajectory.

        Returns:
            Partial credit values for each trajectory. 0.0 for PASS trajectories
            (they already get full outcome reward). Positive for FAIL trajectories.
        """
        if not self.config.enabled:
            return [0.0] * len(group_codes)

        n = len(group_codes)
        credits = [0.0] * n

        # Identify pass and fail indices
        pass_indices = [i for i, ok in enumerate(group_outcomes) if ok]
        fail_indices = [i for i, ok in enumerate(group_outcomes) if not ok]

        if not fail_indices:
            return credits  # All passed — no partial credit needed

        if self.config.use_embeddings and pass_indices:
            # Embedding-based similarity (paper method)
            credits = self._embedding_similarity_credit(
                group_codes, pass_indices, fail_indices, credits
            )
        elif pass_indices:
            # Heuristic-based: simple overlap ratio as proxy for similarity
            credits = self._heuristic_overlap_credit(
                group_codes, pass_indices, fail_indices, credits
            )

        # Add heuristic partial credit from test results
        if group_partial_test_ratios is not None:
            for i in fail_indices:
                ratio = group_partial_test_ratios[i]
                if ratio > 0.0:
                    credits[i] += self.config.heuristic_partial_tests * ratio

        # Add small credit for valid code structure
        for i in fail_indices:
            if self._has_valid_code(group_codes[i]):
                credits[i] += self.config.heuristic_valid_code

        # Cap at alpha
        for i in fail_indices:
            credits[i] = min(credits[i], self.config.alpha)

        return credits

    def compute_batch_partial_credit(
        self,
        all_codes: list[str],
        all_outcomes: list[bool],
        group_size: int,
        partial_test_ratios: list[float] | None = None,
    ) -> list[float]:
        """Compute partial credit for an entire batch grouped by prompt.

        Args:
            all_codes: Code for all trajectories in batch.
            all_outcomes: Pass/fail for each trajectory.
            group_size: Number of trajectories per prompt.
            partial_test_ratios: Optional ratio of tests passed per trajectory.

        Returns:
            Partial credit for each trajectory in the batch.
        """
        batch_size = len(all_codes)
        all_credits = [0.0] * batch_size

        n_groups = -(-batch_size // group_size) if group_size > 0 else 1

        for g in range(n_groups):
            start = g * group_size
            end = min(start + group_size, batch_size)

            group_codes = all_codes[start:end]
            group_outcomes = all_outcomes[start:end]
            group_ratios = partial_test_ratios[start:end] if partial_test_ratios else None

            group_credits = self.compute_group_partial_credit(
                group_codes, group_outcomes, group_ratios
            )

            for i, credit in enumerate(group_credits):
                all_credits[start + i] = credit

        return all_credits

    def extract_code_from_trajectory(self, turns: list[dict[str, str]]) -> str:
        """Extract and concatenate all code blocks from assistant turns.

        Paper: "extract and concatenate code from all turns: c_{i,0} ⊕ c_{i,1} ⊕ ..."
        """
        code_parts: list[str] = []
        for turn in turns:
            if turn.get("role") != "assistant":
                continue
            content = turn.get("content", "")
            # Extract code blocks
            blocks = CODE_BLOCK_PATTERN.findall(content)
            if blocks:
                code_parts.extend(blocks)
            else:
                # If no fenced code, look for <tool_call> content
                tool_match = re.search(r"<tool_call>(.*?)</tool_call>", content, re.DOTALL)
                if tool_match:
                    code_parts.append(tool_match.group(1))
        return "\n".join(code_parts)

    def _embedding_similarity_credit(
        self,
        group_codes: list[str],
        pass_indices: list[int],
        fail_indices: list[int],
        credits: list[float],
    ) -> list[float]:
        """Compute similarity-based credit using Bedrock Titan embeddings."""
        try:
            # Get embeddings for all codes
            pass_embeddings = [self._get_embedding(group_codes[i]) for i in pass_indices]
            fail_embeddings = [self._get_embedding(group_codes[i]) for i in fail_indices]

            if not pass_embeddings or not fail_embeddings:
                return credits

            # Convert to tensors
            pass_tensor = torch.tensor(pass_embeddings, dtype=torch.float32)  # [|P|, dim]
            fail_tensor = torch.tensor(fail_embeddings, dtype=torch.float32)  # [|F|, dim]

            # Normalize
            pass_norm = torch.nn.functional.normalize(pass_tensor, dim=1)
            fail_norm = torch.nn.functional.normalize(fail_tensor, dim=1)

            # Cosine similarity: [|F|, |P|]
            similarities = torch.mm(fail_norm, pass_norm.T)

            # Paper formula: r_acc = (alpha / |P|) * mean_sim
            n_pass = len(pass_indices)
            for idx, i in enumerate(fail_indices):
                mean_sim = similarities[idx].mean().item()
                # Only positive similarity contributes
                mean_sim = max(mean_sim, 0.0)
                credits[i] = (self.config.alpha / n_pass) * mean_sim

        except Exception as e:
            log.warning("Embedding similarity failed, falling back to heuristic: %s", e)
            credits = self._heuristic_overlap_credit(
                group_codes, pass_indices, fail_indices, credits
            )

        return credits

    def _heuristic_overlap_credit(
        self,
        group_codes: list[str],
        pass_indices: list[int],
        fail_indices: list[int],
        credits: list[float],
    ) -> list[float]:
        """Heuristic similarity: token-level Jaccard overlap as proxy for embeddings."""
        # Tokenize pass codes
        pass_token_sets = [set(group_codes[i].split()) for i in pass_indices]

        for idx, i in enumerate(fail_indices):
            fail_tokens = set(group_codes[i].split())
            if not fail_tokens:
                continue

            # Average Jaccard similarity with all pass trajectories
            similarities: list[float] = []
            for pass_set in pass_token_sets:
                if not pass_set:
                    continue
                intersection = len(fail_tokens & pass_set)
                union = len(fail_tokens | pass_set)
                similarities.append(intersection / union if union > 0 else 0.0)

            if similarities:
                mean_sim = sum(similarities) / len(similarities)
                n_pass = len(pass_indices)
                credits[i] = (self.config.alpha / max(n_pass, 1)) * mean_sim

        return credits

    def _get_embedding(self, text: str) -> list[float]:
        """Get embedding from Bedrock Titan. Uses cache for deduplication."""
        # Truncate to avoid token limits (Titan V2 supports 8192 tokens)
        text = text[:32000]

        cache_key = text[:200]  # Short key for cache
        if cache_key in self._embeddings_cache:
            return self._embeddings_cache[cache_key]

        try:
            import boto3
            import json

            client = boto3.client(
                "bedrock-runtime",
                region_name=self.config.embedding_region,
            )
            response = client.invoke_model(
                modelId=self.config.embedding_model,
                body=json.dumps({"inputText": text}),
            )
            result = json.loads(response["body"].read())
            embedding = result["embedding"]
            self._embeddings_cache[cache_key] = embedding
            return embedding
        except Exception as e:
            log.debug("Titan embedding failed: %s", e)
            # Return zero embedding as fallback (will produce 0 similarity)
            return [0.0] * 1024

    def _has_valid_code(self, code: str) -> bool:
        """Check if extracted code contains syntactically valid Python."""
        if not code.strip():
            return False
        try:
            import ast
            ast.parse(code)
            return True
        except SyntaxError:
            # Might be a diff/patch format, which is still valid
            return code.startswith("diff ") or code.startswith("---") or "@@" in code
