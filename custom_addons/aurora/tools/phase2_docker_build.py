import importlib
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Optional

from .util import AuroraPipelineError

_logger = logging.getLogger(__name__)

MULTI_SWE_BENCH_ROOT = Path(__file__).resolve().parents[4] / "multi-swe-bench"

_HARNESS_REPOS_ROOT = MULTI_SWE_BENCH_ROOT / "multi_swe_bench" / "harness" / "repos"

_INSTANCE_WORKDIR = "instances"

_RANGE_RE = re.compile(r"^(.+)_(\d+)_to_(\d+)$")


def _ensure_harness_importable():
    root = str(MULTI_SWE_BENCH_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)

    # Pre-register harness packages in sys.modules so Python doesn't execute
    # their __init__.py files (which have broken cross-language imports).
    # We only need targeted leaf-module imports, not the full repo tree.
    if "multi_swe_bench" in sys.modules:
        return

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


def _resolve_org_dir(lang_dir: Path, org: str) -> Optional[Path]:
    org_dir = lang_dir / org
    if org_dir.is_dir():
        return org_dir
    for candidate in lang_dir.iterdir():
        if candidate.is_dir() and candidate.name.lower() == org.lower():
            return candidate
    return None


def check_instance_registry(org: str, repo: str, lang: str) -> bool:
    _ensure_harness_importable()
    lang_dir = _HARNESS_REPOS_ROOT / lang
    if not lang_dir.is_dir():
        _logger.warning("Language directory not found: %s", lang_dir)
        return False

    org_dir = _resolve_org_dir(lang_dir, org)
    if org_dir is None:
        _logger.warning("Org directory not found under: %s", lang_dir)
        return False

    repo_lower = repo.lower().replace("-", "_")
    for candidate in org_dir.iterdir():
        if candidate.suffix != ".py" or candidate.name == "__init__.py":
            continue
        stem_lower = candidate.stem.lower()
        if stem_lower == repo_lower or stem_lower.startswith(f"{repo_lower}_"):
            _logger.info("Instance registry found: %s", candidate)
            return True

    _logger.warning("No registry files matching '%s' in %s", repo, org_dir)
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
    """Translate Aurora Phase 1 JSONL into harness-compatible format.

    Phase 1 outputs records where 'number' is a hyphen-separated string
    of PR numbers (e.g. "2500-2501") for tag-group bundles.  The harness
    PullRequest dataclass expects 'number' as int and uses
    'number_interval' for versioned registry lookup.

    Returns the number of records written.
    """
    ranges = _build_number_interval_map(org, repo, lang)
    written = 0

    with open(phase1_jsonl, "r") as fin, open(output_path, "w") as fout:
        for line in fin:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue

            raw_number = record.get("number", "")
            if isinstance(raw_number, int):
                primary_number = raw_number
            else:
                parts = str(raw_number).split("-")
                try:
                    primary_number = int(parts[0])
                except (ValueError, IndexError):
                    _logger.warning("Cannot parse PR number from: %s", raw_number)
                    continue

            record["number"] = primary_number

            if ranges:
                interval = _find_interval_for_pr(primary_number, ranges)
                if interval:
                    record["number_interval"] = interval
                else:
                    _logger.warning(
                        "PR #%d doesn't fall in any registry range, skipping",
                        primary_number,
                    )
                    continue
            else:
                record.setdefault("number_interval", "")

            record.setdefault("tag", "")
            record.setdefault("lang", lang)

            fout.write(json.dumps(record, ensure_ascii=False) + "\n")
            written += 1

    _logger.info(
        "Translated %d records from Phase 1 JSONL into harness format", written
    )
    return written


def main(
    phase1_jsonl: str,
    output_dir: str,
    org: str,
    repo: str,
    lang: str,
    max_workers: int = 4,
    force_build: bool = False,
    log_callback: Optional[callable] = None,
) -> dict:
    _ensure_harness_importable()
    _check_docker()

    if not os.path.isfile(phase1_jsonl):
        raise AuroraPipelineError(f"Phase 1 JSONL not found: {phase1_jsonl}")

    if not check_instance_registry(org, repo, lang):
        raise AuroraPipelineError(
            f"No instance registry found for {org}/{repo} (lang={lang}). "
            f"Expected at: {_HARNESS_REPOS_ROOT}/{lang}/{org}/{{repo}}*.py"
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
                results.append({
                    "instance_id": inst.pr.id,
                    "valid": is_valid,
                    "f2p": list(report_data.get("f2p_tests", {}).keys()),
                    "p2p": list(report_data.get("p2p_tests", {}).keys()),
                    "s2p": list(report_data.get("s2p_tests", {}).keys()),
                    "n2p": list(report_data.get("n2p_tests", {}).keys()),
                    "fixed_tests": list(report_data.get("fixed_tests", {}).keys()),
                    "error_msg": report_data.get("error_msg", ""),
                })
            except Exception as exc:
                _logger.warning("Failed to read report for %s: %s", inst.pr.id, exc)
                results.append({
                    "instance_id": inst.pr.id,
                    "valid": False,
                    "error": str(exc),
                })
        else:
            _logger.warning(
                "No report.json at %s for instance %s", instance_dir, inst.pr.id
            )
            results.append({
                "instance_id": inst.pr.id,
                "valid": False,
                "error": "no report generated",
            })

    with open(final_report_path, "w") as f:
        for r in results:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

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
    }
