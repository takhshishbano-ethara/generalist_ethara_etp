# -*- coding: utf-8 -*-

from odoo import models, fields


class RlTrainingMetric(models.Model):
    _name = 'rl.training.metric'
    _description = 'RL Training Metric'
    _order = 'step'

    job_id = fields.Many2one('rl.training.job', string='Training Job',
                             required=True, ondelete='cascade', index=True)
    step = fields.Integer(string='Step', required=True, index=True)

    # Core metrics
    loss = fields.Float(string='Loss', digits=(10, 6))
    reward = fields.Float(string='Reward', digits=(10, 4))
    gradient_norm = fields.Float(string='Gradient Norm', digits=(10, 6))
    learning_rate = fields.Float(string='Learning Rate', digits=(12, 10))

    # Policy metrics
    entropy = fields.Float(string='Entropy', digits=(10, 6))
    kl_divergence = fields.Float(string='KL Divergence', digits=(10, 6))
    clip_fraction = fields.Float(string='Clip Fraction', digits=(6, 4))
    policy_loss = fields.Float(string='Policy Loss', digits=(10, 6))
    value_loss = fields.Float(string='Value Loss', digits=(10, 6))

    # Reward components
    reward_mean = fields.Float(string='Reward Mean', digits=(10, 4))
    reward_std = fields.Float(string='Reward Std', digits=(10, 4))
    advantage_mean = fields.Float(string='Advantage Mean', digits=(10, 4))

    # Throughput
    tokens_per_second = fields.Float(string='Tokens/Second')
    samples_per_second = fields.Float(string='Samples/Second')
    gpu_memory_used = fields.Float(string='GPU Memory (GB)', digits=(6, 2))

    timestamp = fields.Datetime(string='Timestamp',
                                default=fields.Datetime.now)
