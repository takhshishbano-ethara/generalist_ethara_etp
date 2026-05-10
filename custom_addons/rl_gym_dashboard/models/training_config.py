# -*- coding: utf-8 -*-

from odoo import models, fields


class RlTrainingConfig(models.Model):
    _name = 'rl.training.config'
    _description = 'RL Training Configuration'
    _order = 'name'

    name = fields.Char(string='Config Name', required=True)
    model_id = fields.Many2one('rl.training.model', string='Model',
                               ondelete='cascade')

    # Policy Type
    policy_type = fields.Selection([
        ('gtpo', 'GTPO'),
    ], string='Policy Type', default='gtpo')

    # LoRA Configuration
    lora_enabled = fields.Boolean(string='Enable LoRA', default=True)
    lora_rank = fields.Integer(string='LoRA Rank', default=64)
    lora_alpha = fields.Integer(string='LoRA Alpha', default=256)
    lora_dropout = fields.Float(string='LoRA Dropout', default=0.0,
                                digits=(4, 3))
    lora_target_modules = fields.Char(
        string='Target Modules',
        default='linear_qkv,linear_proj,linear_fc1,linear_fc2,in_proj')
    lora_exclude_modules = fields.Char(
        string='Exclude Modules',
        default='*out_proj*')
    lora_a_init = fields.Selection([
        ('kaiming', 'Kaiming'),
        ('xavier', 'Xavier'),
    ], string='LoRA A Init', default='xavier')

    # Training Hyperparameters
    learning_rate = fields.Float(string='Learning Rate', default=3e-6,
                                 digits=(10, 8))
    batch_size = fields.Integer(string='Batch Size', default=64)
    gradient_accumulation_steps = fields.Integer(
        string='Gradient Accumulation Steps', default=4)
    max_steps = fields.Integer(string='Max Steps', default=450)
    warmup_steps = fields.Integer(string='Warmup Steps', default=10)
    weight_decay = fields.Float(string='Weight Decay', default=0.01,
                                digits=(6, 4))
    max_grad_norm = fields.Float(string='Max Gradient Norm', default=1.0,
                                 digits=(4, 2))

    # Policy Configuration (shared)
    gspo_group_size = fields.Integer(string='Group Size', default=8)
    gspo_clip_range = fields.Float(string='Clip Range', default=0.2,
                                   digits=(4, 3))
    gspo_kl_coeff = fields.Float(string='KL Coefficient', default=0.0,
                                  digits=(6, 4))
    clip_low = fields.Float(string='Clip Low', default=0.2, digits=(6, 4))
    clip_high = fields.Float(string='Clip High', default=0.28, digits=(6, 4))

    # GTPO-specific
    gtpo_gamma = fields.Float(string='GTPO Gamma', default=0.9, digits=(4, 3))
    gtpo_ent_threshold = fields.Float(string='Entropy Threshold', default=0.7,
                                      digits=(4, 3))
    gtpo_ent_scale = fields.Float(string='Entropy Scale', default=0.1,
                                  digits=(4, 3))

    # Loss Advanced
    dual_clip = fields.Boolean(string='Dual Clip', default=True)
    dual_clip_coef = fields.Float(string='Dual Clip Coefficient', default=5.0,
                                  digits=(4, 2))
    norm_adv_by_std = fields.Boolean(string='Normalize Advantages', default=False)

    # Generation / Rollout
    temperature = fields.Float(string='Temperature', default=1.0, digits=(4, 2))
    top_p = fields.Float(string='Top P', default=1.0, digits=(4, 2))
    max_new_tokens = fields.Integer(string='Max New Tokens', default=4096)

    # Reward Shaping
    outcome_pass = fields.Float(string='Outcome Pass', default=1.0, digits=(4, 2))
    outcome_fail = fields.Float(string='Outcome Fail', default=-0.1, digits=(4, 2))
    outcome_empty = fields.Float(string='Outcome Empty', default=-0.2, digits=(4, 2))
    outcome_timeout = fields.Float(string='Outcome Timeout', default=-0.5,
                                   digits=(4, 2))
    length_penalty_weight = fields.Float(string='Length Penalty Weight',
                                         default=0.1, digits=(4, 3))
    partial_credit_enabled = fields.Boolean(string='Partial Credit', default=True)
    partial_credit_alpha = fields.Float(string='Partial Credit Alpha',
                                        default=0.5, digits=(4, 2))
    format_penalty_enabled = fields.Boolean(string='Format Penalty', default=True)
    format_penalty_value = fields.Float(string='Format Penalty Value',
                                        default=-0.1, digits=(4, 2))
    overlong_penalty = fields.Boolean(string='Overlong Penalty', default=True)
    overlong_penalty_threshold = fields.Integer(string='Overlong Threshold',
                                               default=10)

    # Monitoring / Safety
    checkpoint_every = fields.Integer(string='Checkpoint Every', default=10)
    eval_every = fields.Integer(string='Eval Every', default=10)
    echo_trap_threshold = fields.Float(string='Echo Trap Threshold',
                                       default=0.02, digits=(6, 4))
    echo_trap_window = fields.Integer(string='Echo Trap Window', default=20)
    grad_explosion_threshold = fields.Float(string='Grad Explosion Threshold',
                                           default=100.0, digits=(6, 2))
    dead_training_window = fields.Integer(string='Dead Training Window',
                                          default=20)

    # Curriculum Configuration
    curriculum_enabled = fields.Boolean(string='Enable Curriculum', default=True)
    curriculum_phases = fields.Integer(string='Curriculum Phases', default=4)
    difficulty_start = fields.Float(string='Start Difficulty', default=0.3,
                                    digits=(4, 2))
    difficulty_end = fields.Float(string='End Difficulty', default=0.9,
                                  digits=(4, 2))
    advance_threshold = fields.Float(string='Advance Threshold', default=0.7,
                                     digits=(4, 2))
    advance_window = fields.Integer(string='Advance Window', default=5)
    phase_max_turns = fields.Char(string='Phase Max Turns',
                                  default='10,20,35,50')

    # Hardware
    num_gpus = fields.Integer(string='Number of GPUs', default=8)
    precision = fields.Selection([
        ('fp32', 'FP32'),
        ('fp16', 'FP16'),
        ('bf16', 'BF16'),
    ], string='Precision', default='bf16')
    tp_size = fields.Integer(string='Tensor Parallel Size', default=2)
    max_model_len = fields.Integer(string='Max Model Length', default=131072)
    docker_containers = fields.Integer(string='Docker Containers', default=64)
    docker_timeout = fields.Integer(string='Docker Timeout', default=1800)
    vllm_gpus = fields.Integer(string='vLLM GPUs', default=2)

    # Scheduler
    scheduler_type = fields.Selection([
        ('cosine', 'Cosine'),
        ('linear', 'Linear'),
        ('constant', 'Constant'),
        ('cosine_with_restarts', 'Cosine with Restarts'),
    ], string='Scheduler', default='cosine')
    min_lr_ratio = fields.Float(string='Min LR Ratio', default=0.1,
                                digits=(4, 3))

    # PRM / Advantage Configuration
    prm_weight = fields.Float(string='PRM Weight', default=0.3, digits=(4, 3))
    shaping_alpha = fields.Float(string='Shaping Alpha', default=0.3,
                                 digits=(4, 3))
    advantage_mode = fields.Selection([
        ('gtpo', 'GTPO (Discounted Returns + Entropy Credit)'),
        ('rloo', 'RLOO (Leave-One-Out Baseline)'),
        ('step_wise', 'Step-Wise PRM'),
        ('hybrid', 'Hybrid (RLOO + Step-Wise)'),
    ], string='Advantage Mode', default='gtpo')
