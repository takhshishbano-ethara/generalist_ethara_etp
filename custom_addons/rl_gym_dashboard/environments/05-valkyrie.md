# Valkyrie

## Abstract

Valkyrie is a reinforcement learning environment for training models to remediate security vulnerabilities in real-world code. Each episode places the model inside an isolated codebase containing an injected CWE-class vulnerability. The model receives a natural-language problem statement, interacts with the codebase through tool calls, and submits a patch. The environment returns a binary reward: +1 when tests pass and no regressions are introduced, 0 otherwise.

Pipeline extends [SWE-smith](https://arxiv.org/abs/2504.21798) (NeurIPS 2025 Spotlight) with CWE-targeted mutation and human curation. Targeting 10,000 task instances across 8 languages and 85+ CWE categories. Currently 20 validated instances with production pipeline active.

---

## Dataset

**Repository**: [ethara/Valkyrie](https://huggingface.co/datasets/ethara/Valkyrie) (private, license: CC-BY-NC-ND-4.0)

### Schema

| Column | Type | Description |
|--------|------|-------------|
| `instance_id` | string | Unique task identifier (`{repo_slug}.{commit_hash}.{mutation_id}`) |
| `patch` | string | Ground-truth fix (unified diff format) |
| `FAIL_TO_PASS` | list\<string\> | Security tests: must pass after fix (reward gate) |
| `PASS_TO_PASS` | list\<string\> | Regression tests: must remain passing (reward gate) |
| `image_name` | string | Docker image URI for isolated execution |
| `repo` | string | Source repository (org/repo format) |
| `problem_statement` | string | Natural-language description of the vulnerability |
| `vulnerability_type` | list\<string\> | CWE identifiers for the injected vulnerability |
| `category` | string | Vulnerability composition: `Atomic CWE` or `Composite CWE` |
| `evaluation` | struct | Baseline agent results (see below) |

### Evaluation Struct

```
evaluation: {
    difficulty: string           // "Easy", "Medium", "Hard"
    num_files_affected: string   // Number of files changed in ground-truth patch
    kimi_k2_5: {
        pass_at_1: string        // "Pass" or "Fail"
        time_of_completion_secs: string
        cost_usd: string
    }
    nova_2_lite: {
        pass_at_1: string        // "Pass" or "Fail"
        time_of_completion_secs: string
        cost_usd: string
    }
}
```

---

## Current Dataset (20 instances)

These numbers reflect the validated initial batch. All figures will grow as production continues toward 10K.

### Repositories

| Repository | Instances |
|------------|-----------|
| FFmpeg/FFmpeg | 6 |
| jqlang/jq | 14 |

### CWE Coverage

7 unique CWEs in current batch:

| CWE | Name |
|-----|------|
| CWE-125 | Out-of-bounds Read |
| CWE-416 | Use After Free |
| CWE-665 | Improper Initialization |
| CWE-670 | Always-Incorrect Control Flow Implementation |
| CWE-754 | Improper Check for Unusual or Exceptional Conditions |
| CWE-787 | Out-of-bounds Write |
| CWE-908 | Use of Uninitialized Resource |

These are predominantly memory-safety CWEs (C codebases). As additional languages and repositories are onboarded, the CWE distribution will broaden to cover injection flaws, access control, cryptographic issues, etc.

### Categories

| Category | Count |
|----------|-------|
| Atomic CWE | 14 |
| Composite CWE | 6 |

- **Atomic CWE**: Vulnerability involves a single CWE pattern
- **Composite CWE**: Vulnerability combines multiple CWE patterns in one instance

### Difficulty Distribution

| Difficulty | Present in current batch |
|------------|------------------------|
| Easy | Yes |
| Medium | Yes |
| Hard | Yes |

### Files Affected (ground-truth patches)

Range: 0-5 files per instance in current batch.

---

## Reward Signal

Binary: **+1** or **0**. No partial credit.

```
reward = 1 if (FAIL_TO_PASS all pass) AND (PASS_TO_PASS all pass) else 0
```

- `FAIL_TO_PASS`: Security-specific tests that exercise the injected vulnerability. These must pass after the agent's fix.
- `PASS_TO_PASS`: Pre-existing tests that passed before the vulnerability was injected. These must remain passing (no regressions).

Both conditions required. A patch that fixes the vulnerability but breaks other functionality receives 0.

**Rationale**: Security fixes are pass/fail by nature. A partial fix to a buffer overflow is still exploitable. Sparse binary reward targets RL methods that learn from rare positive signal.

---

## Training Methodology

Valkyrie's test suites provide deterministic binary reward — no learned reward model needed. The environment serves as a ground-truth verifier: the patch either fixes the vulnerability without regressions, or it doesn't.

**Approach**: Online RL with verifiable reward (GRPO)

1. Warm-start policy from successful frontier agent trajectories
2. Online RL — sample rollouts, let environment verify, update policy on positive-signal episodes
3. Difficulty curriculum — Easy → Hard, shaping reward density as capability grows

This is the same verification-driven RL paradigm behind DeepSeek-R1 and OpenAI o-series reasoning models, applied to code security remediation.

---

## Baseline Calibration

Two agents evaluated on the current 20-instance batch:

| Agent | Pass@1 (current batch) |
|-------|----------------------|
| **Kimi K2.5** (Moonshot AI) | Partial passes observed |
| **Amazon Nova Lite** (Bedrock) | All fail on current batch |

Per-instance results stored in the `evaluation` struct. Aggregate statistics (resolve rate with CI) will be reported once evaluation completes on the full 10K dataset.

---

## Task Composition Pipeline

### Vulnerability Injection

LLM-guided adversarial mutation followed by human curation:

1. Extract code entities (functions, methods) from established open-source repositories
2. Apply CWE-specific mutation prompts that introduce vulnerabilities:
   - Compile without errors or warnings
   - Preserve function signatures and behavior on non-adversarial inputs
   - Appear as plausible developer mistakes
3. Human curators validate realism, difficulty calibration, and test coverage adequacy
4. Test verification confirms reward signal fires correctly (vulnerable code fails security tests, patched code passes)

### Quality Control

Automated QC pipeline validates every instance before inclusion:
- Schema validation (column types, nested struct integrity)
- Data structure validation (ID format, patch format, non-empty test lists, CWE format)
- Cross-row consistency (uniqueness, distribution checks)


---

## Projected Scale (10K target)

| Dimension | Target |
|-----------|--------|
| Total instances | 10,000 |
| Languages | 8 (JavaScript, Python, TypeScript, C, C++, Java, Go, Rust) |
| CWE categories | 85+ |
| Repositories | 100+ |
| Difficulty tiers | Easy / Medium / Hard |

These are production targets. Current dataset (20 instances, 2 repos, 7 CWEs) represents the validated seed from which the pipeline scales.

---

## Links

| Resource | URL |
|----------|-----|
| Dashboard | https://projects.ethara.ai/valkyrie |
| HF Dataset | https://huggingface.co/datasets/ethara/Valkyrie |
| Trajectories | https://github.com/Ethara-Ai/Valkyrie/tree/main/Trajectories |
| SWE-smith paper | https://arxiv.org/abs/2504.21798 |

---

## Technical Tags

`reinforcement-learning` `security` `software-engineering` `agents` `code` `vulnerability-repair`

---

*Project Valkyrie | Ethara AI*
