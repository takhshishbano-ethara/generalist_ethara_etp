# Harness Authoring Handbook

A quickstart for adding a new harness so the Aurora evaluation pipeline can run tests for a GitHub repository.

> **Who this is for**: developers who see *"Missing Harness Registries: `org/repo`"* on a failed evaluation and need to author the missing harness file.

---

## 1. What is a harness?

A harness is a Python file that tells Aurora **how to build a Docker image for a repo and run its tests** for a given PR. Each harness:

1. Registers itself under `@Instance.register("org", "repo")`.
2. Declares a base Docker image + setup script.
3. Emits shell scripts that: check out the PR base commit, run tests, apply patches, re-run tests.
4. Parses the test output and returns a `TestResult`.

Harnesses live in `custom_addons/aurora/tools/harness/repos/<language>/<org>/<repo>.py`. When authored via the UI, they first go to **staging** (pod-local, per developer) before being promoted to production.

---

## 2. Inputs you must provide

| Input | Where it comes from | Example |
|---|---|---|
| **Org + Repo** | GitHub URL `github.com/<org>/<repo>` | `stretchr`, `testify` |
| **Language** | Repo's primary language | `python`, `golang`, `java`, `typescript`, … |
| **Base Docker image** | Official language image on Docker Hub | `python:3.11-slim`, `golang:latest`, `node:20` |
| **Test command** | The repo's standard `CONTRIBUTING.md` or CI config | `pytest`, `go test ./...`, `npm test` |
| **Extra setup** | Whatever the repo needs before tests run | `pip install -e .`, `go mod download`, `npm ci` |
| **Test-log regex** | How the test runner formats pass/fail lines | `--- PASS: (\S+)` for Go, `PASSED (.*)` for pytest |
| **Dataset file** (for staging) | `aurora_output/<org>__<repo>/<org>__<repo>_dataset.jsonl` from a completed pipeline | auto-filled from Source Pipeline |

---

## 3. File structure (three classes)

Every harness file defines **three classes** and ends with an `@Instance.register(...)` decorator:

```python
# Imports
from odoo.addons.aurora.tools.harness.image import Config, File, Image
from odoo.addons.aurora.tools.harness.instance import Instance, TestResult
from odoo.addons.aurora.tools.harness.pull_request import PullRequest


# 1) Base image \u2014 pulls the language toolchain + clones the repo once
class MyRepoImageBase(Image):
    def dependency(self) -> str: ...        # Docker base, e.g. "golang:latest"
    def image_tag(self) -> str: ...         # e.g. "base"
    def workdir(self) -> str: ...           # e.g. "base"
    def files(self) -> list[File]: ...      # files to COPY into this layer
    def dockerfile(self) -> str: ...        # full Dockerfile string

# 2) Per-PR image \u2014 layers PR-specific scripts on top of base
class MyRepoImageDefault(Image):
    def dependency(self) -> Image: ...      # returns MyRepoImageBase(...)
    def image_tag(self) -> str: ...         # e.g. f"pr-{self.pr.number}"
    def workdir(self) -> str: ...
    def files(self) -> list[File]: ...      # run.sh, test-run.sh, fix-run.sh, etc.
    def dockerfile(self) -> str: ...

# 3) Registered Instance \u2014 the entrypoint
@Instance.register("my-org", "my-repo")
class MyRepo(Instance):
    def dependency(self) -> Image: ...      # returns MyRepoImageDefault(...)
    def run(self) -> str: ...               # command that runs tests unchanged
    def test_patch_run(self) -> str: ...    # command after applying test.patch
    def fix_patch_run(self) -> str: ...     # command after applying test+fix patches
    def parse_log(self, log: str) -> TestResult: ...  # PASS/FAIL extraction
```

---

## 4. The three commands Aurora calls

For each PR, Aurora runs **three Docker invocations** in sequence and captures stdout+stderr into log files:

| Method | What should run | Expected result |
|---|---|---|
| `run()` | Tests on the **base commit** (unpatched) | Some tests pass, target tests don't yet exist |
| `test_patch_run()` | Apply `test.patch` then run tests | **Target tests FAIL** (bug not fixed yet) |
| `fix_patch_run()` | Apply `test.patch` + `fix.patch`, then run tests | **Target tests PASS** (bug fixed) |

