# -*- coding: utf-8 -*-

import json
import logging
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
        """Simulate a single training step and record metrics."""
        self.ensure_one()
        if self.state != 'training':
            return False

        step = self.current_step + 1
        total = self.total_steps

        # Simulated metrics with realistic curves
        base_loss = 2.5 * (0.95 ** (step / 10)) + 0.1
        loss = base_loss + random.gauss(0, 0.05)

        base_reward = 0.2 + 0.6 * (1 - 0.97 ** step)
        reward = base_reward + random.gauss(0, 0.03)

        grad_norm = 1.5 * (0.98 ** (step / 5)) + random.gauss(0, 0.1)
        learning_rate = 3e-6 * min(1.0, step / 50)  # warmup

        # Create metric record
        self.env['rl.training.metric'].create({
            'job_id': self.id,
            'step': step,
            'loss': max(0.01, loss),
            'reward': reward,
            'gradient_norm': max(0.01, abs(grad_norm)),
            'learning_rate': learning_rate,
            'entropy': max(0, 1.0 - step / total + random.gauss(0, 0.02)),
            'kl_divergence': abs(random.gauss(0.01, 0.005)),
            'tokens_per_second': max(100, 2500 + random.gauss(0, 400)),
        })

        best = max(self.best_reward, reward)
        vals = {
            'current_step': step,
            'current_loss': max(0.01, loss),
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
