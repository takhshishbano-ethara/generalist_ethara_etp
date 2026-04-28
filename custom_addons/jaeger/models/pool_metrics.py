from odoo import fields, models


class JaegerPoolMetrics(models.Model):
    _name = "jaeger.pool.metrics"
    _description = "Token Pool Metrics Snapshot"
    _order = "timestamp desc"

    timestamp = fields.Datetime(default=fields.Datetime.now, index=True)
    total_tokens = fields.Integer()
    active_count = fields.Integer()
    exhausted_count = fields.Integer()
    expired_count = fields.Integer()
    quarantined_count = fields.Integer()
    leased_count = fields.Integer()
    total_remaining = fields.Integer()
    avg_remaining = fields.Float(digits=(10, 1))
    pool_utilization = fields.Float(digits=(5, 2))
