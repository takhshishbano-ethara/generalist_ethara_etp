# MILO-RL: Complete Architecture & Code Flow

## System Overview

MILO-RL trains `nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16` (hybrid Mamba-Transformer MoE, 31.6B total / 3.2B active) to solve long-horizon multi-turn coding tasks via **GSPO** (Group Sequence Policy Optimization) with gated rewards, frozen MoE routing, and 3-phase curriculum.

---

## Directory Structure

```
milo-aws/
├── configs/                     # YAML configuration files
│   ├── ppo_trainer.yaml         # Main training config (GSPO + LoRA + MoE + rewards)
│   ├── prm.yaml                 # PRM (Process Reward Model) config
│   └── hardware/
│       └── 8xh100.yaml          # 8×H100 hardware profile + ECR settings
├── scripts/                     # Entry points
│   ├── run_stage0.py            # Stage 0: Data preparation & decomposition
│   ├── run_stage1.py            # Stage 1: RFT warmup (rejection sampling + SFT)
│   ├── run_stage2.py            # Stage 2: GSPO RL training (main loop)
│   └── run_eval.py              # Evaluation runner
├── src/
│   ├── core/                    # Configuration & data models
│   │   ├── config.py            # All dataclass configs (487 lines)
│   │   └── schemas.py           # Pydantic models: Turn, Trajectory, TaskSpec, etc.
│   ├── data/                    # Data pipeline
│   │   ├── dataset.py           # MiloDataset: loads JSONL tasks
│   │   ├── decomposer.py        # Multi-PR → single-PR decomposition
│   │   ├── augmentation.py      # Data augmentation strategies
│   │   ├── difficulty.py        # Difficulty scoring (easy/medium/hard)
│   │   └── validator.py         # Task spec validation
│   ├── training/                # Training algorithms
│   │   ├── trainer.py           # MiloTrainer: main orchestrator (401 lines)
│   │   ├── gspo_loss.py         # GSPO loss: segment-level ratio + dual clip (405 lines)
│   │   ├── gated_rewards.py     # G-RA: outcome-gated step rewards (189 lines)
│   │   ├── moe_utils.py         # Frozen router + expert bias + collapse detect (359 lines)
│   │   ├── pivot_selector.py    # PivotRL turn selection (disabled v1)
│   │   ├── curriculum.py        # ScalingInterRLSampler: 3-phase progressive
│   │   ├── reward_manager.py    # Docker execution grading + PRM integration
│   │   └── rft_warmup.py        # Stage 1: rejection sampling + SFT gate
│   ├── rollout/                 # Environment interaction
│   │   ├── docker_executor.py   # Docker SDK: container lifecycle + test exec (274 lines)
│   │   ├── docker_tool.py       # 6-action tool interface (apply_patch, run_cmd, etc.)
│   │   ├── ecr.py               # AWS ECR auth + image pull
│   │   └── patch_utils.py       # Patch extraction & compact filtering
│   ├── prm/                     # Process Reward Model
│   │   ├── scorer.py            # LLM-judge + trained PRM scoring
│   │   ├── shaper.py            # Potential-based reward shaping
│   │   └── step_advantage.py    # Per-step advantage estimation
│   ├── eval/                    # Evaluation
│   │   ├── per_pr.py            # Per-PR evaluation (F2P + P2P)
│   │   └── best_of_n.py         # Best-of-N sampling evaluation
│   └── monitoring/              # Training health
│       ├── kill_conditions.py   # 7 kill conditions + severity levels
│       ├── metrics.py           # MetricsTracker: step-by-step logging
│       └── replay.py            # Rollout replay storage
├── tests/                       # 106 passing tests
├── pyproject.toml               # Dependencies
└── Makefile                     # Build/test commands
```

---

## Complete Data Flow

```
┌─────────────────────────────────────────────────────────────────────────┐
│                        TRAINING PIPELINE FLOW                            │
└─────────────────────────────────────────────────────────────────────────┘

   ┌──────────────┐       ┌──────────────┐       ┌──────────────────────┐
   │  STAGE 0     │──────▶│  STAGE 1     │──────▶│  STAGE 2 (GSPO RL)  │
   │  Data Prep   │       │  RFT Warmup  │       │  Main Training Loop  │
   └──────────────┘       └──────────────┘       └──────────────────────┘
```

