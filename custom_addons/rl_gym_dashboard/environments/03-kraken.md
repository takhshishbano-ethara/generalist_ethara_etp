# KRAKEN
*Repository-Level Performance Optimization · SWE-fficiency Methodology*

Can AI agents optimize code faster than experts?

Kraken builds on the SWE-fficiency methodology to train and test agents on repository-level performance optimization. Given a complete codebase and a slow workload, agents must investigate code semantics, localize bottlenecks, and produce patches that match or exceed expert-level speedup while passing all unit tests. Each instance reconstructs a production pull request from 3,000 open-source Python repositories, with automated timing harnesses and gold-standard speedup baselines for reproducible testing.

## Resources

| Resource | Link |
|----------|------|
| GitHub | https://github.com/Ethara-Ai/Kraken-Dataset |
| Dashboard | http://projects.ethara.ai/kraken |
| HuggingFace | https://huggingface.co/datasets/ethara/Kraken |
| Paper | https://arxiv.org/abs/2511.06090 |

---

## At a Glance

| Property | Value |
|----------|-------|
| Instances | 10,000 |
| Repositories | 3,000 |
| Language | Python |
| Models evaluated | 2 (GLM-5, Nova-2-Lite) |
| Methodology | SWE-fficiency |
| Difficulty levels | 4 (Easy, Medium, Hard, Expert) |
| Max gold speedup | 26.9x |
| Best HSR | 0.313 (GLM-5) |

---

## Data Capabilities

- Repository-level performance bottleneck localization
- Patch generation that preserves correctness (unit test validation)
- Multi-repository coverage: 3,000 open-source Python codebases (web frameworks, graph algorithms, data validation, HTTP clients, template engines, data-science, ML, and HPC)
- Execution-based feedback with real workload speedup measurement
- Cross-function reasoning for optimization across call boundaries

---

## For Frontier Labs

Performance optimization requires deep code reasoning across function boundaries: a capability current agents severely lack. Kraken provides the training signal to teach models how to reason about execution efficiency, a critical skill for production-grade autonomous software engineering.

---

## 1. Executive Summary

**Core question:** Given a complete repository and a slow workload, can an agent localize performance bottlenecks and produce patches that match or exceed expert-level speedups while passing all unit tests?

**Dataset scale:** 10,000 instances drawn from 3,000 open-source Python repositories.

**Key results (20 instances evaluated, 2 models):**

| Metric | GLM-5 | Nova-2-Lite |
|--------|-------|-------------|
| HSR (harmonic mean) | 0.313 | 0.268 |
| Pass@1 (exceed expert) | 7/20 (35%) | 2/20 (10%) |
| Correctness rate | 70% (14/20) | 60% (12/20) |
| Outcome: Pass | 7 | 2 |
| Outcome: Correct but slower | 7 | 10 |
| Outcome: Fail | 6 | 8 |
| Avg cost per instance | $2.41 | $0.27 |

GLM-5 costs ~$2.14/instance average. Nova-2-Lite costs ~$0.09/instance, making it 24x cheaper.

---

## 2. What is Kraken?

Kraken tests AI agents on **repository-level performance optimization**: a task that requires deep code reasoning across function boundaries. Unlike bug-fixing datasets (SWE-bench), Kraken focuses exclusively on making existing, correct code run faster.

### The task

1. The agent receives a full repository checkout and a workload script that exercises a slow code path.
2. The agent investigates the codebase, profiles the workload, localizes bottlenecks, and produces a patch.
3. The harness measures: (a) did all unit tests still pass? (b) how much faster is the workload?
4. The agent's speedup is compared against an expert human's gold-standard patch.

### What makes it hard

- Agents must reason across function boundaries. Expert patches touch 2.2 files on average.
- The optimal edit is rarely in the function the agent first visits. Agents edit the right file but wrong function 30-40% of the time.
- Shortcut strategies (early exits, memoization) yield small wins but miss the deep systemic restructuring experts apply.
- Agents tend to stop optimizing after 30-50 turns ("satisficing"), leaving significant speedup on the table.

---

## 3. Testing Pipeline

Three steps from repository to scored result.

### Step 01: Investigate and Localize
- Agent receives a repository with a known performance bottleneck
- Must investigate the codebase, identify slow code paths
- Localize the optimization target

