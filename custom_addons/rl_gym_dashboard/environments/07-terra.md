# Terra

**Ethara.AI RL Environment · General AI Assistant**

Extends: [GAIA](https://arxiv.org/abs/2311.12983)

---

## What Ethara pitches

Terra trains agentic LLMs on real-world questions that humans solve easily but current AI largely cannot. Given a question with optional file attachments (images, spreadsheets, PDFs), the agent must combine **web search, file parsing, image interpretation, and mathematical reasoning** across multi-step chains to produce a single verifiable answer.

**Pitched capabilities**

- 10,000 multi-modal multi-step questions with exact-match answers
- Binary success/failure reward signal wired into rollout for RL fine-tuning
- Full execution traces: every action, observation, and decision point per agent
- Cost and latency metadata for efficiency-aware training
- Critic-validated scoring with multiple verification attempts per task
- Difficulty-graded curriculum across 3 cognitive complexity levels

**For frontier labs:** Combining reasoning with real-world tool use remains the weakest capability in frontier models. Terra provides the training signal for reliable multi-step problem-solving across diverse tasks at scale.

Three critical needs addressed:

1. **Tool-use reliability**: Multi-step chains with verifiable outcomes provide direct reward signal for teaching agents to sequence tools correctly.

2. **Progressive difficulty**: Three cognitive complexity levels enable curriculum-based training, from simple lookups to deep multi-hop reasoning.

3. **Unambiguous scoring**: Single-string answers eliminate evaluation noise, enabling automated reward computation at scale.

---

## Original paper: GAIA

**Title:** GAIA: a benchmark for General AI Assistants
**Authors:** Mialon, Fourrier, Swift, Wolf, LeCun, Scialom (Meta AI, Hugging Face)
**arXiv:** [2311.12983](https://arxiv.org/abs/2311.12983) (submitted 21 Nov 2023)
**Category:** cs.CL

### Core contribution

GAIA argues that the right measure of a general-AI-assistant is not esoteric academic exams but **real-world questions that are easy for humans and hard for AI**. Questions require chained use of reasoning, multi-modality, web browsing, and general tool use; answers are single strings graded by exact match.

### Benchmark stats (original)

| Item | Value |
|------|-------|
| Total questions | 466 (300 public, 166 hidden test set) |
| Difficulty levels | L1, L2, L3 |
| Evaluation | Exact-match against reference answer |
| Required skills | Reasoning + multi-modality + browsing + tool use |

### Baseline results

| Solver | Score |
|--------|-------|
| Humans | 92% |
| GPT-4 with plugins | 15% |

The ~77-point human-vs-model gap is the tightest articulation of "assistants that actually assist".

---

## What Ethara extends

| Dimension | Original GAIA | Terra |
|-----------|---------------|-------|
| Scale | 466 questions | 10,000 questions |
| Use-mode | Static, competition-scored | RL environment with success-conditioned reward |
| Reward | Correct / incorrect at the end | Exact-match success reward wired into rollout |
| Tools | Expected capability, not orchestrated | Provided as a proper tool surface (web, file parsing, image, math) |
| Traces | Not provided | Full execution traces with cost, tokens, latency per step |
| Curriculum | 3 levels, no training signal | 3 levels with progressive difficulty for RL curriculum |

Terra preserves GAIA's central thesis (everyday hard-for-AI questions) and turns the exact-match oracle into an RL reward. The tooling stack is supplied; the policy learns to *sequence* tools rather than choose one.

---

## Environment Specification

| Property | Value |
|------|-------|
| Total tasks | 10,000 |
| Difficulty levels | 3 (cognitive complexity) |
| Answer format | Single verifiable string, exact-match scored |
| Input modalities | Text, PNG, CSV, MP4, PDF, XLSX |
| Tool surface | Web search, file I/O, code execution (sandboxed) |

### Difficulty Distribution

| Level | Description |
|------|-------|
| 1 | Single-skill tasks with one reasoning trap |
| 2 | Multi-step tasks requiring moderate disambiguation |
| 3 | Deep reasoning, complex math, multi-hop inference with deceptive traps |

Levels are annotated by cognitive complexity, not step count or tool count. A Level 3 task may require zero tools but demand deep logical deduction.

---

## Reference Results

Two models were run on a 20-task subset with access to web search, file operations, and code execution.

### Success Rate by Difficulty

| Level | Kimi K2.5 | Nova 2 Lite |
|------|-------|-------|
| Level 1 (n=4) | 100.0% | 25.0% |
| Level 2 (n=7) | 85.7% | 14.3% |
| Level 3 (n=9) | 0.0% | 0.0% |
| **Overall** | **50.0%** | **10.0%** |

![Success Rate by Difficulty Level](terra_success_rate.png)

### Key Findings

- Both models show strict monotonic performance degradation as cognitive complexity increases
- Level 3 tasks are completely unsolved by both models, with 0% success rate across 9 tasks
- Even on Level 1 tasks, Nova 2 Lite fails 75% of the time despite having full tool access
- The 5x performance gap between models demonstrates the environment discriminates effectively between capabilities

### Cost Analysis

| Model | Total Cost | Cost per Task | Correct | Accuracy |
|------|-------|-------|-------|-------|
| Kimi K2.5 | $4.95 | $0.25 | 10/20 | 50% |
| Nova 2 Lite | $3.63 | $0.18 | 2/20 | 10% |

---

## Delivery Contents

### Dataset

| File | Description |
|------|-------|
| `dataset.jsonl` | 10,000 task definitions. Each line: question text, difficulty level (1/2/3), exact ground-truth answer, file attachment references, annotator metadata with required reasoning steps and tools. |
| `file_attachments/` | Source files referenced by tasks. Formats: PNG, CSV, XLSX, PDF, MP4. Named by task ID. |

### Execution Traces (per task, per model)

| File | Contents |
|------|-------|
| `summary.json` | Final outcome: correct/incorrect, model answer vs ground truth, total API cost ($), prompt tokens, completion tokens, LLM call count, average latency per call. |
| `output.jsonl` | Complete action-observation history. Every tool call (web search, file read, code execution), every observation returned, every reasoning step, in sequential order. |
| `output_critic_attempt_N.jsonl` | Scoring verification logs. A separate critic model evaluates answer correctness with up to 3 independent verification attempts per task. |
| `metadata.json` | Run configuration: model name, SDK version, max iterations, workspace type, dataset parameters. |
| `conversations/` | Compressed conversation archives (.tar.gz). Raw agent state including internal usage metrics and full message history between orchestrator and sandboxed execution environment. |

---

## Training Algorithm: GRPO with Agentic Extensions

### Why GRPO is the foundation

Group Relative Policy Optimization eliminates the critic network required by PPO. For each prompt, it samples a group of G rollouts and computes advantages by normalizing rewards within the group:

```
A_i = (R_i - mean(R_1..G)) / std(R_1..G)
```

This is a direct fit for Terra's binary reward signal. With exact-match scoring (reward is 0 or 1), GRPO's group-relative advantage naturally separates successful from failed trajectories without requiring a learned value function. DeepSeek-R1 proved this approach at scale, and subsequent analysis confirms that with binary rewards and group size as small as 2, GRPO preserves unbiased gradient estimation equivalent to DPO's contrastive structure.

**Why not PPO:** PPO requires a separate critic model trained to predict state values. For long-horizon agent tasks with 20-100+ LLM calls, maintaining an accurate critic is prohibitively expensive and empirically unstable. Recent work shows critic-free methods underperform PPO in continuous control, but in the LLM/RLVR setting the generation cost dominates and GRPO's simplicity wins.

**Why not RLOO/ReMax:** REINFORCE Leave-One-Out and ReMax both remove the critic but use simpler baselines (batch mean or single-sample). They lack GRPO's group-relative normalization which provides tighter variance reduction per prompt.

### Required enhancements for Terra

Standard GRPO has a critical failure mode: when all samples in a group receive the same reward (all 0 or all 1), the advantage is zero and no gradient flows. At current capability levels, the hardest tasks produce uniform failure across all rollouts, yielding zero gradient. Two extensions address this:

**1. Dynamic Sampling (from DAPO)**

Filter out prompts where all G rollouts get identical rewards. Continue sampling until the batch contains only prompts with mixed success/failure, guaranteeing non-zero advantages in every update. Hard tasks get filtered until the policy improves enough to occasionally solve them.

Trade-off: Dynamic sampling requires 3-5x more generation than standard GRPO. For Terra's long trajectories this is expensive, but the alternative (wasting compute on zero-gradient batches) is worse.

**2. Step-Level Exploration (from ARPO)**

Standard GRPO treats the entire multi-turn agent trajectory as one action. This makes credit assignment across 50+ tool calls nearly impossible. ARPO (Agentic Reinforced Policy Optimization) introduces entropy-based adaptive branching at tool-call steps:

- After each tool call, monitor token entropy
- When entropy spikes (indicating decision uncertainty), branch additional partial rollouts from that point
- Assign shared advantages along the common prefix, distinct advantages on branches

This gives step-level signal in a trajectory-level reward setting, directly addressing Terra's long-horizon problem. ARPO achieves equivalent performance to GRPO with half the tool calls.

### Concrete training recipe

| Component | Choice | Rationale |
|-----------|--------|-----------|
| Base algorithm | GRPO | Critic-free, binary-reward native, proven at scale |
| Group size | 8-16 per prompt | Balance between variance reduction and compute; 2-GRPO viable for compute-constrained runs |
| Clipping | Asymmetric (DAPO Clip-Higher) | Relaxed upper bound prevents entropy collapse in long-CoT |
| Sampling | Dynamic filtering | Remove zero-variance groups, critical for hard tasks |
| Loss aggregation | Token-level mean | Prevents long trajectories from dominating short ones |
| Step branching | Entropy-adaptive (ARPO) | Credit assignment for multi-turn tool use |
| Reward shaping | Overlong penalty | Penalize trajectories that exceed context without resolution |
| KL regularization | Soft KL penalty (beta=0.01) | Prevent catastrophic drift from instruction-following base |
| Curriculum | Level-based difficulty progression | Start on L1/L2 (50-100% success), gradually introduce L3 |
| Framework | veRL (supports GRPO, DAPO, ARPO natively) | Production-grade distributed RL for LLMs |

### Curriculum strategy

Terra's difficulty distribution creates a natural curriculum:

1. **Phase 1** (warm-up): Train on Level 1+2 tasks only. Success rates of 50-100% ensure most batches contain mixed rewards and produce gradient signal.

2. **Phase 2** (expansion): Introduce Level 3 tasks at 20% mixture. Dynamic sampling filters out L3 prompts where the agent always fails, focusing compute on the boundary of capability.

3. **Phase 3** (full difficulty): Equal mixture across levels. By this stage the policy has enough capability that even some L3 tasks produce non-zero reward variance.

This progressive approach prevents the zero-gradient problem from dominating early training while ensuring the policy eventually confronts the hardest tasks.

### Implementation path

| Stack | Tool |
|-------|------|
| RL framework | veRL (FSDP + vLLM, native GRPO/DAPO support) |
| Rollout engine | vLLM (efficient batch generation for group sampling) |
| Reward function | Exact-match oracle (Terra's ground-truth answers) |
| Process reward | Not required (outcome reward sufficient with ARPO branching) |
| Compute | 8-16x H100 (32B model) or 4-8x H100 (7B model) |

---

## Links

- **Paper:** https://arxiv.org/abs/2311.12983
- **GitHub:** https://github.com/Ethara-Ai/terra.git
- **HuggingFace:** https://huggingface.co/datasets/ethara/terra
- **Ethara.AI:** https://ethara.ai