### Stage 0: Data Preparation (`scripts/run_stage0.py`)
```
Raw MILO-bench data (multi-PR, multi-language)
    │
    ▼
┌─────────────────────────────┐
│  decomposer.py              │
│  Multi-PR → Single-PR       │
│  (≤50 turns per sub-task)   │
└─────────────────────────────┘
    │
    ▼
┌─────────────────────────────┐
│  difficulty.py              │
│  Score each task 0.0-1.0    │
│  Classify: easy/medium/hard │
└─────────────────────────────┘
    │
    ▼
┌─────────────────────────────┐
│  augmentation.py            │
│  Template diversity         │
│  Language filtering (Py/Go) │
└─────────────────────────────┘
    │
    ▼
┌─────────────────────────────┐
│  validator.py               │
│  Verify Docker images exist │
│  Check test patches apply   │
└─────────────────────────────┘
    │
    ▼
  data/tasks/train.jsonl  (2000-5000 TaskSpec instances)
```

### Stage 1: RFT Warmup (`scripts/run_stage1.py`)
```
  Nemotron-3-Nano-30B-A3B-BF16 (base)
    │
    ▼
┌─────────────────────────────────────┐
│  rft_warmup.py                      │
│                                     │
│  1. Rejection Sampling:             │
│     - Generate N=16 responses/task  │
│     - Grade via DockerExecutor      │
│     - Keep passing trajectories     │
│                                     │
│  2. SFT on passing trajectories     │
│     - LoRA fine-tuning              │
│     - Until pass@1 ≥ 15% (gate)    │
│                                     │
│  Gate: If pass@1 < 15% after K      │
│  iters → abort (model too weak)     │
└─────────────────────────────────────┘
    │
    ▼
  Warmed-up LoRA checkpoint (ready for RL)
```

### Stage 2: GSPO RL Training (`scripts/run_stage2.py`)

This is the main loop. Here's the detailed flow:

