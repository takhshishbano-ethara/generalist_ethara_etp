import json
import logging
import os
from pathlib import Path

import yaml

from odoo import api, fields, models
from odoo.exceptions import UserError

from .pipeline_config import LANGUAGE_SELECTION

_logger = logging.getLogger(__name__)

_EXCLUDED_REPOS_CACHE: set[str] | None = None

DISCOVERY_STATE = [
    ("new", "New"),
    ("enriching", "Enriching"),
    ("validated", "Validated"),
    ("promoted", "Promoted"),
    ("rejected", "Rejected"),
    ("skipped", "Skipped"),
]

ENRICHMENT_STATUS = [
    ("idle", "Idle"),
    ("running", "Running"),
    ("done", "Done"),
    ("failed", "Failed"),
]

SKIP_REASON = [
    ("none", "—"),
    ("done_exact", "Done (exact)"),
    ("done_bare", "Done (bare-name)"),
    ("done_fuzzy", "Done (fuzzy — verify)"),
    ("no_pr_exact", "No PRs"),
    ("no_pr_bare", "No PRs (bare-name)"),
    ("no_pr_fuzzy", "No PRs (fuzzy — verify)"),
    ("manual", "Manual reject"),
]

_ADVISORY_LOCK_ENRICH = 74927463
_ADVISORY_LOCK_PROMOTE = 74927464
_ADVISORY_LOCK_DONE_SYNC = 74927466


