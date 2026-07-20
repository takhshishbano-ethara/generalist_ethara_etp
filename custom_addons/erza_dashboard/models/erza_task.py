from odoo import api, fields, models


DIFFICULTY_SELECTION = [
    ("trivial", "Trivial"),
    ("easy", "Easy"),
    ("medium", "Medium"),
    ("hard", "Hard"),
    ("expert", "Expert"),
]

LANGUAGE_SELECTION = [
    ("go", "Go"),
    ("python", "Python"),
    ("typescript", "TypeScript"),
    ("javascript", "JavaScript"),
    ("rust", "Rust"),
    ("java", "Java"),
    ("c", "C"),
    ("cpp", "C++"),
]


class ErzaTask(models.Model):
    _name = "erza.task"
    _description = "Erza Task"
    _order = "difficulty_seq, uuid"
    _rec_name = "display_name"

    uuid = fields.Char(required=True, index=True, help="UUIDv5 identifying this task.")
    display_name = fields.Char(compute="_compute_display_name", store=True)
    task_slug = fields.Char(help="Human-readable task slug, e.g. multi-swe-bench__gogorm__gorm4888.")
    codebase = fields.Char(help="Source codebase / environment, e.g. go-gorm/gorm.")
    category = fields.Char(default="software-development")
    skill = fields.Char(help="Curated Agent Skill package injected in the with-Skills arm, e.g. debug-triage.")
    difficulty = fields.Selection(DIFFICULTY_SELECTION, required=True, default="medium")
    difficulty_seq = fields.Integer(compute="_compute_difficulty_seq", store=True)
    language = fields.Selection(LANGUAGE_SELECTION, required=True, default="python")
    src_hunks = fields.Integer(help="Number of independent target edit sites the verifier checks.")
    keywords = fields.Char(help="Comma-separated keyword tags copied from task.toml.")
    instruction_url = fields.Char(help="Absolute URL to the instruction.md on GitHub.")
    dataset_url = fields.Char(help="Absolute URL to the dataset/<uuid> directory on GitHub.")
    trajectories_url = fields.Char(help="Absolute URL to the trajectories/<uuid> directory on GitHub.")

    env_cpus = fields.Integer(default=8)
    env_memory_mb = fields.Integer(default=16384)
    env_storage_mb = fields.Integer(default=15360)
    env_gpus = fields.Integer(default=0)
    verifier_timeout_sec = fields.Float(default=7200.0)
    agent_timeout_sec = fields.Float(default=14400.0)

    run_ids = fields.One2many("erza.run", "task_id")
    run_count = fields.Integer(compute="_compute_stats", store=True)
    pass_count = fields.Integer(compute="_compute_stats", store=True)
    mean_score = fields.Float(string="With-Skills Mean Score (%)", compute="_compute_stats", store=True, aggregator="avg")
    pass_rate = fields.Float(string="With-Skills Pass Rate (%)", compute="_compute_stats", store=True, aggregator="avg")
    no_skill_pass_rate = fields.Float(string="No-Skills Pass Rate (%)", compute="_compute_stats", store=True, aggregator="avg")
    mean_delta = fields.Float(string="Mean Δ (pp)", compute="_compute_stats", store=True, aggregator="avg")

    _sql_constraints = [
        ("erza_task_uuid_uniq", "unique(uuid)", "Task UUID must be unique."),
    ]

    @api.depends("uuid", "task_slug")
    def _compute_display_name(self):
        for rec in self:
            rec.display_name = rec.task_slug or rec.uuid or "New Task"

    @api.depends("difficulty")
    def _compute_difficulty_seq(self):
        order = {"trivial": 1, "easy": 2, "medium": 3, "hard": 4, "expert": 5}
        for rec in self:
            rec.difficulty_seq = order.get(rec.difficulty, 99)

    @api.depends("run_ids", "run_ids.score", "run_ids.score_binary",
                 "run_ids.no_skill_binary", "run_ids.delta")
    def _compute_stats(self):
        for rec in self:
            runs = rec.run_ids
            total = len(runs)
            passed = sum(1 for r in runs if r.score_binary)
            ns_passed = sum(1 for r in runs if r.no_skill_binary)
            rec.run_count = total
            rec.pass_count = passed
            rec.mean_score = (sum(r.score for r in runs) / total * 100.0) if total else 0.0
            rec.pass_rate = (passed / total * 100.0) if total else 0.0
            rec.no_skill_pass_rate = (ns_passed / total * 100.0) if total else 0.0
            # Δ stored as a [0,1] score gap; express as percentage points for display.
            rec.mean_delta = (sum(r.delta for r in runs) / total * 100.0) if total else 0.0
