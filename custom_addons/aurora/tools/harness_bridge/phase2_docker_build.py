import importlib
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Callable, Optional

from ..collect.util import AuroraPipelineError

_logger = logging.getLogger(__name__)

MULTI_SWE_BENCH_ROOT = Path(__file__).resolve().parents[5] / "multi-swe-bench"

_HARNESS_REPOS_ROOT = MULTI_SWE_BENCH_ROOT / "multi_swe_bench" / "harness" / "repos"

_INSTANCE_WORKDIR = "instances"

_RANGE_RE = re.compile(r"^(.+)_(\d+)_to_(\d+)$")

_GITHUB_REPO = os.environ.get("AURORA_HARNESS_GIT_REPO", "Ethara-Ai/multi-swe-bench")
_GITHUB_BRANCH = os.environ.get("AURORA_HARNESS_GIT_BRANCH", "main")
_REGISTRY_PATH_PREFIX = "multi_swe_bench/harness/repos"


def _get_github_repo(token: str):
    from github import Auth, Github
    g = Github(auth=Auth.Token(token))
    return g.get_repo(_GITHUB_REPO)


def _find_org_dir_on_github(lang: str, org: str, token: str) -> Optional[str]:
    gh_repo = _get_github_repo(token)
    try:
        contents = gh_repo.get_contents(
            f"{_REGISTRY_PATH_PREFIX}/{lang}", ref=_GITHUB_BRANCH,
        )
    except Exception:
        return None
    if not isinstance(contents, list):
        return None
    for entry in contents:
        if entry.type == "dir" and entry.name.lower() == org.lower():
            return entry.name
    return None


def _sync_registry_from_github(
    org: str, repo: str, lang: str, token: str,
) -> list[str]:
    """Fetch registry files from GitHub and write to local harness path.

    Returns list of local file paths written.
    """
    github_org_name = _find_org_dir_on_github(lang, org, token)
    if not github_org_name:
        _logger.warning(
            "No org directory '%s' found on GitHub under %s/%s",
            org, _REGISTRY_PATH_PREFIX, lang,
        )
        return []

    gh_repo = _get_github_repo(token)
    dir_path = f"{_REGISTRY_PATH_PREFIX}/{lang}/{github_org_name}"
    try:
        entries = gh_repo.get_contents(dir_path, ref=_GITHUB_BRANCH)
    except Exception:
        return []
    if not isinstance(entries, list):
        return []

    repo_lower = repo.lower().replace("-", "_")
    matching = []
    for entry in entries:
        if entry.type != "file" or not entry.name.endswith(".py"):
            continue
        stem = entry.name[:-3].lower()
        if stem == repo_lower or stem.startswith(f"{repo_lower}_"):
            matching.append(entry)

    if not matching:
        _logger.warning(
            "No registry files for '%s' found on GitHub at %s", repo, dir_path,
        )
        return []

    local_org_dir = _HARNESS_REPOS_ROOT / lang / github_org_name
    local_org_dir.mkdir(parents=True, exist_ok=True)

    written = []
    for entry in matching:
        try:
            file_obj = gh_repo.get_contents(entry.path, ref=_GITHUB_BRANCH)
            if isinstance(file_obj, list):
                continue
            content = file_obj.decoded_content.decode("utf-8")
        except Exception as exc:
            _logger.warning("Failed to download %s: %s", entry.path, exc)
            continue

        local_file = local_org_dir / entry.name
        local_file.write_text(content, encoding="utf-8")
        written.append(str(local_file))
        _logger.info("Synced registry file: %s → %s", entry.path, local_file)

    if written:
        _ensure_init_files(lang, github_org_name, repo_lower)

    return written


