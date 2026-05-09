# -*- coding: utf-8 -*-

from odoo import models, fields, api


class RlTrainingWeight(models.Model):
    _name = 'rl.training.weight'
    _description = 'RL Training Weight'
    _order = 'create_date desc'

    name = fields.Char(string='Weight Name', required=True)
    job_id = fields.Many2one('rl.training.job', string='Training Job',
                             required=True, ondelete='cascade')

    # File info
    file_path = fields.Char(string='Local File Path')
    file_size = fields.Float(string='File Size (MB)', digits=(10, 2))
    file_format = fields.Selection([
        ('safetensors', 'SafeTensors'),
        ('pytorch', 'PyTorch (.bin)'),
        ('gguf', 'GGUF'),
        ('onnx', 'ONNX'),
    ], string='Format', default='safetensors')

    # S3 Upload
    s3_bucket = fields.Char(string='S3 Bucket')
    s3_key = fields.Char(string='S3 Key')
    s3_url = fields.Char(string='S3 URL')
    upload_state = fields.Selection([
        ('pending', 'Pending'),
        ('uploading', 'Uploading'),
        ('uploaded', 'Uploaded'),
        ('failed', 'Failed'),
    ], string='Upload Status', default='pending')
    upload_progress = fields.Float(string='Upload Progress (%)', digits=(5, 1))
    upload_error = fields.Text(string='Upload Error')

    # Metadata
    step = fields.Integer(string='Training Step')
    metrics_snapshot = fields.Text(string='Metrics at Save (JSON)')
    is_best = fields.Boolean(string='Is Best Checkpoint', default=False)
    description = fields.Text(string='Notes')

    @api.model
    def get_upload_config(self):
        """Get S3 configuration from system parameters."""
        ICP = self.env['ir.config_parameter'].sudo()
        return {
            'aws_access_key': ICP.get_param('rl_gym.aws_access_key', ''),
            'aws_secret_key': ICP.get_param('rl_gym.aws_secret_key', ''),
            'aws_region': ICP.get_param('rl_gym.aws_region', 'us-east-1'),
            'aws_bucket': ICP.get_param('rl_gym.aws_bucket', ''),
        }
