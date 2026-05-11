# Janus

**Ethara.AI RL Environment · Process-Reward Multimodal Tool Use**

Extends: [Agentic-MME](https://arxiv.org/abs/2604.03016)

---

## What Is Janus

Janus trains models on multi-step multimodal tool use. Given a degraded or ambiguous visual input, the model must **plan and execute a chain of visual manipulation tools coordinated with open-web search** to recover the target information. Unlike outcome-only environments, stepwise checkpoints provide reward at each intermediate tool invocation.

**For frontier labs:** Multi-step tool composition is the critical unsolved capability for autonomous multimodal models. Janus teaches models to select, sequence, and recover from failed tool invocations under ambiguity.

---

## The Problem

Current multimodal AI models are **passive observers** — they look at images and guess answers. The gap isn't knowledge — it's **agency**.

| Capability                          | State of the Art | Human | Gap             |
| ----------------------------------- | ---------------- | ----- | --------------- |
| Single visual operation (L1)        | 70.6%            | 99.0% | 28.4%           |
| Multi-step workflows (L2)           | 47.4%            | 92.6% | 45.2%           |
| Synergistic vision + retrieval (L3) | 33.3%            | 82.3% | **49.0%** |

Even frontier models collapse on L3 (synergistic iterative reasoning), making it a natural RL target.

**Source**: Agentic-MME benchmark (arXiv:2604.03016, April 2026) — 418 real-world tasks, 2,000+ human-annotated stepwise checkpoints, 6 domains, 10+ person-hours annotation per task.

---

## The Insight

The Agentic-MME benchmark reveals that training models to **act** (not just perceive) requires:

1. **Process-level supervision** — not just final answers, but verifiable intermediate steps
2. **Dual-axis reward signals** — visual tool correctness (V-axis) AND retrieval strategy quality (S-axis)
3. **Efficiency penalties** — agents must solve tasks with minimal redundant actions (overthinking metric)

This is exactly what Reinforcement Learning excels at — **training agents through structured reward signals on sequential decision-making**.

---

## What Ethara Extends

| Dimension          | Original Agentic-MME       | Janus                                                                     |
| ------------------ | -------------------------- | ------------------------------------------------------------------------- |
| Use-mode           | Process-verified benchmark | RL environment with process reward                                        |
| Reward             | Checkpoint-level scoring   | Per-step reward on V-axis and S-axis in the training loop                 |
| Input distribution | Clean multimodal tasks     | **Degraded / ambiguous visual inputs** to force tool-chain use      |
| Tool set           | Visual tools + web search  | Same, packaged as an RL-ready environment with stepwise credit assignment |

Janus is the natural extension of Agentic-MME: take the per-step verifier the authors built and *reuse it as a reward function* for reinforcement learning, specifically aimed at degraded-input recovery.

---

## Architecture

### Action Space (17 tools)

| Visual Expansion (13 tools)                       | Knowledge Expansion (4 tools) |
| ------------------------------------------------- | ----------------------------- |
| crop, rotate, flip, resize, enhance               | google_search                 |
| grayscale, autocontrast, blur, sharpen            | google_lens_search            |
| denoise, edge_detect, invert, equalize, threshold | fetch_webpage, download_image |

### Observation Space

- Current image state (original + all transformed images)
- Conversation history (multi-turn reasoning context)
- Tool execution results and search payloads

### Reward Structure (Multi-Signal, Process-Verified)

| Signal                          | Source                                           | Weight  | What It Trains        |
| ------------------------------- | ------------------------------------------------ | ------- | --------------------- |
| **Final Answer Accuracy** | Exact match against ground truth                 | Primary | Correctness           |
| **V-tool reward**         | Did agent invoke the right visual tool?          | Dense   | Tool selection policy |
| **V-true reward**         | Did produced artifact contain required evidence? | Dense   | Tool parameterization |
| **S-axis reward**         | Was search strategy effective?                   | Dense   | Retrieval planning    |
| **Efficiency bonus**      | Fewer steps than human trajectory = bonus        | Shaping | Minimal overthinking  |
| **Overthink penalty**     | `max(0, C_agent - C_human) / (C_human + 1)`    | Shaping | Focused execution     |

### Curriculum Design (3 progressive levels)

```
Level 1 → Level 2 → Level 3
(single-step)  (multi-step)  (synergistic interleaved)
```

- **Level 1** (48.6% of tasks): Single decisive visual operation — learn basic perception-action loop
- **Level 2** (32.1% of tasks): Short multi-step workflows — learn tool chaining and search coordination
- **Level 3** (19.4% of tasks): Iterative hypothesis-verification loops — learn cross-modal synergy under ambiguity

### System Diagram

```
┌─────────────────────────────────────────────────────────┐
│                    RL Training Loop                     │
├─────────────────────────────────────────────────────────┤
│                                                         │
│  ┌──────────┐    ┌──────────────┐    ┌──────────────┐   │
│  │  Policy  │───▶│ Environment  │───▶│   Reward     │   │
│  │  (MLLM)  │◀───│ (Agentic-MME)│◀───│  Calculator  │   │
│  └──────────┘    └──────────────┘    └──────────────┘   │
│       │                 │                    │          │
│       ▼                 ▼                    ▼          │
│  Action:           State:              Signals:         │
│  - tool selection  - image stack       - V-tool (dense) │
│  - parameters      - conv history      - V-true (dense) │
│  - search query    - artifacts         - S-axis (dense) │
│  - code block      - tool results      - Acc (sparse)   │
│                                        - Efficiency     │
└─────────────────────────────────────────────────────────┘
```

## Evidence: Why This Works

### 1. Dense Intermediate Rewards Solve the Sparse Signal Problem

Traditional RL for LLMs uses only final-answer correctness (sparse). Agentic-MME provides **2,000+ stepwise checkpoints** with human-verified intermediate states — enabling dense reward shaping at every decision point.

### 2. The Overthinking Problem is Quantifiable and Trainable

| Model               | Avg Tool Calls/Task | Overthink Ratio | Accuracy |
| ------------------- | ------------------- | --------------- | -------- |
| Human Reference     | 2.15                | —              | 93.8%    |
| Gemini 3 Pro (best) | 4.66                | 0.80            | 56.3%    |
| GPT-5-mini          | 7.22                | 3.36            | 33.5%    |
| DeepeyesV2          | 1.95                | 0.00            | 22.5%    |

Models either **under-explore** (DeepeyesV2: 0 overthink but 22.5% acc) or **over-explore** (GPT-5-mini: 3.36x overthink, 33.5% acc). RL with calibrated efficiency rewards directly optimizes this tradeoff.

### 3. Existing RL-Trained Models Already Show Signal

- **Thyme-RL** (an RL-trained model in the benchmark) shows it can learn tool invocation (`V-tool = 63.3%` on L1) but fails at parameterization (`V-true = 13.0%`)
- This proves the **policy gradient signal exists** — the model learns WHEN to act, but not yet HOW to act precisely
- Janus targets exactly this gap with fine-grained V-true rewards

### 4. Tool APIs vs Code Generation — A Natural Policy Structure

The benchmark's dual interface (Atomic tool-calling vs. Code generation) maps directly to RL policy architectures:

- **Atomic mode** → Discrete action selection over structured tool schemas (natural for PPO/DPO)
- **Code mode** → Sequential token generation with execution feedback (natural for RLHF/GRPO)

### 5. Error Taxonomy Reveals Trainable Failure Modes

| Failure Mode                         | Frequency  | RL Solution                         |
| ------------------------------------ | ---------- | ----------------------------------- |
| Reluctance to act (passive guessing) | ~50%       | Reward for tool invocation (V-tool) |
| Overthinking collapse (loop)         | High       | Overthink penalty + episode budget  |
| Unfaithful execution (wrong params)  | Persistent | Dense V-true rewards                |
| Tool misexecution (syntax errors)    | Code-mode  | Execution feedback in reward        |

---

## Policy Optimization

### Algorithm: GRPO with Turn-Level Advantages (GRPO-ATS)

We use **Group Relative Policy Optimization (GRPO)** as the primary training algorithm — specifically, the GRPO-ATS variant with dual-temperature sampling and turn-level advantage estimation.

**Why GRPO over PPO/DPO/REINFORCE:**

| Algorithm   | Why Not                                                                                                                                              |
| ----------- | ---------------------------------------------------------------------------------------------------------------------------------------------------- |
| PPO         | Requires a value network V(s) over growing image galleries + conversation history — intractable for variable-length multimodal trajectories (1–15 steps) |
| DPO         | Fundamentally single-turn; cannot exploit dense intermediate checkpoint signals                                                                      |
| REINFORCE   | High variance with sparse rewards; GRPO's group-relative baseline reduces variance without a learned baseline                                       |
| Actor-Critic | Expanding observation space (images accumulate in context) makes critic architecturally complex                                                    |

**Why GRPO works here:** For each task, sample G=8–16 trajectory rollouts from the current policy. Compute group-relative advantages at each *turn* (tool invocation boundary):

```
Aᵢ_t = (Rᵢ_t - mean(Rʲ_t)_{j=1..G}) / std(Rʲ_t)
```

This eliminates the value network entirely while providing step-level credit assignment — critical for a 418-task environment where data efficiency matters.

### Dual-Temperature Sampling (from Thyme-RL)

| Generation Phase     | Temperature | Rationale                                               |
| -------------------- | ----------- | ------------------------------------------------------- |
| Reasoning / planning | T = 1.0     | Encourages exploration of tool strategies               |
| Code generation      | T = 0.0     | Ensures syntactic validity (prevents Tool-Misexecution) |
| Tool-call JSON       | T = 0.0     | Ensures valid parameter schemas                         |

This directly addresses the **Tool-Misexecution** failure mode (syntax errors, invalid args) — by using deterministic decoding for structured outputs, we eliminate an entire error class at zero cost.

### Factored Policy Architecture

```
π = π_tool(tool | s_t) × π_params(θ | tool, s_t) × π_stop(stop | s_t)
```

| Head          | Type                           | Purpose                                    |
| ------------- | ------------------------------ | ------------------------------------------ |
| Tool selector | 18-way softmax (17 tools + answer) | Which tool to invoke next                |
| Param head    | Tool-conditioned regression    | bbox coords, angles, scale factors         |
| Stop head     | Binary sigmoid                 | When to emit final answer                  |

The factored decomposition allows **separate credit assignment** — the model gets partial reward for selecting the right tool even with wrong parameters (addressing the Thyme-RL finding: V-tool=63.3% but V-true=13.0%).

### Training Pipeline (4 Phases)

```
Phase 0: SFT Warmstart (200–500 GPU hours)
  └─ Human expert demonstrations (2.15 calls/task average)
  └─ Teaches tool format, calling patterns, image naming conventions

Phase 1: GRPO on L1 Only (V-tool + stop penalty)
  └─ Learn tool selection and when to stop
  └─ Single-step tasks → clean gradient signal

Phase 2: GRPO on L1+L2 (V-tool + V-true + params)
  └─ Learn parameter precision
  └─ Multi-step tasks → credit assignment across turns

Phase 3: GRPO on All Levels (Full composite reward incl. S-axis)
  └─ Learn search integration and cross-modal synergy
  └─ L3 synergistic tasks → interleaved visual + knowledge

Phase 4: Efficiency Refinement (Full + overthink penalty)
  └─ Learn parsimony — do NOT enable efficiency penalty earlier
  └─ Early efficiency pressure prevents exploration of correct-but-long strategies
```

### KL Anchor to Prevent Overthinking Collapse

```
L_total = L_GRPO - β_KL × KL(π || π_SFT)
```

Initialize β=0.1, anneal to 0.01 across phases. The SFT policy embeds human-level efficiency (2.15 calls/task) — KL divergence from it creates implicit pressure against degenerate exploration loops.

### Key Design Decisions (with paper references)

| Decision                                      | Reference                                    | Finding                                                                     |
| --------------------------------------------- | -------------------------------------------- | --------------------------------------------------------------------------- |
| Turn-level advantages (not token or trajectory) | CM2 (arXiv:2602.12268)                      | Turn-level is optimal; step-level amplifies noise                           |
| Dual temperature sampling                     | Thyme-RL (arXiv:2508.11630)                 | T=1 reasoning + T=0 code prevents syntax errors entirely                   |
| Group-relative baseline (no value network)    | DeepSeek-R1 (arXiv:2501.12948)              | GRPO > PPO for multi-step reasoning without value networks                 |
| Multiplicative reward composition             | ToolRLA (arXiv:2603.01620)                  | Wrong tool name collapses reward regardless of param quality — 7pp gain     |
| Staged curriculum (L1→L2→L3)                | CodeV/TAPO (arXiv:2511.19661)               | Dense process rewards on tool outputs are directly applicable here          |
| Reward-conditioned sampling if variance collapse | RC-GRPO (arXiv:2602.03025)                | Multi-turn SFT policies tend to collapse; conditioning prevents degeneracy  |

---

## Reward Modelling

### Design Principle: Hybrid Process-Outcome with No Learned Reward Model

The Agentic-MME checkpoint system **IS already a process reward model** — it's a rule-based PRM with 2,000+ annotations. We do not train a neural PRM. Instead, we wire the existing verification infrastructure directly into the training loop.

### Composite Reward Function

```python
R(τ) = Σ_t [α_tool × r_vtool(t) + α_true × r_vtrue(t) + α_search × r_search(t)]
       + β_acc × r_answer
       - η × efficiency_penalty(n_calls, n_ref)
       - λ_repeat × n_redundant
```

| Signal               | Weight (α/β) | Cost to Compute    | Frequency in Training     |
| -------------------- | ------------ | ------------------ | ------------------------- |
| V-tool (AST match)   | 0.30         | O(1), deterministic | Every gradient update     |
| V-true (visual judge) | 0.25        | 1 LLM call/ckpt   | Every K=4 episodes (cached) |
| S-axis (search judge) | 0.25        | 2 LLM calls/ckpt  | Every K=4 episodes (cached) |
| Final answer (Acc)   | 0.40         | O(1), rule-based   | Every gradient update     |
| Efficiency penalty   | 0.10         | O(1), arithmetic   | Every gradient update     |
| Redundancy penalty   | 0.15         | O(1), IoU check    | Every gradient update     |

### Reward Decomposition by Signal Type

#### 1. V-tool Reward (Deterministic, Free, Every Step)

At each step t, check if the invoked tool matches any unsatisfied checkpoint's `expected_op`:

```python
r_vtool(t) = 1.0 if tool_invoked matches checkpoint requirement
             0.0 otherwise
```

Source: AST-based tracer in `ast_ops.py` → `infer_ops_and_saves()` already extracts canonical operations from code traces. Zero LLM cost.

#### 2. V-true Reward (Visual Faithfulness Judge)

For steps where V-tool passes, query a VLM judge on the output artifact:

```python
r_vtrue(t) = VLM_judge(
    image=transformed_image_N.png,
    question=checkpoint.visual_question,  # e.g., "What road name is visible?"
    expected=checkpoint.expected_answer
)
```

**Cache aggressively** — same (image, question) pair always yields same judgment. Use cheap judge (gpt-4o-mini). Compute every K=4 rollouts, reuse cached results for intervening episodes.

#### 3. S-axis Reward (Search Strategy Judge)

For search actions, decompose into intent + retrieval:

```python
r_search(t) = 0.4 × r_intent + 0.6 × r_retrieval

r_intent   = LLM_judge("Does query target the right entity?")
r_retrieval = LLM_judge("Do results contain expected answer?")
```

This factored signal teaches both query formulation and result extraction independently — addressing the **Bad search query** failure mode (15% of errors).

#### 4. Efficiency Penalty (Non-Linear, Increasing)

```python
def efficiency_penalty(n_calls, n_ref):
    """Quadratic penalty: first extra call costs 0.05,
       5th extra costs 0.25 — rapidly discourages long tails."""
    excess = max(0, n_calls - n_ref)
    return min(1.0, 0.05 * excess + 0.02 * excess**2)
```

Additionally, a constant **per-step cost** of -0.02 for every tool call regardless of correctness creates continuous pressure toward shorter trajectories.

**Critical**: Do NOT enable until Phase 4. Early efficiency pressure prevents exploration of correct-but-long strategies, which are prerequisite for learning efficient ones.

#### 5. Redundancy Penalty

If the same tool is called with parameter IoU > 0.8 (e.g., two near-identical crops), the second call receives zero reward and full efficiency penalty:

```python
def is_redundant(action_t, action_history):
    for prev in action_history:
        if prev.tool == action_t.tool:
            if bbox_iou(prev.params, action_t.params) > 0.8:
                return True
    return False
```

This prevents **shotgunning** — invoking every tool once to "collect" checkpoint rewards.

### Multiplicative vs Additive Composition (Critical Design Choice)

Following ToolRLA (arXiv:2603.01620), we use **multiplicative** composition for checkpoint verification:

```python
# WRONG (additive): allows correct answer via wrong process
R = 0.5 * checkpoint_score + 0.5 * outcome

# CORRECT (multiplicative): wrong process collapses reward
R = checkpoint_fidelity × (0.6 * outcome + 0.4 * process_bonus)
```

A missed required operation should **collapse** the overall reward — this prevents the model from learning shortcuts that produce correct answers without faithful tool use (the exact problem CodeV identified: "high final-answer accuracy often hides unfaithful visual reasoning").

### Task-Adaptive Dual-Axis Weighting

For tasks requiring BOTH visual AND search:

```python
w_v = n_visual_checkpoints / n_total_checkpoints  # task-specific
w_s = n_search_checkpoints / n_total_checkpoints  # task-specific

R_process = w_v × R_visual + w_s × R_search
```

This naturally weights tasks by their actual composition:
- L1 tasks (visual only): w_v ≈ 1.0, w_s ≈ 0.0
- L2 tasks (visual + search): w_v ≈ 0.5, w_s ≈ 0.5
- L3 tasks (deeply interleaved): varies per task

### Stopping Reward (Addresses Bimodal Failure)

Train the stop head with explicit signal:

| Condition                                  | Reward   | Purpose                      |
| ------------------------------------------ | -------- | ---------------------------- |
| Stop after all checkpoints satisfied       | +1.0     | Reward correct termination   |
| Stop before any checkpoint satisfied       | -0.5     | Penalize premature guessing  |
| Each step after all checkpoints satisfied  | -0.1     | Penalize overthinking        |

This addresses the **bimodal failure**: models either stop too early (passive guessing, ~50% of errors) or never stop (overthinking collapse, ~12% of errors).

### Partial Credit for Tool Selection (Factored Reward)

```python
r_step = r_tool_selection × (0.4 + 0.6 × r_param_quality)
```

- `r_tool_selection = 1.0` if correct tool (from V-tool checkpoint)
- `r_param_quality` = IoU for bbox crops, normalized angular distance for rotation, etc.

The model gets **40% credit** for selecting the right tool even with wrong parameters. This prevents the gradient signal from vanishing entirely on partially-correct actions — critical for the 19% "correct tool, wrong params" failure mode.

### Implementation Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  Base VLM (Qwen2.5-VL-7B or similar)                       │
├─────────────────────────────────────────────────────────────┤
│  LoRA adapters (rank 64, all attention layers)              │
├───────────────┬────────────────┬────────────────────────────┤
│  Tool Head    │  Param Head    │  Stop Head                 │
│  (18-way      │  (per-tool     │  (binary sigmoid)          │
│  softmax)     │  regression)   │                            │
└───────────────┴────────────────┴────────────────────────────┘
                    ↓ GRPO update ↓
┌─────────────────────────────────────────────────────────────┐
│  Reward Computer (deterministic — NO neural reward model)   │
│  ├── V-tool: AST matching via ast_ops.py (free)             │
│  ├── V-true: Cached VLM judge (periodic, amortized)         │
│  ├── S-axis: Cached LLM judge (periodic, amortized)         │
│  ├── Accuracy: Rule-based exact match (free)                │
│  ├── Efficiency: Arithmetic penalty (free)                  │
│  └── Redundancy: IoU-based duplicate detection (free)       │
└─────────────────────────────────────────────────────────────┘
```

### Data Efficiency Mitigations (418 tasks is small for RL)

| Strategy                       | Details                                                                         |
| ------------------------------ | ------------------------------------------------------------------------------- |
| Multiple rollouts per task     | G=8–16 trajectories per task per epoch                                          |
| Image augmentation             | Random crops/rotations of originals → synthetic L1 tasks                        |
| Question paraphrasing          | Generate 3–5 paraphrases per task → multiplies effective task count              |
| Cached deterministic search    | All search results stored per-task JSON → reproducible RL without live API cost |
| Checkpoint-based curriculum    | Reweight undersampled task types and failure modes                               |

### Key References

| Paper                              | Contribution to This Design                                                  |
| ---------------------------------- | ---------------------------------------------------------------------------- |
| DeepSeek-R1 (arXiv:2501.12948)     | GRPO algorithm — eliminates value network for multi-step reasoning           |
| Thyme-RL (arXiv:2508.11630)        | GRPO-ATS dual temperature — T=1 reasoning + T=0 code/JSON                   |
| CodeV/TAPO (arXiv:2511.19661)      | Tool-aware process rewards on visual artifacts — closest analog              |
| CM2 (arXiv:2602.12268)             | Turn-level advantages optimal for multi-turn tool use                         |
| ToolRLA (arXiv:2603.01620)         | Multiplicative reward composition > additive (7pp improvement)               |
| Kimi-K2.5 PARL (arXiv:2602.02276) | Frozen sub-agent RL + staged reward shaping for tool execution               |
| MT-GRPO+IRC (arXiv:2604.02869)     | Warning: naïve dense rewards can HURT (up to 14pp). Calibrate discriminativeness |
| StepTool (arXiv:2410.07745)        | Step-grained reward shaping for multi-step tool use                          |
| SWEET-RL (arXiv:2503.15478)        | Asymmetric critic with oracle info at training time                          |

---

## Competitive Position

### Why Now

1. **RL for LLMs is proven** — DeepSeek-R1, OpenAI o-series, and Thyme-RL demonstrate that RL training produces qualitatively different reasoning capabilities
2. **Multimodal agents are the next frontier** — Every major lab (OpenAI, Google, Anthropic) is building tool-using vision agents
3. **Process-verified data is the bottleneck** — Agentic-MME's 10+ person-hours/task annotation is prohibitively expensive to replicate; we have first-mover access

### Competitive Moat

| Advantage                                  | Why It's Defensible                                                          |
| ------------------------------------------ | ---------------------------------------------------------------------------- |
| **Process-verified training data**   | 2,000+ checkpoints, 10+ hrs/task annotation — extremely costly to replicate |
| **Dual-axis reward signals**         | No other benchmark provides both visual and retrieval process verification   |
| **Calibrated difficulty curriculum** | L1→L2→L3 progression with empirically validated difficulty separation      |
| **Deterministic replay**             | Cached search results enable reproducible RL training without live API costs |
| **Unified interface**                | Same environment supports code-gen and tool-calling policies                 |

---

### Benchmark Stats

| Item                 | Value                                                                     |
| -------------------- | ------------------------------------------------------------------------- |
| Real-world tasks     | 418                                                                       |
| Domains              | 6 (Diagram, Finance, Society, Life, Culture, Science)                     |
| Difficulty levels    | 3 (L1 visual-only, L2 visual + search, L3 synergistic iterative)          |
| Stepwise checkpoints | 2,000+ human-annotated                                                    |
| Evaluation modes     | **Gen** (sandboxed Python) and **Atm** (function-calling API) |

### Dual-Axis Scoring

- **V-axis** — visual evidence extraction + tool-invocation correctness
- **S-axis** — search strategy + retrieval quality
- **Overthinking** — efficiency metric penalising needless tool calls

### Key Metrics

| Metric    | Definition                                              |
| --------- | ------------------------------------------------------- |
| Acc       | Final answer accuracy (normalized exact match)          |
| S         | Fraction of passed S-axis checkpoints (search strategy) |
| V         | Fraction of passed V-axis checkpoints (visual evidence) |
| V-tool    | Was the required visual tool invoked?                   |
| V-true    | Does the produced artifact contain required evidence?   |
| Overthink | `max(0, C_agent - C_human) / (C_human + 1)`           |

### Coordinate System

Bounding boxes use normalized 0–1000 scale (not pixels). `[x1, y1, x2, y2]` where `(0,0)` = top-left, `(1000,1000)` = bottom-right.

---

## Links

- **Paper:** https://arxiv.org/abs/2604.03016
- **Ethara.AI:** https://projects.ethara.ai/janus