```
┌─────────────────────────────────────────────────────────────────────────┐
│                    GSPO TRAINING LOOP (450 steps)                        │
│                                                                         │
│  for step in range(450):                                                │
│                                                                         │
│    ┌─────────────────────────────────────────────────────────────────┐  │
│    │  1. CURRICULUM SAMPLING                                         │  │
│    │     curriculum.py → ScalingInterRLSampler                       │  │
│    │                                                                 │  │
│    │     Phase 1 (steps 0-100):  max_turns=15, difficulty=[easy]     │  │
│    │     Phase 2 (steps 100-250): max_turns=30, difficulty=[easy,med]│  │
│    │     Phase 3 (steps 250-450): max_turns=50, difficulty=[all]     │  │
│    │                                                                 │  │
│    │     → Returns batch_indices (128 task indices)                   │  │
│    └─────────────────────────────────────────────────────────────────┘  │
│                              │                                          │
│                              ▼                                          │
│    ┌─────────────────────────────────────────────────────────────────┐  │
│    │  2. ROLLOUT GENERATION (vLLM)                                   │  │
│    │                                                                 │  │
│    │     For each task in batch (128 tasks × 8 rollouts = 1024):     │  │
│    │       - Load model via vLLM (TP=2, max_model_len=131072)       │  │
│    │       - temperature=1.0, top_p=1.0 (unbiased sampling)          │  │
│    │       - Multi-turn interaction via DockerSandboxTool:            │  │
│    │           Turn 1: Model generates action (tool call)            │  │
│    │           Turn 2: Tool executes in Docker → observation          │  │
│    │           Turn 3: Model generates next action                    │  │
│    │           ...                                                    │  │
│    │           Turn N: Model calls "submit" (patch extracted)         │  │
│    │                                                                 │  │
│    │     → list[Trajectory] with turns, raw_response, patch          │  │
│    └─────────────────────────────────────────────────────────────────┘  │
│                              │                                          │
│                              ▼                                          │
│    ┌─────────────────────────────────────────────────────────────────┐  │
│    │  3. REWARD COMPUTATION                                          │  │
│    │     reward_manager.py → MiloRewardManager                       │  │
│    │                                                                 │  │
│    │     3a. Extract patches from responses                          │  │
│    │     3b. Docker execution grading:                                │  │
│    │         - Pull ECR image for each task                          │  │
│    │         - Apply patch to repo in container                       │  │
│    │         - Run test suite (F2P: fail-to-pass, P2P: pass-to-pass) │  │
│    │         - Binary: 1.0 if F2P=all_pass AND P2P=all_pass          │  │
│    │     3c. PRM scoring (if enabled):                                │  │
│    │         - Score each turn via LLM-judge or trained PRM          │  │
│    │         - Potential shaping: Φ(s') - γ*Φ(s)                     │  │
│    │     3d. Compact filtering: mask out empty/timeout trajectories   │  │
│    │                                                                 │  │
│    │     → rewards tensor [batch_size], shaped_returns, masks        │  │
│    └─────────────────────────────────────────────────────────────────┘  │
│                              │                                          │
│                              ▼                                          │
│    ┌─────────────────────────────────────────────────────────────────┐  │
│    │  4. GATED REWARD SHAPING (G-RA)                                 │  │
│    │     gated_rewards.py → GatedRewardComputer                      │  │
│    │                                                                 │  │
│    │     Outcome classification:                                     │  │
│    │       PASS → +1.0, FAIL → -0.1, EMPTY → -0.2, TIMEOUT → -0.5  │  │
│    │                                                                 │  │
│    │     Gate rule (gate_threshold = -0.5):                           │  │
│    │       if outcome_reward > -0.5:                                  │  │
│    │         total = outcome + 0.05*sum(step_rewards) + 0.1*len_pen  │  │
│    │       else:                                                      │  │
│    │         total = outcome + 0.1*len_penalty  (PRM gated OFF)      │  │
│    │                                                                 │  │
│    │     Length penalty (from LHT-SWE paper):                         │  │
│    │       if episode_len >= 0.7*max_turns:                           │  │
│    │         penalty = (threshold - length) / (max - threshold)       │  │
│    │                                                                 │  │
│    │     → final_rewards tensor [batch_size]                         │  │
│    └─────────────────────────────────────────────────────────────────┘  │
│                              │                                          │
│                              ▼                                          │
│    ┌─────────────────────────────────────────────────────────────────┐  │
│    │  5. ADVANTAGE COMPUTATION (RLOO / per-group)                    │  │
│    │                                                                 │  │
│    │     For each prompt group (8 rollouts per prompt):              │  │
│    │       A_i = (r_i - mean(group)) / std(group)                    │  │
│    │                                                                 │  │
│    │     No value function (GRPO-style leave-one-out baseline)       │  │
│    │     Advantages assigned per-segment (per assistant turn)        │  │
│    └─────────────────────────────────────────────────────────────────┘  │
│                              │                                          │
│                              ▼                                          │
│    ┌─────────────────────────────────────────────────────────────────┐  │
│    │  6. GSPO LOSS COMPUTATION                                       │  │
│    │     gspo_loss.py → GSPOLossComputer                             │  │
│    │                                                                 │  │
│    │     6a. Token-level log-ratios:                                  │  │
│    │         log_ratio_t = log π_θ(y_t|...) - log π_old(y_t|...)    │  │
│    │                                                                 │  │
│    │     6b. Segment-level ratio (per assistant turn):                │  │
│    │         For each turn k (segment):                              │  │
│    │           mean_log_ratio_k = mean(log_ratio_t for t in turn_k)  │  │
│    │           r_k = exp(mean_log_ratio_k)                           │  │
│    │                                                                 │  │
│    │     6c. Clipped surrogate (GSPO clip ranges 1000× tighter):     │  │
│    │         surr1 = r_k * A_k                                       │  │
│    │         surr2 = clip(r_k, 1-3e-4, 1+4e-4) * A_k               │  │
│    │         L = min(surr1, surr2)                                    │  │
│    │                                                                 │  │
│    │     6d. Dual clip (for negative advantages):                     │  │
│    │         if A_k < 0: L = max(L, 5.0 * A_k)                      │  │
│    │                                                                 │  │
│    │     6e. Aggregation: seq-mean-token-mean                         │  │
│    │         loss = -mean_over_batch(mean_per_seq(L per segment))    │  │
│    │                                                                 │  │
│    │     → loss scalar + metrics (clip_frac, approx_kl, mean_ratio) │  │
│    └─────────────────────────────────────────────────────────────────┘  │
│                              │                                          │
│                              ▼                                          │
│    ┌─────────────────────────────────────────────────────────────────┐  │
│    │  7. MoE AUXILIARY LOSS                                          │  │
│    │     moe_utils.py → MoETrainingManager                          │  │
│    │                                                                 │  │
│    │     7a. Compute seq_aux_loss (load balancing):                   │  │
│    │         f_i = token_fraction_to_expert_i                         │  │
│    │         P_i = mean_sigmoid_prob_for_expert_i                     │  │
│    │         aux_loss = num_experts * sum(f_i * P_i) * coeff(1e-4)   │  │
│    │                                                                 │  │
│    │     7b. Total loss:                                              │  │
│    │         total_loss = gspo_loss + aux_loss                        │  │
│    │                                                                 │  │
│    │     → total_loss for backward pass                              │  │
│    └─────────────────────────────────────────────────────────────────┘  │
│                              │                                          │
│                              ▼                                          │
│    ┌─────────────────────────────────────────────────────────────────┐  │
│    │  8. GRADIENT UPDATE                                             │  │
│    │                                                                 │  │
│    │     - Backward pass through LoRA parameters only                │  │
│    │     - Gradient clipping: max_grad_norm = 1.0                    │  │
│    │     - Optimizer step (AdamW, lr=3e-6)                            │  │
│    │     - Router weights FROZEN (requires_grad=False)                │  │
│    │     - Expert bias update (aux-loss-free):                        │  │
│    │         imbalance = actual_ratio - target_ratio                  │  │
│    │         bias += imbalance * 0.001                                │  │
│    └─────────────────────────────────────────────────────────────────┘  │
│                              │                                          │
│                              ▼                                          │
│    ┌─────────────────────────────────────────────────────────────────┐  │
│    │  9. MONITORING & KILL CONDITIONS                                │  │
│    │     kill_conditions.py → KillConditionMonitor                   │  │
│    │                                                                 │  │
│    │     Checks every step:                                          │  │
│    │       - Reward collapse (mean < 0.01 for 30 steps)              │  │
│    │       - Gradient explosion (grad_norm > 10.0)                    │  │
│    │       - Echo trap (repetition ratio > 0.6)                       │  │
│    │       - KL divergence spike (approx_kl > 0.1)                   │  │
│    │       - Expert collapse (dead experts > 10%)                    │  │
│    │       - OOM detection                                            │  │
│    │       - Docker failure cascade (3+ consecutive)                  │  │
│    │                                                                 │  │
│    │     Severity levels:                                            │  │
│    │       warning → log only                                        │  │
│    │       recoverable → auto-adjust (temp↑, lr↓, batch↓)           │  │
│    │       fatal → stop training, save checkpoint                    │  │
│    └─────────────────────────────────────────────────────────────────┘  │
│                              │                                          │
│                              ▼                                          │
│    ┌─────────────────────────────────────────────────────────────────┐  │
│    │  10. CURRICULUM UPDATE                                          │  │
│    │      curriculum.py → ScalingInterRLSampler.update()             │  │
│    │                                                                 │  │
│    │      - Track success_rate over window                           │  │
│    │      - If success_rate > advance_threshold AND step >= phase_end│  │
│    │        → advance to next phase (more turns, harder tasks)       │  │
│    │      - Hard_bias: oversample failing tasks                      │  │
│    └─────────────────────────────────────────────────────────────────┘  │
│                              │                                          │
│                              ▼                                          │
│    ┌─────────────────────────────────────────────────────────────────┐  │
│    │  11. CHECKPOINT & EVAL                                          │  │
│    │                                                                 │  │
│    │      Every eval_every steps:                                    │  │
│    │        - Run per_pr evaluation on held-out set                  │  │
│    │        - Track best pass@1                                       │  │
│    │        - Save best checkpoint                                    │  │
│    │      Every checkpoint_every steps:                               │  │
│    │        - Save full state (step, lr, temp, curriculum, metrics)  │  │
│    │        - Evict old checkpoints (keep N=5)                       │  │
│    └─────────────────────────────────────────────────────────────────┘  │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## Component Interaction Diagram

```
                    ┌────────────────────────────────┐
                    │       MiloTrainer.fit()         │
                    │       (orchestrator)            │
                    └───────────────┬────────────────┘
                                    │
          ┌─────────────────────────┼─────────────────────────┐
          │                         │                         │
          ▼                         ▼                         ▼
