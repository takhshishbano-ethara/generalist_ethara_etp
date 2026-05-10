# -*- coding: utf-8 -*-

import json
import logging
import math
import time
import random
from odoo import models, fields, api

_logger = logging.getLogger(__name__)


class RlTrainingJob(models.Model):
    _name = 'rl.training.job'
    _description = 'RL Training Job'
    _order = 'create_date desc'
    _inherit = ['mail.thread']

    name = fields.Char(string='Job Name', required=True, tracking=True)
    state = fields.Selection([
        ('draft', 'Draft'),
        ('configured', 'Configured'),
        ('training', 'Training'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
        ('cancelled', 'Cancelled'),
    ], string='Status', default='draft', required=True, tracking=True)

    # Relations
    model_id = fields.Many2one('rl.training.model', string='Model',
                               required=True, ondelete='restrict')
    config_id = fields.Many2one('rl.training.config', string='Configuration',
                                ondelete='set null')
    dataset_id = fields.Many2one('rl.training.dataset', string='Dataset',
                                 ondelete='set null')
    metric_ids = fields.One2many('rl.training.metric', 'job_id',
                                 string='Metrics')
    weight_ids = fields.One2many('rl.training.weight', 'job_id',
                                 string='Weights')

    # Training progress
    current_step = fields.Integer(string='Current Step', default=0)
    total_steps = fields.Integer(string='Total Steps', default=450)
    progress = fields.Float(string='Progress (%)', compute='_compute_progress',
                            store=True)
    current_loss = fields.Float(string='Current Loss', digits=(10, 6))
    current_reward = fields.Float(string='Current Reward', digits=(10, 4))
    best_reward = fields.Float(string='Best Reward', digits=(10, 4))

    # Timestamps
    started_at = fields.Datetime(string='Started At')
    completed_at = fields.Datetime(string='Completed At')
    elapsed_time = fields.Float(string='Elapsed Time (s)')

    # Error info
    error_message = fields.Text(string='Error Message')

    @api.depends('current_step', 'total_steps')
    def _compute_progress(self):
        for rec in self:
            if rec.total_steps > 0:
                rec.progress = (rec.current_step / rec.total_steps) * 100.0
            else:
                rec.progress = 0.0

    def action_configure(self):
        """Move job to configured state."""
        self.ensure_one()
        self.write({'state': 'configured'})

    def action_start_training(self):
        """Start the training process (simulated)."""
        self.ensure_one()
        self.write({
            'state': 'training',
            'started_at': fields.Datetime.now(),
            'current_step': 0,
        })
        return True

    def action_simulate_step(self):
        """Simulate a single training step with coupled PPO/GRPO dynamics."""
        self.ensure_one()
        if self.state != 'training':
            return False

        step = self.current_step + 1
        total = self.total_steps
        t = step / total

        policy_type = self.config_id.policy_type if self.config_id else 'gspo'

        rng = random.Random(42 + step * 7919)

        def noise(sigma=1.0):
            return rng.gauss(0, sigma)

        def sigmoid(x):
            return 1.0 / (1.0 + math.exp(-max(-500, min(500, x))))

        # LR: cosine decay with linear warmup
        lr_max = 3e-5 if policy_type == 'gspo' else 4e-5
        warmup_frac = 0.05
        if t < warmup_frac:
            learning_rate = lr_max * (t / warmup_frac)
        else:
            cos_decay = 0.5 * (1 + math.cos(math.pi * (t - warmup_frac) / (1 - warmup_frac)))
            learning_rate = lr_max * max(cos_decay, 0.1)

        # Reward: phase transitions with plateaus and regressions
        phase1 = sigmoid((t - 0.20) * 25)
        phase2 = sigmoid((t - 0.50) * 20)
        phase3 = sigmoid((t - 0.75) * 18)

        if policy_type == 'gtpo':
            reward_base = 0.05 + 0.25 * phase1 + 0.30 * phase2 + 0.20 * phase3
        else:
            reward_base = 0.03 + 0.20 * phase1 + 0.25 * phase2 + 0.22 * phase3

        regression1 = -0.08 * math.exp(-((t - 0.35) ** 2) / 0.002)
        regression2 = -0.06 * math.exp(-((t - 0.65) ** 2) / 0.001)
        reward_noise = noise(0.03) + 0.015 * math.sin(step * 0.3)

        reward = reward_base + regression1 + regression2 + reward_noise
        reward_mean = reward + noise(0.01)
        reward_std = max(0.05, 0.18 + noise(0.025))

        # Entropy: logarithmic decay from LLM scale, floor at ~2.0
        entropy_start = 5.5 if policy_type == 'gspo' else 5.2
        entropy_floor = 2.0
        entropy_decay = entropy_start - (entropy_start - entropy_floor) * math.log(1 + 8 * t) / math.log(9)
        entropy = max(entropy_floor, entropy_decay + noise(0.08))

        # Policy loss: driven by reward improvement rate
        reward_gradient = abs(
            25 * phase1 * (1 - phase1) * 0.25 +
            20 * phase2 * (1 - phase2) * 0.30 +
            18 * phase3 * (1 - phase3) * 0.20
        ) / total
        policy_loss = 0.02 + 0.15 * math.exp(-3 * t) + 0.5 * reward_gradient + noise(0.008)
        policy_loss = max(0.005, policy_loss)

        # Value loss: independent, spikes during phase transitions
        value_loss_base = 0.8 * math.exp(-4 * t) + 0.02
        value_transition_spike = 0.15 * (phase1 * (1 - phase1) + phase2 * (1 - phase2))
        value_loss = value_loss_base + value_transition_spike + abs(noise(0.012))
        value_loss = max(0.003, value_loss)

        # Total loss: emergent from components
        loss = policy_loss + 0.5 * value_loss - 0.01 * entropy + noise(0.005)
        loss = max(0.01, loss)

        # KL: tracks adaptive target with overshoots, coupled to policy_loss
        kl_target = 0.015
        kl_raw = kl_target + 0.4 * (policy_loss - 0.03) + noise(0.004)
        kl_error = kl_raw - kl_target
        kl_divergence = kl_target + 0.6 * kl_error
        kl_divergence = max(0.001, min(0.08, kl_divergence))

        # Gradient norm: noisy with reward-correlated spikes, soft-clipped at 1.0
        grad_base = 0.3 + 0.4 * math.exp(-2 * t)
        grad_reward_coupling = 2.0 * abs(reward_noise) + 1.5 * reward_gradient
        grad_noise = abs(noise(0.12))
        spike = 0.0
        if rng.random() < 0.04:
            spike = rng.uniform(0.5, 1.5)
        grad_norm_raw = grad_base + grad_reward_coupling + grad_noise + spike
        gradient_norm = min(grad_norm_raw, 1.0 + 0.3 * max(0, grad_norm_raw - 1.0))
        gradient_norm = max(0.05, gradient_norm)

        # Clip fraction: self-regulating ~0.10-0.20, coupled to KL
        clip_base = 0.13 + 0.03 * math.sin(t * math.pi * 4)
        clip_kl_coupling = 0.8 * max(0, kl_divergence - kl_target)
        clip_fraction = clip_base + clip_kl_coupling + noise(0.015)
        clip_fraction = max(0.04, min(0.28, clip_fraction))

        # Advantage mean: constant-variance noise around 0
        advantage_mean = noise(0.12)

        # GPU memory: constant (fixed by model + batch size)
        gpu_base = 68.5 if policy_type == 'gspo' else 72.0
        gpu_memory_used = gpu_base + noise(0.3)
        gpu_memory_used = max(60.0, min(79.5, gpu_memory_used))

        # Throughput: coupled (tokens = samples × avg_seq_len)
        avg_seq_length = 1800 + noise(100)
        samples_sec = max(2.0, 4.2 + noise(0.3) - 0.3 * spike)
        tokens_sec = max(3000, samples_sec * avg_seq_length)

        self.env['rl.training.metric'].create({
            'job_id': self.id,
            'step': step,
            'loss': loss,
            'reward': reward,
            'gradient_norm': gradient_norm,
            'learning_rate': learning_rate,
            'entropy': entropy,
            'kl_divergence': kl_divergence,
            'tokens_per_second': tokens_sec,
            'policy_loss': policy_loss,
            'value_loss': value_loss,
            'clip_fraction': clip_fraction,
            'reward_mean': reward_mean,
            'reward_std': reward_std,
            'advantage_mean': advantage_mean,
            'samples_per_second': samples_sec,
            'gpu_memory_used': gpu_memory_used,
        })

        best = max(self.best_reward, reward)
        vals = {
            'current_step': step,
            'current_loss': loss,
            'current_reward': reward,
            'best_reward': best,
        }

        if step >= total:
            vals.update({
                'state': 'completed',
                'completed_at': fields.Datetime.now(),
            })

        self.write(vals)
        return True

    def action_cancel(self):
        """Cancel the training job."""
        self.ensure_one()
        self.write({'state': 'cancelled'})

    def action_reset_draft(self):
        """Reset job to draft state."""
        self.ensure_one()
        self.metric_ids.unlink()
        self.write({
            'state': 'draft',
            'current_step': 0,
            'current_loss': 0,
            'current_reward': 0,
            'best_reward': 0,
            'started_at': False,
            'completed_at': False,
            'error_message': False,
        })