Aurora compares the three logs via `parse_log()` and marks the PR *resolved* if target tests went `FAIL → PASS`.

The standard pattern is to emit three shell scripts (`run.sh`, `test-run.sh`, `fix-run.sh`) in `files()` and have each method return `"bash /home/<script>.sh"`.

---

## 5. `parse_log()` — the regex you must tune

You receive the raw stdout/stderr from the test run. You must return a `TestResult(passed_count, failed_count, skipped_count, passed_tests, failed_tests, skipped_tests)`.

| Test framework | Pass regex | Fail regex |
|---|---|---|
| `go test -v` | `--- PASS: (\S+)` | `--- FAIL: (\S+)` |
| `pytest -v` | `PASSED (.+)` | `FAILED (.+)` |
| `mvn test` (JUnit) | `Tests run: \d+.*? in (\S+)` | `<<< FAILURE! in (\S+)` |
| `jest` | `\u2713 (.+)` | `\u2717 (.+)` |

**Rule**: a test name must only ever appear in ONE set. If a test passes then later fails (flaky), prefer the `fail` set.

---

## 6. Reference template

The cleanest reference is [`custom_addons/aurora/tools/harness/repos/golang/istio/istio.py`](custom_addons/aurora/tools/harness/repos/golang/istio/istio.py). For a different language, copy it and change only:

1. The three class names
2. `dependency()` \u2192 the language base image
3. The shell scripts in `files()` \u2192 the test commands for that toolchain
4. `@Instance.register(...)` \u2192 your org/repo
5. `parse_log()` regexes

A working concise example is `registry_file_manually_vreated_to_be_uploaded_to_ui/testify.py` (Go, stretchr/testify).

---

## 7. Generating the harness with an AI agent (recommended shortcut)

Writing a harness by hand is mechanical: 90% of the file is identical to an existing same-language harness, and only the test command + setup lines change. Hand this job to an AI coding agent (Claude, Cursor, Copilot, etc.) with **two inputs**:

### Input 1 — A reference harness in the same language

Pick the closest existing file from `custom_addons/aurora/tools/harness/repos/<language>/`. Prefer a repo that uses the same test framework as yours. Quick picks:

| Language | Good starting reference |
|---|---|
| `python` (pytest) | `repos/python/psf/requests.py`, `repos/python/pallets/flask.py` |
| `golang` (`go test`) | `repos/golang/istio/istio.py`, `repos/golang/spf13/cobra.py` |
| `java` (maven) | `repos/java/…` (pick the closest Maven project) |
| `typescript` / `javascript` (jest/vitest) | `repos/typescript/…` |
| `rust` (`cargo test`) | `repos/rust/…` |

### Input 2 — The dataset file for the missing repo

Produced by the collect pipeline at:

```
aurora_output/<org>__<repo>/<org>__<repo>_dataset.jsonl
```

The AI uses it to: (a) confirm the `org`/`repo` values, (b) sample a couple of `fix_patch` / `test_patch` entries to infer the test framework and file layout, (c) detect which test files the project modifies most.

### Prompt template

Paste this into the agent, attaching both files:

```
You are writing a new harness for the Aurora evaluation pipeline.

ATTACHED:
  1. Reference harness (same language): <paste path / file contents>
  2. Dataset for the missing repo: <paste path / file contents>

TASK:
  Produce a single Python file modeled EXACTLY on the reference harness,
  but adapted for `<ORG>/<REPO>`.

REQUIREMENTS:
  - Change the three class names to `<Repo>ImageBase`, `<Repo>ImageDefault`, `<Repo>`.
  - Change `@Instance.register("<ORG>", "<REPO>")` to match the dataset.
  - Pick a base Docker image appropriate for the repo's language version
    (check the repo's go.mod / pyproject.toml / package.json if known).
  - Keep the shell scripts (run.sh, test-run.sh, fix-run.sh) but update
    the actual test command to what the repo's CI uses. Look at one of
    the attached PRs' test_patch to infer the test framework.
  - Keep `parse_log()` regex if the reference already uses the same
    test framework; otherwise update to the correct PASS/FAIL pattern.
  - Do NOT invent fields that aren't in the reference.
  - Output: a single .py file ready for upload, under 100KB.

Validate yourself against these rules:
  - @Instance.register decorator present
  - Instance class has: run, test_patch_run, fix_patch_run, parse_log
  - Image class has: dependency, files, dockerfile
  - File is valid Python (no syntax errors)
```