┌──────────────────┐  ┌──────────────────────┐  ┌──────────────────┐
│ ScalingInterRL   │  │  vLLM Rollout Engine │  │ KillCondition    │
│ Sampler          │  │  + DockerSandboxTool │  │ Monitor          │
│                  │  │                      │  │                  │
│ - 3 phases       │  │ - Multi-turn gen     │  │ - 7 conditions   │
│ - difficulty     │  │ - Tool calling       │  │ - Auto-recovery  │
│ - hard_bias      │  │ - Docker containers  │  │ - Fatal stop     │
└────────┬─────────┘  └──────────┬───────────┘  └────────┬─────────┘
         │                       │                        │
         │ batch_indices         │ trajectories           │ kill_action
         │                       │                        │
         └───────────────────────┼────────────────────────┘
                                 │
                                 ▼
          ┌──────────────────────────────────────────────────┐
          │              MiloRewardManager                    │
          │                                                  │
          │  ┌──────────────┐  ┌─────────────┐  ┌────────┐ │
          │  │DockerExecutor│  │ PRM Scorer  │  │ Patch  │ │
          │  │(ECR + Docker)│  │(LLM Judge)  │  │ Utils  │ │
          │  └──────┬───────┘  └──────┬──────┘  └────┬───┘ │
          │         │                 │               │      │
          │         ▼                 ▼               ▼      │
          │    F2P/P2P results   step_scores     patches     │
          └──────────────────────────┬───────────────────────┘
                                     │
                                     ▼
          ┌──────────────────────────────────────────────────┐
          │           GatedRewardComputer (G-RA)             │
          │                                                  │
          │  outcome_reward ───┐                             │
          │  step_rewards ─────┼──▶ Gate Logic ──▶ total_r   │
          │  length_penalty ───┘                             │
          └──────────────────────────┬───────────────────────┘
                                     │
                                     ▼
          ┌──────────────────────────────────────────────────┐
          │            GSPOLossComputer                       │
          │                                                  │
          │  log_probs ────┐                                 │
          │  old_log_probs ┼──▶ Segment Ratios ──▶ Clip ──▶ │
          │  advantages ───┘    (per-turn)         Loss      │
          │  segment_ids                                     │
          └──────────────────────────┬───────────────────────┘
                                     │
                                     ▼
          ┌──────────────────────────────────────────────────┐
          │           MoETrainingManager                      │
          │                                                  │
          │  - freeze_router_params()                         │
          │  - update_expert_bias() (sigmoid bias adjustment)│
          │  - compute_seq_aux_loss() (load balancing)       │
          │  - check_expert_collapse() (dead expert alert)   │
          └──────────────────────────────────────────────────┘
