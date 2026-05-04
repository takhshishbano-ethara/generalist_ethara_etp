Refactor Jaeger Phase 1 pipeline: Bifurcate PR filtering into independent SWE/LHT/RCT subpipelines after get_all_prs.

The Problem:

Currently, all pipeline modes share a single entry path but diverge at different points with inconsistent criteria. The wrong assumption being corrected: RCT PRs are NOT a subset of SWE PRs. Any merged PR can be a valid RCT candidate regardless of SWE's strict filtering criteria (minimum stars, changed lines, files changed, test presence). Forcing RCT through SWE's filter silently discards the majority of valid bounty-related PRs, producing a tiny dataset that cripples Phase 2 and 3.

The Insight:

get_all_prs is the shared foundation — it fetches every PR regardless of mode. Everything AFTER get_all_prs must branch independently per mode. Each mode has fundamentally different definitions of "a valid PR":

SWE / hard_swe: Strict structural criteria (code changes, test presence, file count thresholds, changed lines thresholds, must resolve an issue). Filters aggressively because SWE-Bench needs PRs that represent genuine bug fixes with test coverage.
RCT: Lenient structural criteria. Filters primarily based on bounty/security signals (has linked bounty, has reward mention, has security label, references CVE, linked to bug bounty platforms). A PR with 1 changed file and no tests is still valid RCT if it fixes a bounty.
LHT: Requires merged only (no issue requirement). Then groups by version-tag ranges via a 6-step pipeline (fetch tags → group PRs → fetch issues → build unified-diff dataset). Already implemented in tools/get_version_tags.py, tools/group_prs_by_tags.py, tools/build_lht_dataset.py and _run_lht_pipeline() in jaeger_stage2_scrape.py. The K8s dispatch path (worker/entrypoint.py) currently handles LHT via run_lht_pipeline().
The Architecture Change:

BEFORE (wrong — shared filter, then mode-specific divergence later):
get_all_prs → filter_prs (SWE criteria bleeds into RCT) → ...

AFTER (correct — immediate independent subpipelines):
get_all_prs ─┬─→ [SWE path]: filter_prs (strict SWE criteria) → get_related_issues → merge_prs_with_issues → build_dataset
             ├─→ [RCT path]: filter_prs_rct (lenient + bounty signal) → get_related_issues → merge_prs_with_issues → filter_by_bounty_data → build_dataset
             └─→ [LHT path]: filter_prs (mode=lht, merged only) → get_version_tags → group_prs_by_tags → get_related_issues → build_lht_dataset
Each mode produces its own {org}__{repo}_raw_dataset.jsonl with the same downstream schema consumed by _create_instances_from_dataset() / _create_instances_from_s3().

What to implement:

1. New tool: tools/filter_prs_rct.py

RCT-specific filtering with lenient structural criteria:

NO minimum star count requirement
NO minimum changed lines threshold
NO minimum files changed threshold
NO requirement for test files in the PR
YES: must be a merged PR (merged_at != None)
YES: must have a clear problem statement (linked issue OR descriptive body with >50 chars)
Positive RCT signals to score (presence = higher score, absence ≠ disqualification):
Labels containing: bounty, security, vulnerability, CVE, reward, bug-bounty
Body/title mentions: dollar amounts ($), reward tiers, bounty program names
References external platforms: HackerOne, Immunefi, Bugcrowd, Synack
Linked issue has security/bounty labels
Signature must follow vendored tools contract:

def main(pool, out_dir, prs_file, progress_callback=None):
    """Filter PRs for RCT mode with lenient criteria + bounty scoring."""
Output: {org}__{repo}_rct_filtered_prs.jsonl

2. New tool: tools/filter_by_bounty_data.py

Post-merge enrichment step — after merge_prs_with_issues, score and rank by bounty relevance:

Has linked issue with bounty/reward/security label
Issue body mentions dollar amounts, reward tiers, or bounty program names
PR or issue references external bounty platforms
Produces scored output with bounty_confidence field (0.0–1.0), keeps ALL records (ranking, not binary filter)
Signature:

def main(out_dir, org, repo, merged_file=None):
    """Score and rank merged PRs by bounty relevance."""
