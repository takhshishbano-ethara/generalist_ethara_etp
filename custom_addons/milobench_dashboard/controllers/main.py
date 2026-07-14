import json

from odoo import http
from odoo.http import request


DEFAULT_GITHUB_URL = "https://github.com/EtharaOrion/milo-bench-samples"
DEFAULT_HUGGINGFACE_URL = "https://huggingface.co/datasets/ethara/milo-bench-samples"
DEFAULT_PAPER_URL = "https://github.com/EtharaOrion/milo-bench-samples#readme"


class MilobenchController(http.Controller):

    @http.route("/milobench-samples", type="http", auth="public", website=True, sitemap=True)
    def portal_page(self, **_kw):
        param = request.env["ir.config_parameter"].sudo()
        module = request.env["ir.module.module"].sudo().search(
            [("name", "=", "milobench_dashboard")], limit=1
        )
        asset_version = (module.latest_version or "0") + "-" + str(
            module.write_date and int(module.write_date.timestamp()) or 0
        )
        return request.render(
            "milobench_dashboard.portal_showcase",
            {
                "github_url": param.get_param("milobench_dashboard.github_url", DEFAULT_GITHUB_URL),
                "huggingface_url": param.get_param("milobench_dashboard.huggingface_url", DEFAULT_HUGGINGFACE_URL),
                "paper_url": param.get_param("milobench_dashboard.paper_url", DEFAULT_PAPER_URL),
                "asset_version": asset_version,
            },
        )

    @http.route("/milobench-samples/api/summary", type="http", auth="public", website=True, sitemap=False, csrf=False)
    def api_summary(self, **_kw):
        return self._json(self._build_summary())

    @http.route("/milobench-samples/api/tasks", type="http", auth="public", website=True, sitemap=False, csrf=False)
    def api_tasks(self, **_kw):
        return self._json(self._build_tasks())

    @http.route("/milobench-samples/api/runs", type="http", auth="public", website=True, sitemap=False, csrf=False)
    def api_runs(self, **_kw):
        return self._json(self._build_runs())

    @http.route("/milobench-samples/api/dataset", type="http", auth="public", website=True, sitemap=False, csrf=False)
    def api_dataset(self, **_kw):
        return self._json({
            "summary": self._build_summary(),
            "tasks": self._build_tasks(),
            "runs": self._build_runs(),
        })

    def _json(self, payload):
        return request.make_response(
            json.dumps(payload),
            headers=[("Content-Type", "application/json"), ("Cache-Control", "public, max-age=60")],
        )

    def _build_summary(self):
        env = request.env
        Task = env["milobench.task"].sudo()
        Run = env["milobench.run"].sudo()
        Model = env["milobench.model"].sudo()

        tasks = Task.search([])
        runs = Run.search([])
        models_recs = Model.search([])

        total_runs = len(runs)
        passed_runs = sum(1 for r in runs if r.score_binary)
        total_cost = sum(r.cost_usd for r in runs)

        tier_stats = {}
        for tier, _label in Task._fields["difficulty"].selection:
            tier_tasks = tasks.filtered(lambda t, tier=tier: t.difficulty == tier)
            tier_runs = runs.filtered(lambda r, tier=tier: r.difficulty == tier)
            tier_stats[tier] = {
                "task_count": len(tier_tasks),
                "run_count": len(tier_runs),
                "mean_score": (sum(r.score for r in tier_runs) / len(tier_runs) * 100.0) if tier_runs else 0.0,
                "pass_rate": (sum(1 for r in tier_runs if r.score_binary) / len(tier_runs) * 100.0) if tier_runs else 0.0,
                "per_model": {},
            }
            for m in models_recs:
                mruns = tier_runs.filtered(lambda r, m=m: r.model_id.id == m.id)
                tier_stats[tier]["per_model"][m.key] = {
                    "run_count": len(mruns),
                    "pass_rate": (sum(1 for r in mruns if r.score_binary) / len(mruns) * 100.0) if mruns else 0.0,
                    "mean_score": (sum(r.score for r in mruns) / len(mruns) * 100.0) if mruns else 0.0,
                    "mean_cost_usd": (sum(r.cost_usd for r in mruns) / len(mruns)) if mruns else 0.0,
                }

        language_stats = {}
        for lang_key, lang_label in Task._fields["language"].selection:
            lang_tasks = tasks.filtered(lambda t, lang_key=lang_key: t.language == lang_key)
            if not lang_tasks:
                continue
            language_stats[lang_key] = {"label": lang_label, "task_count": len(lang_tasks)}

        model_stats = []
        for m in models_recs:
            mruns = runs.filtered(lambda r, m=m: r.model_id.id == m.id)
            model_stats.append({
                "key": m.key,
                "display_name": m.display_name,
                "provider": m.provider,
                "color": m.color,
                "run_count": len(mruns),
                "pass_count": sum(1 for r in mruns if r.score_binary),
                "pass_at_3": (sum(1 for r in mruns if r.score_binary) / len(mruns) * 100.0) if mruns else 0.0,
                "mean_score": (sum(r.score for r in mruns) / len(mruns) * 100.0) if mruns else 0.0,
                "total_cost_usd": sum(r.cost_usd for r in mruns),
            })

        return {
            "task_count": len(tasks),
            "model_count": len(models_recs),
            "run_count": total_runs,
            "passed_run_count": passed_runs,
            "pass_rate_overall": (passed_runs / total_runs * 100.0) if total_runs else 0.0,
            "mean_score_overall": (sum(r.score for r in runs) / total_runs * 100.0) if total_runs else 0.0,
            "total_cost_usd": total_cost,
            "language_count": len(language_stats),
            "codebase_count": len({t.codebase for t in tasks if t.codebase}),
            "tiers": tier_stats,
            "languages": language_stats,
            "models": model_stats,
        }

    def _build_tasks(self):
        Task = request.env["milobench.task"].sudo()
        rows = []
        for t in Task.search([]):
            rows.append({
                "uuid": t.uuid,
                "task_slug": t.task_slug,
                "codebase": t.codebase,
                "category": t.category,
                "difficulty": t.difficulty,
                "language": t.language,
                "src_hunks": t.src_hunks,
                "keywords": t.keywords,
                "instruction_url": t.instruction_url,
                "dataset_url": t.dataset_url,
                "trajectories_url": t.trajectories_url,
                "run_count": t.run_count,
                "pass_count": t.pass_count,
                "pass_rate": t.pass_rate,
                "mean_score": t.mean_score,
                "env": {
                    "cpus": t.env_cpus,
                    "memory_mb": t.env_memory_mb,
                    "storage_mb": t.env_storage_mb,
                    "gpus": t.env_gpus,
                },
            })
        return rows

    def _build_runs(self):
        Run = request.env["milobench.run"].sudo()
        rows = []
        for r in Run.search([]):
            rows.append({
                "task_uuid": r.task_id.uuid,
                "model_key": r.model_id.key,
                "model_display": r.model_id.display_name,
                "run_number": r.run_number,
                "difficulty": r.difficulty,
                "language": r.language,
                "score": r.score,
                "score_binary": r.score_binary,
                "recall": r.recall,
                "regression_factor": r.regression_factor,
                "targets_total": r.targets_total,
                "targets_hit": r.targets_hit,
                "preserve_set_total": r.preserve_set_total,
                "broken_preserve_count": r.broken_preserve_count,
                "cost_usd": r.cost_usd,
                "tokens_input": r.tokens_input,
                "tokens_cache": r.tokens_cache,
                "tokens_output": r.tokens_output,
                "n_episodes": r.n_episodes,
                "duration_sec": r.duration_sec,
                "status": r.status,
                "started_at": r.started_at.isoformat() if r.started_at else None,
                "finished_at": r.finished_at.isoformat() if r.finished_at else None,
                "result_url": r.result_url,
                "trajectory_url": r.trajectory_url,
            })
        return rows