```

---

## Key Data Models (`src/core/schemas.py`)

```python
class Turn:
    role: "user" | "assistant" | "tool" | "system"
    content: str
    token_count: int
    prm_score: float | None

class Trajectory:
    task_id: str
    turns: list[Turn]           # Multi-turn conversation
    patch: str                  # Final diff submitted
    reward: float               # Binary (0/1) from Docker
    step_rewards: list[float]   # Per-turn PRM scores
    shaped_return: float        # Final gated+shaped scalar
    episode_length: int         # Number of turns
    curriculum_phase: int       # Which phase generated this

class TaskSpec:
    task_id: str
    repo: str                   # e.g. "django/django"
    language: "python" | "go"
    problem_statement: str      # PR description
    test_patch: str             # Tests to pass
    docker_image: str           # ECR URI
    difficulty: "easy" | "medium" | "hard"
    difficulty_score: float     # 0.0-1.0 continuous
```

---

## Configuration Hierarchy (`src/core/config.py`)

```python
@dataclass
class MiloConfig:
    model_path: str = "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16"
    lora: LoRAConfig           # rank=64, alpha=64, 6 target modules
    gspo: GSPOConfig           # clip=3e-4/4e-4, segment-level, freeze_router
    curriculum: CurriculumConfig  # 3 phases (15/30/50 turns)
    hardware: HardwareConfig   # 8×H100, 131K context, Docker settings
    monitoring: MonitoringConfig  # eval/checkpoint intervals
    rft: RFTConfig             # Stage 1 params
    eval: EvalConfig           # Evaluation settings
    prm: PRMConfig             # PRM scorer config
    ecr: ECRConfig             # AWS ECR authentication
    gated_reward: GatedRewardConfig  # G-RA gate logic
    moe: MoEConfig             # 128 experts, top-6, frozen router