def _ensure_init_files(lang: str, org_dir_name: str, repo_safe: str):
    lang_dir = _HARNESS_REPOS_ROOT / lang
    org_dir = lang_dir / org_dir_name

    org_init = org_dir / "__init__.py"
    if not org_init.exists():
        org_init.write_text(f"from .{repo_safe} import *\n")
    else:
        existing = org_init.read_text()
        import_line = f"from .{repo_safe} import *"
        if import_line not in existing:
            with open(org_init, "a") as f:
                f.write(f"\n{import_line}\n")

    for py_file in org_dir.iterdir():
        if py_file.suffix != ".py" or py_file.name == "__init__.py":
            continue
        stem = py_file.stem
        if stem.lower() != repo_safe and stem.lower().startswith(f"{repo_safe}_"):
            existing = org_init.read_text()
            import_line = f"from .{stem} import *"
            if import_line not in existing:
                with open(org_init, "a") as f:
                    f.write(f"\n{import_line}\n")

    lang_init = lang_dir / "__init__.py"
    if not lang_init.exists():
        lang_init.write_text(f"from .{org_dir_name} import *\n")
    else:
        existing = lang_init.read_text()
        import_line = f"from .{org_dir_name} import *"
        if import_line not in existing:
            with open(lang_init, "a") as f:
                f.write(f"\n{import_line}\n")


def _ensure_harness_importable():
    root = str(MULTI_SWE_BENCH_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)

    # Pre-register harness packages in sys.modules so Python doesn't execute
    # their __init__.py files (which have broken cross-language imports).
    # We only need targeted leaf-module imports, not the full repo tree.
    if "multi_swe_bench" in sys.modules:
        return

    import importlib
    import types

    stubs = [
        "multi_swe_bench",
        "multi_swe_bench.harness",
        "multi_swe_bench.harness.repos",
    ]
    repos_root = MULTI_SWE_BENCH_ROOT / "multi_swe_bench" / "harness" / "repos"
    if repos_root.is_dir():
        for lang_dir in repos_root.iterdir():
            if lang_dir.is_dir() and not lang_dir.name.startswith("_"):
                lang_pkg = f"multi_swe_bench.harness.repos.{lang_dir.name}"
                stubs.append(lang_pkg)
                for org_dir in lang_dir.iterdir():
                    if org_dir.is_dir() and not org_dir.name.startswith("_"):
                        stubs.append(f"{lang_pkg}.{org_dir.name}")

    for pkg_path in stubs:
        if pkg_path not in sys.modules:
            stub = types.ModuleType(pkg_path)
            stub.__path__ = [
                str(MULTI_SWE_BENCH_ROOT / Path(*pkg_path.split(".")))
            ]
            stub.__package__ = pkg_path
            sys.modules[pkg_path] = stub

    _SHADOW_ALIASES = {
        "multi_swe_bench.harness.image": "odoo.addons.aurora.tools.harness.image",
        "multi_swe_bench.harness.instance": "odoo.addons.aurora.tools.harness.instance",
        "multi_swe_bench.harness.pull_request": "odoo.addons.aurora.tools.harness.pull_request",
        "multi_swe_bench.harness.test_result": "odoo.addons.aurora.tools.harness.test_result",
        "multi_swe_bench.harness.constant": "odoo.addons.aurora.tools.harness.constant",
        "multi_swe_bench.harness.dataset": "odoo.addons.aurora.tools.harness.dataset",
        "multi_swe_bench.harness.docker_util": "odoo.addons.aurora.tools.harness.docker_util",
        "multi_swe_bench.harness.git_util": "odoo.addons.aurora.tools.harness.git_util",
        "multi_swe_bench.harness.report": "odoo.addons.aurora.tools.harness.report",
        "multi_swe_bench.harness.python_test": "odoo.addons.aurora.tools.harness.python_test",
    }
    for alias, shadow in _SHADOW_ALIASES.items():
        if alias in sys.modules:
            continue
        try:
            sys.modules[alias] = importlib.import_module(shadow)
        except Exception as exc:
            _logger.debug("Shadow alias %s -> %s not available: %s", alias, shadow, exc)


