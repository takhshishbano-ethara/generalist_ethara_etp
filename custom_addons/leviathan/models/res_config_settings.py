from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    leviathan_extraction_service_url = fields.Char(
        string="Extraction Service URL",
        config_parameter="leviathan.extraction_service_url",
        help="Base URL of the external Leviathan extraction microservice",
    )
    leviathan_extraction_access_key_id = fields.Char(
        string="Extraction AWS Access Key ID",
        config_parameter="leviathan.extraction_access_key_id",
    )
    leviathan_extraction_secret_access_key = fields.Char(
        string="Extraction AWS Secret Access Key",
        config_parameter="leviathan.extraction_secret_access_key",
    )
    leviathan_bedrock_inference_arn = fields.Char(
        string="Bedrock Inference ARN",
        config_parameter="leviathan.bedrock_inference_arn",
        help="e.g., arn:aws:bedrock:us-east-1:123456:inference-profile/...",
    )
    leviathan_bedrock_region = fields.Char(
        string="Bedrock Region",
        config_parameter="leviathan.bedrock_region",
        default="us-east-1",
    )
    leviathan_bedrock_access_key_id = fields.Char(
        string="Bedrock Access Key ID",
        config_parameter="leviathan.bedrock_access_key_id",
        help="Leave empty to use EKS pod IAM role (IRSA)",
    )
    leviathan_bedrock_secret_access_key = fields.Char(
        string="Bedrock Secret Access Key",
        config_parameter="leviathan.bedrock_secret_access_key",
    )
    leviathan_max_llm_attempts = fields.Integer(
        string="Max LLM Attempts",
        config_parameter="leviathan.max_llm_attempts",
    )
    leviathan_s3_bucket = fields.Char(
        string="S3 Bucket Name",
        config_parameter="leviathan.s3_bucket",
    )
    leviathan_s3_access_key_id = fields.Char(
        string="S3 Access Key ID",
        config_parameter="leviathan.s3_access_key_id",
    )
    leviathan_s3_secret_access_key = fields.Char(
        string="S3 Secret Access Key",
        config_parameter="leviathan.s3_secret_access_key",
    )
    leviathan_s3_region = fields.Char(
        string="S3 Region",
        config_parameter="leviathan.s3_region",
    )
    leviathan_s3_folder = fields.Char(
        string="S3 Folder",
        config_parameter="leviathan.s3_folder",
        default="leviathan",
    )
    leviathan_s3_cdn_url = fields.Char(
        string="S3 CDN URL",
        config_parameter="leviathan.s3_cdn_url",
        help="e.g., https://cdn.example.com",
    )