```

### Key Config Values

| Parameter | Value | Source |
|-----------|-------|--------|
| LoRA rank | 64 | Oracle recommendation (2× for RL) |
| LoRA targets | `linear_qkv, linear_proj, linear_fc1, linear_fc2, in_proj, out_proj` | NVIDIA Megatron-Bridge PEFT recipe |
| GSPO clip_low | 3e-4 | GSPO paper (arXiv:2507.18071) |
| GSPO clip_high | 4e-4 | GSPO paper |
| importance_sampling | "segment" | Per-turn ratio for multi-turn |
| freeze_router | True | NVIDIA Nemotron-3 RL recipe |
| moe_aux_loss_coeff | 0.0001 | NVIDIA Megatron-Bridge |
| expert_bias_update_rate | 0.001 | DeepSeek/NVIDIA approach |
| gate_threshold | -0.5 | Oracle fix (partial credit) |
| learning_rate | 3e-6 | Nemotron-Cascade 2 |
| total_steps | 450 | Nemotron-Cascade 2 |
| batch_size | 128 | Nemotron-3 paper |
| group_size | 8 | GSPO paper |
| temperature | 1.0 | LHT-SWE (unbiased sampling) |

---

## Docker Execution Flow (`src/rollout/`)

```
┌─────────────────────────────────────────────────────┐
│           DockerSandboxTool (multi-turn)             │
│                                                     │
│  6 Actions available to the model:                  │
│    1. apply_patch  → Apply unified diff             │
│    2. run_command  → Execute shell command           │
│    3. read_file    → Read file contents             │
│    4. list_files   → List directory                 │
│    5. grep         → Search file contents           │
│    6. submit       → Submit final patch + grade     │
│                                                     │
│  Episode lifecycle:                                 │
│    1. Pull ECR image (ecr.py → ECRAuthManager)     │
│    2. Create container (repo mounted, tests ready)  │
│    3. Model interacts via tool calls                │
│    4. On "submit": extract final patch              │
│    5. Run F2P tests (must all pass)                 │
│    6. Run P2P tests (regression check)             │
│    7. Tear down container                           │
└─────────────────────────────────────────────────────┘

ECR Image Resolution:
  task.docker_image → ecr.resolve_image_uri()
  → "426628337772.dkr.ecr.ap-south-1.amazonaws.com/rfp-coding-q1-tag/<image>"
  → ECRAuthManager.get_auth_token() → docker pull
```

---

## GSPO Algorithm (Mathematical Detail)

The core difference from GRPO: **sequence-level ratio** instead of token-level.

```
Standard GRPO (token-level, UNSTABLE for MoE):
  w_t = π_θ(y_t|...) / π_old(y_t|...)
  Each token ratio fluctuates when MoE routing changes (~10% experts swap per step)

GSPO (sequence/segment-level, STABLE for MoE):
  For segment k (one assistant turn):
    s_k = exp( mean_{t ∈ segment_k}[ log π_θ(y_t) - log π_old(y_t) ] )

  This is the GEOMETRIC MEAN of token ratios within the turn.
  Insensitive to individual expert routing changes because it uses
  the aggregate sequence-level probability only.

Clipping (1000× tighter than GRPO):
  GRPO: clip at [1-0.2, 1+0.28]     → wide, token-level
  GSPO: clip at [1-3e-4, 1+4e-4]   → tight, sequence-level

  Why tighter? Sequence-level ratios have much lower variance.
  Tight clips prevent even small off-policy drift from accumulating.
```

---

## Gated Rewards (G-RA) Logic

```python
# Priority: Outcome > Format > PRM step scores
# Gate rule: lower-priority rewards zeroed if higher-priority fails

outcome = classify(trajectory)  # PASS/FAIL/EMPTY/TIMEOUT
outcome_reward = {PASS: 1.0, FAIL: -0.1, EMPTY: -0.2, TIMEOUT: -0.5}

if outcome_reward > gate_threshold(-0.5):
    # Gate OPEN: step rewards contribute
    total = outcome_reward + 0.05 * sum(prm_step_scores) + 0.1 * length_penalty
else:
    # Gate CLOSED: only outcome + penalty (PRM wasted compute saved)
    total = outcome_reward + 0.1 * length_penalty
```

**Why gate_threshold = -0.5?** (Oracle fix)
- At 0.0: Only PASS opens gate → <5% of episodes in Phase 3 → PRM dead
- At -0.5: PASS + FAIL open gate → ~40% of episodes provide PRM signal
- EMPTY and TIMEOUT still gated (model didn't try → no learning signal)

---

## MoE Training Strategy

```
┌─────────────────────────────────────────────────────┐
│  Nemotron-3-Nano-30B-A3B Architecture:              │
│                                                     │
│  52 Layers, hybrid pattern:                         │
│  MEMEM*EMEMEM*EMEMEM*EMEMEM*EMEMEM*EMEMEMEM*EMEMEMEME │
│                                                     │
│  M = Mamba-2 (SSM) layer                           │
│  E = Expert MoE layer (128 routable + 2 shared)    │
│  * = GQA Transformer attention layer               │
│                                                     │
│  Per token: 6 experts activated (top-6 sigmoid)     │
│  Router: sigmoid gating (NOT softmax top-k)        │
│  Hidden: 2688, Expert FFN: 1856                    │
└─────────────────────────────────────────────────────┘