### Step 02: Optimize and Patch
- Produce a code patch that improves runtime performance
- Measured against expert gold-standard speedup
- Scored via Speedup Ratio (SR) metric

### Step 03: Verify Correctness
- Patched code must pass all covering correctness tests
- Incorrect patches penalized: SR = 1/Gold_Speedup
- Correctness is never sacrificed for speed

---

## 4. Methodology (SWE-fficiency)

Four principles govern the Kraken scoring system.

### Principle 01: Speedup Ratio (SR)

```
Speedup_LM   = T_pre / T_post        (how much the agent sped up the workload)
Speedup_gold = T_pre / T_gold        (how much the expert sped it up)
SR           = Speedup_LM / Speedup_gold
```

SR = 1.0 means the agent matched the expert. Values above 1.0 indicate the agent exceeded expert performance.

### Principle 02: Harmonic Mean

- Individual SR values aggregated via harmonic mean
- Penalizes inconsistency across instances
- Prevents a single outlier from inflating the score

### Principle 03: Correctness Gating

- Patches must pass Covering Test Suite (CTS)
- PASSED and XFAIL count as passing
- SKIPPED tests excluded from the denominator
- ERROR or FAILED counts as a break
- Failed patches penalized: SR = 1/Gold_Speedup
- Correctness is never sacrificed for speed

### Principle 04: Pass@1 Protocol

- Each model gets exactly one attempt per instance
- Mirrors real-world single-submission workflow
- No retry, no cherry-picking best runs

### Outcome classification

| Outcome | Definition |
|---------|-----------|
| Pass | Tests pass AND HSR >= 1.0 (agent matches or beats expert) |
| Correct (Slow) | Tests pass AND HSR < 1.0 (agent improved speed, but less than expert) |
| Fail | Tests fail OR patch could not be applied |

### Anti-gaming protections

1. **Introspection guard (paper section A.6):** AST scanner rejects patches that use stack-frame inspection, `sys._getframe`, `gc.get_referrers`, or dynamic imports of `inspect`. Prevents reward hacking via call-graph manipulation.
2. **Fork-per-run isolation (paper section A.7):** Each timing iteration runs in a forked child process via `multiprocessing.get_context('fork')`. Prevents lru_cache, module-level caches, or global state from leaking between runs and inflating speedups.

---

## 5. Dataset Composition

### Overview

| Property | Value |
|----------|-------|
| Total instances | 10,000 |
| Repositories covered | 3,000 |
| Models tested | 2 (GLM-5, Nova-2-Lite) |
| Instances evaluated (pilot) | 20 |
| Total agent runs (pilot) | 40 |
| Difficulty levels | 4 (Easy, Medium, Hard, Expert) |
| Language | Python |

### Repository coverage (pilot: 20 evaluated instances)

| Repository | Domain | Instances | License |
|------------|--------|-----------|---------|
| networkx/networkx | Graph algorithms | 10 | 3-Clause BSD |
| pallets/flask | Web framework | 4 | BSD-3-Clause |
| pydantic/pydantic | Data validation | 2 | MIT |
| fastapi/fastapi | Async web framework | 2 | MIT |
| encode/httpx | HTTP client | 1 | BSD-3-Clause |
| pallets/jinja | Template engine | 1 | BSD-3-Clause |

### Difficulty distribution (pilot: 20 evaluated instances)

| Difficulty | Count | Description |
|------------|-------|-------------|
| Easy | 4 | Gold speedup < 1.1x, localized fix |
| Medium | 5 | Gold speedup 1.1x to 2.0x, moderate reasoning |
| Hard | 9 | Gold speedup > 2.0x or complex multi-file fix |
| Expert | 2 | Gold speedup > 2.0x with deep architectural changes |

### Gold speedup range (pilot)

- Minimum: 1.02x (networkx-7971)
- Maximum: 26.92x (encode-httpx-2423)
- Mean: 4.11x across 20 evaluated instances

---

## 6. Results (20 Evaluated Instances)

### Key metrics

| Metric | GLM-5 | Nova-2-Lite |
|--------|-------|-------------|
| HSR Harmonic Mean | 0.313 | 0.268 |
| Correctness Rate | 70% | 60% |
| Pass@1 | 7 / 20 | 2 / 20 |
| Outcome Split | 6 fail, 7 slow, 7 pass | 8 fail, 10 slow, 2 pass |
| Avg Cost | $2.41 | $0.27 |

