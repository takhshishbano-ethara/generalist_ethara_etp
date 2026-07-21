import json
import logging
import os

from odoo import api, fields, models

_logger = logging.getLogger(__name__)

RUN_STATUS_SELECTION = [
    ("scored", "Scored"),
    ("invalid", "Invalid"),  # source status for legitimate model-side run failures
    ("failed", "Failed"),
    ("error", "Error"),
    ("unknown", "Unknown"),
]


class ErzaRun(models.Model):
    _name = "erza.bench.run"
    _description = "Erza Model Run"
    _order = "task_id, model_id, run_number"
    _rec_name = "display_name"

    task_id = fields.Many2one("erza.bench.task", required=True, ondelete="cascade", index=True)
    model_id = fields.Many2one("erza.bench.model", required=True, ondelete="cascade", index=True)
    run_number = fields.Integer(required=True, default=1)
    display_name = fields.Char(compute="_compute_display_name", store=True)

    difficulty = fields.Selection(related="task_id.difficulty", store=True, readonly=True)
    language = fields.Selection(related="task_id.language", store=True, readonly=True)
    codebase = fields.Char(related="task_id.codebase", store=True, readonly=True)

    # Paired evaluation (Erza core): the same task is run twice on the same
    # model / harness / container - once WITH the curated Skill, once WITHOUT.
    # `score`/`score_binary` are the with-Skills arm; `no_skill_*` the matched
    # control; `delta` (Δ) is the reported efficacy gap.
    score = fields.Float(help="With-Skills continuous score in [0, 1] (verifier recall x regression_factor).")
    score_binary = fields.Boolean(help="True when the with-Skills run fully solved the task (score == 1.0).")
    no_skill_score = fields.Float(help="No-Skills continuous score in [0, 1] for the matched control run.")
    no_skill_binary = fields.Boolean(help="True when the no-Skills control run fully solved the task.")
    delta = fields.Float(
        string="Δ efficacy",
        compute="_compute_delta",
        store=True,
        help="Skill efficacy = with-Skills score − no-Skills score. Kept even when zero or negative.",
    )
    recall = fields.Float()
    regression_factor = fields.Float(default=1.0)

    targets_total = fields.Integer()
    targets_hit = fields.Integer()
    preserve_set_total = fields.Integer()
    broken_preserve_count = fields.Integer()

    cost_usd = fields.Float(digits=(10, 4))
    tokens_input = fields.Integer()
    tokens_cache = fields.Integer()
    tokens_output = fields.Integer()
    n_episodes = fields.Integer()
    duration_sec = fields.Integer()

    status = fields.Selection(RUN_STATUS_SELECTION, default="scored")
    started_at = fields.Datetime()
    finished_at = fields.Datetime()

    result_url = fields.Char(help="Absolute URL to this run's result.json on GitHub.")
    trajectory_url = fields.Char(help="Absolute URL to this run's trajectory folder on GitHub.")

    _sql_constraints = [
        (
            "erza_run_task_model_run_uniq",
            "unique(task_id, model_id, run_number)",
            "Only one entry per (task, model, run_number).",
        ),
    ]

    @api.depends("task_id.display_name", "model_id.display_name", "run_number")
    def _compute_display_name(self):
        for rec in self:
            task = rec.task_id.display_name or "?"
            model = rec.model_id.display_name or "?"
            rec.display_name = f"{task} · {model} · run_{rec.run_number}"

    @api.depends("score", "no_skill_score")
    def _compute_delta(self):
        for rec in self:
            rec.delta = (rec.score or 0.0) - (rec.no_skill_score or 0.0)

    @api.model
    def seed_from_bundle(self):
        payload = self._load_bundle_payload()
        if not payload:
            _logger.warning("erza: bundle payload missing; skipping seed")
            return False

        Model = self.env["erza.bench.model"]
        Task = self.env["erza.bench.task"]

        model_by_key = {}
        for entry in payload.get("models", []):
            rec = Model.search([("key", "=", entry["key"])], limit=1)
            values = {
                "key": entry["key"],
                "name": entry.get("display_name") or entry.get("name") or entry["key"],
                "provider": entry.get("provider"),
                "provider_route": entry.get("provider_route"),
                "color": entry.get("color") or "#845EF7",
                "sequence": entry.get("sequence", 10),
            }
            if rec:
                rec.write(values)
            else:
                rec = Model.create(values)
            model_by_key[entry["key"]] = rec

        task_by_uuid = {}
        for entry in payload.get("tasks", []):
            rec = Task.search([("uuid", "=", entry["uuid"])], limit=1)
            values = {
                "uuid": entry["uuid"],
                "task_slug": entry.get("task_slug"),
                "codebase": entry.get("codebase"),
                "category": entry.get("category") or "software-development",
                "skill": entry.get("skill"),
                "difficulty": entry.get("difficulty") or "medium",
                "language": entry.get("language") or "python",
                "src_hunks": entry.get("src_hunks") or 0,
                "keywords": entry.get("keywords"),
                "instruction_url": entry.get("instruction_url"),
                "dataset_url": entry.get("dataset_url"),
                "trajectories_url": entry.get("trajectories_url"),
                "env_cpus": entry.get("env_cpus") or 8,
                "env_memory_mb": entry.get("env_memory_mb") or 16384,
                "env_storage_mb": entry.get("env_storage_mb") or 15360,
                "env_gpus": entry.get("env_gpus") or 0,
                "verifier_timeout_sec": entry.get("verifier_timeout_sec") or 7200.0,
                "agent_timeout_sec": entry.get("agent_timeout_sec") or 14400.0,
            }
            if rec:
                rec.write(values)
            else:
                rec = Task.create(values)
            task_by_uuid[entry["uuid"]] = rec

        created = updated = 0
        for entry in payload.get("runs", []):
            task = task_by_uuid.get(entry["task_uuid"])
            model = model_by_key.get(entry["model_key"])
            if not task or not model:
                continue
            values = {
                "task_id": task.id,
                "model_id": model.id,
                "run_number": entry.get("run_number", 1),
                "score": entry.get("score") or 0.0,
                "score_binary": bool(entry.get("score_binary")),
                "no_skill_score": entry.get("no_skill_score") or 0.0,
                "no_skill_binary": bool(entry.get("no_skill_binary")),
                "recall": entry.get("recall") or 0.0,
                "regression_factor": (
                    entry.get("regression_factor")
                    if entry.get("regression_factor") is not None
                    else 1.0
                ),  # keep a legitimate 0.0 (all preserve-tests broken); `or 1.0` would corrupt it
                "targets_total": entry.get("targets_total") or 0,
                "targets_hit": entry.get("targets_hit") or 0,
                "preserve_set_total": entry.get("preserve_set_total") or 0,
                "broken_preserve_count": entry.get("broken_preserve_count") or 0,
                "cost_usd": entry.get("cost_usd") or 0.0,
                "tokens_input": entry.get("tokens_input") or 0,
                "tokens_cache": entry.get("tokens_cache") or 0,
                "tokens_output": entry.get("tokens_output") or 0,
                "n_episodes": entry.get("n_episodes") or 0,
                "duration_sec": entry.get("duration_sec") or 0,
                "status": entry.get("status") or "scored",
                "started_at": self._parse_dt(entry.get("started_at")),
                "finished_at": self._parse_dt(entry.get("finished_at")),
                "result_url": entry.get("result_url"),
                "trajectory_url": entry.get("trajectory_url"),
            }
            rec = self.search(
                [
                    ("task_id", "=", task.id),
                    ("model_id", "=", model.id),
                    ("run_number", "=", values["run_number"]),
                ],
                limit=1,
            )
            if rec:
                rec.write(values)
                updated += 1
            else:
                self.create(values)
                created += 1

        _logger.info(
            "erza: seeded %s models, %s tasks, %s runs created, %s runs updated",
            len(model_by_key), len(task_by_uuid), created, updated,
        )
        return True

    @api.model
    def _load_bundle_payload(self):
        module_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        path = os.path.join(module_dir, "data", "erza_dataset.json")
        if not os.path.isfile(path):
            return None
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)

    @staticmethod
    def _parse_dt(value):
        if not value:
            return False
        return value.replace("T", " ").split(".")[0].split("+")[0].strip().rstrip("Z")

    def action_open_trajectory(self):
        self.ensure_one()
        if not self.trajectory_url:
            return False
        return {
            "type": "ir.actions.act_url",
            "url": self.trajectory_url,
            "target": "new",
        }