### After the agent produces the file

1. Eyeball it — does `dependency()` return a plausible base image? Are the shell scripts running the right test command?
2. Upload via **Aurora → Harness Staging**. The AST validator catches structural mistakes immediately.
3. Click **Test Harness** on 3–4 PRs. The test log tells you exactly what to fix.
4. Iterate: paste the failing test log back to the agent with "fix this" — usually a missing apt package or wrong build step.

Most harnesses reach "tested" status in 2–3 iterations this way.

---

## 8. Upload workflow (UI path)

1. Odoo \u2192 **Aurora \u2192 Harness Staging \u2192 New**
2. Fill **GitHub Org**, **GitHub Repo**, **Language**
3. **Source Pipeline** \u2192 pick the completed collect pipeline (auto-fills dataset file)
4. **Harness File** \u2192 upload your `.py`
5. **Save** \u2192 AST validation runs (checks for `@Instance.register`, required methods)
6. **Test Harness** \u2192 runs mini-eval with 3\u20134 PRs (~3 minutes)
7. If green \u2192 **Run Full Evaluation** on the rest of the dataset
8. Review report \u2192 **Notify Admin** \u2192 Admin commits file to `tools/harness/repos/<lang>/<org>/<repo>.py` and redeploys
9. On redeploy, staging file is cleaned up and stage becomes `deployed`

**Shortcut from the evaluation form**: when an evaluation fails with *"Missing Harness Registries"*, click the **Upload Harness** button in the red alert. The staging form opens with org, repo, pipeline, and dataset already filled in.

---

## 9. Validation rules (enforced on upload)

- File must end in `.py` and be \u2264 100 KB
- Must parse as valid Python (AST check)
- Must contain `@Instance.register(...)` decorator
- The `Instance` subclass must define: `run`, `test_patch_run`, `fix_patch_run`, `parse_log`
- The `Image` subclass must define: `dependency`, `files`, `dockerfile`
- Only **one active** staging record per `(org, repo)` across all users

---

## 10. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `No instances matched for <org>/<repo>` | `@Instance.register` key typo or mismatch with dataset | Ensure decorator args exactly match dataset `org`/`repo` |
| `Docker build failed` during Test Harness | Missing apt packages or wrong base image | Add packages via `extra_packages()` or pick a richer base image |
| All three logs identical | Scripts don't actually apply patches | Verify `git apply /home/test.patch` runs inside each script |
| 0 resolved, many unresolved | Base image's toolchain version is too new/old for old PRs | Use a version-pinned base (e.g. `python:3.9`, `golang:1.19`) or register interval-based variants |
| Test takes >10 minutes per PR | Full test suite instead of targeted tests | Limit to the modified test files; use `get_modified_files(self.pr.test_patch)` helper |

---

## 11. Version-specific variants (advanced)

If a repo's test setup changed across versions, register multiple harnesses using PR-number intervals:

```python
@Instance.register("my-org", "my-repo_0_to_5000")      # old PRs
class MyRepoOld(Instance): ...

@Instance.register("my-org", "my-repo_5001_to_99999")  # new PRs
class MyRepoNew(Instance): ...
```

Aurora picks the matching interval automatically via `Instance.create()` based on `pr.number`. See `golang/kubernetes/` or `python/pypa/` for real examples.

---

## Appendix: `PullRequest` fields available to your harness

| Field | Type | Use |
|---|---|---|
| `pr.org`, `pr.repo` | `str` | GitHub coordinates |
| `pr.number` | `int` | PR number |
| `pr.base.sha` | `str` | Commit to `git checkout` |
| `pr.base.ref` | `str` | Branch name (e.g. `main`) |
| `pr.fix_patch` | `str` | Unified diff of the fix |
| `pr.test_patch` | `str` | Unified diff of the new tests |
| `pr.resolved_issues` | `list[ResolvedIssue]` | Linked issues with title/body |
| `pr.title`, `pr.body` | `str` | PR metadata |

Good luck \u2014 start from `testify.py`, tweak for your repo, upload, iterate.
