# -*- coding: utf-8 -*-

import json
import logging
import requests

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)

HF_TOKEN = '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'
HF_API_BASE = 'https://huggingface.co/api'


class RlGymController(http.Controller):

    @http.route('/rl_gym/models', type='json', auth='user')
    def get_models(self):
        models = request.env['rl.training.model'].search_read(
            [('active', '=', True)],
            ['name', 'technical_name', 'model_type', 'description',
             'parameter_count', 'architecture'],
            order='sequence, name'
        )
        return models

    @http.route('/rl_gym/datasets/search', type='json', auth='user')
    def search_datasets(self, query='', author='ethara'):
        headers = {'Authorization': f'Bearer {HF_TOKEN}'}
        params = {'author': author, 'search': query, 'limit': 50}
        try:
            resp = requests.get(
                f'{HF_API_BASE}/datasets',
                headers=headers, params=params, timeout=15
            )
            resp.raise_for_status()
            datasets = resp.json()
            return [{
                'id': ds.get('id', ''),
                'name': ds.get('id', '').split('/')[-1],
                'description': ds.get('description', ''),
                'downloads': ds.get('downloads', 0),
                'likes': ds.get('likes', 0),
                'tags': ds.get('tags', []),
                'last_modified': ds.get('lastModified', ''),
            } for ds in datasets]
        except requests.RequestException as e:
            _logger.warning('HuggingFace API error: %s', e)
            return {'error': str(e)}

    @http.route('/rl_gym/datasets/info', type='json', auth='user')
    def get_dataset_info(self, repo_id):
        headers = {'Authorization': f'Bearer {HF_TOKEN}'}
        try:
            resp = requests.get(
                f'{HF_API_BASE}/datasets/{repo_id}',
                headers=headers, timeout=15
            )
            resp.raise_for_status()
            info = resp.json()

            preview = self._fetch_dataset_preview(repo_id, headers)

            return {
                'id': info.get('id'),
                'description': info.get('description', ''),
                'citation': info.get('citation', ''),
                'card_data': info.get('cardData', {}),
                'tags': info.get('tags', []),
                'downloads': info.get('downloads', 0),
                'splits': info.get('splits', {}),
                'preview': preview,
            }
        except requests.RequestException as e:
            _logger.warning('HuggingFace dataset info error: %s', e)
            return {'error': str(e)}

    def _fetch_dataset_preview(self, repo_id, headers):
        try:
            resp = requests.get(
                f'{HF_API_BASE}/datasets/{repo_id}/parquet',
                headers=headers, timeout=10
            )
            if resp.status_code == 200:
                parquet_info = resp.json()
                return {'parquet_files': parquet_info}
        except requests.RequestException:
            pass

        try:
            resp = requests.get(
                f'https://datasets-server.huggingface.co/first-rows?dataset={repo_id}&config=default&split=train',
                headers=headers, timeout=10
            )
            if resp.status_code == 200:
                data = resp.json()
                return {
                    'columns': [f['column']['name'] for f in data.get('features', [])],
                    'rows': data.get('rows', [])[:10],
                }
        except requests.RequestException:
            pass

        return None

    @http.route('/rl_gym/datasets/save', type='json', auth='user')
    def save_dataset(self, values):
        dataset = request.env['rl.training.dataset'].create(values)
        return {'id': dataset.id, 'name': dataset.name}

    @http.route('/rl_gym/training/create_job', type='json', auth='user')
    def create_training_job(self, values, model_id=None, config_id=None):
        vals = dict(values or {})
        if model_id:
            vals['model_id'] = int(model_id)
        if config_id:
            vals['config_id'] = int(config_id)
        vals.setdefault('total_steps', 450)
        job = request.env['rl.training.job'].create(vals)
        return {'id': job.id, 'name': job.name, 'state': job.state}

    @http.route('/rl_gym/training/start', type='json', auth='user')
    def start_training(self, job_id):
        job = request.env['rl.training.job'].browse(int(job_id))
        if not job.exists():
            return {'error': 'Job not found'}
        job.action_start_training()
        return {'state': job.state, 'started_at': str(job.started_at)}

    @http.route('/rl_gym/training/step', type='json', auth='user')
    def training_step(self, job_id):
        job = request.env['rl.training.job'].browse(int(job_id))
        if not job.exists():
            return {'error': 'Job not found'}
        job.action_simulate_step()
        return {
            'state': job.state,
            'current_step': job.current_step,
            'total_steps': job.total_steps,
            'progress': job.progress,
            'current_loss': job.current_loss,
            'current_reward': job.current_reward,
            'best_reward': job.best_reward,
        }

    @http.route('/rl_gym/training/metrics', type='json', auth='user')
    def get_training_metrics(self, job_id, since_step=0):
        metrics = request.env['rl.training.metric'].search_read(
            [('job_id', '=', int(job_id)), ('step', '>', int(since_step))],
            ['step', 'loss', 'reward', 'gradient_norm', 'learning_rate',
             'entropy', 'kl_divergence', 'tokens_per_second'],
            order='step asc',
            limit=100
        )
        return metrics

    @http.route('/rl_gym/training/status', type='json', auth='user')
    def get_training_status(self, job_id):
        job = request.env['rl.training.job'].browse(int(job_id))
        if not job.exists():
            return {'error': 'Job not found'}
        return {
            'id': job.id,
            'state': job.state,
            'current_step': job.current_step,
            'total_steps': job.total_steps,
            'progress': job.progress,
            'current_loss': job.current_loss,
            'current_reward': job.current_reward,
            'best_reward': job.best_reward,
            'started_at': str(job.started_at) if job.started_at else None,
            'completed_at': str(job.completed_at) if job.completed_at else None,
        }

    @http.route('/rl_gym/weights/upload', type='json', auth='user')
    def upload_weights(self, weight_id):
        weight = request.env['rl.training.weight'].browse(int(weight_id))
        if not weight.exists():
            return {'error': 'Weight not found'}

        config = weight.get_upload_config()
        if not config.get('aws_bucket'):
            return {'error': 'S3 not configured. Set system parameters: rl_gym.aws_*'}

        weight.write({'upload_state': 'uploading', 'upload_progress': 0})

        try:
            import boto3
            s3 = boto3.client(
                's3',
                aws_access_key_id=config['aws_access_key'],
                aws_secret_access_key=config['aws_secret_key'],
                region_name=config['aws_region'],
            )

            s3_key = f"models/{weight.job_id.name}/{weight.name}"
            s3.upload_file(
                weight.file_path,
                config['aws_bucket'],
                s3_key,
            )

            s3_url = f"https://{config['aws_bucket']}.s3.{config['aws_region']}.amazonaws.com/{s3_key}"
            weight.write({
                'upload_state': 'uploaded',
                'upload_progress': 100,
                's3_bucket': config['aws_bucket'],
                's3_key': s3_key,
                's3_url': s3_url,
            })
            return {'state': 'uploaded', 's3_url': s3_url}

        except Exception as e:
            weight.write({
                'upload_state': 'failed',
                'upload_error': str(e),
            })
            return {'error': str(e)}

    @http.route('/rl_gym/weights/create', type='json', auth='user')
    def create_weight(self, values):
        weight = request.env['rl.training.weight'].create(values)
        return {'id': weight.id, 'name': weight.name}

    @http.route('/rl_gym/inference/run', type='json', auth='user')
    def run_inference(self, job_id, prompt, max_tokens=256, temperature=0.7):
        job = request.env['rl.training.job'].browse(int(job_id))
        if not job.exists():
            return {'error': 'Job not found'}
        if job.state != 'completed':
            return {'error': 'Training not completed'}

        simulated_response = self._simulate_inference(prompt, max_tokens, temperature)
        return {
            'prompt': prompt,
            'response': simulated_response,
            'tokens_used': len(simulated_response.split()),
            'model': job.model_id.name,
            'job': job.name,
        }

    def _simulate_inference(self, prompt, max_tokens, temperature):
        responses = [
            "Based on the training data analysis, the optimal approach involves "
            "implementing a multi-step verification process that validates each "
            "intermediate result before proceeding to the final computation.",
            "The model identifies three key patterns in the input: structural "
            "consistency, semantic coherence, and logical flow. Each pattern "
            "contributes to the overall confidence score of the prediction.",
            "After analyzing the provided context, the recommended solution "
            "leverages curriculum-based learning with progressive difficulty "
            "scaling to achieve robust generalization across domains.",
        ]
        import hashlib
        idx = int(hashlib.md5(prompt.encode()).hexdigest(), 16) % len(responses)
        return responses[idx]

    @http.route('/rl_gym/config/save', type='json', auth='user')
    def save_config(self, values, model_id=None, job_name=''):
        # Map frontend field names to model field names
        field_map = {
            'gradient_accumulation': 'gradient_accumulation_steps',
            'curriculum_stages': 'curriculum_phases',
            'gpu_count': 'num_gpus',
        }
        mapped = {}
        config_fields = request.env['rl.training.config']._fields
        for k, v in (values or {}).items():
            mapped_key = field_map.get(k, k)
            if mapped_key in config_fields:
                mapped[mapped_key] = v

        if model_id:
            mapped['model_id'] = int(model_id)
        mapped.setdefault('name', job_name or 'default-config')

        config = request.env['rl.training.config'].create(mapped)
        return {'id': config.id, 'name': config.name}

    @http.route('/rl_gym/config/defaults', type='json', auth='user')
    def get_config_defaults(self, model_id=None):
        domain = []
        if model_id:
            domain = [('model_id', '=', int(model_id))]
        configs = request.env['rl.training.config'].search_read(
            domain,
            order='name',
            limit=20
        )
        return configs