def _resolve_org_dir(lang_dir: Path, org: str) -> Optional[Path]:
    org_dir = lang_dir / org
    if org_dir.is_dir():
        return org_dir
    for candidate in lang_dir.iterdir():
        if candidate.is_dir() and candidate.name.lower() == org.lower():
            return candidate
    return None


def check_instance_registry(
    org: str, repo: str, lang: str, github_token: str = "",
) -> bool:
    if not github_token:
        _logger.warning("No GitHub token provided, cannot check registry on GitHub")
        return False

    synced = _sync_registry_from_github(org, repo, lang, github_token)
    if synced:
        _logger.info(
            "Synced %d registry file(s) from GitHub for %s/%s", len(synced), org, repo,
        )
        return True

    _logger.warning("No registry found on GitHub for %s/%s (lang=%s)", org, repo, lang)
    return False


def _import_all_repo_modules(org: str, repo: str, lang: str):
    _ensure_harness_importable()

    lang_dir = _HARNESS_REPOS_ROOT / lang
    org_dir = _resolve_org_dir(lang_dir, org)
    if org_dir is None:
        raise AuroraPipelineError(f"Org directory not found: {lang_dir}/{org}")

    repo_lower = repo.lower().replace("-", "_")
    loaded = 0

    for candidate in sorted(org_dir.iterdir()):
        if candidate.suffix != ".py" or candidate.name == "__init__.py":
            continue
        stem_lower = candidate.stem.lower()
        if stem_lower == repo_lower or stem_lower.startswith(f"{repo_lower}_"):
            module_name = (
                f"multi_swe_bench.harness.repos.{lang}.{org_dir.name}.{candidate.stem}"
            )
            try:
                importlib.import_module(module_name)
                loaded += 1
                _logger.info("Loaded harness module: %s", module_name)
            except Exception as exc:
                _logger.error("Failed to import %s: %s", module_name, exc)
                raise AuroraPipelineError(
                    f"Failed to import harness module {module_name}: {exc}"
                ) from exc

    if loaded == 0:
        raise AuroraPipelineError(
            f"No harness modules found for {org}/{repo} (lang={lang})"
        )

    _logger.info("Loaded %d harness module(s) for %s/%s", loaded, org, repo)


def _check_docker():
    import subprocess

    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            timeout=10,
        )
        if result.returncode != 0:
            raise AuroraPipelineError(
                "Docker daemon is not running. Start Docker before running Phase 2."
            )
    except FileNotFoundError:
        raise AuroraPipelineError("Docker is not installed.")
    except subprocess.TimeoutExpired:
        raise AuroraPipelineError("Docker daemon not responding (timed out).")


def _build_number_interval_map(
    org: str, repo: str, lang: str,
) -> list[tuple[str, int, int]]:
    """Scan registry files and return [(interval_name, min_pr, max_pr), ...].

    Registry files are named like starlette_3055_to_2813.py where the pattern
    is {repo}_{max}_to_{min}.  If only {repo}.py exists (no ranges), an empty
    list is returned — meaning number_interval should stay blank.
    """
    lang_dir = _HARNESS_REPOS_ROOT / lang
    org_dir = _resolve_org_dir(lang_dir, org)
    if org_dir is None:
        return []

    repo_lower = repo.lower().replace("-", "_")
    ranges = []

    for candidate in org_dir.iterdir():
        if candidate.suffix != ".py" or candidate.name == "__init__.py":
            continue
        stem = candidate.stem
        stem_lower = stem.lower()

        if stem_lower == repo_lower:
            continue

        m = _RANGE_RE.match(stem)
        if m and m.group(1).lower().replace("-", "_") == repo_lower:
            hi = int(m.group(2))
            lo = int(m.group(3))
            ranges.append((stem, min(lo, hi), max(lo, hi)))

    ranges.sort(key=lambda r: r[1])
    return ranges


def _find_interval_for_pr(
    pr_number: int,
    ranges: list[tuple[str, int, int]],
) -> str:
    for name, lo, hi in ranges:
        if lo <= pr_number <= hi:
            return name
    return ""


