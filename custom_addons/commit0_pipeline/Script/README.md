# Commit0 Automation Pipeline

Fully automated GitHub repository discovery, filtering, and processing pipeline for the [Commit0](https://github.com/commit-0) benchmark.

## Overview

A single-file, zero-config Python pipeline that finds high-quality pure-Python repositories on GitHub, validates them against Commit0 benchmark criteria, and produces structured YAML configs + documentation PDFs — all uploaded to Google Drive. The script bootstraps its own virtual environment and installs all dependencies automatically.

## Features

### Discovery & Filtering
- Star-range partitioned GitHub search across 20 star brackets (100k+ down to 500+)
- Two-stage filtering: structural checks (10+ criteria) → Commit0 benchmark validation
- Targets 50 pure-Python repos with docs, tests, and proper project structure
- Up to 20 discovery rounds with automatic exclusion of previously seen repos

### PDF Documentation
- 3-layer fallback: direct download → Playwright BFS crawl (30 pages) → README fallback
- Both raw PDF and bz2-compressed versions retained
- Progressive wait strategies (`networkidle` → `domcontentloaded` → `load`) for heavy SPAs

### YAML Generation`
- Auto-detects `src_dir`, `test_dir`, `test_cmd`, `python_version`, `install_cmd`
- Per-repo `.yml` files + consolidated `repos.yml`
- Commit SHA/tag pinning for reproducibility

### Google Drive Upload
- OAuth2 authentication with token caching
- Thread-safe parallel uploads (thread-local service objects for macOS SSL safety)
- Chunked resumable uploads with retry and exponential backoff

### Infrastructure
- Self-bootstrapping venv — auto-installs all dependencies + Playwright Chromium
- Dual logging: console + timestamped log file
- Unicode-aware table formatting for aligned terminal output (handles emoji/CJK)
- State tracking via `processed_repos.json` for resumability

## Prerequisites

- Python 3.10+
- Git
- GitHub personal access token (set as `GITHUB_TOKEN` environment variable)
- *(Optional)* Google Cloud OAuth credentials for Drive upload (`credentials.json`)

## Quick Start

```bash
export GITHUB_TOKEN="ghp_..."
python3 commit0_automation.py
```

That's it. The script will:
1. Create a virtual environment (`.venv_commit0`)
2. Install all dependencies (`requests`, `PyYAML`, `PyMuPDF`, `playwright`, etc.)
3. Install Playwright Chromium
4. Re-execute itself inside the venv
5. Run the full pipeline end-to-end

## Pipeline Steps

| Step | Name | Description |
|------|------|-------------|
| 0 | **Bootstrap** | Creates venv, installs dependencies, re-execs inside venv |
| 1 | **Discovery** | Star-range partitioned GitHub search (up to 20 rounds) |
| 2 | **Filter 1** | Structural validation — 10+ parallel checks |
| 3 | **Filter 2** | Commit0 benchmark validation (pytest, no GPU, installable, etc.) |
| 4 | **Clone** | Shallow clone (`--depth 1`) of selected repos |
| 5 | **Build Dataset** | Sets up `commit-0/build_dataset` tooling |
| 6 | **PDF Generation** | 3-layer fallback documentation capture |
| 7 | **YAML Generation** | Auto-detect project metadata and write configs |
| 8 | **Drive Upload** | Parallel upload to Google Drive |
| 9 | **Summary** | Rich terminal tables with per-repo status |

## Configuration

Key constants defined at the top of the script:

| Constant | Default | Description |
|---|---|---|
| `TARGET_REPO_COUNT` | 50 | Target number of repos to discover |
| `MIN_FILTER2_PASS` | 3 | Minimum repos passing Filter 2 before accepting results |
| `MAX_RETRY_ROUNDS` | 20 | Maximum discovery rounds |
| `FILTER_WORKERS` | 8 | Parallel filter threads |
| `CLONE_WORKERS` | 8 | Parallel clone threads |
| `YAML_WORKERS` | 6 | Parallel YAML generation threads |
| `PDF_WORKERS` | 3 | Parallel PDF generation threads |
| `UPLOAD_WORKERS` | 3 | Parallel Drive upload threads |

## Output Structure

```
output/
├── raw_repos.jsonl              # Raw GitHub search results
├── filtered_stage1.json         # Repos passing Filter 1
├── final_selected.json          # Repos passing Filter 2
├── repos.yml                    # Consolidated YAML config
├── <org>_<repo>.yml             # Per-repo YAML files
└── pdf/
    └── <org>_<repo>/
        ├── <repo>.pdf           # Raw documentation PDF
        └── <repo>.pdf.bz2      # Compressed PDF

logs/
└── commit0_YYYYMMDD_HHMMSS.log # Timestamped run log
```

## Google Drive Setup

To enable automatic upload to Google Drive:

1. Create a project in [Google Cloud Console](https://console.cloud.google.com/)
2. Enable the **Google Drive API**
3. Create **OAuth 2.0 credentials** (application type: Desktop)
4. Download the credentials file as `credentials.json` into the script directory
5. On first run, a browser window opens for authentication — subsequent runs use the cached `token.json`

Drive folder structure created automatically:

```
Automation Scripts Data/
└── <YYYY-MM-DD>/
    ├── Raw Data/       # YAML configs and metadata
    └── PDF/            # Documentation PDFs and bz2 archives
```

## Filter Criteria

### Filter 1 — Structural Validation

- Not a fork or archived repository
- Not an ML framework, CLI tool, or native wrapper (keyword blocklist)
- Repository size < 500 MB
- Stars ≥ 3,000 (fallback threshold)
- Python language ratio ≥ 95%
- No native C extensions (`ext_modules` / `cythonize` in build configs)
- Has documentation website (homepage, ReadTheDocs, GitHub Pages, or `docs/` directory)
- Proper project structure: build config + tests + source/package directory
- Valid Python syntax in sampled files

### Filter 2 — Commit0 Benchmark Validation

**Required (must pass all):**
- Uses `pytest` as test framework
- No GPU usage (`torch.cuda`, `tensorflow` GPU ops, etc.)

**Recommended (must pass ≥ 2):**
- Installable via `pip` (has `setup.py`, `setup.cfg`, or `pyproject.toml`)
- Stars ≥ 5,000
- Good project structure score
- Python 3.10+ compatible

**Additional checks:**
- Dependency count < 100
- Test isolation (no shared mutable state across test files)

## License

MIT
