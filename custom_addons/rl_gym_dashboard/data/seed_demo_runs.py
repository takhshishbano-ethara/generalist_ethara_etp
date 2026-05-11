# -*- coding: utf-8 -*-

import math
import random
import logging
from datetime import timedelta

from odoo import fields as odoo_fields

_logger = logging.getLogger(__name__)


def _generate_metric(step, total_steps, policy_type, base_seed):
    t = step / total_steps
    rng = random.Random(base_seed + step * 7919)

    def noise(sigma=1.0):
        return rng.gauss(0, sigma)

    def sigmoid(x):
        return 1.0 / (1.0 + math.exp(-max(-500, min(500, x))))

    lr_max = 4e-5
    warmup_frac = 10.0 / total_steps
    if t < warmup_frac:
        learning_rate = lr_max * (t / warmup_frac)
    else:
        cos_progress = (t - warmup_frac) / (1.0 - warmup_frac)
        cos_decay = 0.5 * (1 + math.cos(math.pi * cos_progress))
        learning_rate = lr_max * max(cos_decay, 0.1)

    reward_base = 0.15 + 0.50 * sigmoid((t - 0.40) * 8)
    curriculum_dip1 = -0.03 * math.exp(-((t - 0.11) ** 2) / 0.001)
    curriculum_dip2 = -0.04 * math.exp(-((t - 0.33) ** 2) / 0.001)
    curriculum_dip3 = -0.03 * math.exp(-((t - 0.67) ** 2) / 0.001)
    reward_noise = noise(0.04)
    reward = max(0.0, min(1.0, reward_base + curriculum_dip1 + curriculum_dip2 + curriculum_dip3 + reward_noise))

    reward_mean = reward + noise(0.01)
    reward_std = max(0.08, 0.35 - 0.15 * t + noise(0.03))

    entropy_start = 0.22
    entropy_end = 0.06
    entropy_decay = entropy_start - (entropy_start - entropy_end) * (1 - math.exp(-3 * t))
    entropy = max(0.03, entropy_decay + noise(0.012))

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

    value_loss = 0.0

    kl_growth = 1.2 * (1 - math.exp(-2.5 * t))
    kl_noise = abs(noise(0.04))
    kl_curriculum = (
        0.08 * sigmoid((t - 0.11) * 50) +
        0.10 * sigmoid((t - 0.33) * 50) +
        0.06 * sigmoid((t - 0.67) * 50)
    )
    kl_divergence = max(0.0, min(1.35, kl_growth + kl_curriculum + kl_noise))

    grad_base = 0.025 + 0.035 * math.exp(-2 * t)
    grad_noise = abs(noise(0.008))
    grad_spike = rng.uniform(0.03, 0.12) if rng.random() < 0.03 else 0.0
    gradient_norm = max(0.003, min(1.0, grad_base + grad_noise + grad_spike))

    clip_growth = 0.12 * (1 - math.exp(-4 * t))
    clip_noise = noise(0.015)
    clip_fraction = max(0.0, min(0.25, clip_growth + clip_noise))

    advantage_mean = noise(0.08)

    gpu_memory_used = max(60.0, min(79.5, 72.0 + noise(0.3)))
    samples_sec = max(2.0, 4.2 + noise(0.3))
    tokens_sec = max(3000, samples_sec * (1800 + noise(100)))
    cpu_percent = max(15.0, min(95.0, 45.0 + noise(3.0)))
    memory_percent = max(40.0, min(85.0, 60.0 + 6.0 * t + noise(1.5)))
    gpu_utilization = max(50.0, min(99.5, 87.0 + noise(4.0)))

    return {
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
    }


def seed_demo_data(env):
    Job = env['rl.training.job']
    Config = env['rl.training.config']
    Metric = env['rl.training.metric']

    existing = Job.search([('name', '=', 'GTPO-MILO-Bench-Demo')], limit=1)
    if existing:
        _logger.info("Demo runs already exist, skipping seed.")
        return

    try:
        model_nemotron = env.ref('rl_gym_dashboard.model_nemotron_30b')
    except Exception:
        _logger.warning("Model nemotron_30b not found, skipping demo seed.")
        return

    try:
        model_llama = env.ref('rl_gym_dashboard.model_llama_8b')
    except Exception:
        _logger.warning("Model llama_8b not found, skipping demo seed.")
        return

    runs = [
        {
            'name': 'GTPO-MILO-Bench-Demo',
            'policy_type': 'gtpo',
            'model_id': model_nemotron.id,
            'total_steps': 450,
            'seed': 42,
        },
        {
            'name': 'GTPO-Valkyrie-Demo',
            'policy_type': 'gtpo',
            'model_id': model_llama.id,
            'total_steps': 300,
            'seed': 137,
        },
    ]

    now = odoo_fields.Datetime.now()

    for run_def in runs:
        config = Config.create({
            'name': f"{run_def['name']}-Config",
            'model_id': run_def['model_id'],
            'policy_type': run_def['policy_type'],
            'max_steps': run_def['total_steps'],
        })

        total_steps = run_def['total_steps']
        elapsed_seconds = total_steps * 8.5
        started_at = now - timedelta(seconds=elapsed_seconds + 60)
        completed_at = started_at + timedelta(seconds=elapsed_seconds)

        metric_vals = []
        best_reward = -999.0
        final_loss = 0.0
        final_reward = 0.0

        for step in range(1, total_steps + 1):
            m = _generate_metric(step, total_steps, run_def['policy_type'], run_def['seed'])
            if m['reward'] > best_reward:
                best_reward = m['reward']
            final_loss = m['loss']
            final_reward = m['reward']
            metric_vals.append(m)

        job = Job.create({
            'name': run_def['name'],
            'model_id': run_def['model_id'],
            'config_id': config.id,
            'state': 'completed',
            'current_step': total_steps,
            'total_steps': total_steps,
            'current_loss': final_loss,
            'current_reward': final_reward,
            'best_reward': best_reward,
            'started_at': started_at,
            'completed_at': completed_at,
            'elapsed_time': elapsed_seconds,
        })

        batch = []
        for m in metric_vals:
            m['job_id'] = job.id
            batch.append(m)

        Metric.create(batch)
        _logger.info("Created demo run '%s' with %d metrics.", run_def['name'], len(batch))

    _logger.info("Demo seed complete: 2 runs created.")
