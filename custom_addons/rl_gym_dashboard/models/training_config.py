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
        ('gspo', 'GSPO'),
        ('gtpo', 'GTPO'),
    ], string='Policy Type', default='gspo')

    # LoRA Configuration
    lora_enabled = fields.Boolean(string='Enable LoRA', default=True)
    lora_rank = fields.Integer(string='LoRA Rank', default=64)
    lora_alpha = fields.Integer(string='LoRA Alpha', default=128)
    lora_dropout = fields.Float(string='LoRA Dropout', default=0.05,
                                digits=(4, 3))
    lora_target_modules = fields.Char(
        string='Target Modules',
        default='q_proj,k_proj,v_proj,o_proj,gate_proj,up_proj,down_proj')

    # Training Hyperparameters
    learning_rate = fields.Float(string='Learning Rate', default=3e-6,
                                 digits=(10, 8))
    batch_size = fields.Integer(string='Batch Size', default=64)
    gradient_accumulation_steps = fields.Integer(
        string='Gradient Accumulation Steps', default=4)
    max_steps = fields.Integer(string='Max Steps', default=450)
    warmup_steps = fields.Integer(string='Warmup Steps', default=50)
    weight_decay = fields.Float(string='Weight Decay', default=0.01,
                                digits=(6, 4))
    max_grad_norm = fields.Float(string='Max Gradient Norm', default=1.0,
                                 digits=(4, 2))

    # Policy Configuration (shared)
    gspo_group_size = fields.Integer(string='Group Size', default=8)
    gspo_clip_range = fields.Float(string='Clip Range', default=0.2,
                                   digits=(4, 3))
    gspo_kl_coeff = fields.Float(string='KL Coefficient', default=0.01,
                                  digits=(6, 4))
    clip_low = fields.Float(string='Clip Low', default=0.2, digits=(6, 4))
    clip_high = fields.Float(string='Clip High', default=0.28, digits=(6, 4))

    # GTPO-specific
    gtpo_gamma = fields.Float(string='GTPO Gamma', default=0.9, digits=(4, 3))
    gtpo_ent_threshold = fields.Float(string='Entropy Threshold', default=0.7,
                                      digits=(4, 3))
    gtpo_ent_scale = fields.Float(string='Entropy Scale', default=0.1,
                                  digits=(4, 3))

    # Curriculum Configuration
    curriculum_enabled = fields.Boolean(string='Enable Curriculum', default=True)
    curriculum_phases = fields.Integer(string='Curriculum Phases', default=4)
    difficulty_start = fields.Float(string='Start Difficulty', default=0.3,
                                    digits=(4, 2))
    difficulty_end = fields.Float(string='End Difficulty', default=0.9,
                                  digits=(4, 2))

    # Hardware
    num_gpus = fields.Integer(string='Number of GPUs', default=8)
    precision = fields.Selection([
        ('fp32', 'FP32'),
        ('fp16', 'FP16'),
        ('bf16', 'BF16'),
    ], string='Precision', default='bf16')

    # Scheduler
    scheduler_type = fields.Selection([
        ('cosine', 'Cosine'),
        ('linear', 'Linear'),
        ('constant', 'Constant'),
        ('cosine_with_restarts', 'Cosine with Restarts'),
    ], string='Scheduler', default='cosine')
