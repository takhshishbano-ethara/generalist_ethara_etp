# MARS

REINFORCEMENT

---

Agentic Code RL Environment with Human Curated Reward Signals

by [Ethara AI](https://www.ethara.ai)

---

## S01. Overview

Mars is a collection of 10,000 human curated instances designed to train and stress-test frontier coding agents. Each instance packages a real-world programming task with structured reward signals: a sandboxed runtime, deterministic test harnesses, and graded difficulty tiers. Two models (GLM-5 and Nova-2-Lite) are run against each instance. Pass rates are reported per difficulty tier and task category.

### At a glance

| | |
|---|---|
| Instances | **10,000** |
| Models tested | **2** (GLM-5, Nova-2-Lite) |
| Categories | **6** |
| Difficulty tiers | **3** (Easy, Medium, Hard) |
| Reward signal coverage | 100% verified |
| Agent | Terminus-2 v2.0.0 |
| Total trials | 20,000 (1 trial per model per instance) |

---

## S02. Foundation: Terminal-Bench

Mars is built on [Terminal-Bench 2.0](https://arxiv.org/abs/2601.11868). Terminal-Bench provides 89 manually verified tasks where agents operate in hermetic Docker containers and are graded purely by test-script state checks on the final container state. No LLM-as-judge. No partial credit.

Best frontier result: GPT-5.2 + Codex CLI at 63%. All frontier models score below 65%. This ceiling motivates Mars: the tasks are hard enough to produce meaningful training signal for RL.

Mars repurposes Terminal-Bench as a reward-bearing RL environment: same hermetic containers, same deterministic reset, same outcome-based verification, applied to policy training rather than leaderboard scoring. The task pool is expanded to 10,000 instances from the original 89-task research set.

---

## S03. Key Metrics (10,000 Instances)

| Metric | Value | Note |
|--------|-------|------|
| RL Instances | 10,000 | 100% reward signal coverage |
| Frontier Models | 2 | GLM-5 and Nova-2-Lite |
| Task Categories | 6 | algorithms, data-querying, debugging, file-operations, optimization, software-engineering |
| Difficulty Tiers | 3 | Easy, Medium, Hard |
| Total Reward Signals | 56,500 | Individual test assertions across all instances |

---

## S04. Pipeline

Three stages turn a raw task into a scored RL environment.

**Phase 01: Environment Construction**

- Identify target real-world programming problems across six categories
- Write human curated reward signals (deterministic test harnesses with 3 to 18 assertions per instance)
- Package sandboxed runtimes with graded difficulty metadata
- Each instance ships: `task.toml` (metadata), `environment/` (runtime), `tests/` (reward signals), `instruction.md` (agent prompt)

**Phase 02: Agent Execution**

- Deploy frontier models into isolated instances
- Agents produce solutions under resource and time constraints (600s to 1800s depending on difficulty)
- Capture per-instance outputs and full interaction histories

**Phase 03: Reward Signal Verification**

- Execute reward signal harnesses against agent outputs
- Score binary pass/fail per test case (reward in {0, 1})
- Aggregate to instance-level and category-level pass rates
- An instance is "solved" only when all reward signals pass (reward = 1)

---

## S05. Results

### Overall Performance

| Metric | GLM-5 | Nova-2-Lite | Delta |
|--------|-------|-------------|-------|
| Instances Solved | 6,500 / 10,000 (65.0%) | 4,500 / 10,000 (45.0%) | +20.0% GLM-5 |
| Total Reward Signals Passed | 49,545 / 56,500 (87.6%) | 34,522 / 56,500 (61.1%) | +26.5% GLM-5 |
| Total Cost | $361 | $17,445 | 48x Nova |
| Avg Cost / Instance | $0.036 | $1.745 | 48x Nova |
| Cost / Solve | $0.055 | $3.877 | 70x Nova |
| Avg Agent Time | 106.8s | 524.5s | 4.9x Nova |
| Avg Episodes | 7.3 | 66.5 | 9.1x Nova |
| Total Input Tokens | 293M | 55.9B | 191x Nova |
| Total Output Tokens | 20.5M | 265M | 12.9x Nova |

GLM-5 achieves higher pass rates at 48x lower cost. The cost disparity is driven by Nova-2-Lite's catastrophic retry behavior on hard and medium instances.

---

### Success Rate by Difficulty Tier

Both models show clear degradation as difficulty increases. GLM-5 maintains a consistent lead across all tiers. Nova-2-Lite collapses entirely on Hard instances.

| Difficulty | Instances | GLM-5 | Nova-2-Lite |
|-----------|-------------|-------|-------------|
| Easy | 3,500 | 85.7% | 71.4% |
| Medium | 3,500 | 71.4% | 57.1% |
| Hard | 3,000 | 33.3% | 0.0% |

---

### Success Rate by Task Category

Performance varies sharply by category. File-operations and algorithms show the highest GLM-5 pass rates. Data-querying and optimization expose the largest model gap.

| Category | Instances | GLM-5 | Nova-2-Lite |
|----------|-------------|-------|-------------|
| algorithms | 520 | 100.0% | 100.0% |
| data-querying | 1,560 | 33.3% | 0.0% |
| debugging | 3,120 | 66.7% | 66.7% |
| file-operations | 1,040 | 100.0% | 50.0% |
| optimization | 1,040 | 50.0% | 0.0% |
| software-engineering | 2,720 | 66.9% | 50.0% |

---

### Success Rate vs. Reward Signal Density

Instances with more test cases (higher reward signal density) correlate with lower pass rates. At 12 reward signals per instance, both models score 0%.

| Reward Signals | Instances | GLM-5 | Nova-2-Lite |
|---------------|-------------|-------|-------------|
| 3 | 3,500 | 85.7% | 71.4% |
| 5 | 3,500 | 71.4% | 57.1% |
| 9 | 2,500 | 40.0% | 0.0% |
| 12 | 500 | 0.0% | 0.0% |

---

## S06. Dataset Composition

Distribution of instances across difficulty tiers and categories.

| | Easy | Medium | Hard | Total |
|---|---|---|---|---|
| algorithms | 520 | 0 | 0 | 520 |
| data-querying | 520 | 520 | 520 | 1,560 |
| debugging | 1,040 | 1,040 | 1,040 | 3,120 |
| file-operations | 520 | 520 | 0 | 1,040 |
| optimization | 0 | 520 | 520 | 1,040 |
| software-engineering | 900 | 900 | 920 | 2,720 |
| **Total** | **3,500** | **3,500** | **3,000** | **10,000** |

---

## S07. Cost and Efficiency Analysis

### Cost per Instance

Nova-2-Lite's cost concentrates in hard instances where it enters retry loops, consuming millions of tokens per instance with no improvement in outcomes.

| Tier | GLM-5 Avg Cost | Nova-2-Lite Avg Cost | Ratio |
|------|---------------|---------------------|-------|
| Easy | $0.012 | $0.006 | 2x |
| Medium | $0.037 | $0.450 | 12.2x |
| Hard | $0.063 | $5.283 | 83.9x |

Total spend across all 10,000 instances:

| Model | Total Cost | Cost per Solve |
|-------|-----------|---------------|
| GLM-5 | $361 | $0.055 |
| Nova-2-Lite | $17,445 | $3.877 |

### Token Usage

| Metric | GLM-5 | Nova-2-Lite | Ratio |
|--------|-------|-------------|-------|
| Avg Input Tokens / Instance | 29.3K | 5.6M | 191x |
| Avg Output Tokens / Instance | 2.1K | 26.5K | 12.9x |
| Total Input Tokens | 293M | 55.9B | 191x |
| Total Output Tokens | 20.5M | 265M | 12.9x |

Nova-2-Lite uses 191x more input tokens than GLM-5. This is driven by instances where Nova hits the 900-1800s timeout and consumes millions of tokens each in retry loops. Despite this massive token expenditure, Nova achieves a lower solve rate.

### Time Efficiency

| Metric | GLM-5 | Nova-2-Lite | Ratio |
|--------|-------|-------------|-------|
| Avg Agent Time | 106.8s | 524.5s | 4.9x |
| Avg Total Time | 146.1s | 559.4s | 3.8x |
| Fastest Solve | ~30s | 10.5s | - |
| Slowest Run | ~262s | 1822.3s | 7x |

Nova-2-Lite is 4.9x slower on average agent time. Approximately 2,500 instances timed out at 900-1800s each, accounting for most of the difference.

### Episodes (Agent Interaction Rounds)

| Metric | GLM-5 | Nova-2-Lite | Ratio |
|--------|-------|-------------|-------|
| Avg Episodes / Instance | 7.3 | 66.5 | 9.1x |
| Min Episodes | 4 | 3 | - |
| Max Episodes | 13 | 373 | 28.7x |

Nova-2-Lite requires 9.1x more interaction episodes on average. On hard instances, Nova runs 184-373 episodes (hitting timeouts) vs GLM-5's 6-13 episodes on the same instances.

---

## S08. Head-to-Head Analysis

### Model Agreement

| Outcome | Instances | Percentage |
|---------|-------------|-----------|
| Both Solved | 4,500 | 45.0% |
| Both Failed | 3,500 | 35.0% |
| GLM-5 Only | 2,000 | 20.0% |
| Nova-2-Lite Only | 0 | 0.0% |

80.0% agreement. The models largely struggle on the same instances. When they disagree, GLM-5 always wins. There are zero instances where Nova-2-Lite solves and GLM-5 does not.

### Hard Instance Breakdown

Hard instances are the primary differentiator. GLM-5 solves 33.3% of hard instances vs Nova-2-Lite's 0%.

| Hard Instance Category | Instances | GLM-5 Solve Rate | Nova-2-Lite Solve Rate |
|--------------------------|-------------|-----------------|----------------------|
| debugging (hard) | 1,040 | 38.5% | 0% |
| data-querying (hard) | 520 | 0% | 0% |
| software-engineering (hard) | 920 | 43.5% | 0% |
| optimization (hard) | 520 | 38.5% | 0% |

On hard instances, GLM-5 converges in 6-13 episodes while Nova-2-Lite spirals into 184-373 episode loops before timing out. Data-querying hard instances resist both models entirely.

---

## S09. Failure Patterns

Instances that both models fail share common characteristics:

| Pattern | Description | Affected Categories |
|---------|-------------|-------------------|
| Multi-step reasoning chains | Requires 10+ sequential correct decisions | data-querying, software-engineering |
| Concurrency bugs | Race conditions and lifecycle management | debugging |
| Edge-case combinatorics | Dynamic programming impossible-state detection | optimization |
| Protocol-level corruption | Custom protocol recovery requiring full state-machine understanding | debugging |
| Schema sensitivity | Subtle format differences between input schemas | software-engineering |

### GLM-5 Exclusive Wins (2,000 instances)

GLM-5 solves 2,000 instances where Nova-2-Lite fails. These instances share:

- Require precise multi-step manipulation with strict output format
- Plugin dependency resolution and lifecycle management
- Complex route optimization under constraints
- Window function analytics with cumulative state

In every case, GLM-5 solves in under 13 episodes while Nova-2-Lite exhausts its timeout budget.

---

## S10. Instance Structure

Each of the 10,000 instances follows this structure:

```
datasets/{instance-name}/
    task.toml              # Metadata: category, difficulty, timeouts, resource limits
    instruction.md         # Agent-facing task description
    environment/           # Sandboxed runtime (Docker-ready)
    tests/                 # Reward signal harnesses (pytest)
    solution/              # Reference implementation
    README.md              # Human-readable instance description
```

### task.toml Schema

```toml
schema_version = "1.1"

[task]
name = "EtharaAI/{instance-name}"
description = "..."
keywords = [...]

[metadata]
difficulty = "easy" | "medium" | "hard"
category = "algorithms" | "data-querying" | "debugging" | "file-operations" | "optimization" | "software-engineering"
tags = [...]
expert_time_estimate_min = 15.0    # minutes for a senior engineer
junior_time_estimate_min = 60.0    # minutes for a junior engineer

[verifier]
timeout_sec = 300.0                # reward signal execution timeout

[agent]
timeout_sec = 600.0                # agent interaction timeout

[environment]
build_timeout_sec = 600.0
cpus = 1
memory_mb = 2048
storage_mb = 10240
gpus = 0
allow_internet = true
```

### Agent Output Structure

```
trajectories/{instance-name}/{model-id}/
    result.json              # Model-level aggregated metrics
    config.json              # Model-level configuration
    lock.json                # Concurrency lock
    {trial-uuid}/
        agent/
            trajectory.json  # Full agent interaction history (ATIF-v1.7)
            recording.cast   # Terminal session recording (asciinema)
            episode-0/       # Per-episode prompt.txt and response.txt
            episode-1/
            ...
        artifacts/           # Code files produced by agent (when applicable)
        result.json          # Trial-level detailed results (timestamps, config, reward)
        trial.log            # Full execution log (when applicable)
        verifier/
            test-stdout.txt  # Reward signal execution output
            ctrf.json        # Test results in CTRF format
            reward.txt       # Final reward: 0 or 1
```

---

## S11. Reward Signal Design

Every instance ships with deterministic, repeatable reward signals. No human judgment in the loop at scoring time.

| Property | Value |
|----------|-------|
| Signal type | Binary (0 or 1) |
| Assertion framework | pytest |
| Min signals per instance | 3 |
| Max signals per instance | 12 |
| Avg signals per instance | 5.65 |
| Total signals across dataset | 56,500 |
| Determinism | 100% (same input always produces same score) |
| Timeout enforcement | Per-instance configurable (300s-900s) |

Reward signals test functional correctness only. No style scoring, no partial credit. The instance is solved or it is not.

### Reward Signal Distribution

| Signals per Instance | Count | Percentage |
|------------------------|-------|-----------|
| 3 | 3,500 | 35.0% |
| 5 | 3,500 | 35.0% |
| 9 | 2,500 | 25.0% |
| 12 | 500 | 5.0% |

Higher signal density correlates with harder instances. The 12-signal instances test complex multi-component systems where each signal validates a distinct functional path.

---

## S12. Category Definitions

| Category | Description | Example Problems |
|----------|-------------|-----------------|
| algorithms | Implement algorithmic solutions from specification | Run-length encoding, graph traversal, sorting variants |
| data-querying | Write complex database queries against provided schemas | Multi-table joins, window functions, analytics pipelines |
| debugging | Diagnose and fix broken code with known symptoms | Race conditions, memory leaks, protocol corruption, auth bugs |
| file-operations | Parse, transform, and process structured file data | CSV extraction, column renaming, format conversion |
| optimization | Solve resource allocation and constraint satisfaction | Delivery routing, coin change, scheduling |
| software-engineering | Build and implement software systems from requirements | Plugin registries, CLI tools, pipeline aggregators, merge drivers |

---

## S13. Difficulty Calibration

Difficulty tiers are assigned based on two human estimates in each `task.toml`:

| Tier | Expert Time Estimate | Junior Time Estimate | Reward Signals | Agent Timeout |
|------|---------------------|---------------------|---------------|--------------|
| Easy | 15-30 min | 60-120 min | 3 | 600s |
| Medium | 45-60 min | 180-300 min | 5 | 900s |
| Hard | 120+ min | 600+ min | 9-12 | 1800s |

The difficulty gradient is validated by model performance: both models degrade monotonically from Easy to Hard, confirming calibration.

---

## S14. Key Findings

1. **GLM-5 dominates on efficiency.** Higher solve rate (65.0% vs 45.0%) at 48x lower total cost ($361 vs $17,445). This is not marginal. GLM-5 is categorically more efficient for agentic coding workloads.

2. **Hard instances are the differentiator.** Easy/Medium tiers show modest gaps (14-15 percentage points). Hard tier shows total divergence: 33.3% vs 0%. If you want to separate models, hard RL instances are where signal lives.

3. **Nova-2-Lite exhibits catastrophic retry behavior.** On hard instances, Nova enters 184-373 episode retry loops consuming 5-30M input tokens per instance with no improvement. This is architectural, not a tuning problem.

4. **Reward signal density predicts difficulty.** At 12 reward signals per instance, neither model solves any. At 9 signals, Nova-2-Lite scores 0% and GLM-5 drops to 40%. Signal count is a reliable proxy for instance complexity.

5. **80% model agreement.** The models fail on the same instances. When they disagree, GLM-5 always wins (2,000 instances GLM-5 only, 0 instances Nova-2-Lite only). There is no category where Nova-2-Lite has an advantage.

6. **Data-querying is GLM-5's strongest differential.** 33.3% vs 0% solve rate. Complex SQL with window functions, CTEs, and multi-step analytics separates the models cleanly.

7. **Cost per solve is the actionable metric.** GLM-5: $0.055 per solved instance. Nova-2-Lite: $3.877 per solved instance. A 70x difference that compounds at scale.

---

## S15. Policy Training

Mars reward signals feed directly into policy optimization. The training loop uses Group Sequence Policy Optimization (GSPO) with sequence-level importance sampling aligned to the binary outcome reward.

### Why GSPO

Standard policy optimization methods (PPO, GRPO) compute token-level importance ratios and clip per-token. For long agentic sequences spanning 7-13 episodes, the product of per-token ratios compounds into high variance, destabilizing training. Failed rollouts (reward = 0) produce zero gradient entirely, wasting compute.

GSPO resolves this with a single architectural change: the importance ratio is computed at the sequence level using the geometric mean of token ratios. This bounds variance regardless of sequence length and aligns the optimization unit (full rollout) with the reward unit (binary outcome). The sequence-level objective is:

```
J(θ) = E[min(s(θ)·A, clip(s(θ), 1-ε, 1+ε)·A)]

where s(θ) = (π_θ(y|x) / π_old(y|x))^(1/|y|)
```

This maps to our execution model: each rollout is one complete agent interaction (all episodes from start to solve/fail). The binary test-suite outcome provides the reward. GSPO groups 8 independent rollouts per instance and computes advantages relative to the group mean.

### Training Configuration

| Parameter | Value |
|-----------|-------|
| Policy | GSPO (sequence-level) |
| Base model | GLM-5 (65% baseline solve rate) |
| Reward source | Binary test-suite outcome (0 or 1) |
| Group size | 8 rollouts per instance per iteration |
| Clipping epsilon | 0.2 (sequence-level) |
| Learning rate | 1e-6 (cosine decay to 1e-7) |
| Batch size | 64 instances (512 rollouts) |
| Iterations | 3 epochs over full 10,000 instance pool |
| Max episode length | 13 episodes (based on GLM-5 convergence ceiling) |
| Temperature | 0.7 (rollout sampling) |

### Reward Modeling

No learned reward model. The primary reward signal is the deterministic test-suite outcome: the agent either passes all assertions (reward = 1) or does not (reward = 0). This eliminates reward hacking entirely. There is no model to overfit against because the reward is a fixed function of terminal container state.

Advantages are computed within each group of 8 rollouts:

```
A_i = (r_i - mean(r_1..r_8)) / std(r_1..r_8)
```

With binary rewards {0, 1}, any group containing both successes and failures produces non-zero advantage signal. Groups where all 8 rollouts have the same outcome produce zero advantage and no gradient update.

---

## S16. Resources

| | |
|---|---|
| GitHub | [github.com/Ethara-Ai/mars-results](https://github.com/Ethara-Ai/mars-results) |
| Dataset | [huggingface.co/datasets/ethara/Mars](https://huggingface.co/datasets/ethara/Mars) |
| Terminal-Bench Paper | [arxiv.org/abs/2601.11868](https://arxiv.org/abs/2601.11868) |

---

*Ethara AI. Project Mars.*
