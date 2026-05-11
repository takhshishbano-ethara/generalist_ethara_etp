# -*- coding: utf-8 -*-

import json
import logging
import math
import time
import random
import threading

import odoo
from odoo import models, fields, api
from odoo.modules.registry import Registry
from odoo.service.server import CommonServer

_logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════════
# Server-side Training Runner (daemon thread)
# Autonomously advances all jobs in 'training' state every ~1.5s
# ═══════════════════════════════════════════════════════════════════════════════

_stop_event = threading.Event()
_runner_thread = None
_runner_lock = threading.Lock()


def _on_server_stop():
    _stop_event.set()


CommonServer.on_stop(_on_server_stop)


class TrainingRunner(threading.Thread):
    """Daemon thread that auto-advances all jobs in 'training' state."""

    def __init__(self, dbname):
        super().__init__(daemon=True, name='rl_gym.TrainingRunner')
        self.dbname = dbname

    def run(self):
        _logger.info("TrainingRunner started for db=%s", self.dbname)
        while not _stop_event.is_set():
            try:
                self._tick()
            except Exception:
                _logger.exception("TrainingRunner tick error")
                _stop_event.wait(5)
                continue
            _stop_event.wait(1.5)
        _logger.info("TrainingRunner stopped")

    def _tick(self):
        registry = Registry(self.dbname)
        with registry.cursor() as cr:
            env = odoo.api.Environment(cr, odoo.SUPERUSER_ID, {})
            jobs = env['rl.training.job'].search([('state', '=', 'training')])
            for job in jobs:
                if _stop_event.is_set():
                    break
                try:
                    job.action_simulate_step()
                    cr.commit()
                except Exception:
                    _logger.exception("TrainingRunner step error job=%s step=%s, skipping",
                                      job.id, job.current_step + 1)
                    cr.rollback()
                    # Skip the failed step to avoid infinite retry loop
                    try:
                        with registry.cursor() as cr2:
                            env2 = odoo.api.Environment(cr2, odoo.SUPERUSER_ID, {})
                            stuck_job = env2['rl.training.job'].browse(job.id)
                            next_step = stuck_job.current_step + 1
                            if next_step >= stuck_job.total_steps:
                                stuck_job.write({
                                    'current_step': stuck_job.total_steps,
                                    'state': 'completed',
                                    'completed_at': fields.Datetime.now(),
                                })
                            else:
                                stuck_job.write({'current_step': next_step})
                            cr2.commit()
                    except Exception:
                        _logger.exception("TrainingRunner failed to skip step for job=%s", job.id)


def start_training_runner(dbname):
    """Start the runner if not already alive."""
    global _runner_thread
    with _runner_lock:
        if _runner_thread and _runner_thread.is_alive():
            return
        _stop_event.clear()
        _runner_thread = TrainingRunner(dbname)
        _runner_thread.start()


