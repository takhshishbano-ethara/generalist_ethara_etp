"""Typed dataclass configuration hierarchy for MILO-RL pipeline."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from omegaconf import DictConfig, OmegaConf

log = logging.getLogger(__name__)


def _warn_unknown_keys(raw: dict, valid_fields: dict | set, section: str) -> None:
    """Warn about keys in raw that are not in valid_fields (catches typos)."""
    unknown = set(raw.keys()) - set(valid_fields)
    if unknown:
        log.warning(
            "Unknown keys in '%s' config (possible typo): %s",
            section,
            sorted(unknown),
        )


@dataclass
class LoRAConfig:
    rank: int = 64
    alpha: int = 64
    target_modules: str = "linear_qkv,linear_proj,linear_fc1,linear_fc2,in_proj,out_proj"
    dropout: float = 0.0
    modules_to_save: str = ""


@dataclass
class GSPOConfig:
    """GSPO/GTPO loss hyperparameters.

    When loss_type="gtpo", clip_low/clip_high use DAPO-style asymmetric range (0.2/0.28).
    When loss_type="gspo", clip_low/clip_high use segment-level small values (3e-4/4e-4).
    """
    learning_rate: float = 3e-6
    beta_kl: float = 0.0
    group_size: int = 8
    batch_size: int = 64
    clip_low: float = 0.2
    clip_high: float = 0.28
    norm_adv_by_std: bool = False
    length_normalize: bool = True
    compact_filtering: bool = True
    temperature: float = 1.0
    top_p: float = 1.0
    top_k: int = -1
    max_grad_norm: float = 1.0
    warmup_steps: int = 10
    total_steps: int = 450
    epochs_per_rollout: int = 1
    micro_batch_size: int = 1
    importance_sampling: str = "segment"  # "token" | "sequence" | "segment"
    loss_type: str = "gtpo"  # "gspo" | "gtpo"
    loss_agg_mode: str = "seq-mean-token-mean"
    freeze_router: bool = True
    moe_aux_loss_coeff: float = 0.0001
    expert_bias_update_rate: float = 0.001
    overlong_penalty: bool = True
    overlong_penalty_threshold: int = 10  # L_thr in turns


@dataclass
class CurriculumPhaseConfig:
    phase_id: int = 1
    max_turns: int = 10
    step_start: int = 0
    step_end: int = 50
    difficulty_filter: list[str] = field(default_factory=lambda: ["easy"])
    expected_success_rate: float = 0.3
    hard_bias: float = 1.0


@dataclass
class CurriculumConfig:
    phases: list[CurriculumPhaseConfig] = field(
        default_factory=lambda: [
            CurriculumPhaseConfig(
                phase_id=1,
                max_turns=10,
                step_start=0,
                step_end=50,
                difficulty_filter=["easy"],
                expected_success_rate=0.3,
            ),
            CurriculumPhaseConfig(
                phase_id=2,
                max_turns=20,
                step_start=50,
                step_end=150,
                difficulty_filter=["easy", "medium"],
                expected_success_rate=0.2,
            ),
            CurriculumPhaseConfig(
                phase_id=3,
                max_turns=35,
                step_start=150,
                step_end=300,
                difficulty_filter=["easy", "medium", "hard"],
                expected_success_rate=0.15,
                hard_bias=1.5,
            ),
            CurriculumPhaseConfig(
                phase_id=4,
                max_turns=50,
                step_start=300,
                step_end=500,
                difficulty_filter=["easy", "medium", "hard"],
                expected_success_rate=0.1,
                hard_bias=2.0,
            ),
        ]
    )
    variance_target_low: float = 0.05
    variance_target_high: float = 0.4
    advance_threshold: float = 0.7
    advance_window: int = 5


@dataclass
class HardwareConfig:
    n_gpus: int = 8
    training_gpus: int = 6
    vllm_gpus: int = 2
    tp_size: int = 2
    gpu_memory_utilization: float = 0.85
    max_model_len: int = 131072
    max_concurrent_sequences: int = 32
    docker_containers: int = 64
    docker_cpu_per_container: float = 1.0
    docker_mem_per_container: str = "4g"
    docker_timeout: int = 1800


@dataclass
class ECRConfig:
    enabled: bool = False
    account_id: str = "426628337772"
    region: str = "ap-south-1"
    repository: str = "rfp-coding-q1-tag"
    refresh_buffer_seconds: int = 1800
    patch_path: str = "/home/fix.patch"
    evaluation_command: str = (
        'bash -c "apt update ; apt install -y patch ; '
        "sed -i 's@git apply.*@patch --batch --fuzz=5 -p1 -i /home/test.patch;"
        "patch --batch --fuzz=5 -p1 -i /home/fix.patch@g' /home/fix-run.sh ; "
        'chmod +x /home/*.sh ; /home/fix-run.sh"'
    )


@dataclass
class MonitoringConfig:
    echo_trap_threshold: float = 0.02
    echo_trap_window: int = 20
    grad_explosion_threshold: float = 100.0
    grad_explosion_window: int = 3
    forgetting_threshold: float = 0.05
    dead_training_window: int = 20
    mode_collapse_ngram_overlap: float = 0.9
    checkpoint_every: int = 10
    keep_checkpoints: int = 20
    eval_every: int = 10
    use_wandb: bool = False
    wandb_project: str = "milo-rl"


@dataclass
class RFTConfig:
    n_per_task: int = 16
    temperature: float = 0.7
    top_p: float = 0.95
    max_tokens: int = 32768
    sft_lr: float = 4e-6
    sft_epochs: int = 6
    sft_batch_size: int = 4
    gate_threshold: float = 0.15
    teacher_model: str = "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16"
    teacher_fallback_threshold: float = 0.05


@dataclass
class EvalConfig:
    eval_set_size: int = 200
    best_of_n: int = 8
    per_pr_n: int = 1
    temperature: float = 1.0
    max_turns: int = 50
    full_milo_max_turns: int = 480
    full_milo_context_strategy: str = "first_4k_last_20k"


@dataclass
class Stage0Config:
    milo_data_dir: str = "data/milo-bench/"
    languages: list[str] = field(default_factory=lambda: ["python", "go"])
    augmentation_strategies: list[str] = field(
        default_factory=lambda: ["commit_reversion", "ast_mutation", "llm_bug_injection"]
    )
    llm_endpoint: str = ""
    target_task_count: int = 1500
    validation_timeout: int = 300
    output_dir: str = "data/tasks/"
    eval_split_size: int = 200


@dataclass
class PRMConfig:
    """Process Reward Model configuration."""

    enabled: bool = False
    mode: str = "bedrock"  # "llm_judge" | "trained" | "self_prm" | "bedrock"
    advantage_mode: str = "hybrid"  # "rloo" | "step_wise" | "hybrid" | "gtpo"

    # LLM-as-Judge
    judge_endpoint: str = "http://localhost:8001/v1/chat/completions"
    judge_model: str = "gpt-4o"
    judge_votes: int = 3
    judge_timeout: float = 30.0
    judge_max_concurrent: int = 16

    # Trained PRM
    prm_model_path: str = "Qwen/Qwen2.5-Coder-1.5B-Instruct"
    prm_checkpoint: str = ""
    prm_lora_rank: int = 8

    # Bedrock Claude
    bedrock_model_arn: str = ""
    bedrock_region: str = "ap-south-1"

    # Potential-based shaping (TIPS)
    shaping_alpha: float = 0.05
    outcome_gate: bool = True
    gate_mode: str = "add_on_success"  # "add_on_success" | "hard_gate" | "multiply"

    # Teacher refresh
    teacher_refresh_steps: int = 200
    teacher_model_path: str = ""

    # Filtering
    min_group_variance: float = 0.01

    # GTPO discounted returns
    gtpo_gamma: float = 0.9


@dataclass
class GatedRewardConfig:
    """Gated Rewards (G-RA) configuration."""

    enabled: bool = True
    outcome_pass: float = 1.0
    outcome_fail: float = -0.1
    outcome_empty: float = -0.2
    outcome_timeout: float = -0.5
    length_penalty_weight: float = 0.1
    prm_weight: float = 0.05
    gate_threshold: float = 0.0  # Hard gate: only PASS (1.0 > 0.0) opens gate (ReCode principle)
    pivot_enabled: bool = False  # disabled for v1 (PivotRL not validated on multi-turn)
    pivot_variance_threshold: float = 0.0
    pivot_difficulty_threshold: float = 0.8

    # Partial credit for failed trajectories (GTPO paper Section 3.4)
    partial_credit_enabled: bool = True
    partial_credit_alpha: float = 0.5
    partial_credit_use_embeddings: bool = True
    partial_credit_heuristic_patch: float = 0.1
    partial_credit_heuristic_tests: float = 0.3
    partial_credit_heuristic_code: float = 0.05

    # Format penalties (GTPO paper Section 3.3)
    format_penalty_enabled: bool = True
    format_penalty_value: float = -0.1
    format_first_turn_tool_required: bool = True


@dataclass
class MoEConfig:
    """MoE-specific training configuration."""

    num_experts: int = 128
    num_shared_experts: int = 2
    top_k: int = 6
    router_score_function: str = "sigmoid"
    router_dtype: str = "fp32"
    freeze_router: bool = True
    aux_loss_coeff: float = 0.0001
    expert_bias_update_rate: float = 0.001
    router_load_balancing_type: str = "seq_aux_loss"


@dataclass
class NemoRLConfig:
    """NeMo-RL integration configuration."""

    backend: str = "fsdp2"  # "fsdp2" or "megatron"
    tensor_parallel_size: int = 2
    sequence_parallel: bool = True
    colocated: bool = True  # colocate policy + generation on same GPUs
    num_nodes: int = 1
    gpus_per_node: int = 8
    max_model_len: int = 131072
    gpu_memory_utilization: float = 0.85
    num_generations_per_prompt: int = 8
    num_prompts_per_step: int = 8
    max_rollout_turns: int = 50
    max_concurrent_containers: int = 64
    container_timeout: int = 1800
    prm_mode: str = "bedrock"  # "bedrock" or "local"
    vllm_backend: str = "vllm"  # "vllm" or "sglang" or "megatron"
    recipe_path: str = "configs/nemo_grpo_milo.yaml"
    resume_from: str = ""


@dataclass
class MiloConfig:
    model_path: str = "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16"
    lora: LoRAConfig = field(default_factory=LoRAConfig)
    gspo: GSPOConfig = field(default_factory=GSPOConfig)
    curriculum: CurriculumConfig = field(default_factory=CurriculumConfig)
    hardware: HardwareConfig = field(default_factory=HardwareConfig)
    monitoring: MonitoringConfig = field(default_factory=MonitoringConfig)
    rft: RFTConfig = field(default_factory=RFTConfig)
    eval: EvalConfig = field(default_factory=EvalConfig)
    stage0: Stage0Config = field(default_factory=Stage0Config)
    prm: PRMConfig = field(default_factory=PRMConfig)
    ecr: ECRConfig = field(default_factory=ECRConfig)
    gated_reward: GatedRewardConfig = field(default_factory=GatedRewardConfig)
    moe: MoEConfig = field(default_factory=MoEConfig)
    nemo_rl: NemoRLConfig = field(default_factory=NemoRLConfig)
    data_dir: str = "data/"
    output_dir: str = "outputs/"
    run_id: str = "run_001"


def load_milo_config(config_path: str | Path) -> MiloConfig:
    path = Path(config_path)
    if not path.exists():
        msg = f"Config file not found: {path}"
        raise FileNotFoundError(msg)

    raw = OmegaConf.load(path)
    if raw is None:
        return MiloConfig()
    container = OmegaConf.to_container(raw, resolve=True)
    if not isinstance(container, dict):
        raise TypeError(f"Expected dict from OmegaConf, got {type(container)}")

    container = _detect_and_wrap_section(container, path.stem)
    return _dict_to_milo_config(container)


def _detect_and_wrap_section(d: dict, filename: str) -> dict:
    top_keys = set(d.keys())
    stage0_signals = {"milo_data_dir", "augmentation", "splitting", "languages"}
    rft_signals = {"generation", "sft", "gate", "teacher"}
    curriculum_signals = {"phases", "advancement", "variance"}
    hardware_signals = {"compute", "memory", "docker"}
    eval_signals = {"per_pr", "best_of_n", "full_milo"}
    prm_signals = {"judge_endpoint", "shaping_alpha", "advantage_mode", "judge_model"}

    if top_keys & prm_signals and "prm" not in top_keys:
        return {"prm": d}
    if top_keys & stage0_signals and "stage0" not in top_keys:
        return {"stage0": d}
    if top_keys & rft_signals and "rft" not in top_keys:
        return {"rft": d}
    if top_keys & curriculum_signals and "curriculum" not in top_keys:
        return {"curriculum": d}
    if top_keys & hardware_signals and "hardware" not in top_keys:
        return {"hardware": d}
    if top_keys & eval_signals and "eval" not in top_keys:
        return {"eval": d}
    return d


def load_from_hydra(cfg: DictConfig) -> MiloConfig:
    """Convert a Hydra DictConfig to typed MiloConfig."""
    container = OmegaConf.to_container(cfg, resolve=True)
    if not isinstance(container, dict):
        raise TypeError(f"Expected dict from OmegaConf, got {type(container)}")
    return _dict_to_milo_config(container)


def _flatten_stage0(d: dict) -> dict:
    flat = {}
    flat["milo_data_dir"] = d.get("milo_data_dir", "data/milo-bench/")
    flat["languages"] = d.get("languages", ["python", "go"])
    aug = d.get("augmentation", {})
    flat["augmentation_strategies"] = aug.get("strategies", ["commit_reversion", "ast_mutation", "llm_bug_injection"])
    flat["llm_endpoint"] = aug.get("llm_endpoint", "")
    flat["target_task_count"] = aug.get("target_task_count", 1500)
    val = d.get("validation", {})
    flat["validation_timeout"] = val.get("timeout", 300)
    flat["output_dir"] = d.get("output_dir", d.get("docker", {}).get("output_dir", "data/tasks/"))
    split = d.get("splitting", {})
    flat["eval_split_size"] = split.get("eval_size", 200)
    return flat


def _flatten_rft(d: dict) -> dict:
    flat = {}
    gen = d.get("generation", {})
    flat["n_per_task"] = gen.get("n_per_task", 16)
    flat["temperature"] = gen.get("temperature", 0.7)
    flat["top_p"] = gen.get("top_p", 0.95)
    flat["max_tokens"] = gen.get("max_tokens", 32768)
    sft = d.get("sft", {})
    flat["sft_lr"] = sft.get("lr", 4e-6)
    flat["sft_epochs"] = sft.get("epochs", 6)
    flat["sft_batch_size"] = sft.get("batch_size", 4)
    gate = d.get("gate", {})
    flat["gate_threshold"] = gate.get("threshold", 0.15)
    teacher = d.get("teacher", {})
    flat["teacher_model"] = teacher.get("model", "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16")
    flat["teacher_fallback_threshold"] = teacher.get("fallback_threshold", 0.05)
    return flat


def _flatten_curriculum(d: dict) -> dict:
    flat = {}
    flat["phases"] = d.get("phases", [])
    adv = d.get("advancement", {})
    flat["advance_threshold"] = adv.get("threshold", 0.7)
    flat["advance_window"] = adv.get("window", 5)
    var = d.get("variance", {})
    flat["variance_target_low"] = var.get("target_low", 0.05)
    flat["variance_target_high"] = var.get("target_high", 0.4)
    return flat


def _flatten_hardware(d: dict) -> dict:
    flat = {}
    compute = d.get("compute", {})
    flat["n_gpus"] = compute.get("n_gpus", 8)
    training_gpus = compute.get("training_gpus", [0, 1, 2, 3, 4, 5])
    flat["training_gpus"] = len(training_gpus) if isinstance(training_gpus, list) else training_gpus
    vllm_gpus = compute.get("vllm_gpus", [6, 7])
    flat["vllm_gpus"] = len(vllm_gpus) if isinstance(vllm_gpus, list) else vllm_gpus
    flat["tp_size"] = compute.get("tp_size", 2)
    mem = d.get("memory", {})
    flat["gpu_memory_utilization"] = mem.get("gpu_memory_utilization", 0.85)
    flat["max_model_len"] = mem.get("max_model_len", 32768)
    flat["max_concurrent_sequences"] = mem.get("max_concurrent_sequences", 32)
    docker = d.get("docker", {})
    flat["docker_containers"] = docker.get("containers", 64)
    flat["docker_cpu_per_container"] = docker.get("cpu_per_container", 1.0)
    flat["docker_mem_per_container"] = docker.get("mem_per_container", "4g")
    flat["docker_timeout"] = docker.get("timeout", 1800)
    return flat


def _flatten_eval(d: dict) -> dict:
    flat = {}
    per_pr = d.get("per_pr", {})
    flat["eval_set_size"] = per_pr.get("eval_set_size", d.get("eval_set_size", 200))
    flat["per_pr_n"] = per_pr.get("n_attempts", 1)
    flat["temperature"] = per_pr.get("temperature", d.get("temperature", 1.0))
    flat["max_turns"] = per_pr.get("max_turns", d.get("max_turns", 50))
    bon = d.get("best_of_n", {})
    flat["best_of_n"] = bon.get("n", 8)
    fm = d.get("full_milo", {})
    flat["full_milo_max_turns"] = fm.get("max_turns", 480)
    flat["full_milo_context_strategy"] = fm.get("context_strategy", "first_4k_last_20k")
    return flat


def _dict_to_milo_config(d: dict) -> MiloConfig:
    """Recursively construct MiloConfig from a plain dict."""
    lora_raw = d.get("lora", {})
    _warn_unknown_keys(lora_raw, LoRAConfig.__dataclass_fields__, "lora")
    lora = LoRAConfig(**{k: v for k, v in lora_raw.items() if k in LoRAConfig.__dataclass_fields__})
    gspo_raw = d.get("gspo", d.get("grpo", {}))
    _warn_unknown_keys(gspo_raw, GSPOConfig.__dataclass_fields__, "gspo")
    gspo = GSPOConfig(**{k: v for k, v in gspo_raw.items() if k in GSPOConfig.__dataclass_fields__})

    curriculum_raw = dict(d.get("curriculum", {}))
    if any(isinstance(v, dict) for v in curriculum_raw.values()):
        curriculum_raw = _flatten_curriculum(curriculum_raw)
    phases_raw = curriculum_raw.pop("phases", [])
    phases = [
        CurriculumPhaseConfig(**{k: v for k, v in p.items() if k in CurriculumPhaseConfig.__dataclass_fields__})
        for p in phases_raw
    ]
    curriculum_fields = {k: v for k, v in curriculum_raw.items() if k in CurriculumConfig.__dataclass_fields__}
    curriculum = CurriculumConfig(phases=phases, **curriculum_fields)

    hardware_raw = d.get("hardware", {})
    if any(isinstance(v, dict) for v in hardware_raw.values()):
        hardware_raw = _flatten_hardware(hardware_raw)
    hardware = HardwareConfig(**{k: v for k, v in hardware_raw.items() if k in HardwareConfig.__dataclass_fields__})

    monitoring = MonitoringConfig(**{k: v for k, v in d.get("monitoring", {}).items() if k in MonitoringConfig.__dataclass_fields__})

    rft_raw = d.get("rft", {})
    if any(isinstance(v, dict) for v in rft_raw.values()):
        rft_raw = _flatten_rft(rft_raw)
    rft = RFTConfig(**{k: v for k, v in rft_raw.items() if k in RFTConfig.__dataclass_fields__})

    eval_raw = d.get("eval", {})
    if any(isinstance(v, dict) for v in eval_raw.values()):
        eval_raw = _flatten_eval(eval_raw)
    eval_cfg = EvalConfig(**{k: v for k, v in eval_raw.items() if k in EvalConfig.__dataclass_fields__})

    stage0_raw = d.get("stage0", {})
    if any(isinstance(v, dict) for v in stage0_raw.values()):
        stage0_raw = _flatten_stage0(stage0_raw)
    stage0 = Stage0Config(**{k: v for k, v in stage0_raw.items() if k in Stage0Config.__dataclass_fields__})

    prm_raw = d.get("prm", {})
    _warn_unknown_keys(prm_raw, PRMConfig.__dataclass_fields__, "prm")
    prm = PRMConfig(**{k: v for k, v in prm_raw.items() if k in PRMConfig.__dataclass_fields__})

    ecr_raw = d.get("ecr", {})
    _warn_unknown_keys(ecr_raw, ECRConfig.__dataclass_fields__, "ecr")
    ecr = ECRConfig(**{k: v for k, v in ecr_raw.items() if k in ECRConfig.__dataclass_fields__})

    gated_raw = d.get("gated_reward", {})
    _warn_unknown_keys(gated_raw, GatedRewardConfig.__dataclass_fields__, "gated_reward")
    gated_reward = GatedRewardConfig(**{k: v for k, v in gated_raw.items() if k in GatedRewardConfig.__dataclass_fields__})

    moe_raw = d.get("moe", {})
    _warn_unknown_keys(moe_raw, MoEConfig.__dataclass_fields__, "moe")
    moe = MoEConfig(**{k: v for k, v in moe_raw.items() if k in MoEConfig.__dataclass_fields__})

    nemo_rl_raw = d.get("nemo_rl", {})
    _warn_unknown_keys(nemo_rl_raw, NemoRLConfig.__dataclass_fields__, "nemo_rl")
    nemo_rl = NemoRLConfig(**{k: v for k, v in nemo_rl_raw.items() if k in NemoRLConfig.__dataclass_fields__})

    return MiloConfig(
        model_path=d.get("model_path", MiloConfig.model_path),
        lora=lora,
        gspo=gspo,
        curriculum=curriculum,
        hardware=hardware,
        monitoring=monitoring,
        rft=rft,
        eval=eval_cfg,
        stage0=stage0,
        prm=prm,
        ecr=ecr,
        gated_reward=gated_reward,
        moe=moe,
        nemo_rl=nemo_rl,
        data_dir=d.get("data_dir", MiloConfig.data_dir),
        output_dir=d.get("output_dir", MiloConfig.output_dir),
        run_id=d.get("run_id", MiloConfig.run_id),
    )