def _translate_phase1_jsonl(
    phase1_jsonl: str,
    org: str,
    repo: str,
    lang: str,
    output_path: str,
) -> int:
    """Translate Aurora Phase 1 LHT JSONL into harness-compatible format.

    Phase 1 emits LHT records with fields:
      instance_id, tag_start, tag_end, pr_numbers (list[int]),
      pr_attribution_method, version_scheme, base:{sha,ref}, head:{sha,ref}.

    The harness ``PullRequest`` dataclass still expects a single integer
    ``number`` and optional ``number_interval`` for registry range matching.
    This translator keeps the harness contract intact while registering LHT
    metadata in-process so the Odoo executor can persist the full identity
    (instance_id, tag_start, tag_end, pr_numbers, …).

    Returns the number of records written.
    """
    try:
        from ...models.evaluation_executor import register_lht_metadata
    except Exception:
        register_lht_metadata = None

    ranges = _build_number_interval_map(org, repo, lang)
    written = 0
    fallback_prs: list[int] = []

    with open(phase1_jsonl, "r") as fin, open(output_path, "w") as fout:
        for line in fin:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue

            pr_numbers = record.get("pr_numbers") or []
            if not isinstance(pr_numbers, list) or not pr_numbers:
                _logger.warning(
                    "LHT record missing or empty pr_numbers, skipping: instance_id=%s",
                    record.get("instance_id", "<unknown>"),
                )
                continue
            try:
                primary_number = int(pr_numbers[0])
            except (TypeError, ValueError):
                _logger.warning(
                    "LHT pr_numbers[0] is not an integer, skipping: %r", pr_numbers,
                )
                continue

            record["number"] = primary_number

            if ranges:
                interval = _find_interval_for_pr(primary_number, ranges)
                if interval:
                    record["number_interval"] = interval
                else:
                    _logger.warning(
                        "PR #%d in %s/%s does not match any registry interval; "
                        "falling back to base harness %s.py",
                        primary_number, org, repo, repo,
                    )
                    record["number_interval"] = ""
                    fallback_prs.append(primary_number)
            else:
                record.setdefault("number_interval", "")

            record.setdefault("tag", "")
            record.setdefault("lang", lang)

            tag_start = record.get("tag_start") or ""
            tag_end = record.get("tag_end") or ""
            instance_id = (
                record.get("instance_id")
                or (f"{org}__{repo}-{tag_start}..{tag_end}" if tag_start and tag_end else "")
            )
            if not instance_id:
                _logger.warning(
                    "LHT record missing instance_id and tag_start/tag_end, skipping: %r",
                    record,
                )
                continue

            if register_lht_metadata is not None:
                register_lht_metadata(org, repo, primary_number, {
                    "instance_id": instance_id,
                    "tag_start": tag_start,
                    "tag_end": tag_end,
                    "pr_numbers": ",".join(str(n) for n in pr_numbers),
                    "pr_attribution_method": record.get("pr_attribution_method") or "",
                    "version_scheme": record.get("version_scheme") or "",
                })

            fout.write(json.dumps(record, ensure_ascii=False) + "\n")
            written += 1

    if fallback_prs:
        try:
            with open(output_path + ".fallback_summary.json", "w") as sf:
                json.dump({
                    "org": org,
                    "repo": repo,
                    "base_fallback_prs": sorted(set(fallback_prs)),
                }, sf)
        except OSError as exc:
            _logger.warning("Failed to write fallback summary: %s", exc)

    _logger.info(
        "Translated %d LHT records from Phase 1 JSONL into harness format "
        "(base-fallback: %d PR(s))",
        written, len(fallback_prs),
    )
    return written


