from odoo import fields, models


class JaegerConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    # ── GitHub ────────────────────────────────────────────────────────────
    jaeger_github_tokens = fields.Char(
        string="GitHub Tokens",
        config_parameter="jaeger.github_tokens",
        help="Comma-separated GitHub tokens for API access. More tokens = higher throughput.",
    )
    jaeger_output_dir = fields.Char(
        string="Output Directory",
        config_parameter="jaeger.output_dir",
        default="/tmp/jaeger_data",
    )
    jaeger_retry_attempts = fields.Integer(
        string="Retry Attempts",
        config_parameter="jaeger.retry_attempts",
        default=3,
    )
    jaeger_delay_on_error = fields.Integer(
        string="Delay on Error (s)",
        config_parameter="jaeger.delay_on_error",
        default=300,
    )
    jaeger_max_active_tasks = fields.Integer(
        string="Max Active Tasks",
        config_parameter="jaeger.max_active_tasks",
        default=3,
    )

    # ── K8s Dispatch ──────────────────────────────────────────────────────
    jaeger_dispatch_mode = fields.Selection(
        [("local", "Local (Background Thread)"), ("k8s", "Kubernetes (EKS)")],
        string="Pipeline Dispatch Mode",
        config_parameter="jaeger.dispatch_mode",
        default="local",
    )
    jaeger_k8s_job_image = fields.Char(
        string="K8s Job Docker Image",
        config_parameter="jaeger.k8s_job_image",
        help="e.g. 426628337772.dkr.ecr.ap-south-1.amazonaws.com/ethara-prod-backend:latest",
    )

    # ── S3 Storage ────────────────────────────────────────────────────────
    jaeger_s3_bucket = fields.Char(
        string="S3 Bucket",
        config_parameter="jaeger.s3_bucket",
    )
    jaeger_s3_region = fields.Char(
        string="S3 Region",
        config_parameter="jaeger.s3_region",
        default="ap-south-1",
    )
    jaeger_s3_prefix = fields.Char(
        string="S3 Key Prefix",
        config_parameter="jaeger.s3_prefix",
        default="jaeger/phase1",
    )

    # ── Docker / ECR ──────────────────────────────────────────────────────
    jaeger_docker_workdir = fields.Char(
        string="Docker Workdir",
        config_parameter="jaeger.docker_workdir",
        default="/tmp/jaeger_docker",
    )
    jaeger_docker_build_mode = fields.Selection(
        [("local", "Local Docker"), ("kaiju", "Kaiju (K8s)")],
        string="Docker Build Mode",
        config_parameter="jaeger.docker_build_mode",
        default="local",
    )
    jaeger_max_build_workers = fields.Integer(
        string="Max Build Workers",
        config_parameter="jaeger.max_build_workers",
        default=8,
    )
    jaeger_max_run_workers = fields.Integer(
        string="Max Run Workers",
        config_parameter="jaeger.max_run_workers",
        default=8,
    )
    jaeger_docker_platform = fields.Char(
        string="Docker Platform",
        config_parameter="jaeger.docker_platform",
        default="linux/amd64",
    )
    jaeger_ecr_prefix = fields.Char(
        string="ECR Prefix",
        config_parameter="jaeger.ecr_prefix",
    )
    jaeger_human_mode = fields.Boolean(
        string="Human Mode (Sequential Tests)",
        config_parameter="jaeger.human_mode",
        default=True,
    )
    jaeger_agent_timeout = fields.Integer(
        string="Agent Timeout (s)",
        config_parameter="jaeger.agent_timeout",
        default=1800,
    )

    # ── EKS ───────────────────────────────────────────────────────────────
    jaeger_eks_cluster = fields.Char(
        string="EKS Cluster",
        config_parameter="jaeger.eks_cluster",
    )
    jaeger_eks_namespace = fields.Char(
        string="EKS Namespace",
        config_parameter="jaeger.eks_namespace",
        default="jaeger",
    )

    # ── LLM / Trajectory ─────────────────────────────────────────────────
    jaeger_default_model = fields.Char(
        string="Default LLM Model",
        config_parameter="jaeger.default_model",
        default="claude",
    )
    jaeger_default_k = fields.Integer(
        string="Default K (pass@k)",
        config_parameter="jaeger.default_k",
        default=8,
    )
    jaeger_default_workers = fields.Integer(
        string="Default Workers",
        config_parameter="jaeger.default_workers",
        default=1,
    )
    jaeger_conversation_timeout = fields.Integer(
        string="Conversation Timeout (s)",
        config_parameter="jaeger.conversation_timeout",
        default=3600,
    )
    jaeger_temperature = fields.Float(
        string="Temperature",
        config_parameter="jaeger.temperature",
        default=1.0,
    )
    jaeger_llm_config_template = fields.Char(
        string="LLM Config Template (JSON)",
        config_parameter="jaeger.llm_config_template",
    )

    # ── RabbitMQ (deprecated — kept for DB view compatibility) ─────────────
    jaeger_rabbitmq_host = fields.Char(
        string="RabbitMQ Host",
        config_parameter="jaeger.rabbitmq_host",
        default="localhost",
    )
    jaeger_rabbitmq_port = fields.Integer(
        string="RabbitMQ Port",
        config_parameter="jaeger.rabbitmq_port",
        default=5672,
    )
    jaeger_rabbitmq_user = fields.Char(
        string="RabbitMQ User",
        config_parameter="jaeger.rabbitmq_user",
        default="guest",
    )
    jaeger_rabbitmq_password = fields.Char(
        string="RabbitMQ Password",
        config_parameter="jaeger.rabbitmq_password",
    )
    jaeger_rabbitmq_vhost = fields.Char(
        string="RabbitMQ VHost",
        config_parameter="jaeger.rabbitmq_vhost",
        default="/",
    )