class AuroraDiscovery(models.Model):
    _name = "aurora.discovery"
    _description = "Aurora Repository Discovery"
    _inherit = ["mail.thread"]
    _order = "quality_score desc, id desc"

    github_org = fields.Char(string="GitHub Org", required=True, tracking=True)
    github_repo = fields.Char(string="GitHub Repo", required=True, tracking=True)
    full_name = fields.Char(
        compute="_compute_full_name", store=True, index=True,
    )
    state = fields.Selection(
        DISCOVERY_STATE, default="new", required=True, tracking=True,
    )
    source_tags = fields.Char(
        help="Comma-separated sources: search,trending,topics,curated",
    )
    discovery_count = fields.Integer(default=1)
    first_discovered = fields.Datetime(default=fields.Datetime.now)
    last_seen = fields.Datetime(default=fields.Datetime.now)

    stars = fields.Integer()
    forks = fields.Integer()
    open_issues = fields.Integer()
    primary_language = fields.Char()
    language_pct = fields.Float(digits=(5, 1))
    has_tests = fields.Boolean()
    has_ci = fields.Boolean()
    license_spdx = fields.Char()
    size_kb = fields.Integer()
    last_pushed = fields.Datetime()
    topics = fields.Char()
    description = fields.Text()
    default_branch = fields.Char(default="main")

    quality_score = fields.Integer(
        compute="_compute_quality_score", store=True,
    )
    auto_promote = fields.Boolean(
        compute="_compute_auto_promote", store=True,
    )

    pipeline_id = fields.Many2one("aurora.pipeline", string="Created Pipeline", readonly=True)
    rejection_reason = fields.Text()
    user_id = fields.Many2one(
        "res.users", default=lambda self: self.env.user, tracking=True,
    )

    enrichment_status = fields.Selection(ENRICHMENT_STATUS, default="idle")
    enrichment_log = fields.Text()
    last_enrichment = fields.Datetime()

    skip_reason = fields.Selection(
        SKIP_REASON, default="none", tracking=True,
        help="Why this discovery is skipped (synced from auroraScraping "
             "done_repo.txt / no_pr.txt). Bare and fuzzy matches need "
             "manual verification.",
    )
    skip_reason_synced_at = fields.Datetime(string="Skip reason last synced")

    _sql_constraints = [
        ("org_repo_unique", "UNIQUE(github_org, github_repo)",
         "This repository has already been discovered."),
    ]

    @api.depends("github_org", "github_repo")
    def _compute_full_name(self):
        for rec in self:
            if rec.github_org and rec.github_repo:
                rec.full_name = f"{rec.github_org}/{rec.github_repo}"
            else:
                rec.full_name = False

    @api.depends("stars", "forks", "language_pct", "has_tests", "has_ci",
                 "license_spdx", "open_issues", "last_pushed")
    def _compute_quality_score(self):
        now = fields.Datetime.now()
        for rec in self:
            score = 0
            if rec.last_pushed:
                days = (now - rec.last_pushed).days
                if days <= 30:
                    score += 30
                elif days <= 180:
                    score += 15
                elif days <= 365:
                    score += 5
            if rec.stars >= 5000:
                score += 20
            elif rec.stars >= 2000:
                score += 15
            elif rec.stars >= 1000:
                score += 10
            elif rec.stars >= 500:
                score += 5
            if rec.language_pct >= 80:
                score += 20
            elif rec.language_pct >= 60:
                score += 10
            elif rec.language_pct >= 40:
                score += 5
            if rec.has_tests:
                score += 10
            if rec.has_ci:
                score += 10
            if rec.license_spdx:
                score += 5
            if rec.open_issues >= 50:
                score += 5
            rec.quality_score = min(score, 100)

    @api.depends("quality_score", "state")
    def _compute_auto_promote(self):
        threshold = int(
            self.env["ir.config_parameter"].sudo().get_param(
                "aurora.discovery_auto_promote_threshold", "80"
            )
        )
        for rec in self:
            rec.auto_promote = rec.quality_score >= threshold and rec.state == "validated"

    @api.model
    def _load_excluded_repos(self) -> set[str]:
        global _EXCLUDED_REPOS_CACHE
        if _EXCLUDED_REPOS_CACHE is not None:
            return _EXCLUDED_REPOS_CACHE

        yaml_path = Path(__file__).parent.parent / "data" / "excluded_repos.yaml"
        excluded: set[str] = set()
        if yaml_path.is_file():
            try:
                with yaml_path.open("r", encoding="utf-8") as fh:
                    data = yaml.safe_load(fh) or {}
                for entry in data.get("excluded_repos", []):
                    org = entry.get("org", "")
                    repo = entry.get("repo", "")
                    if org and repo:
                        excluded.add(f"{org}/{repo}")
            except Exception as exc:
                _logger.warning("Failed to load excluded_repos.yaml: %s", exc)

        _EXCLUDED_REPOS_CACHE = excluded
        return excluded

    def _should_skip(self, org: str, repo: str) -> bool:
        full_name = f"{org}/{repo}"
        if full_name in self._load_excluded_repos():
            return True
        if self.search_count([("github_org", "=", org), ("github_repo", "=", repo)]):
            return True
        if self.env["aurora.pipeline"].search_count([
            ("github_org", "=", org), ("github_repo", "=", repo),
        ]):
            return True
        if self.env["aurora.pipeline"].search_count([
            ("github_org", "=", org), ("github_repo", "=", repo),
            ("stage", "=", "failed"),
        ]):
            return True
        return False

    def action_promote_to_pipeline(self):
        self.ensure_one()
        if self.state == "promoted":
            raise UserError("This repository has already been promoted.")
        pipeline = self.env["aurora.pipeline"].create({
            "github_org": self.github_org,
            "github_repo": self.github_repo,
        })
        self.write({"state": "promoted", "pipeline_id": pipeline.id})
        return {
            "type": "ir.actions.act_window",
            "res_model": "aurora.pipeline",
            "res_id": pipeline.id,
            "view_mode": "form",
            "target": "current",
        }

    def action_reject(self):
        self.ensure_one()
        self.write({"state": "rejected"})

    def action_enrich(self):
        self.ensure_one()
        from .discovery_executor import submit_enrichment_async
        db_name = self.env.cr.dbname
        submit_enrichment_async(db_name, self.env.uid, self.id)

    def _cron_auto_promote(self):
        if not self.env.cr.execute(
            "SELECT pg_try_advisory_lock(%s)", [_ADVISORY_LOCK_PROMOTE]
        ):
            return
        try:
            candidates = self.search([
                ("state", "=", "validated"),
                ("auto_promote", "=", True),
                ("pipeline_id", "=", False),
            ], limit=10)
            for rec in candidates:
                try:
                    rec.action_promote_to_pipeline()
                except Exception as exc:
                    _logger.warning("Auto-promote failed for %s: %s", rec.full_name, exc)
        finally:
            self.env.cr.execute("SELECT pg_advisory_unlock(%s)", [_ADVISORY_LOCK_PROMOTE])

    def _cron_enrich_pending(self):
        if not self.env.cr.execute(
            "SELECT pg_try_advisory_lock(%s)", [_ADVISORY_LOCK_ENRICH]
        ):
            return
        try:
            pending = self.search([
                ("state", "=", "new"),
                ("enrichment_status", "=", "idle"),
            ], limit=50)
            from .discovery_executor import submit_enrichment_batch_async
            if pending:
                db_name = self.env.cr.dbname
                submit_enrichment_batch_async(db_name, self.env.uid, pending.ids)
        finally:
            self.env.cr.execute("SELECT pg_advisory_unlock(%s)", [_ADVISORY_LOCK_ENRICH])

    def _cron_run_discovery(self):
        _ADVISORY_LOCK_DISCOVERY = 74927465
        cr = self.env.cr
        cr.execute("SELECT pg_try_advisory_lock(%s)", [_ADVISORY_LOCK_DISCOVERY])
        if not cr.fetchone()[0]:
            return
        try:
            from .credential_manager import decrypt_value
            ICP = self.env["ir.config_parameter"].sudo()

            tokens_records = self.env["aurora.github.token"].sudo().search([
                ("state", "=", "active"),
            ], limit=3)
            if not tokens_records:
                _logger.warning("Discovery cron: no active tokens")
                return

            tokens = []
            for t in tokens_records:
                try:
                    tokens.append(decrypt_value(ICP, t.token))
                except Exception:
                    continue
            if not tokens:
                return

            excluded = self._load_excluded_repos()
            existing_discoveries = set(
                self.search([]).mapped(lambda r: f"{r.github_org}/{r.github_repo}")
            )
            existing_pipelines = set(
                self.env["aurora.pipeline"].search([]).mapped(
                    lambda r: f"{r.github_org}/{r.github_repo}"
                )
            )
            excluded = excluded | existing_discoveries | existing_pipelines

            from ..tools.collect.discover_repos import _search_repos, _enrich_repo
            from ..tools.collect.util import TokenRotator
            from dateutil.parser import parse as parse_dt

            min_stars = int(ICP.get_param("aurora.discovery_min_stars", "1000"))
            min_lang_pct = float(ICP.get_param("aurora.discovery_min_language_pct", "60.0"))
            per_lang = 100

            rotator = TokenRotator(tokens)
            languages = [code for code, _ in LANGUAGE_SELECTION]

            for lang in languages:
                try:
                    repos = _search_repos(rotator, lang, min_stars, per_lang, excluded)
                    for repo in repos:
                        repo = _enrich_repo(rotator, repo)
                        if repo.get("language_pct", 0) < min_lang_pct:
                            _logger.info(
                                "Discovery filter: dropping %s — language_pct=%.1f < %.1f",
                                repo.get("full_name", "?"),
                                repo.get("language_pct", 0),
                                min_lang_pct,
                            )
                            continue
                        org = repo["github_org"]
                        repo_name = repo["github_repo"]
                        existing = self.search([
                            ("github_org", "=", org), ("github_repo", "=", repo_name),
                        ], limit=1)
                        if existing:
                            existing.write({
                                "discovery_count": existing.discovery_count + 1,
                                "last_seen": fields.Datetime.now(),
                            })
                        else:
                            vals = {
                                "github_org": org,
                                "github_repo": repo_name,
                                "stars": repo.get("stars", 0),
                                "forks": repo.get("forks", 0),
                                "open_issues": repo.get("open_issues", 0),
                                "primary_language": repo.get("primary_language", ""),
                                "language_pct": repo.get("language_pct", 0.0),
                                "has_tests": repo.get("has_tests", False),
                                "has_ci": repo.get("has_ci", False),
                                "license_spdx": repo.get("license_spdx", ""),
                                "size_kb": repo.get("size_kb", 0),
                                "topics": repo.get("topics", ""),
                                "description": (repo.get("description", "") or "")[:500],
                                "default_branch": repo.get("default_branch", "main"),
                                "source_tags": "search",
                                "state": "validated",
                                "enrichment_status": "done",
                            }
                            last_pushed = repo.get("last_pushed", "")
                            if last_pushed:
                                try:
                                    dt = parse_dt(last_pushed)
                                    if dt.tzinfo:
                                        dt = dt.replace(tzinfo=None)
                                    vals["last_pushed"] = dt
                                except Exception:
                                    pass
                            self.create(vals)
                        excluded.add(f"{org}/{repo_name}")
                    self.env.cr.commit()
                except Exception as exc:
                    _logger.warning("Discovery cron failed for language %s: %s", lang, exc)
                    self.env.cr.rollback()
        finally:
            cr.execute("SELECT pg_advisory_unlock(%s)", [_ADVISORY_LOCK_DISCOVERY])

    def _cron_sync_done_repo(self):
        cr = self.env.cr
        cr.execute("SELECT pg_try_advisory_lock(%s)", [_ADVISORY_LOCK_DONE_SYNC])
        if not cr.fetchone()[0]:
            return
        try:
            from . import done_repo_sync
            try:
                repo_full, branch, token = done_repo_sync.get_config(self.env)
            except ValueError as exc:
                _logger.info("auroraScraping sync skipped: %s", exc)
                return
            try:
                done_idx, no_pr_idx = done_repo_sync.sync_done_repos(
                    token, repo_full, branch, force_refresh=True,
                )
            except Exception as exc:
                _logger.warning("auroraScraping sync failed: %s", exc)
                return
            new_rows = self.search([
                ("state", "=", "new"),
                "|", ("skip_reason", "=", False), ("skip_reason", "=", "none"),
            ], limit=500)
            now = fields.Datetime.now()
            updated = 0
            for rec in new_rows:
                done_match, done_tier = done_repo_sync.is_repo_done(
                    rec.github_org, rec.github_repo, done_idx,
                )
                if done_match:
                    rec.write({
                        "skip_reason": f"done_{done_tier}",
                        "skip_reason_synced_at": now,
                    })
                    updated += 1
                    continue
                no_pr_match, no_pr_tier = done_repo_sync.is_repo_no_pr(
                    rec.github_org, rec.github_repo, no_pr_idx,
                )
                if no_pr_match:
                    rec.write({
                        "skip_reason": f"no_pr_{no_pr_tier}",
                        "skip_reason_synced_at": now,
                    })
                    updated += 1
            self.env.cr.commit()
            if updated:
                _logger.info(
                    "auroraScraping skip_reason updated on %d discovery rows", updated,
                )
        finally:
            cr.execute("SELECT pg_advisory_unlock(%s)", [_ADVISORY_LOCK_DONE_SYNC])

    def action_refresh_done_list(self):
        from . import done_repo_sync
        repo_full, branch, _token = done_repo_sync.get_config(self.env)
        done_repo_sync.invalidate_cache(repo_full, branch)
        self._cron_sync_done_repo()
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Done list synced",
                "message": "auroraScraping done_repo.txt + no_pr.txt refreshed.",
                "type": "success",
            },
        }

    def action_open_discovery_wizard(self):
        return {
            "type": "ir.actions.act_window",
            "name": "Discover Repositories",
            "res_model": "aurora.discovery.wizard",
            "view_mode": "form",
            "target": "new",
        }