def ensure_runner_alive(dbname):
    """Restart the daemon if it died (e.g. worker restart)."""
    global _runner_thread
    with _runner_lock:
        if _runner_thread and _runner_thread.is_alive():
            return
        _stop_event.clear()
        _runner_thread = TrainingRunner(dbname)
        _runner_thread.start()
        _logger.info("TrainingRunner auto-restarted for db=%s", dbname)


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
        if self.state != 'draft':
            return False
        self.write({'state': 'configured'})

    def action_start_training(self):
        self.ensure_one()
        if self.state in ('completed', 'cancelled', 'failed', 'training'):
            return False
        self.write({
            'state': 'training',
            'started_at': fields.Datetime.now(),
            'current_step': 0,
        })
        start_training_runner(self.env.cr.dbname)
        return True

    def action_simulate_step(self):
        self.ensure_one()
        if self.state != 'training':
            return False

        step = self.current_step + 1
        total = self.total_steps or 1
        t = step / total

        rng = random.Random(self.id * 104729 + step * 7919)

        def noise(sigma=1.0):
            return rng.gauss(0, sigma)

        def sigmoid(x):
            return 1.0 / (1.0 + math.exp(-max(-500, min(500, x))))

        # LR: cosine decay with linear warmup
        cfg = self.config_id
        lr_max = cfg.learning_rate if cfg and cfg.learning_rate else 4e-5
        warmup_steps_cfg = cfg.warmup_steps if cfg and cfg.warmup_steps else 10
        warmup_frac = warmup_steps_cfg / total
        if t < warmup_frac:
            learning_rate = lr_max * (t / warmup_frac)
        else:
            cos_progress = (t - warmup_frac) / (1.0 - warmup_frac)
            cos_decay = 0.5 * (1 + math.cos(math.pi * cos_progress))
            learning_rate = lr_max * max(cos_decay, 0.1)

        # Reward: single S-curve with curriculum dips
        # SFT baseline ~0.15, GTPO ceiling ~0.65 for multi-turn coding
        reward_base = 0.15 + 0.50 * sigmoid((t - 0.40) * 8)
        curriculum_dip1 = -0.03 * math.exp(-((t - 0.11) ** 2) / 0.001)
        curriculum_dip2 = -0.04 * math.exp(-((t - 0.33) ** 2) / 0.001)
        curriculum_dip3 = -0.03 * math.exp(-((t - 0.67) ** 2) / 0.001)
        reward_noise = noise(0.04)
        reward = reward_base + curriculum_dip1 + curriculum_dip2 + curriculum_dip3 + reward_noise
        reward = max(0.0, min(1.0, reward))

        reward_mean = reward + noise(0.01)
        reward_std = max(0.08, 0.35 - 0.15 * t + noise(0.03))

        # Entropy: per-token response entropy (instruct model generating code)
        entropy_start = 0.22
        entropy_end = 0.06
        entropy_decay = entropy_start - (entropy_start - entropy_end) * (1 - math.exp(-3 * t))
        entropy = max(0.03, entropy_decay + noise(0.012))

        # Policy loss = negative clipped surrogate (oscillates near 0)
        loss_trend = -0.025 * sigmoid((t - 0.2) * 10)
        loss_variance = 0.015 + 0.005 * math.sin(t * math.pi * 6)
        loss_batch_noise = noise(loss_variance)
        if rng.random() < 0.05:
            loss_batch_noise += rng.uniform(0.01, 0.03)
        loss_curriculum_spike = (
            0.02 * math.exp(-((t - 0.11) ** 2) / 0.0005) +
            0.025 * math.exp(-((t - 0.33) ** 2) / 0.0005) +
            0.02 * math.exp(-((t - 0.67) ** 2) / 0.0005)
        )
        policy_loss = max(-0.08, min(0.05, loss_trend + loss_batch_noise + loss_curriculum_spike))
        loss = policy_loss

        # GTPO is critic-free — no value loss
        value_loss = 0.0

        # KL: monotonically growing (β_kl=0.0 means no penalty)
        kl_growth = 1.2 * (1 - math.exp(-2.5 * t))
        kl_noise = abs(noise(0.04))
        kl_curriculum = (
            0.08 * sigmoid((t - 0.11) * 50) +
            0.10 * sigmoid((t - 0.33) * 50) +
            0.06 * sigmoid((t - 0.67) * 50)
        )
        kl_divergence = max(0.0, min(1.35, kl_growth + kl_curriculum + kl_noise))

        # Gradient norm: LoRA scale (rank-64, much lower than full-FT)
        grad_base = 0.025 + 0.035 * math.exp(-2 * t)
        grad_noise = abs(noise(0.008))
        grad_spike = 0.0
        if rng.random() < 0.03:
            grad_spike = rng.uniform(0.03, 0.12)
        gradient_norm = max(0.003, min(1.0, grad_base + grad_noise + grad_spike))

        # Clip fraction: emerges from policy divergence (correlated with KL via shared time dependence)
        clip_growth = 0.12 * (1 - math.exp(-4 * t))
        clip_noise = noise(0.015)
        clip_fraction = max(0.0, min(0.25, clip_growth + clip_noise))

        advantage_mean = noise(0.08)

        # System metrics
        gpu_memory_used = max(60.0, min(79.5, 72.0 + noise(0.3)))
        samples_sec = max(2.0, 4.2 + noise(0.3))
        tokens_sec = max(3000, samples_sec * (1800 + noise(100)))
        cpu_percent = max(15.0, min(95.0, 45.0 + noise(3.0)))
        memory_percent = max(40.0, min(85.0, 60.0 + 6.0 * t + noise(1.5)))
        gpu_utilization = max(50.0, min(99.5, 87.0 + noise(4.0)))

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
            'cpu_percent': cpu_percent,
            'memory_percent': memory_percent,
            'gpu_utilization': gpu_utilization,
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
        if self.state in ('completed', 'cancelled'):
            return False
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
