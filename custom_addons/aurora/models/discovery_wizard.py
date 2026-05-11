import json
import logging

from dateutil.parser import parse as parse_dt

from odoo import api, fields, models

from .pipeline_config import LANGUAGE_SELECTION

_logger = logging.getLogger(__name__)

ALL_LANGUAGE_SELECTION = [("all", "All Languages")] + LANGUAGE_SELECTION


class AuroraDiscoveryWizard(models.TransientModel):
    _name = "aurora.discovery.wizard"
    _description = "Discover Repositories"

    source_search = fields.Boolean("GitHub Search API", default=True)
    min_stars = fields.Integer("Min Stars", default=1000)
    language = fields.Selection(ALL_LANGUAGE_SELECTION, string="Language", default="all")
    max_results = fields.Integer("Max Results", default=50)
    min_language_pct = fields.Float("Min Language %", default=60.0)

    state = fields.Selection([
        ("config", "Configure"),
        ("running", "Running"),
        ("done", "Done"),
    ], default="config")
    result_count = fields.Integer("New Unique Repos", readonly=True)
    total_searched = fields.Integer("Total from GitHub", readonly=True)
    already_excluded = fields.Integer("Already Excluded", readonly=True)
    incompatible_count = fields.Integer("Incompatible (Low Lang %)", readonly=True)
    log = fields.Text(readonly=True)

    def action_discover(self):
        self.ensure_one()
        Discovery = self.env["aurora.discovery"]
        excluded = Discovery._load_excluded_repos()

        existing_discoveries = set(
            Discovery.search([]).mapped(lambda r: f"{r.github_org}/{r.github_repo}")
        )
        existing_pipelines = set(
            self.env["aurora.pipeline"].search([]).mapped(
                lambda r: f"{r.github_org}/{r.github_repo}"
            )
        )
        excluded = excluded | existing_discoveries | existing_pipelines

        from ..tools.collect.util import TokenRotator

        ICP = self.env["ir.config_parameter"].sudo()

        tokens_records = self.env["aurora.github.token"].sudo().search([
            ("state", "=", "active"),
        ], limit=3)
        if not tokens_records:
            self.write({"state": "done", "log": "ERROR: No active tokens in pool."})
            return self._reopen()

        from ..models.credential_manager import decrypt_value
        tokens = []
        for t in tokens_records:
            try:
                tokens.append(decrypt_value(ICP, t.token))
            except Exception:
                continue

        if not tokens:
            self.write({"state": "done", "log": "ERROR: Could not decrypt tokens."})
            return self._reopen()

        self.write({"state": "running", "log": "Running discovery...\n"})
        self.env.cr.commit()

        languages_to_search = (
            [code for code, _ in LANGUAGE_SELECTION]
            if self.language == "all"
            else [self.language]
        )
        per_lang_max = max(1, self.max_results // len(languages_to_search))

        from ..tools.collect.discover_repos import _search_repos, _enrich_repo

        rotator = TokenRotator(tokens)
        new_count = 0
        updated_count = 0
        skipped_count = 0
        total_from_api = 0

        try:
            for lang in languages_to_search:
                _logger.info("Discovery: searching language=%s (max %d)", lang, per_lang_max)

                repos = _search_repos(rotator, lang, self.min_stars, per_lang_max, excluded)
                total_from_api += len(repos)

                for i, repo in enumerate(repos):
                    _logger.info("[%d/%d] Enriching %s", i + 1, len(repos), repo["full_name"])
                    repo = _enrich_repo(rotator, repo)

                    if repo.get("language_pct", 0) < self.min_language_pct:
                        _logger.info("  Skipped %s: %.1f%% < %.1f%%",
                                     repo["full_name"], repo.get("language_pct", 0), self.min_language_pct)
                        skipped_count += 1
                        continue

                    org = repo["github_org"]
                    repo_name = repo["github_repo"]

                    existing = Discovery.search([
                        ("github_org", "=", org), ("github_repo", "=", repo_name),
                    ], limit=1)
                    if existing:
                        existing.write({
                            "discovery_count": existing.discovery_count + 1,
                            "last_seen": fields.Datetime.now(),
                        })
                        updated_count += 1
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
                        Discovery.create(vals)
                        new_count += 1

        except Exception as exc:
            _logger.exception("Discovery failed")
            self.write({"state": "done", "log": f"ERROR: {exc}"})
            return self._reopen()

        lang_label = "all languages" if self.language == "all" else self.language
        log_lines = [
            f"Languages searched: {', '.join(languages_to_search)}",
            f"Max results per language: {per_lang_max}",
            f"Total repos from GitHub API: {total_from_api}",
            f"Already excluded (known repos): {len(excluded)} in exclusion list",
            f"Passed enrichment: {new_count + updated_count} repos",
            f"  - New unique: {new_count}",
            f"  - Already discovered (updated): {updated_count}",
            f"Incompatible (below {self.min_language_pct}% {lang_label}): {skipped_count}",
            "",
            "Discovery complete.",
        ]

        self.write({
            "state": "done",
            "result_count": new_count,
            "total_searched": total_from_api,
            "already_excluded": len(excluded),
            "incompatible_count": skipped_count,
            "log": "\n".join(log_lines),
        })
        return self._reopen()

    def action_view_results(self):
        return {
            "type": "ir.actions.act_window",
            "name": "Discovery Results",
            "res_model": "aurora.discovery",
            "view_mode": "list,form",
            "target": "current",
        }

    def _reopen(self):
        return {
            "type": "ir.actions.act_window",
            "res_model": self._name,
            "res_id": self.id,
            "view_mode": "form",
            "target": "new",
        }