Output: {org}__{repo}_bounty_scored.jsonl (or overwrites the merged file in-place — decide based on downstream needs)

3. Modify worker/entrypoint.py — Add run_rct_pipeline()

New RCT pipeline function (6 steps):

get_all_prs (shared)
filter_prs_rct (lenient + bounty signals)
get_related_issues
merge_prs_with_issues
filter_by_bounty_data (bounty scoring/ranking)
build_dataset (mode="rct")
Update main() routing:

if PIPELINE_MODE == "rct":
    s3_paths, counts = run_rct_pipeline(...)
elif PIPELINE_MODE == "lht":
    s3_paths, counts = run_lht_pipeline(...)
else:
    s3_paths, counts = run_swe_pipeline(...)
Progress webhooks should reflect 6 steps for RCT: "Step 2/6", "Step 3/6", etc.

4. Modify models/jaeger_stage2_scrape.py — ORM path routing

Update run_scrape_pipeline() routing to handle all three modes explicitly:

if self.pipeline_mode in ("swe", "hard_swe"):
    raise UserError("SWE pipeline runs via K8s dispatch (action_collect_prs).")
elif self.pipeline_mode == "rct":
    self._run_rct_pipeline(tokens, out_dir)
elif self.pipeline_mode == "lht":
    self._run_lht_pipeline(tokens, out_dir)
else:
    raise UserError(f"Unknown pipeline mode: {self.pipeline_mode}")
Add _run_rct_pipeline() method mirroring the entrypoint's RCT path but using ORM writes for progress.

5. Update upload_outputs() in worker/entrypoint.py

Add RCT-specific output files to the S3 upload map:

rct_files = {
    "rct_filtered": f"{org}__{repo_name}_rct_filtered_prs.jsonl",
    "bounty_scored": f"{org}__{repo_name}_bounty_scored.jsonl",
}
6. Add "rct" to PIPELINE_MODE_SELECTION

In models/jaeger_repository.py:

PIPELINE_MODE_SELECTION = [
    ("swe", "SWE (Single-PR Tasks)"),
    ("hard_swe", "Hard SWE (≥5 files, ≥100 lines)"),
    ("lht", "LHT (Long-Horizon Tasks)"),
    ("rct", "RCT (Real Coder Tasks)"),
]
7. Auto-set task_category for RCT repos

When RCT pipeline completes and instances are created:

if self.pipeline_mode == "rct" and not self.task_category:
    self.write({"task_category": "real_coder"})
What NOT to change:

tools/get_all_prs.py — stays as-is, fetches everything regardless of mode
tools/build_dataset.py — stays as-is, consumes merged JSONL and produces raw_dataset (pass mode="rct" for any RCT-specific behavior if needed)
tools/get_related_issues.py — stays as-is, works on any filtered PR list
tools/merge_prs_with_issues.py — stays as-is
tools/filter_prs.py — stays as-is (still used by SWE and LHT modes)
LHT tools (get_version_tags.py, group_prs_by_tags.py, build_lht_dataset.py) — already implemented, do not modify
The webhook/S3/K8s Job infrastructure — just refactored, don't touch
Dockerfile.worker — already has git and packaging installed for LHT; no new system deps needed for RCT
Key constraint: New tools (filter_prs_rct.py, filter_by_bounty_data.py) must follow the same pattern as existing vendored tools: pure Python, no Odoo imports, take args (pool/out_dir/input_file), write JSONL output, use logging, importable as from tools.filter_prs_rct import main.

Expected outcomes for a repo like kubernetes/kubernetes (~80,000 PRs):

Mode	PRs passing filter	Why
SWE	~2,000	Strict: needs tests, meaningful code changes, resolved issue
hard_swe	~500	Even stricter: ≥5 files, ≥100 lines changed
RCT	~8,000+	Lenient: any merged PR with problem statement; bounty-scored
LHT	~1,200 merged → ~30 tag-group bundles	Grouped by version ranges, not individual PRs
This is correct — RCT produces a MUCH larger candidate pool because the structural bar is intentionally lower. Phase 2/3 (Docker build + test execution) naturally narrows it further. LHT produces fewer but larger/harder tasks (multi-PR bundles).