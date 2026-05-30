from odoo import fields, models


class LokiDashboard2ConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    loki_dashboard_2_title = fields.Char(
        string="Clinical Dashboard Title",
        config_parameter="loki_dashboard_2.title",
        help="Title displayed on the public /loki2 clinical dashboard.",
    )

    loki_s3_bucket = fields.Char(
        string="S3 Bucket",
        config_parameter="loki_dashboard_2.s3_bucket",
        help="Bucket holding WSI tiles and clinical PDFs (e.g. production-grtlabs-tag).",
    )
    loki_s3_region = fields.Char(
        string="S3 Region",
        config_parameter="loki_dashboard_2.s3_region",
        default="us-east-1",
    )
    loki_s3_prefix = fields.Char(
        string="S3 Key Prefix",
        config_parameter="loki_dashboard_2.s3_prefix",
        default="loki_dashboard",
        help="Folder under the bucket. All keys become <prefix>/wsi/... or <prefix>/docs/...",
    )
    loki_s3_endpoint = fields.Char(
        string="S3 Endpoint URL",
        config_parameter="loki_dashboard_2.s3_endpoint",
        help="Leave blank for AWS S3. Set for R2/MinIO/etc.",
    )
    loki_s3_ttl_wsi = fields.Integer(
        string="WSI URL TTL (seconds)",
        config_parameter="loki_dashboard_2.s3_ttl_wsi",
        default=3600,
    )
    loki_s3_ttl_doc = fields.Integer(
        string="Document URL TTL (seconds)",
        config_parameter="loki_dashboard_2.s3_ttl_doc",
        default=300,
    )
