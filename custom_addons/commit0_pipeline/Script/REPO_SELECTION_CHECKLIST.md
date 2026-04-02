# Repo Selection Checklist (Commit0 Benchmark)

For each candidate Python repo, check every point below. Any **MUST** that fails = repo is rejected.
Based on Commit0 paper (ICLR 2025, Section 3.1).

## How to Use

1. Pick a candidate GitHub repo
2. Go through each section, mark Pass/Fail
3. If any MUST fails — stop, repo is out
4. If all MUSTs pass and most SHOULDs pass — repo is a good candidate

---

## 1. Language (MUST)

- [ ] 95% Python (check GitHub language bar)
- [ ] No C/C++/Rust wrappers — no `ctypes`, `cffi`, `pybind11`, Cython `.pyx`, `maturin`/`pyo3`
- [ ] No compiled extensions — no `Extension()` in `setup.py`, no native build backends

## 2. Tests (MUST)

- [ ] Uses pytest — `pytest` in deps, has `conftest.py` files
- [ ] 90% test coverage — run `pytest --cov` or check coverage badge
- [ ] Tests finish in <30 minutes on a single CPU
- [ ] No GPU required — no `torch.cuda`, no `@pytest.mark.gpu`

## 3. Documentation (MUST)

- [ ] Has a docs website — not just a README (ReadTheDocs, GitHub Pages, Sphinx, MkDocs)
- [ ] Has a user guide — "Getting Started" / "Tutorial" section exists
- [ ] Has API reference — function signatures documented
- [ ] Type specs in docs — input/output types specified (actual type annotations or parameter tables, not just prose)

## 4. GitHub Quality (SHOULD)

- [ ] >=5,000 stars
- [ ] Not a fork
- [ ] Not archived
- [ ] Not a ML framework, CLI app, or native wrapper (e.g., not tensorflow, youtube-dl, Pillow)

## 5. Project Structure (SHOULD)

- [ ] Clear source directory — `src/<pkg>/` or `<pkg>/` with `__init__.py`
- [ ] Clear test directory — `tests/` or `test/` exists with actual test files
- [ ] Installable — has `pyproject.toml` or `setup.py`
- [ ] `pytest --collect-only` finds tests — tests actually import and collect

## 6. Build (SHOULD)

- [ ] Installs cleanly in Docker — `pip install -e .` works in `python:3.10-slim-bookworm`
- [ ] Dependencies resolve — no broken/unpinned deps
- [ ] No special system packages — no `apt-get install libfoo-dev` needed (or minimal)

## 7. Code Quality (CHECK)

- [ ] All `.py` files parse — no syntax errors in source directory
- [ ] Source and tests are separate — not mixed together in the same directory
- [ ] Logic lives in functions/methods — not in top-level module code
- [ ] Minimal metaprogramming — no heavy `exec()`/`eval()`/dynamic class generation

## 8. Test Reliability (CHECK)

- [ ] Tests are not flaky — same results when run 3 times
- [ ] No network calls in tests — no `requests.get`, `httpx`, `socket` in test files
- [ ] No external services needed — no Redis, PostgreSQL, etc. in test fixtures
- [ ] No filesystem side effects — no writing to absolute paths, no temp dirs without cleanup
- [ ] Test order doesn't matter — passes with `pytest --randomly`

## 9. Size & Complexity (CHECK)

- [ ] Python 3.10+ compatible
- [ ] <100 transitive dependencies
- [ ] No circular imports — between modules in source directory
- [ ] 50-500 public functions — not too trivial, not too massive
- [ ] Functions are non-trivial — not just one-line getters/setters
- [ ] Tests check behavior, not implementation — not asserting on internal state or mocking everything

---

## Quick Evaluation Form

```
Repo: ___________________________
URL:  https://github.com/________
Date: ___________________________
Checked by: _____________________

Category          Result
─────────────────────────────
Language          ☐ Pass  ☐ Fail
Tests             ☐ Pass  ☐ Fail
Docs              ☐ Pass  ☐ Fail
GitHub            ☐ Pass  ☐ Fail
Structure         ☐ Pass  ☐ Fail
Build             ☐ Pass  ☐ Fail
Code Quality      ☐ Pass  ☐ Fail
Reliability       ☐ Pass  ☐ Fail
Size              ☐ Pass  ☐ Fail

VERDICT:  ☐ ACCEPT  ☐ REJECT
```
