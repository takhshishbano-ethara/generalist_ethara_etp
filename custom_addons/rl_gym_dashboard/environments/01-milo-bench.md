# MILO-BENCH: Complete Training & Architecture Report

**Project**: MILO-RL v4 — Long-Horizon Agent Training  
**Model**: NVIDIA Nemotron-3-Nano-30B-A3B  
**Organization**: Ethara.AI  
**Date**: May 2026  

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [The MILO-BENCH Environment](#2-the-milo-bench-environment)
3. [Base Model Architecture](#3-base-model-architecture)
4. [Training Algorithm — GTPO](#4-training-algorithm--gtpo)
5. [Reward Architecture — Gated RLVR + PRM](#5-reward-architecture--gated-rlvr--prm)
6. [MoE-Specific Training Strategy](#6-moe-specific-training-strategy)
7. [LoRA Configuration](#7-lora-configuration)
8. [Training Pipeline (3 Stages)](#8-training-pipeline-3-stages)
9. [Curriculum Design](#9-curriculum-design)
10. [Rollout Infrastructure](#10-rollout-infrastructure)
11. [Hardware & Cost](#11-hardware--cost)
12. [Monitoring & Kill Conditions](#12-monitoring--kill-conditions)
13. [Evaluation Protocol](#13-evaluation-protocol)
14. [Reward Function Design](#14-reward-function-design)
15. [Research Grounding](#15-research-grounding)
16. [Risk Analysis & Mitigations](#16-risk-analysis--mitigations)
17. [Key Design Decisions & Rationale](#17-key-design-decisions--rationale)

---

## 1. Executive Summary

MILO-RL trains a 31.6B-parameter Mixture-of-Experts model (3.2B active per token) to solve **long-horizon multi-turn software evolution tasks** — spanning 2 to 100+ consecutive production pull requests across 8 programming languages.

**Core innovations**:
- **GTPO** (Group Turn-level Policy Optimization): Segment-level importance ratios that stabilize RL training for MoE architectures with turn-level advantage assignment
- **Gated Rewards (G-RA)**: Hierarchical reward system where step-level Process Reward Model scores only accumulate when the outcome reward is positive — prevents reward hacking
- **PivotRL turn selection**: Trains only on high-variance decision points, yielding 4× compute efficiency
- **Frozen MoE router**: Expert routing weights kept frozen during RL to prevent catastrophic routing collapse

**Target performance**:
- >30% pass@1 on held-out 200 tasks
- >35% on SWE-bench Verified (500 instances)
- >15% on full MILO-bench (50 multi-PR instances)

**Budget**: $30K–$67K per training run (8×H100, 600–1350 GPU-hours)

---

## 2. The MILO-BENCH Environment

### 2.1 What Is MILO-BENCH

MILO-BENCH (Multi-Language Long-Horizon Software Evolution) leverages the methodology extended from **SWE-EVO** (arXiv:2512.18470) to evaluate agents on sustained software development over extended timelines.

| Property | Value |
|----------|-------|
| Task type | Sequences of 2–100+ consecutive production pull requests |
| Languages | 8 (Python, Rust, Go, TypeScript, JavaScript, Java, C, C++) |
| Avg files modified | 21 per instance |
| Avg lines edited | 610 per instance |
| Avg tests per instance | 874 |
| Total instances | 48 evolution tasks across 7 repositories |
| Repositories | scikit-learn, pydantic, requests, conan, dask, dvc, modin |
| Best published score | 25% resolved (frontier model + OpenHands agent) |
| SWE-bench comparison | 72.8% resolved (single-issue, much easier) |

### 2.2 Why MILO-BENCH Is Hard

Unlike single-issue benchmarks (SWE-bench), MILO-BENCH requires:

1. **Long-horizon coherence**: Maintaining context across 50+ turns of development
2. **Multi-file reasoning**: Average 21 files modified per task
3. **Version evolution**: Tasks represent real version-to-version evolution, not isolated bugs
4. **Cross-language support**: Must handle 8 programming languages
5. **Test density**: 874 tests per instance means regressions are easily caught

### 2.3 Training Scope

For MILO-RL v4, we scope to:
- **Python + Go** (covers 60-70% of MILO-bench, strongest test coverage)
- ~35 instances from the full 48 after language filtering
- Decomposed into 1200-1800 verified sub-tasks (single-PR granularity)

---

## 3. Base Model Architecture

### 3.1 Model Selection: Nemotron-3-Nano-30B-A3B

| Parameter | Value |
|-----------|-------|
| Total Parameters | 31.6B |
| Active Parameters | 3.2B (3.6B incl. embeddings) |
| Architecture | Hybrid Mamba-2 + GQA Transformer + MoE |
| Num Layers | 52 |
| Hidden Size | 2688 |
| Attention Heads | 32 (Q) / 2 (KV), GQA |
| Head Dimension | 128 |
| Mamba State Dim | 128 |
| Mamba Groups | 8 |
| Mamba Heads | 64 |
| Expert Count | 128 routable + 2 shared |
| Active Experts (top-k) | 6 per token |
| Expert FFN Hidden | 1856 |
| Shared Expert FFN | 3712 |
| Router | Sigmoid gating (MLP), fp32 |
| Context Length | Up to 1M tokens (pretrained at 8K, extended) |
| Activation | Squared ReLU |
| Normalization | RMSNorm |

### 3.2 Layer Pattern

The 52 layers use a repeating pattern of three layer types:

- **Mamba-2 (SSM)** — long-range dependencies via recurrent state (~20 layers)
- **Expert MoE FFN** — conditional computation for efficiency (~24 layers)
- **GQA Transformer attention** — sparse attention for KV cache efficiency (~8 layers)

The sparse attention placement means KV cache memory stays small even at 131K context, while Mamba layers handle long-range dependencies through recurrent state.

### 3.3 Why This Model

1. Already RL-trained by NVIDIA across multiple environments including agentic SWE tasks
2. Proven: Nemotron-Cascade 2 achieved SWE-bench 50.2% avg@4, LiveCodeBench 87.2%
3. 3.3× higher inference throughput than Qwen3-30B-A3B-Thinking
4. 1M context window for long-horizon tasks
5. Already has agentic reasoning — we're FURTHER specializing, not teaching from scratch

---

## 4. Training Algorithm — GTPO

### 4.1 Why Not Standard GRPO

Standard GRPO computes token-level importance ratios. This fails for MoE models:

- **MoE routing instability**: ~10% of experts change per gradient step. When different experts process the same token under the current vs old policy, the ratio becomes meaningless.
- **Trust region bounds scale O(T²) with sequence length**. For 50-turn episodes with 50K+ tokens, bounds become vacuous.
- **Empirical evidence**: Multiple papers (arXiv:2507.18071, 2511.20347, 2512.23075) demonstrate instability of token-level ratios in long-context or MoE settings.

### 4.2 GTPO Algorithm

GTPO uses **sequence-level importance ratios** (geometric mean of token-level ratios):

```
s_i(θ) = exp(1/|y_i| × Σ_t log[π_θ(y_{i,t} | x, y_{i,<t}) / π_θ_old(y_{i,t} | x, y_{i,<t})])
```

This compresses the entire sequence into a single scalar, avoiding compounding instability.

**Objective**:
```
J_GTPO(θ) = E[1/G × Σ_i min(s_i(θ) × Â_i, clip(s_i(θ), 1-ε_low, 1+ε_high) × Â_i)]
```

**Advantage** (group normalization):
```
Â_i = (r(x, y_i) - mean({r(x, y_j)})) / std({r(x, y_j)})
```

### 4.3 Segment-Level GTPO for Multi-Turn

For 50-turn episodes, a single ratio over the entire trajectory is too coarse. Instead, compute per RESPONSE SEGMENT — each turn's response tokens form one segment.

Each turn's response is treated as an independent "sequence" for ratio computation. The advantage is still computed at the trajectory level, but the ratio is per-segment. This prevents a single bad turn from corrupting the ratio for the entire episode.

### 4.4 GTPO Hyperparameters

| Parameter | Value | Source |
|-----------|-------|--------|
| ε_low (left clip) | 0.2 | DAPO-style asymmetric (arXiv:2503.14476) |
| ε_high (right clip) | 0.28 | DAPO-style asymmetric |
| Group size G | 8 | Variance reduction vs compute balance |
| Mini-batches per rollout | 4 | Standard |
| Learning rate | 3e-6 | Nemotron-Cascade 2 (§3.2) |
| Max grad norm | 1.0 | Standard |
| KL coefficient (β) | 0.0 | No KL penalty — LoRA + clip suffices |
| Temperature (rollout) | 1.0 | Unbiased sampling for valid ratios |
| Batch size (prompts/step) | 8 | NeMo-RL production config |
| Rollouts per prompt | 8 | = group_size G |
| Total rollouts per step | 64 | 8 prompts × 8 rollouts |
| Gradient updates per rollout | 1 | Strict on-policy |
| Importance sampling | segment | Per-turn (not token, not full-sequence) |
| Loss aggregation | token-level | Production NeMo-RL config |

### 4.5 Dual Clip (Negative Advantages)

For negative advantages (A_k < 0), apply dual clip with coefficient 5.0. This prevents the loss from becoming unboundedly negative on failed trajectories.

### 4.6 Turn-Level Advantage (GTPO Extension)

GTPO extends beyond standard sequence-level optimization by computing **return-based advantages at the turn level**:

- Each turn receives an advantage score based on how much that specific action contributed to the final outcome
- Uses a discount factor (γ=0.9) to propagate outcome reward back to earlier turns
- This enables credit assignment to individual decisions in long multi-turn episodes
- Result: +3.9% improvement on code+commonsense tasks over standard GRPO (arXiv:2511.14846)

---

## 5. Reward Architecture — Gated RLVR + PRM

### 5.1 Hierarchical Gated Rewards (G-RA Pattern)

The reward system follows a strict priority hierarchy with gating:

```
Priority hierarchy:
  Level 1 (highest): Outcome reward (binary test execution)
  Level 2: PRM step scores (execution-gated)
  Level 3: Format/structure rewards (format-gated)

Gate logic:
  Step rewards ONLY contribute when outcome reward > gate_threshold
  Format rewards ONLY contribute when outcome reward > gate_threshold AND action is well-formed
```

The gating ensures step-level and format rewards ONLY contribute when the model actually attempts the task. Without gating, models learn to maximize intermediate rewards while ignoring the objective (reward hacking documented in arXiv:2508.05170).

### 5.2 Outcome Reward (RLVR — Binary Test Execution)

| Outcome | Reward | Meaning |
|---------|--------|---------|
| All tests pass (F2P + P2P) | +1.0 | Task fully solved |
| Submitted but tests fail | -0.1 | Attempted but incorrect |
| Empty patch / no submission | -0.2 | Gave up without trying |
| Exceeded max turns (timeout) | -0.5 | Explored endlessly without committing |

- **F2P** = Fail-to-Pass tests (bug-reproducing tests that should now pass)
- **P2P** = Pass-to-Pass tests (existing tests that must not regress)

The asymmetric penalties are intentional: a model that tries and fails (-0.1) is better than one that gives up (-0.5). This shapes exploration toward attempting submissions even when uncertain.

### 5.3 Gate Threshold Design

**gate_threshold = 0.0**: Only PASS (+1.0) opens the PRM gate.

- FAIL (-0.1), EMPTY (-0.2), and TIMEOUT (-0.5) all keep PRM gated OFF
- This means PRM signal only flows for trajectories that actually solved the task
- Aligns with CG-GRPO (arXiv:2508.05170) which demonstrates that ungated PRM causes reward hacking

### 5.4 Step-Count Penalty (from LHT-SWE, arXiv:2508.03501)

A linear penalty that starts at 0 when the model exceeds a turn threshold and reaches -1.0 at maximum turns:

- Threshold and maximum vary by curriculum phase (10/15/20 turns threshold; 40/60/80 maximum)
- Replaces overlong filtering which breaks on-policy training by discarding valid trajectories
- Discourages long-winded exploration without penalizing necessary complexity

### 5.5 PRM Integration (Execution-Gated)

The PRM provides per-step scores but they are ONLY accumulated when the gate is open:

```
Final reward per trajectory:
  R_total = R_outcome + α × R_length + β × (R_outcome > 0) × Σ_t PRM_score(t)

  α = 0.1 (length penalty weight)
  β = 0.3 (PRM weight, production config)
```

The PRM uses GTPO-style turn-level advantage assignment with γ=0.9 discount factor. In production, the PRM is served via Bedrock and provides per-step quality scores.

### 5.6 PivotRL Turn Selection (Training Signal Filtering)

From PivotRL (arXiv:2603.21383): **71% of turns yield ZERO learning signal** because the model either always succeeds or always fails from that state.

For each turn state in the rollout batch:
- Compute variance of rewards across all rollouts from that state
- Compute mean reward from that state
- Include turn in training ONLY if variance > 0 AND mean < 0.8

This filters deterministic states and already-solved states.

**Result**: 4× compute efficiency. Same final accuracy with 75% fewer gradient updates.

---

## 6. MoE-Specific Training Strategy

### 6.1 Frozen Router

All router and gate parameters are frozen during RL training.

**Rationale**: The router was trained during pretraining on trillions of tokens. RL training data is orders of magnitude smaller. Unfreezing causes catastrophic routing collapse where a few experts absorb all traffic. NVIDIA confirmed empirically across multiple RL campaigns (arXiv:2512.20848).

### 6.2 Auxiliary Loss-Free Load Balancing

Uses expert bias updates (DeepSeek-style) rather than gradient-based auxiliary loss:
- Auxiliary loss coefficient: 0.0001
- Expert bias enabled with update rate: 0.001
- Load balancing type: sequential auxiliary loss

The bias term is updated via exponential moving average of expert utilization, nudging underused experts to receive more tokens without backpropagating through the router.

### 6.3 Expert Routing Configuration

| Parameter | Value |
|-----------|-------|
| Number of experts | 128 routable + 2 shared |
| Top-k per token | 6 |
| Scoring function | Sigmoid (not softmax) |
| Scaling factor | 2.5 |
| Router precision | fp32 |

- Sigmoid scoring allows multiple experts to have high scores simultaneously
- Scaling factor 2.5 amplifies score differences for decisive routing
- fp32 router prevents numerical issues in gating decisions

### 6.4 Collapse Detection

Alert conditions:
- Any single expert receives >50% of tokens (monopoly) → kill condition
- Any expert receives <1% of mean tokens (dead expert) → log warning
- Router entropy < 0.5 for 10 consecutive steps → kill condition

---

## 7. LoRA Configuration

### 7.1 Target Modules

Six module types are targeted following NVIDIA's Megatron-Bridge PEFT recipe:

| Module | Layer Type | Purpose |
|--------|-----------|---------|
| Q/K/V projection | GQA Attention | Adapt attention patterns |
| Attention output | GQA Attention | Adapt attention output mixing |
| FFN up-projection | Shared Expert | Adapt shared expert behavior |
| FFN down-projection | Shared Expert | Adapt shared expert output |
| Mamba input projection | Mamba-2 | Adapt SSM input processing |
| Mamba output projection | Mamba-2 | Adapt SSM output (excluded in production config) |

> **Note**: The production NeMo-RL config excludes the Mamba output projection, using 5 target modules instead of 6.

### 7.2 LoRA Hyperparameters

| Parameter | Design Reference | Production Config |
|-----------|-----------------|-------------------|
| Rank | 32 | 64 |
| Alpha | 32 | 256 |
| Effective multiplier | 1.0 | 4.0 |
| Dropout | 0.0 | 0.0 |
| Initialization | Xavier (A), Zero (B) | Xavier (A) |

Production training uses the higher-rank configuration for stronger adaptation capacity.

### 7.3 What We DO NOT Target

- **Routable expert MLPs** (128 experts × memory explosion; router may not route tokens to adapted experts)
- **Router/gate weights** (frozen during RL)
- **Embedding layers** (too large, minimal behavioral benefit)
- **LM head** (tied to embeddings)

### 7.4 Trainable Parameter Count

~60M parameters (LoRA adapters only) out of 31.6B total.

- LoRA weights in bf16: ~120MB
- LoRA optimizer states (Adam): ~480MB (fp32 moments)
- Total LoRA overhead: ~600MB per GPU
- <1% of model's total memory footprint

---

## 8. Training Pipeline (3 Stages)

### Stage 0: Data Preparation (4-6 weeks)

1. Start with MILO-bench 48 instances → filter to Python + Go → ~35 instances
2. Decompose multi-PR sequences → per-PR sub-tasks (~500-700)
3. Bug injection amplification → 2-3× expansion (+1000-1500 additional tasks)
4. External mining → supplement gaps (+200-400)
5. Validate: all tasks must have failing tests, passing tests, and working Docker builds

**Output**: 1200-1800 verified sub-tasks (each ≤50 turns), plus 200 held-out for evaluation

**Decomposition strategy**: Each MILO-bench instance contains multiple PRs. Split into per-PR tasks where each has a failing test (F2P), passing tests (P2P), and a Docker image with the exact repo state.

**Bug injection**: For passing tests, inject bugs (reverse the fix) to create additional instances. Validated by checking injected bug causes test failure.

**Difficulty scoring**: Based on lines changed, files touched, test complexity, estimated turns from baseline rollouts.

### Stage 1: RFT Warmup (3-5 days)

| Parameter | Value |
|-----------|-------|
| Method | Rejection sampling → SFT on passing trajectories |
| LoRA | rank=32, alpha=32, 6 target modules |
| Context | 65K tokens |
| Hardware | 8×H100, Expert Parallel |
| Learning rate | 5e-5, cosine decay, 800 warmup steps |
| Epochs | 3-6 over collected trajectories |
| Gate to proceed | ≥15% pass@1 on easy subset |

**Process**:
1. Run base model on easy+medium tasks with 8 rollouts each
2. Collect all trajectories that pass tests
3. SFT on passing trajectories with standard cross-entropy loss
4. Validate: if pass@1 < 15% on easy subset, collect more data and repeat

**Purpose**:
- Teaches the model the tool-use format (bash commands, file editing syntax)
- Provides reasonable starting policy (cold-start RL on hard tasks fails)

### Stage 2: Reinforcement Learning (6-12 weeks)

| Parameter | Value |
|-----------|-------|
| Algorithm | GTPO (segment-level, multi-turn) |
| Learning rate | 3e-6 |
| Batch | 8 prompts × 8 rollouts = 64 trajectories/step |
| Total steps | 450 |
| Curriculum | Progressive difficulty (see Section 9) |
| Reward | Gated RLVR (outcome gates PRM) |
| Turn selection | PivotRL (σ²>0, μ<0.8) |
| Router | FROZEN |
| Sampling | temp=1.0, no top_k, no min_p (unbiased) |
| Overlong handling | Penalize via R_length, do NOT filter |

---

## 9. Curriculum Design

### 9.1 Four-Phase Progressive Difficulty

| Phase | Steps | Max Turns | Difficulties | Hard Bias | Expected Success |
|-------|-------|-----------|-------------|-----------|-----------------|
| 1 | 0–50 | 10 | easy | 1.0 | 30% |
| 2 | 50–150 | 20 | easy, medium | 1.0 | 20% |
| 3 | 150–300 | 35 | all | 1.5 | 15% |
| 4 | 300–500 | 50 | all | 2.0 | 10% |

### 9.2 Phase Transition Logic

**Advancement criteria**: Advance when success_rate > 0.7 over a window of 5 steps AND current step ≥ phase end.

**Performance gates**:
- Phase 1→2: pass@1 easy > 40% AND medium > 15%
- Phase 2→3: pass@1 medium > 35% AND hard > 10%

If gates aren't met within step budget, extend current phase (never skip ahead).

### 9.3 Variance Target

Task difficulty kept in range where success variance is 0.05–0.40 (optimal for RL learning signal).

### 9.4 Hard Bias (Difficulty Oversampling)

In later phases, hard_bias > 1.0 oversamples tasks the model is failing. This ensures the model doesn't just repeat easy tasks for positive reward without improving on challenging ones.

### 9.5 Context Length Progression

| Phase | Context Window |
|-------|---------------|
| 1-2 | 65K tokens |
| 3 | 100K tokens |
| 4 | 131K tokens |

Progressive context expansion prevents OOM issues while allowing the model to handle increasingly large codebases.

---

## 10. Rollout Infrastructure

### 10.1 Docker Execution

| Parameter | Value |
|-----------|-------|
| Container registry | AWS ECR (ap-south-1) |
| Pre-built images | 150, multi-language |
| Concurrency | 64 Docker containers |
| Timeout per turn | 300 seconds |
| Timeout per episode | 1800 seconds |

Each container provides:
- Repository at exact commit state
- All dependencies pre-installed
- Test execution harness
- File system isolation

### 10.2 Agent Tool Actions (6 Available)

| Action | Purpose |
|--------|---------|
| Apply patch | Apply unified diff to files |
| Run command | Execute shell command |
| Read file | Read file contents |
| List files | List directory structure |
| Search/grep | Search file contents |
| Submit | Submit final patch and trigger grading |

### 10.3 Episode Lifecycle

1. Pull container image from registry
2. Create container with repo mounted and tests ready
3. Model interacts via tool calls (multi-turn conversation)
4. On "submit": extract final patch
5. Run F2P tests (failing tests must now pass)
6. Run P2P tests (existing tests must still pass)
7. Tear down container

### 10.4 Rollout Timing

| Phase | Turns/episode | Time per batch | Batches/step | Total per step |
|-------|---------------|----------------|--------------|----------------|
| Phase 1 | ~10 | ~10 min | 8 | ~1.5 hrs |
| Phase 2 | ~20 | ~20 min | 8 | ~3 hrs |
| Phase 3 | ~35 | ~35 min | 8 | ~4.5 hrs |
| Phase 4 | ~50 | ~50 min | 8 | ~6.5 hrs |

---

## 11. Hardware & Cost

### 11.1 Compute Configuration

| Parameter | Value |
|-----------|-------|
| Instance | p5.48xlarge (8×H100 80GB) |
| Parallelism | EP=8 (Expert Parallel across 8 GPUs) |
| Memory per GPU | ~4GB model shard + ~8GB LoRA + activations + KV cache |
| Inference engine | vLLM (TP=2, 4 inference replicas) |

**Expert Parallelism (EP=8)**: Each GPU holds 16 routable experts + both shared experts. All-to-all communication routes tokens to correct GPU.

### 11.2 Cost Estimate

| Component | Estimate |
|-----------|----------|
| Per step (wall clock) | ~2-4 hours avg |
| Total steps | 450 |
| Total GPU-hours | 600-1350 |
| Spot rate | ~$50/hr |
| **Cost per run** | **$30K-67K** |
| Planned runs | 2 |
| **Total budget** | **$60K-135K** |

**Budget allocation**:
- Run 1: Full training pipeline, identify failure modes
- Run 2: Adjusted hyperparameters based on Run 1
- Reserve 20% for ablations and debugging

---

## 12. Monitoring & Kill Conditions

### 12.1 Kill Conditions (Automatic Training Stop)

| # | Condition | Threshold | Window |
|---|-----------|-----------|--------|
| 1 | Reward collapse | mean reward < 0.01 | 20 consecutive steps |
| 2 | Gradient explosion | grad norm > 100 | 5 consecutive steps |
| 3 | KL divergence | KL(π_θ ‖ π_ref) > 50 | Instantaneous |
| 4 | Loss NaN/Inf | any non-finite value | Instantaneous |
| 5 | Expert collapse | any expert >50% tokens | 10 consecutive steps |
| 6 | Zero-gradient | >90% batches zero advantage | 15 consecutive steps |
| 7 | Router entropy | < 0.5 | 10 consecutive steps |
| 8 | Echo trap | repetition ratio > 0.6 | Auto-recovery |
| 9 | Docker cascade | 3+ consecutive failures | Fatal stop |

### 12.2 Severity Levels

- **Warning**: Log only, continue training
- **Recoverable**: Auto-adjust (temperature↑, learning rate↓, batch↓)
- **Fatal**: Stop training, save checkpoint immediately

### 12.3 Logging

**Every step**:
- Mean/std/min/max reward
- Gradient norm (global + per-module)
- Expert utilization histogram (128 experts)
- Router entropy per layer
- Mean turns-to-solve for passing trajectories
- PivotRL filtering rate
- Segment-level ratio statistics
- KL divergence from reference

**Trajectory inspection (every step)**:
- Top-5 trajectories by reward
- Bottom-5 trajectories by reward
- 5 random trajectories
- Full text saved to S3

**Checkpoints**:
- Every 10 steps: full LoRA + optimizer state
- Every 100 steps: held-out evaluation (200 tasks, pass@1)
- Best checkpoint tracked by held-out pass@1

---

## 13. Evaluation Protocol

### 13.1 Primary Metrics

| Metric | Description | Target |
|--------|-------------|--------|
| Pass@1 | Single-attempt success on held-out 200 tasks | >30% |
| Pass@8 | Best-of-8 attempts on held-out set | >55% |
| Avg turns-to-solve | Mean turn count on passing trajectories | <25 |

### 13.2 Secondary Metrics

| Metric | Description | Target |
|--------|-------------|--------|
| Full MILO-bench score | Multi-PR end-to-end (original 48 instances) | >15% |
| SWE-bench Verified | External benchmark, 500 instances | >35% |
| Format compliance | Tool calls well-formed (parseable) | >95% |
| Completion rate | Model submits vs timing out | >80% |
| Regression rate | P2P tests broken by submitted patches | <5% |

### 13.3 Evaluation Protocol

1. **Held-out eval (every 100 steps)**: pass@1 on 200 tasks, greedy decoding (temp=0), per-difficulty breakdown
2. **Full eval (end of training)**: pass@8 on all held-out, full MILO-bench, SWE-bench Verified, compare against base + Stage 1
3. **Ablation checkpoints**: Save at phase transitions for ablation studies

---

## 14. Reward Function Design

### 14.1 Five Reward Modes

| Mode | Description |
|------|-------------|
| Sparse binary | 1.0 if resolved, 0.0 if not |
| Sparse centered | +1.0 if resolved, -1.0 if not |
| Dense partial | Weighted combination of tests passing + tests kept |
| Shaped | Dense partial + file/patch overlap signals |
| Gold shaped | Shaped + gold-solution similarity (decays to 0 over training) |

### 14.2 Reward Signals

| Signal | Source | Range | Weight |
|--------|--------|-------|--------|
| Resolved | Test harness | {0,1} | 5.0 |
| F2P ratio | Test execution | [0,1] | 1.0 |
| P2P ratio | Test execution | [0,1] | 0.3 |
| Patch applies | Harness | {0,1} | 0.2 |
| File overlap | Jaccard(predicted vs gold files) | [0,1] | 0.1 |
| Line overlap | Jaccard(predicted vs gold lines) | [0,1] | 1.0 |
| Hunk overlap | Jaccard(predicted vs gold hunks) | [0,1] | 0.5 |
| Test intrusion | Penalty for modifying test files | [0,1] | -0.5 |
| Verifier confidence | Trained verifier model | [0,1] | 0.3 |
| Length overflow | Excess turns / target | [0,∞) | -0.2 |

### 14.3 Gold-Patch Decay Schedule

Gold-patch similarity weights decay from 1.0 to 0.0 over training. By end of training, the policy must solve without gold-patch assistance. Without decay, policy learns to copy gold prefix (DeepSeek-Math finding).

### 14.4 Advantage Computation

Group-normalized advantage with epsilon=1e-4 (not default 1e-8) so groups with identical rewards produce near-zero advantages instead of spurious noise.

---

## 15. Research Grounding

| Component | Paper | arXiv ID | Key Contribution |
|-----------|-------|----------|-----------------|
| GTPO | Group Turn-level Policy Optimization | 2511.14846 | Turn-level reward assignment, return-based advantage |
| GSPO | Group Sequence Policy Optimization | 2507.18071 | Sequence-level ratio stabilizes MoE RL |
| PivotRL | High-Variance Turn Selection | 2603.21383 | 4× efficiency by training only on pivot turns |
| LHT-SWE | Long-Horizon Training | 2508.03501 | Two-stage curriculum, 11%→39% SWE-bench |
| Gated Rewards | G-RA Hierarchical Gating | 2508.10548 | Outcome gates step rewards, 47%→94% completion |
| Nemotron-Cascade 2 | NVIDIA 30B-A3B Recipe | 2603.19220 | SWE-bench 50.2%, frozen router approach |
| Nemotron 3 Nano | Model Architecture | 2512.20848 | Hybrid Mamba-Transformer-MoE, frozen router RL |
| SAPO | Soft Adaptive Clipping | 2511.20347 | Fallback if clipping is too aggressive |
| ReCode / CG-GRPO | Gated PRM | 2508.05170 | Execution-gated PRM prevents reward hacking |
| MILO-BENCH / SWE-EVO | Benchmark | 2512.18470 | Multi-language long-horizon evolution |
| DAPO | Asymmetric Clipping | 2503.14476 | Clip-Higher, dynamic sampling |
| Scaling Inter-RL | Curriculum | 2509.08755 | Progressive difficulty for RL agents |
| SWE-RL | Rule-based RL | 2502.18449 | Gold-patch similarity reward → 41% SWE-bench |

---

## 16. Risk Analysis & Mitigations

### 16.1 Technical Risks

| Risk | Impact | Likelihood | Mitigation |
|------|--------|-----------|-----------|
| MoE routing collapse | Training fails | Medium | Frozen router + collapse detection + kill condition |
| Reward hacking (PRM) | Model games rewards | High | G-RA gating + production PRM weight (0.3) |
| Context length OOM | Reduced batch | Medium | Curriculum starts at 65K, grows to 131K |
| Docker infrastructure failures | Stalled rollouts | Low | 300s timeout, 64 parallel containers, retry logic |
| Cold-start RL failure | No learning | Medium | Stage 1 RFT warmup ensures ≥15% pass@1 |
| Overlong episodes | Wasted compute | High | Length penalty + turn limits per phase |
| Gold-patch leakage | Overfitting | Medium | Decay schedule → 0.0 multiplier by end |

### 16.2 Budget Risks

| Risk | Impact | Mitigation |
|------|--------|-----------|
| Training converges slowly | Over budget | Phase gates — extend phase instead of proceeding |
| Need 3+ runs | $200K+ | Reserve 20% for ablations, start with conservative params |
| Spot instance preemption | Lost progress | Checkpoint every 10 steps, fast recovery |

---

## 17. Key Design Decisions & Rationale

### 17.1 Why Segment-Level Ratios (Not Token or Full-Sequence)

| Granularity | Problem |
|-------------|---------|
| Token-level | MoE routing changes ~10% per step → meaningless ratios |
| Full-sequence | Too coarse for 50-turn episodes → single bad turn corrupts everything |
| **Segment-level** | **Goldilocks: stable enough for MoE, fine-grained enough per turn** |

### 17.2 Why Frozen Router

The router saw trillions of tokens during pretraining. RL data is 5-6 orders of magnitude smaller. Unfreezing causes:
- Expert monopoly (few experts absorb all traffic)
- Routing oscillation (different experts per step)
- Load imbalance (some experts go permanently dead)

NVIDIA confirmed empirically across Nemotron-3 and Cascade-2 RL campaigns.

### 17.3 Why Gated Rewards (Not Raw PRM)

Raw PRM (no gating) causes reward hacking:
- Model learns to produce steps that score high with PRM
- But these steps don't actually solve the problem
- The PRM becomes a proxy that diverges from the true objective

Gating ensures PRM only provides signal when the model actually solves the task.

### 17.4 Why No KL Penalty

With LoRA + GTPO clipping, the model can't drift far from reference:
- LoRA: only ~60M params trainable out of 31.6B — inherent regularization
- Clipping: prevents large policy updates per step
- Adding KL penalty on top would be over-regularized → slower learning

### 17.5 Why PivotRL (Not Training All Turns)

71% of turns are uninformative:
- Some turns always succeed regardless of action → zero gradient
- Some turns always fail regardless of action → zero useful gradient
- Training on these wastes compute without improving policy

PivotRL identifies the ~29% of turns where the model's action actually matters.

### 17.6 Why Asymmetric Outcome Penalties

| Outcome | Penalty | Reasoning |
|---------|---------|-----------|
| PASS | +1.0 | Clear reward for success |
| FAIL | -0.1 | Trying and failing is still valuable exploration |
| EMPTY | -0.2 | Giving up is worse than trying |
| TIMEOUT | -0.5 | Endless exploration without committing is worst |

This encourages the model to attempt submissions even when uncertain, while harshly penalizing endless exploration without commitment.

### 17.7 Why Temperature 1.0 (No top_k/min_p)

From LHT-SWE (arXiv:2508.03501):
- top_k/min_p truncation corrupts importance sampling ratios
- GTPO requires unbiased π_θ(y|x) for valid ratio computation
- Temperature 1.0 gives the true model distribution
- Any truncation makes the ratio π_θ/π_old invalid because both policies are truncated differently

---

## Appendix: Evaluation Baselines

| System | SWE-bench Verified | MILO-bench | Notes |
|--------|-------------------|------------|-------|
| Frontier model + OpenHands | — | 25% | Best published on MILO-bench |
| Nemotron-Cascade 2 | 50.2% (avg@4) | — | Same base architecture |
| SWE-agent-LM-32B | 40.2% | — | SWE-Smith trained, dense model |
| **Our target (3.2B active)** | **>35%** | **>15%** | **10× smaller than baselines** |

---

## Links

### Original Research
- **Paper (SWE-EVO):** https://arxiv.org/abs/2512.18470

### Ethara MILO-Bench
- **Ethara project dashboard:** https://projects.ethara.ai/milobench
- **Ethara.AI:** https://ethara.ai

---

*Document version: 2.0*  
*Last updated: May 2026*  
*Source: ARCHITECTURE_PLAN.md, NeMo-RL production config, PROJECTS.MD*