During RL training:
  1. Router weights FROZEN (requires_grad=False)
     - Prevents routing instability
     - NVIDIA's proven approach (Nemotron-3 paper)

  2. Expert bias updated (aux-loss-free):
     - After each step: count tokens per expert
     - Adjust bias: overloaded experts get penalty, underloaded get bonus
     - Rate: 0.001 per step

  3. Seq_aux_loss (gradient-based backup):
     - loss = 1e-4 * num_experts * sum(f_i * P_i)
     - Prevents catastrophic collapse even with frozen router

  4. Collapse detection:
     - Alert if any expert receives < 1% of mean tokens
     - Log collapsed expert IDs for debugging
```

---

## LoRA Target Modules

```python
# From NVIDIA's official Megatron-Bridge PEFT recipe:
target_modules = [
    "linear_qkv",   # QKV projection in GQA attention layers (*)
    "linear_proj",   # Output projection in attention layers (*)
    "linear_fc1",    # Up/gate projection in MoE FFN expert layers (E)
    "linear_fc2",    # Down projection in MoE FFN expert layers (E)
    "in_proj",       # Input projection in Mamba-2 SSM layers (M)
    "out_proj",      # Output projection in Mamba-2 SSM layers (M)
]

# LoRA applied to ALL layers (52 layers × targets per layer)
# rank=64, alpha=64 → effective multiplier = 1.0
# NO dropout (0.0) — RL needs full signal, no regularization noise
# Router NOT in targets — frozen separately
```

---

## Training Hyperparameters Summary

```yaml
# Model
model: nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16
architecture: hybrid Mamba-Transformer MoE
total_params: 31.6B
active_params: 3.2B

# LoRA
rank: 64
alpha: 64
targets: [linear_qkv, linear_proj, linear_fc1, linear_fc2, in_proj, out_proj]

# GSPO
algorithm: GSPO (segment-level importance sampling)
clip_low: 0.0003
clip_high: 0.0004
dual_clip: true (coef=5.0)
kl_coef: 0.0 (no KL penalty — LoRA + clipping sufficient)
loss_agg: seq-mean-token-mean
norm_adv_by_std: true

# Training
lr: 3e-6
total_steps: 450
batch_size: 128 (prompts per step)
group_size: 8 (rollouts per prompt)
max_grad_norm: 1.0
temperature: 1.0 (unbiased)
warmup_steps: 10

# Curriculum (3 phases)
phase_1: steps 0-100,   max_turns=15, difficulty=[easy]
phase_2: steps 100-250, max_turns=30, difficulty=[easy, medium]
phase_3: steps 250-450, max_turns=50, difficulty=[easy, medium, hard]

# Rewards
outcome: {pass: 1.0, fail: -0.1, empty: -0.2, timeout: -0.5}
gate_threshold: -0.5
prm_weight: 0.05
length_penalty_weight: 0.1

# MoE
freeze_router: true
aux_loss_coeff: 0.0001
expert_bias_update_rate: 0.001
num_experts: 128 (+ 2 shared)
top_k: 6

# Hardware
gpus: 8×H100 80GB
tp_size: 2 (for vLLM serving)
max_model_len: 131072
docker_containers: 64 (concurrent)
docker_timeout: 1800s
```

---

## Entry Point Flow (`scripts/run_stage2.py`)

```python
def main():
    # 1. Load config
    config = load_milo_config("configs/ppo_trainer.yaml")

    # 2. Load dataset
    dataset = MiloDataset.from_jsonl("data/tasks/train.jsonl")

    # 3. Build task registry
    task_registry = {t.task_id: t for t in dataset.tasks}
    task_difficulties = {i: task.difficulty for i, task in enumerate(dataset.tasks)}

    # 4. Create executor (Docker + ECR)
    executor = DockerExecutor(
        max_concurrent=64,
        timeout=1800,
        ecr_config=ecr_config,
    )

    # 5. Create curriculum sampler
    curriculum = ScalingInterRLSampler(
        task_difficulties=task_difficulties,
        curriculum_config=config.curriculum,
    )

    # 6. Create reward manager
    reward_manager = MiloRewardManager(
        tokenizer=None,  # tokenizer loaded separately
        executor=executor,
        task_registry=task_registry,
    )

    # 7. Create trainer & run
    trainer = MiloTrainer(
        config=config,
        reward_manager=reward_manager,
        curriculum=curriculum,
    )
    trainer.fit()  # Main loop: 450 steps