def _assemble_dataset(
    workdir: Path,
    report_dir: Path,
    org: str,
    repo: str,
    instances: list,
    phase1_jsonl: str,
    log_callback: Optional[Callable] = None,
) -> dict:
    """Assemble the SWE-bench format dataset JSONL and final_report.json.

    Mirrors the official gen_report.py ``run_dataset()`` flow:
    1. Reads each per-instance report.json via the official Report class.
    2. Builds Dataset objects by merging Phase-1 PR data with report data.
    3. Writes ``final_report.json`` (aggregated summary).
    4. Writes ``{org}__{repo}_dataset.jsonl`` (full-fat per-instance records).
    """
    _report_mod = importlib.import_module("multi_swe_bench.harness.report")
    _dataset_mod = importlib.import_module("multi_swe_bench.harness.dataset")
    _pr_mod = importlib.import_module("multi_swe_bench.harness.pull_request")
    Report = _report_mod.Report
    FinalReport = _report_mod.FinalReport
    Dataset = _dataset_mod.Dataset

    raw_dataset: dict = {}
    with open(phase1_jsonl, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            pr = _pr_mod.PullRequest.from_json(line)
            raw_dataset[pr.id] = pr

    valid_reports: list = []
    invalid_reports: list = []
    failed_ids: list[str] = []

    for inst in instances:
        dep = inst.dependency()
        instance_dir = (
            workdir
            / inst.pr.org
            / inst.pr.repo
            / _INSTANCE_WORKDIR
            / dep.workdir()
        )
        report_path = instance_dir / "report.json"

        if inst.pr.id not in raw_dataset:
            continue

        if not report_path.exists():
            failed_ids.append(inst.pr.id)
            continue

        try:
            with open(report_path, "r", encoding="utf-8") as rf:
                report = Report.from_json(rf.read())
            if report.valid:
                valid_reports.append(report)
            else:
                invalid_reports.append(report)
        except Exception as exc:
            _logger.warning(
                "Failed to deserialize report for %s: %s", inst.pr.id, exc,
            )
            failed_ids.append(inst.pr.id)

    # --- final_report.json ---
    # FinalReport.from_reports expects (valid, invalid, failed_tasks).
    # We don't have ReportTask objects for failed ones, so build a minimal
    # FinalReport manually matching the official schema.
    fr_data = {
        "total_instances": len(valid_reports) + len(invalid_reports) + len(failed_ids),
        "submitted_instances": len(valid_reports) + len(invalid_reports) + len(failed_ids),
        "completed_instances": len(valid_reports) + len(invalid_reports),
        "incomplete_instances": len(failed_ids),
        "resolved_instances": len(valid_reports),
        "unresolved_instances": len(invalid_reports),
        "empty_patch_instances": 0,
        "error_instances": len(failed_ids),
        "submitted_ids": (
            [r.id for r in valid_reports]
            + [r.id for r in invalid_reports]
            + failed_ids
        ),
        "completed_ids": (
            [r.id for r in valid_reports]
            + [r.id for r in invalid_reports]
        ),
        "incomplete_ids": failed_ids,
        "resolved_ids": [r.id for r in valid_reports],
        "unresolved_ids": [r.id for r in invalid_reports],
        "empty_patch_ids": [],
        "error_ids": failed_ids,
    }

    final_report_json_path = report_dir / "final_report.json"
    with open(final_report_json_path, "w", encoding="utf-8") as f:
        json.dump(fr_data, f, indent=4, ensure_ascii=False)

    _logger.info("Wrote %s", final_report_json_path)

    dataset_records: list = []
    for report in valid_reports:
        pr = raw_dataset.get(report.id)
        if pr is None:
            continue
        ds = Dataset.build(pr, report)
        dataset_records.append(ds)

    dataset_records.sort(reverse=True)

    dataset_jsonl_path = report_dir / f"{org}__{repo}_dataset.jsonl"
    with open(dataset_jsonl_path, "w", encoding="utf-8") as f:
        for ds in dataset_records:
            f.write(ds.json())
            f.write("\n")

    _logger.info(
        "Wrote %d dataset records to %s", len(dataset_records), dataset_jsonl_path,
    )

    if log_callback:
        log_callback(
            f"Dataset assembled: {len(dataset_records)} resolved records → "
            f"{dataset_jsonl_path.name}"
        )

    return {
        "final_report_json": str(final_report_json_path),
        "dataset_jsonl": str(dataset_jsonl_path),
        "dataset_count": len(dataset_records),
        "final_report": fr_data,
    }


def main(
    phase1_jsonl: str,
    output_dir: str,
    org: str,
    repo: str,
    lang: str,
    max_workers: int = 4,
    force_build: bool = False,
    log_callback: Optional[Callable] = None,
    github_token: str = "",
) -> dict:
    _ensure_harness_importable()
    _check_docker()

    if not os.path.isfile(phase1_jsonl):
        raise AuroraPipelineError(f"Phase 1 JSONL not found: {phase1_jsonl}")

    if not check_instance_registry(org, repo, lang, github_token=github_token):
        raise AuroraPipelineError(
            f"No instance registry found for {org}/{repo} (lang={lang}) "
            f"on GitHub repo {_GITHUB_REPO}."
        )

    _import_all_repo_modules(org, repo, lang)

    # Import CliArgs via importlib to avoid triggering harness/__init__.py
    # which cascades into all repo __init__.py files (some are broken).
    _bd = importlib.import_module("multi_swe_bench.harness.build_dataset")
    CliArgs = _bd.CliArgs

    workdir = Path(output_dir) / "phase2_workdir"
    workdir.mkdir(parents=True, exist_ok=True)
    log_dir = workdir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    repo_dir = workdir / "repos"
    repo_dir.mkdir(parents=True, exist_ok=True)

    translated_jsonl = str(workdir / "harness_input.jsonl")
    translated_count = _translate_phase1_jsonl(
        phase1_jsonl, org, repo, lang, translated_jsonl,
    )
    if translated_count == 0:
        raise AuroraPipelineError(
            "No Phase 1 records matched any harness registry range. "
            "Check that the instance registry covers the PR number ranges "
            "in the Phase 1 output."
        )

    if log_callback:
        log_callback(
            f"Phase 2: {translated_count} record(s) translated for harness"
        )

    cli_args = CliArgs(
        mode="instance",
        workdir=workdir,
        raw_dataset_files=[translated_jsonl],
        force_build=force_build,
        output_dir=None,
        specifics=None,
        skips=None,
        repo_dir=repo_dir,
        need_clone=True,
        global_env=None,
        clear_env=True,
        stop_on_error=False,
        max_workers=max_workers,
        max_workers_build_image=max_workers,
        max_workers_run_instance=max_workers,
        run_cmd="",
        test_patch_run_cmd="",
        fix_patch_run_cmd="",
        log_dir=log_dir,
        log_level="INFO",
        log_to_console=True,
        parse_log=True,
        run_log=True,
        human_mode=True,
    )

    if log_callback:
        log_callback(f"Phase 2: {len(cli_args.instances)} instance(s) to process")

    if not cli_args.instances:
        raise AuroraPipelineError(
            "No valid instances created from Phase 1 JSONL. "
            "Check that the dataset contains entries matching the instance registry "
            "and that number_interval fields are set correctly."
        )

    if log_callback:
        log_callback("Phase 2: Building Docker images...")

    cli_args.run_mode_image()
    image_count = len(cli_args.instances)

    if log_callback:
        log_callback(
            f"Phase 2: {image_count} image(s) built, running test instances..."
        )

    cli_args.run_mode_instance_only()

    report_dir = Path(output_dir) / "phase2_reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    final_report_path = report_dir / f"{org}__{repo}_phase2_report.jsonl"

    resolved = 0
    total = 0
    results = []

    for inst in cli_args.instances:
        dep = inst.dependency()
        instance_dir = (
            workdir
            / inst.pr.org
            / inst.pr.repo
            / _INSTANCE_WORKDIR
            / dep.workdir()
        )
        report_path = instance_dir / "report.json"
        total += 1

        if report_path.exists():
            try:
                with open(report_path) as rf:
                    report_data = json.load(rf)
                is_valid = report_data.get("valid", False)
                if is_valid:
                    resolved += 1

                run_r = report_data.get("run_result") or {}
                test_r = report_data.get("test_patch_result") or {}
                fix_r = report_data.get("fix_patch_result") or {}

                try:
                    from ...models.evaluation_executor import _lookup_lht
                    lht = _lookup_lht(inst.pr.org, inst.pr.repo, inst.pr.number)
                except Exception:
                    lht = {}

                results.append({
                    "instance_id": lht.get("instance_id") or inst.pr.id,
                    "tag_start": lht.get("tag_start", ""),
                    "tag_end": lht.get("tag_end", ""),
                    "pr_numbers": lht.get("pr_numbers", ""),
                    "pr_attribution_method": lht.get("pr_attribution_method", ""),
                    "version_scheme": lht.get("version_scheme", ""),
                    "valid": is_valid,
                    "f2p": list(report_data.get("f2p_tests", {}).keys()),
                    "p2p": list(report_data.get("p2p_tests", {}).keys()),
                    "s2p": list(report_data.get("s2p_tests", {}).keys()),
                    "n2p": list(report_data.get("n2p_tests", {}).keys()),
                    "fixed_tests": list(report_data.get("fixed_tests", {}).keys()),
                    "run_passed": run_r.get("passed_count", 0),
                    "run_failed": run_r.get("failed_count", 0),
                    "run_skipped": run_r.get("skipped_count", 0),
                    "test_passed": test_r.get("passed_count", 0),
                    "test_failed": test_r.get("failed_count", 0),
                    "test_skipped": test_r.get("skipped_count", 0),
                    "fix_passed": fix_r.get("passed_count", 0),
                    "fix_failed": fix_r.get("failed_count", 0),
                    "fix_skipped": fix_r.get("skipped_count", 0),
                    "error_msg": report_data.get("error_msg", ""),
                })
            except Exception as exc:
                _logger.warning("Failed to read report for %s: %s", inst.pr.id, exc)
                try:
                    from ...models.evaluation_executor import _lookup_lht
                    lht = _lookup_lht(inst.pr.org, inst.pr.repo, inst.pr.number)
                except Exception:
                    lht = {}
                results.append({
                    "instance_id": lht.get("instance_id") or inst.pr.id,
                    "tag_start": lht.get("tag_start", ""),
                    "tag_end": lht.get("tag_end", ""),
                    "pr_numbers": lht.get("pr_numbers", ""),
                    "valid": False,
                    "error": str(exc),
                })
        else:
            _logger.warning(
                "No report.json at %s for instance %s", instance_dir, inst.pr.id
            )
            try:
                from ...models.evaluation_executor import _lookup_lht
                lht = _lookup_lht(inst.pr.org, inst.pr.repo, inst.pr.number)
            except Exception:
                lht = {}
            results.append({
                "instance_id": lht.get("instance_id") or inst.pr.id,
                "tag_start": lht.get("tag_start", ""),
                "tag_end": lht.get("tag_end", ""),
                "pr_numbers": lht.get("pr_numbers", ""),
                "valid": False,
                "error": "no report generated",
            })

    with open(final_report_path, "w") as f:
        for r in results:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    dataset_result = _assemble_dataset(
        workdir=workdir,
        report_dir=report_dir,
        org=org,
        repo=repo,
        instances=cli_args.instances,
        phase1_jsonl=translated_jsonl,
        log_callback=log_callback,
    )

    if log_callback:
        log_callback(
            f"Phase 2 complete: {resolved}/{total} resolved, "
            f"{image_count} images built"
        )

    return {
        "report_file": str(final_report_path),
        "image_count": image_count,
        "instance_count": total,
        "resolved_count": resolved,
        "results": results,
        "final_report_json": dataset_result["final_report_json"],
        "dataset_jsonl": dataset_result["dataset_jsonl"],
        "dataset_count": dataset_result["dataset_count"],
    }