GLM-5 passes 7 of 20 instances outright. Nova-2-Lite passes 2 of 20 but produces correct (slow) patches on 10 more. GLM-5 costs ~$2.14/instance avg. Nova-2-Lite costs ~$0.09/instance, 24x cheaper.

### Per-instance results

| # | Instance | Difficulty | Gold Speedup | GLM-5 HSR | Nova HSR | GLM-5 Outcome | Nova Outcome |
|---|----------|-----------|-------------|-----------|----------|---------------|--------------|
| 01 | encode__httpx-2423 | Hard | 26.92x | 0.0371 | 0.0371 | Fail | Fail |
| 02 | fastapi__fastapi-15318 | Hard | 9.36x | 0.1068 | 0.1068 | Fail | Fail |
| 03 | fastapi__fastapi-15372 | Hard | 1.61x | 0.6222 | 0.6222 | Fail | Fail |
| 04 | networkx__networkx-6337 | Medium | 2.08x | 0.8430 | 0.6306 | Correct (Slow) | Correct (Slow) |
| 05 | networkx__networkx-7971 | Easy | 1.02x | 0.9895 | 0.9880 | Correct (Slow) | Correct (Slow) |
| 06 | networkx__networkx-8023 | Hard | 1.21x | **5.2298** | 0.8316 | **Pass** | Correct (Slow) |
| 07 | networkx__networkx-8056 | Hard | 13.09x | **1.1087** | 0.2495 | **Pass** | Correct (Slow) |
| 08 | networkx__networkx-8206 | Hard | 5.08x | **1.4980** | 0.1968 | **Pass** | Fail |
| 09 | networkx__networkx-8266 | Easy | 1.05x | **1.0099** | **1.0109** | **Pass** | **Pass** |
| 10 | networkx__networkx-8296 | Easy | 1.20x | 0.9817 | 0.8324 | Correct (Slow) | Correct (Slow) |
| 11 | networkx__networkx-8460 | Medium | 1.37x | **1.0311** | 0.7299 | **Pass** | Fail |
| 12 | networkx__networkx-8561 | Easy | 1.02x | 0.9926 | 0.9759 | Correct (Slow) | Correct (Slow) |
| 13 | networkx__networkx-8615 | Hard | 4.04x | 0.3067 | 0.2475 | Correct (Slow) | Fail |
| 14 | pallets__flask-5229 | Hard | 1.13x | **1.0553** | 0.8846 | **Pass** | Correct (Slow) |
| 15 | pallets__flask-5818 | Expert | 2.50x | 0.4003 | 0.4003 | Fail | Fail |
| 16 | pallets__flask-5939 | Medium | 1.26x | 0.7979 | 0.8013 | Correct (Slow) | Correct (Slow) |
| 17 | pallets__flask-5964 | Medium | 1.10x | **1.0019** | **1.0214** | **Pass** | **Pass** |
| 18 | pallets__jinja-1516 | Medium | 1.25x | 0.8000 | 0.7998 | Fail | Correct (Slow) |
| 19 | pydantic__pydantic-10868 | Hard | 6.51x | 0.1536 | 0.1536 | Fail | Fail |
| 20 | pydantic__pydantic-11255 | Expert | 1.67x | 0.7179 | 0.5948 | Correct (Slow) | Correct (Slow) |

### Expanded detail fields (per instance, per model)

Each instance in the dataset viewer provides:

| Field | Description |
|-------|-------------|
| Instance ID | Unique identifier (e.g., `networkx__networkx-8460`) |
| Repo | Source repository (e.g., `networkx/networkx`) |
| Difficulty | Easy / Medium / Hard / Expert |
| Gold Speedup | Expert's speedup factor |
| Language | Python |
| Outcome | PASS / CORRECT (SLOW) / FAIL |
| HSR | Instance-level Speedup Ratio |
| Speedup (LM) | Raw agent speedup factor |
| Speedup (Adj) | Adjusted speedup (penalized if incorrect) |
| Tests | Tests passed / total |
| Correctness % | Percentage of covering tests passed |
| Files modified | Number of files the agent edited |
| Tool calls | Number of actions taken by the agent |
| Cost | Inference cost in USD |
| Time | Wall-clock time of agent run |

---

## 7. Key Findings

### 7.1 Super-human instances (agent beat the expert)