```

---

## Evaluation Flow (`src/eval/`)

```
┌──────────────────────────────────────┐
│  per_pr.py                           │
│  - Load checkpoint                   │
│  - For each test task:               │
│    - Generate N=1 response (greedy)  │
│    - Grade via Docker                │
│    - Record pass@1                   │
│  - Report: pass@1, per-difficulty    │
└──────────────────────────────────────┘

┌──────────────────────────────────────┐
│  best_of_n.py                        │
│  - Load checkpoint                   │
│  - For each test task:               │
│    - Generate N=8 responses (temp=1) │
│    - Grade all via Docker            │
│    - pass@k = 1 - C(n-c,k)/C(n,k)  │
│  - Report: pass@1, pass@4, pass@8   │
└──────────────────────────────────────┘
```

---

## Error Handling & Recovery

| Failure Mode | Detection | Recovery |
|---|---|---|
| Docker timeout | `DockerResult.timed_out=True` | Mask trajectory, penalize (-0.5) |
| ECR auth expired | HTTP 401 from registry | `ECRAuthManager.refresh()` (12h token) |
| Expert collapse | `check_expert_collapse()` | Log warning + increase bias update rate |
| Gradient explosion | `grad_norm > 10.0` | Halve learning rate |
| Echo trap | repetition_ratio > 0.6 | Increase temperature ×1.2 |
| Reward collapse | mean_reward < 0.01 for 30 steps | Fatal stop (model broken) |
| OOM | CUDA error caught | Reduce batch size (suggested) |
| 3 consecutive Docker failures | Counter in trainer | Fatal stop (infra broken) |

---

## File Dependencies (Import Graph)

```
scripts/run_stage2.py
  └── src/core/config.py (load_milo_config)
  └── src/data/dataset.py (MiloDataset)
  └── src/rollout/docker_executor.py (DockerExecutor)
  └── src/training/curriculum.py (ScalingInterRLSampler)
  └── src/training/reward_manager.py (MiloRewardManager)
  └── src/training/trainer.py (MiloTrainer)
        └── src/monitoring/kill_conditions.py (KillConditionMonitor)
        └── src/monitoring/metrics.py (MetricsTracker)
        └── src/monitoring/replay.py (RolloutReplayStore)
        └── src/prm/step_advantage.py (StepAdvantageEstimator)
        └── src/training/gspo_loss.py (GSPOLossComputer)
        └── src/training/gated_rewards.py (GatedRewardComputer)
        └── src/training/moe_utils.py (MoETrainingManager)

src/training/reward_manager.py
  └── src/rollout/docker_executor.py (DockerExecutor, DockerResult)
  └── src/rollout/patch_utils.py (extract_patch, is_compact_filtered)
  └── src/prm/scorer.py (LLMJudgeScorer, TrainedPRMScorer)
  └── src/prm/shaper.py (PotentialShaper)

src/rollout/docker_executor.py
  └── src/rollout/ecr.py (ECRAuthManager, ECRImageManager)
  └── docker (Docker SDK)
```

---

## Testing Coverage

```
106 tests passing across:
  - tests/core/          → config loading, schema validation
  - tests/training/      → trainer, curriculum, rewards, GSPO loss
  - tests/rollout/       → docker executor, tool actions, ECR auth
  - tests/prm/           → scorer, shaper, step advantage
  - tests/monitoring/    → kill conditions, metrics, replay
  - tests/data/          → dataset loading, decomposer, augmentation
  - tests/eval/          → per_pr, best_of_n
```

---

## Research Papers Grounding

| Component | Paper | Key Contribution |
|---|---|---|
| GSPO loss | arXiv:2507.18071 | Sequence-level ratio stabilizes MoE RL |
| Gated Rewards | arXiv:2508.10548 | G-RA: outcome gates step rewards (47%→93%) |
| Length penalty | arXiv:2508.03501 | LHT-SWE: penalize overlong episodes |
| Frozen router | arXiv:2512.20848 | NVIDIA Nemotron-3 RL recipe |
| Curriculum | arXiv:2509.08755 | ScalingInter-RL progressive difficulty |
| Expert bias | DeepSeek-V3 | Aux-loss-free load balancing via bias |
| LoRA targets | NVIDIA Megatron-Bridge | Official PEFT recipe for NemotronH |
| Cascade RL | arXiv:2603.19220 | Nemotron-Cascade 2: 30B-A3B full-param RL recipe |
| PivotRL | arXiv:2603.21383 | High-variance turn selection (disabled v1) |