9 out of 40 agent runs achieved HSR > 1.0 with correct patches:

| Model | Instance | Agent Speedup | Expert Speedup | HSR |
|-------|----------|--------------|----------------|-----|
| GLM-5 | networkx-8023 | 6.32x | 1.21x | **5.23** |
| GLM-5 | networkx-8206 | 7.61x | 5.08x | **1.50** |
| GLM-5 | networkx-8056 | 14.51x | 13.09x | **1.11** |
| GLM-5 | flask-5229 | 1.19x | 1.13x | 1.06 |
| GLM-5 | networkx-8460 | 1.41x | 1.37x | 1.03 |
| GLM-5 | networkx-8266 | 1.06x | 1.05x | 1.01 |
| GLM-5 | flask-5964 | 1.10x | 1.10x | 1.00 |
| Nova-2-Lite | flask-5964 | 1.13x | 1.10x | 1.02 |
| Nova-2-Lite | networkx-8266 | 1.06x | 1.05x | 1.01 |

The standout: **networkx-8023 / GLM-5** achieved 5.23x the expert's speedup (6.32x vs 1.21x) while passing all 56 covering tests.

### 7.2 Model comparison

| Dimension | GLM-5 | Nova-2-Lite |
|-----------|-------|-------------|
| HSR (harmonic mean) | 0.313 | 0.268 |
| Pass@1 rate | 35% (7/20) | 10% (2/20) |
| Correctness rate | 70% (14/20) | 60% (12/20) |
| Avg cost per instance | $2.41 | $0.27 |
| Cost efficiency | 24x more expensive | Baseline |
| Best single HSR | 5.23 | 1.02 |
| Outcome: Pass | 7 | 2 |
| Outcome: Correct (Slow) | 7 | 10 |
| Outcome: Fail | 6 | 8 |

GLM-5 is the stronger model across all metrics, achieving 3.5x the pass rate. It consistently finds meaningful optimizations rather than producing near-identity patches. Nova-2-Lite's strength is cost efficiency at 24x cheaper per instance, still achieving 60% correctness.

### 7.3 Failure patterns

Observed failure modes (consistent with SWE-fficiency paper findings):

1. **Function-level mislocalization:** Agents edit the right file but wrong function 30-40% of the time.
2. **Satisficing behavior:** Agents stop at 30-50 turns once any speedup is found, leaving optimization budget unused.
3. **Shortcut bias:** Agents prefer identity checks, early exits, and memoization over expert-style systemic restructuring.
4. **Workload overfitting:** Some patches break semantics (e.g., returning original DataFrame, monkey-patching numpy functions).
5. **Invasive edits:** Module-level mutable caches or global monkey-patches that are not composable.

### 7.4 Difficulty correlation

Performance degrades with difficulty, but not monotonically:

| Difficulty | GLM-5 Pass Rate | Nova-2-Lite Pass Rate |
|------------|----------------|----------------------|
| Easy (4) | 25% (1/4) | 25% (1/4) |
| Medium (5) | 40% (2/5) | 20% (1/5) |
| Hard (9) | 44% (4/9) | 0% (0/9) |
| Expert (2) | 0% (0/2) | 0% (0/2) |

GLM-5 is notably strong on Hard instances (4/9 pass), suggesting it has learned deep optimization patterns. Both models struggle with Expert difficulty, which requires multi-file architectural changes.

---

## 8. Why This Matters

Performance optimization requires capabilities current models severely lack:

1. **Cross-function reasoning:** The optimal edit is rarely in the function the agent first visits. Expert patches span 2.2 files on average and touch code that requires understanding the full call stack.

2. **Execution-aware reasoning:** Unlike bug-fixing, optimization requires reasoning about runtime behavior, memory allocation, algorithmic complexity, and cache hierarchies.

3. **Composability discipline:** Expert patches are localized and composable. Agent patches tend toward invasive global state (module-level caches, monkey-patches) that are fragile in production.

4. **Training signal scarcity:** Performance optimization produces cleaner reward signal than bug-fixing (continuous speedup metric vs binary pass/fail), making it ideal for RL-based fine-tuning.

Kraken provides 10,000 expert-annotated instances with full execution traces, timing data, and correctness verification. This is training data for teaching models to reason about efficiency: a critical capability gap between current agents and production-grade autonomous software engineering.

---
