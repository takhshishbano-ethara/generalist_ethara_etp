# Kaiju

**Ethara.AI RL Environment · Library Generation from Scratch**

Extends: [Commit0](https://arxiv.org/abs/2412.01769)

---

## What Kaiju Is

Kaiju is a reinforcement learning environment where an agent must write an entire software library from scratch. Given only the documentation, public API interface, and a human-curated test suite, the agent must produce a complete implementation that passes all tests, satisfies linters, and hits coverage targets. Everything runs inside fully dockerized sandboxes with interactive reward signals at each step.

The environment spans **8 programming languages** (Python, Java, Go, Rust, TypeScript, JavaScript, C, C++) with **10,000+ library instances** at full scale. Each instance is a real open-source library, not a toy problem.

**Key result from initial runs**: Test-driven reward signals improve agent performance by +23.5 percentage points (GLM-5: 30.2% to 53.6%, p < 10^-6). Lint reward signals provide zero measurable benefit and can actively harm code quality (the **Lint Paradox**).

---

## Why This Matters for RL

Current code generation environments test isolated functions (HumanEval) or single bug fixes (SWE-bench). These are solved problems. Kaiju gives RL agents a compositional reasoning challenge:

- **Cross-module dependency management**: hundreds of functions calling each other
- **Framework convention adherence**: Django ORM patterns, Flask app factories
- **Type system navigation**: Rust lifetimes, TypeScript generics, Go interfaces
- **Build system integration**: CMake, Maven, Cargo, npm

A successful policy must maintain internal consistency across hundreds of files while satisfying a dense reward surface (test pass rate, lint compliance, compilation success).

| Environment | Tasks | Languages | Granularity | Challenge |
|-------------|-------|-----------|-------------|-----------|
| HumanEval | 164 | 1 | Single function | Write `is_palindrome()` |
| MBPP | 974 | 1 | Single function | Write `count_vowels()` |
| SWE-bench | 2,294 | 1 | Bug fix | Fix one issue |
| SWE-Bench++ | 11,133 | 11 | Bug fix | Fix one issue, multi-language |
| Commit0 | 54 | 1 | From-scratch library | Rebuild 54 Python libraries |
| **Kaiju** | **10,000+** | **8** | **From-scratch library** | **Full library, 8 languages, 3-stage reward loop** |

---

## Original Paper: Commit0

**Title:** Commit0: Library Generation from Scratch
**Authors:** Wenting Zhao, Nan Jiang, Celine Lee, Justin T Chiu, Claire Cardie, Matthias Galle, Alexander M. Rush (Cornell, Cohere)
**arXiv:** [2412.01769](https://arxiv.org/abs/2412.01769) (submitted 2 Dec 2024)
**Category:** cs.SE
**License:** CC-BY-4.0

Commit0 hands an AI system a library specification + API stubs and requires it to fill in the entire implementation. Harder than function-level generation because the agent must satisfy a full test suite across many files and maintain internal consistency.

Reward signals in Commit0:
- Execution feedback from unit tests
- Static analysis / linting
- Coverage reporting

Starting state: API stubs + docstrings, no implementation bodies. Success: library tests pass. Observation: current frontier agents pass some tests but none fully reproduce any library.

---

## What Ethara Extends

| Dimension | Original Commit0 | Kaiju |
|-----------|------------------|-------|
| Language coverage | Python only | Python, Rust, Go, TypeScript, JavaScript, Java, C, C++ |
| Use-mode | Static run | RL training environment |
| Reward surface | Tests + static analysis | Tests + linting + code coverage integrated into the reward loop |
| Packaging | Research harness | Fully dockerized per-library sandboxes tuned for RL rollouts |
| Scale | 54 libraries | 10,000+ instances (target) |
| Data quality | Automated | Human-curated test suites from real open-source libraries |

Kaiju preserves the Commit0 insight (libraries, not functions) and turns it into an RL-grade signal by widening the language matrix and folding coverage into the interactive feedback that the policy sees during rollout.

---

## Environment Design

### Instance Preparation

Each environment instance is a real-world open-source library, prepared through:

1. **Fork & Clone**: Original repository forked to controlled GitHub organization
2. **Stub**: Every function body replaced with a language-appropriate placeholder:
   - Python: `pass` (preserving signatures, types, docstrings, decorators)
   - Go: `_ = "STUB: not implemented"` + zero-value returns
   - Rust: `panic!("STUB: not implemented")`
   - Java: `throw new UnsupportedOperationException("STUB: not implemented")`
   - TypeScript: `throw new Error("STUB")`
   - JavaScript: `throw new Error("STUB")`
   - C: `STUB_PANIC()` macro + abort()
   - C++: `throw std::logic_error("STUB: not implemented")`
3. **Verify**: Stubbed code must still compile/parse (preserves signatures, types, imports)
4. **Package**: Each instance bundled with specification PDF, Dockerfile, setup scripts, human-curated test suite, metadata JSON

The stubbing preserves 100% structural completeness. All function signatures, type annotations, decorators, class hierarchies, and module relationships remain intact. Only the implementation bodies are removed.

### Per-Language Stubbing Intelligence

Each stubber preserves critical language-specific semantics:

- **Python**: Fixed-point (10-iteration) transitive analysis identifies import-time functions (decorators, class-level calls, module init). These are NOT stubbed to preserve importability.
- **Go**: Skips `init()` and `main()`; generates type-correct zero-value returns for all return types
- **Rust**: Smart `impl Trait` returns: `impl Iterator<Item=T>` becomes `std::iter::empty::<T>()`, `impl Display` becomes `String::new()`, `impl Future<Output=T>` becomes `std::future::ready(panic!())`, unknown becomes `loop {}`
- **Java**: Configurable via JSON (preserveJavadoc, stubPrivateMethods, stubConstructors, skipAnnotations)
- **TypeScript/JavaScript**: Two-pass architecture: (1) collect import-time function names via call-graph to fixed point, (2) stub all others with smart type-aware returns

### Reward Loop (3-Stage Pipeline)

```
+----------------------------------------------------------+
|                 3-STAGE REWARD PIPELINE                    |
+--------------+----------------+--------------------------+
|   Stage 1    |    Stage 2     |        Stage 3           |
|   DRAFT      |  LINT REFINE   |     TEST REFINE          |
+--------------+----------------+--------------------------+
| Agent sees   | Agent sees     | Agent sees               |
|   tests      |   lint output  |   test RESULTS           |
| No test      | No test        | Iterates on              |
|   execution  |   execution    |   failures               |
| Override     | Additive       | Additive                 |
|   previous   |   (no override)|   (no override)          |
+--------------+----------------+--------------------------+
| Baseline     | Code quality   | Functional               |
| generation   | refinement     | correctness              |
+--------------+----------------+--------------------------+
```

Each stage runs the agent for up to 3 iterations (configurable). A watchdog monitors activity and kills stalled runs after 900 seconds of inactivity (1800s for Java due to longer build times). After each stage, the full test suite is run and reward signals recorded.

### Execution Backends

The agent operates inside a sandboxed Docker container with full build toolchain. Three backends:

- **Local Docker**: direct container execution
- **Modal**: cloud-based serverless sandboxes
- **E2B**: HTTP-based code interpreter sandboxes

---

## The 8 Languages

| Language | Stubber Technology | Lint Stack | Test Framework | Build System | Status |
|----------|-------------------|------------|----------------|--------------|--------|
| **Python** | `ast` (stdlib) | ruff, pre-commit | pytest | pip/setuptools | Live |
| **Go** | `go/ast` (native binary) | goimports, staticcheck, go vet | go test -json | go build | Live |
| **Java** | `javaparser` (Maven JAR) | Java linters | JUnit/Maven Surefire | Maven | Live |
| **Rust** | `syn v2` (Cargo binary) | rustfmt, clippy | cargo test | Cargo | Live |
| **TypeScript** | `ts-morph` (ts-node) | eslint, prettier | jest/vitest | npm/tsc | Live |
| **JavaScript** | `ts-morph` (shared) | eslint, prettier | jest/mocha | npm | Live |
| **C** | `libclang` (Python bindings) | clang-tidy, clang-format | CMake/CTest | CMake | Live |
| **C++** | `libclang` (shared with C) | clang-tidy, clang-format | CMake/CTest + GTest | CMake | Live |

---

## Results (20 Python Libraries, 2 Models)

### Dataset Profile

| Difficulty | Libraries | Tests | Files | Description |
|-----------|-----------|-------|-------|-------------|
| Easy | 6 | 1,338 | 69 | Small utilities, thin wrappers |
| Medium | 7 | 2,567 | 88 | Framework plugins, multi-module |
| Hard | 7 | 4,069 | 284 | Complex SDKs, deep dependency chains |
| **Total** | **20** | **7,974** | **441** | |

### Headline Numbers

| Metric | GLM-5 | Nova-2-Lite |
|--------|-------|-------------|
| Stage 1 (Draft) pass rate | 30.2% | 15.6% |
| Stage 2 (Lint) pass rate | 31.5% | 18.1% |
| Stage 3 (Test) pass rate | **53.6%** | **31.7%** |
| Best single library | 88.7% (extruct) | 96.6% (sqlmodel) |
| Worst single library | 10.9% (llama_deploy) | 5.2% (graphene-django) |
| Mean cost per library | $3.75 | $2.38 |
| Mean time per library | 1.81 hours | 1.02 hours |
| Total cost | $74.98 | $47.69 |
| Total time | 36.2 hours | 20.4 hours |

Combined: $122.67, 56.5 hours for all 40 runs (20 libraries x 2 models).

### The Test Reward Signal Effect

```
GLM-5:   30.2% -[+1.3pp lint]-> 31.5% -[+22.1pp test]-> 53.6%
Nova-2:  15.6% -[+2.5pp lint]-> 18.1% -[+13.6pp test]-> 31.7%
```

- Stage 1 to Stage 3: Wilcoxon signed-rank test p < 10^-6 (GLM-5) and p < 10^-4 (Nova-2-Lite). Highly significant for both.
- Stage 1 to Stage 2: p = 0.75 (GLM-5), p = 0.38 (Nova-2-Lite). Not significant. Lint reward signals are statistically indistinguishable from noise.

### The Lint Paradox

Lint reward was expected to improve code quality. Instead:
- Global effect: Zero measurable improvement (p > 0.3 for both models)
- Harmful cases: bolt-python dropped -22 percentage points at Stage 2 under GLM-5
- Mechanism: Models refactor to satisfy lint rules but break cross-function state in the process. Renaming variables, splitting functions, and reorganizing imports destroys the fragile dependency web of a freshly-generated library.

Implication: For from-scratch generation, lint reward should be applied AFTER functional correctness is achieved, not before.

### The Complexity Wall

Performance degrades sharply with library size:

| File Count | GLM-5 S3 | Nova-2-Lite S3 |
|-----------|----------|----------------|
| 1-5 files | ~57% | ~43% |
| 6-10 files | ~52% | ~38% |
| 11-20 files | ~47% | ~32% |
| 21-40 files | ~38% | ~25% |
| 41-100 files | ~25% | ~18% |
| 100+ files | ~16% | ~12% |

This reveals a fundamental scaling limitation: as cross-module dependencies grow, models lose coherence. A function in `module_a.py` that calls `module_b.helper()` which depends on `module_c.Config` requires maintaining consistency across context windows.

### Visual Results

#### Figure 1: Stage 3 Pass Rate by Files Affected

![Stage 3 Pass Rate by Files Affected](https://projects.ethara.ai/kaiju_dashboard/static/src/portal/img/chart_combined.png)

Both models show clear performance degradation from ~57% at 1-5 files to ~16% beyond 100 files, a 3.5x collapse.

#### Figure 2: Mean Pass Rate by Model and Stage (Heatmap)

![Mean Pass Rate by Model and Stage](https://projects.ethara.ai/kaiju_dashboard/static/src/portal/img/commit0_table1_heatmap.png)

Test reward (Stage 2 to Stage 3) is the only intervention that produces meaningful improvement. Lint refinement (Stage 1 to Stage 2) shows negligible change.

#### Figure 3: Stage-wise Performance by Difficulty Tier

![Stage Performance by Difficulty](https://projects.ethara.ai/kaiju_dashboard/static/src/portal/img/commit0_fig4_difficulty_stages.png)

Hard libraries remain stubbornly low even after test refinement (GLM-5: 38.9%, Nova: 24.0%), while easy libraries benefit most from the test reward loop (GLM-5: 64.2%, Nova: 45.8%).

### Per-Library Results (Stage 3)

| Library | Tests | GLM-5 S3 | Nova-2 S3 | Best | Difficulty |
|---------|-------|----------|-----------|------|------------|
| sqlmodel | 174 | 54.6% | **96.6%** | 96.6% | Easy |
| extruct | 71 | **88.7%** | 59.2% | 88.7% | Easy |
| apispec | 624 | **74.8%** | 28.0% | 74.8% | Easy |
| flask-cors | 96 | **72.9%** | 39.6% | 72.9% | Easy |
| datadogpy | 287 | **53.7%** | 31.7% | 53.7% | Easy |
| boolean.py | 86 | **40.7%** | 19.8% | 40.7% | Easy |
| flake8 | 444 | **86.3%** | 75.2% | 86.3% | Medium |
| nornir | 115 | **80.0%** | 33.9% | 80.0% | Medium |
| fastapi-users | 581 | **71.3%** | 32.2% | 71.3% | Medium |
| jaxtyping | 279 | **58.4%** | 5.4% | 58.4% | Medium |
| django-simple-history | 338 | **47.6%** | 16.9% | 47.6% | Medium |
| graphene-django | 346 | **39.0%** | 5.2% | 39.0% | Medium |
| vcrpy | 464 | **32.5%** | 22.0% | 32.5% | Medium |
| doctr | 516 | **64.3%** | 26.9% | 64.3% | Hard |
| praw | 955 | **46.4%** | 19.1% | 46.4% | Hard |
| bolt-python | 812 | **44.3%** | 31.2% | 44.3% | Hard |
| docker-py | 1,056 | **35.7%** | 30.0% | 35.7% | Hard |
| pandas-datareader | 189 | **36.0%** | 16.9% | 36.0% | Hard |
| django-unfold | 367 | **34.6%** | 31.3% | 34.6% | Hard |
| llama_deploy | 174 | 10.9% | **12.6%** | 12.6% | Hard |

GLM-5 mean S3: 53.6% | Nova-2-Lite mean S3: 31.7% | Overall best: sqlmodel 96.6% (Nova-2-Lite)

### Cost Efficiency

| Metric | GLM-5 | Nova-2-Lite |
|--------|-------|-------------|
| Cost per test passed | ~$0.018 | ~$0.020 |
| Mean cost per library | $3.75 | $2.38 |
| Median cost per library | $2.09 | $1.51 |
| Max cost (single run) | $14.07 | $13.58 |
| Min cost (single run) | $0.46 | $0.18 |

GLM-5 is 1.57x more expensive on average but achieves 1.69x higher pass rates. The per-test-passed cost ends up nearly identical between models.

---

## Common Failure Modes

Three systematic failure patterns that expose fundamental limitations:

### 1. Cross-Module Dependency Bugs
Models generate functions that call other functions with incorrect signatures, missing parameters, or wrong return types. `module_a.process(data)` generated but `module_b.process(data, config)` implemented. Mismatch invisible within a single file.

### 2. Framework Convention Violations
Django expects `django.db.models.Model` inheritance with specific Meta classes. Flask expects app factory patterns with blueprint registration. Models generate working code that violates these conventions, causing test failures in framework-specific assertions.

### 3. Type System Gaps
Rust lifetime annotations, TypeScript conditional types, Java generics with bounded wildcards. Models default to overly permissive types (`Any`, `interface{}`, `Box<dyn Any>`) that fail type-checking assertions.

---

## Infrastructure

### Architecture

```
+------------------------------------------------------------------+
|                      run_pipeline_<lang>.sh                        |
|              (Orchestrates full RL episode)                        |
+------------------------------------------------------------------+
|                                                                    |
|   +----------+     +----------+     +----------+                  |
|   | Stage 1  |---->| Stage 2  |---->| Stage 3  |                  |
|   |  Draft   |     |   Lint   |     |   Test   |                  |
|   +----+-----+     +----+-----+     +----+-----+                  |
|        |                 |                 |                        |
|        v                 v                 v                        |
|   +----------------------------------------------+                 |
|   |           Agent (aider-chat fork)             |                 |
|   |  Bedrock ARN models (GLM-5, Nova, etc.)      |                 |
|   |  Thinking capture (8 monkey-patches)          |                 |
|   |  Reward signal + cost logging                 |                 |
|   +----------------------+-----------------------+                 |
|                           |                                        |
|                           v                                        |
|   +----------------------------------------------+                 |
|   |         Harness (commit0 package)             |                 |
|   |  Docker/Modal/E2B execution backends          |                 |
|   |  Per-language Spec classes                    |                 |
|   |  Test parsers (go test -json, Surefire XML)   |                 |
|   |  Patch generation + container exec            |                 |
|   +----------------------------------------------+                 |
|                                                                    |
+------------------------------------------------------------------+
```

### Technology Stack

| Layer | Technology | Purpose |
|-------|-----------|---------|
| Orchestration | Bash (run_pipeline_*.sh) | End-to-end pipeline driver with watchdog, cost extraction, pass@k |
| Agent | Python (aider-chat fork) | LLM to codebase interaction, edit application, git commit per turn |
| LLM Backend | AWS Bedrock | GLM-5, Nova-2-Lite, Nova-Premier, Kimi K2.5, MiniMax via inference profiles |
| Harness | Python (commit0 package) | Setup, build, test, score, lint, save lifecycle |
| Execution | Docker / Modal / E2B | Sandboxed code execution with full toolchains |
| Dataset | HuggingFace + local JSON | Instance metadata, test IDs, specifications |
| Stubbers | Per-language native tools | AST-level function body replacement |

### Model Support

Models via AWS Bedrock application inference profiles:

- **GLM-5** (ZhipuAI): Best performer in initial runs
- **Nova-2-Lite** (Amazon): Cost-efficient baseline
- **Nova-Premier** (Amazon): Higher capability tier
- **Kimi K2.5** (Moonshot AI): Extended context window
- **MiniMax M2.5**: Alternative architecture
- **Claude Opus 4** (Anthropic): Via ARN profile mapping

Dynamic pricing registration enables cost tracking per model per run.

---

## Dataset & Resources

### Available Artifacts

| Resource | URL | Contents |
|----------|-----|----------|
| Dataset | [ethara/Kaiju](https://huggingface.co/datasets/ethara/Kaiju) | Full dataset with datacards, instance metadata, human-curated test suites |
| Agent Logs (GitHub) | [Ethara-Ai/kaiju_ots](https://github.com/Ethara-Ai/kaiju_ots) | Agent logs, reward signal outputs, run artifacts |
| Dashboard | [projects.ethara.ai/kaiju](https://projects.ethara.ai/kaiju) | Interactive results, dataset viewer, pipeline visualization |

### Instance Schema

```json
{
  "instance_id": "ethara__sqlmodel",
  "repo": "ethara/sqlmodel",
  "original_repo": "tiangolo/sqlmodel",
  "base_commit": "abc123...",
  "reference_commit": "def456...",
  "setup": {
    "install": "pip install -e .",
    "packages": ["sqlmodel"],
    "pip_packages": ["pytest", "coverage"],
    "pre_install": ["apt-get install -y ..."],
    "python": "3.12",
    "specification": "specs/sqlmodel_spec.pdf"
  },
  "test": {
    "test_cmd": "pytest tests/ -x --timeout=60",
    "test_dir": "tests/"
  },
  "src_dir": "sqlmodel/"
}
```

### Scale Targets

| Milestone | Libraries | Languages | Status |
|-----------|-----------|-----------|--------|
| Paper (v1) | 20 | 1 (Python) | Complete |
| Multi-language pilot | 100+ | 5 (Python, Go, Java, Rust, TS) | In progress |
| Full Kaiju | 10,000+ | 8 (all) | Planned |

---

## Key Contributions

1. **First multi-language from-scratch RL environment**: Extends Commit0 from 1 language to 8
2. **Lint Paradox discovery**: Empirical evidence that lint reward harms from-scratch generation (p > 0.3, sometimes -22pp)
3. **Complexity wall quantification**: Pass rates drop 3.5x from 1-5 files to 100+ files
4. **Cost-performance frontier**: $0.018-0.020 per test passed, enabling economic modeling of AI-assisted development
5. **Open infrastructure**: Entire pipeline (stubbers, harness, agent, reward loop) open for community extension
6. **Reproducibility**: Full agent logs, costs, and reward signals published alongside results

---

## Links

- **Paper:** https://arxiv.org/abs/2412.01769
- **Commit0 (original project):** https://commit-0.github.io/
- **Dashboard:** https://projects.ethara.ai/kaiju
- **GitHub:** https://github.com/Ethara-Ai/kaiju_ots
- **HuggingFace:** https://huggingface.co/datasets/ethara/Kaiju
- **Ethara.AI:** https://ethara.ai

---

## Authors

Suryansh Rana, Sarvex Jatasra, Aditya Pathak, Prafful Gupta, Madhur Parwal, Vaibhav Singh, Aman Yadav, Lalit Kumar Patra, Amrit Raj, Dhawal Bathre, Piyush Agrawal, Shubhi Khandelwal, Aditi Singh Baghel, Krishna Bairagi, Utkarsh Jain, Mirza Anzar Baig

**Ethara AI Labs**

---

## License

Dataset and agent logs: **CC BY-NC-ND 4.0**

Infrastructure (commit0 package, stubbers, pipeline): Open source

---

## RL Training: RLVR with GRPO

### The RLVR Principle

Kaiju environments provide deterministic, verifiable reward signals. Test suites return pass/fail with zero ambiguity. Compilation succeeds or fails. No annotator disagreement, no preference labeling, no reward model drift. This makes Kaiju a natural RLVR (Reinforcement Learning with Verifiable Rewards) setting where the environment IS the reward function.

We train with **GRPO (Group Relative Policy Optimization)** (Shao et al., 2024; Guo et al., 2025), the algorithm behind DeepSeek-R1. GRPO eliminates the critic network and uses within-group advantage normalization over verifiable outcomes.

### Training vs Inference (Critical Distinction)

```
TRAINING (RLVR loop):
  Input:  spec + stubs (single prompt)
  Action: model generates per-FILE patches (file-level granularity, NOT monolithic)
  Verify: compile check per file, then full test suite in Docker sandbox
  Reward: hierarchical (see below)
  Update: GRPO policy gradient with per-file credit assignment

INFERENCE (deployment):
  The trained policy deploys in the 3-stage agentic loop
  where it receives lint/test feedback between iterations.
  This is inference-time compute, not RL training.
```

### Why GRPO for RLVR

1. **The environment supplies the reward directly**. No learned proxy, no neural reward model. The test runner returns an exact scalar.
2. **No value network**. Frees 40-50% VRAM vs PPO. Critical when generating library-scale outputs.
3. **Group normalization handles sparsity**. When all G completions fail (R=0 for all), advantage is zero, batch is skipped. No wasted gradient updates on prompts the policy cannot yet solve.
4. **Proven at code scale**. DeepSeek-R1: Codeforces 2029, LiveCodeBench 65.9% with GRPO on verifiable rewards.

### Key Design Decision: File-Level Decomposition

**Problem**: A full library can be 5,000-50,000 tokens. GRPO is validated on sequences up to ~8K tokens (DeepSeek-R1 code tasks). Treating 50K tokens as a single action creates:
- Credit assignment failure (one broken file poisons the whole reward)
- Importance ratio instability (product of 30K per-token ratios compounds exponentially)
- Diversity collapse (G=16 yields ~4 structural families, not 16 independent attempts)

**Solution**: Decompose generation into per-file actions within a single rollout:

```
For library with N source files:
  1. Model receives: full spec + all stubs + file dependency graph
  2. For each file f_i (in topological order):
     - Generate implementation of f_i (typically 200-2000 tokens)
     - Compile check f_i in isolation (syntax + imports)
     - Record per-file compile status
  3. After all files generated:
     - Execute full test suite
     - Record tests_passed / total_tests
  4. Per-file reward assignment (see Reward Design)
```

This keeps individual generation steps in the 200-2000 token range where GRPO is validated. The importance ratio is computed per-file (not per-library), preventing exponential compounding.

### GRPO Algorithm (as implemented)

```
For each training iteration:
  1. Sample K=8 library instances from current curriculum tier
  2. For each file in each library, generate G=4 completions from pi_theta
     (G=4 per FILE, not per library. Effective diversity is higher because
      different file choices compound across the library structure)
  3. Select best file completion per file via compile check (greedy assembly)
  4. Run assembled library through Docker test suite
  5. Compute file-level rewards (see Reward Design)
  6. For each file-generation group of G=4 completions:
     mu = mean(R_file_1 ... R_file_G)
     sigma = std(R_file_1 ... R_file_G)
     if sigma < 1e-8: skip (degenerate group)
     A_i = (R_file_i - mu) / (sigma + 1e-6)
  7. Policy gradient (per-file level, in log-space for numerical stability):
     L = -E[ min(rho_i * A_i, clip(rho_i, 1-eps, 1+eps) * A_i) ]
       + beta * KL(pi_theta || pi_ref)
     where rho_i = exp(log_pi_theta(file_i | context) - log_pi_old(file_i | context))
     (log-space computation prevents overflow from token-product ratios)
```

### Reward Design: Hierarchical Verifiable Rewards

The reward is hierarchical, providing signal at multiple granularities:

```
LEVEL 1 - Per-file compile (immediate, dense):
  r_compile(f_i) = 1.0 if file compiles in isolation, else 0.0

LEVEL 2 - Cross-module link (intermediate):
  r_link = 1.0 if full library compiles (all imports resolve), else 0.0

LEVEL 3 - Test pass rate (terminal, sparse but informative):
  r_test = tests_passed / total_tests

COMPOSITE FILE REWARD:
  R(f_i) = 0.2 * r_compile(f_i) + 0.1 * r_link + 0.7 * r_test

  - r_compile is per-file: provides gradient even when tests are unreachable
  - r_link gates meaningful test signal: if linking fails, r_test = 0 by necessity
  - r_test dominates: the verifiable outcome we optimize for
  - Lint is excluded (p=0.75 null intervention proven by our own data)
```

**Why hierarchical, not pure terminal?** Because the original design (R = tests_passed/total_tests with R=0 for non-compiling code) has a fatal sparsity problem: on Hard-tier libraries, models produce non-compiling code in 60-80% of attempts. With pure terminal reward, 60-80% of attempts get R=0 and contribute no useful gradient. The per-file compile signal provides dense, verifiable reward that guides the policy toward compilable output FIRST, then toward test-passing output.

### Hyperparameters (Complete Training Recipe)

| Parameter | Value | Rationale |
|-----------|-------|-----------|
| Group size G | 4 per file | Smaller G is feasible because file-level generation is short (200-2K tokens). 4 diverse file implementations × N files = combinatorial diversity at library level |
| Libraries per batch K | 8 | Larger K compensates for smaller G. Diverse prompts provide varied gradient signal |
| Clipping epsilon | 0.2 | Standard (Schulman et al., 2017) |
| KL coefficient beta | 0.01 initial, anneal to 0.04 over 50 iterations | Looser KL early allows exploration; tighten to prevent drift as policy improves |
| Reference policy | SFT checkpoint (frozen) | The supervised model from initial Kaiju runs |
| Learning rate | 1e-6 with cosine decay to 1e-7 over 200 iterations | Conservative; library generation is sensitive to policy shifts |
| Max sequence length | 4,096 tokens per file (truncate + assign R=0 if exceeded) | Prevents OOM. Most library files are under 2K tokens |
| Gradient accumulation | 4 steps | Effective batch = K×4 = 32 libraries worth of file-level gradients |
| Checkpoint frequency | Every 5 iterations | At ~4h/iter, losing a checkpoint costs 20h |
| Total iterations | 200 (Easy) + 150 (Medium) + 100 (Hard) = 450 | Diminishing returns per tier |
| Generation timeout | 120s per file, 1800s per library | Hard kill. R=0 for timed-out files |
| Docker test timeout | 600s per library | Matches existing commit0 evaluate timeout |
| OOM fallback | Truncate to 4096 tokens, assign R=0, log for analysis | Never crash the training loop |

### Curriculum Strategy

| Phase | Libraries | Duration | Transition Criterion |
|-------|-----------|----------|---------------------|
| Warm-up | Easy (6 libs: apispec, boolean.py, datadogpy, extruct, flask-cors, sqlmodel) | 200 iters | Mean R > 0.5 across Easy tier for 10 consecutive evals |
| Main | Medium (7 libs: django-simple-history, fastapi-users, flake8, graphene-django, jaxtyping, nornir, vcrpy) | 150 iters | Mean R > 0.3 across Medium tier for 10 consecutive evals |
| Hardening | Hard (7 libs: bolt-python, django-unfold, docker-py, doctr, llama_deploy, pandas-datareader, praw) | 100 iters | Training budget exhausted |

**Transition is gated, not scheduled.** If Easy doesn't converge in 200 iterations, we don't move to Medium. The criterion (mean R > threshold for 10 consecutive evals) ensures the policy has stable capability before encountering harder instances.

### Two-Phase Training Architecture

```
+------------------+      +-------------------+      +-----------------+
| GENERATION PHASE |      | VERIFICATION      |      | UPDATE PHASE    |
|                  |      |                   |      |                 |
| vLLM inference   | ---> | Docker sandbox    | ---> | DeepSpeed ZeRO-3|
| (tensor parallel,|      | (Ray workers,     |      | (gradient acc., |
|  G=4 per file,   |      |  1 container per  |      |  all-reduce,    |
|  files sequenced)|      |  library assembly,|      |  clip+KL loss)  |
| policy weights   |      |  parallel test    |      | policy weights  |
| loaded READ-ONLY |      |  execution)       |      | UPDATED         |
+------------------+      +-------------------+      +-----------------+
        ^                                                    |
        |                                                    |
        +----------- sync updated weights ------------------+
```

**Wall-clock estimate per iteration:**
- Generation: K=8 libraries × avg 10 files × G=4 × ~30s/file = ~2.5h (parallelized across 4 GPUs via vLLM tensor parallel)
- Verification: 8 Docker containers in parallel, ~10 min each = ~15 min total
- Update: gradient computation + sync = ~20 min
- **Total: ~3-4 hours per iteration**


### Inference-Time Scaling Comparison

Before committing to RLVR training, we validated that training-time optimization beats pure inference-time scaling:

| Method | Compute Budget | Mean S3 (Easy) | Mean S3 (All) |
|--------|---------------|----------------|---------------|
| Base SFT (no RL) | 1x | 64.2% | 53.6% |
| Best-of-4 (inference) | 4x | 71.8% | 58.3% |
| Best-of-16 (inference) | 16x | 76.1% | 61.9% |
| GRPO-trained + 3-stage deploy | 1x train + 1x infer | 79.4% | 65.2% |
| GRPO-trained + best-of-4 | 1x train + 4x infer | 84.7% | 70.1% |

**Key insight:** RLVR training and inference-time scaling are complementary, not competing. Training improves the base distribution; inference-time compute samples from that improved distribution. The combination dominates either alone.

### Feasibility Validation (Completed Before Full Training)

We ran a 2-week spike before committing to full training:

1. **10 iterations on Easy tier only** (boolean.py + flask-cors + extruct)
2. **Measured**: wall-clock (3.2h/iter actual), gradient norms (stable at 0.01-0.1), reward distribution (sigma > 0 in 85% of file-level groups), diversity (3.1 unique structural approaches per G=4 group)
3. **Validated**: per-file decomposition produces non-degenerate advantages in >80% of batches (vs <40% with monolithic generation)
4. **Confirmed**: learning curve shows monotonic improvement from iter 1-10 on Easy tier (mean R: 0.42 -> 0.58)

Without this spike, we would not have committed to the full 450-iteration budget.

### Why Not PPO / DPO / REINFORCE?

| Method | Rejected | Reason |
|--------|----------|--------|
| PPO | Yes | Value network doubles VRAM. At library scale, the value function cannot accurately estimate returns (most states look identical until tests run). Per-file decomposition helps but doesn't fully solve the value estimation problem. |
| DPO | Yes | Requires preference pairs. Cannot consume verifiable scalar rewards directly. Converting test scores to pairwise preferences loses information and introduces noise. |
| REINFORCE | Yes | Extremely high variance without baseline at file-level granularity. GRPO's group normalization provides a natural, adaptive baseline. |
| RLOO | Partial | Leave-one-out baseline is similar in spirit. Less validated at scale. GRPO's formulation is directly inherited from DeepSeek-R1. |

### Training Outcomes

After GRPO RLVR training on the full Kaiju environment (450 iterations) 

- **Warm-up convergence**: Non-trivial file-level advantages emerge within 15 iterations on Easy tier. Per-file compile reward provides gradient signal from iteration 1. boolean.py and flask-cors converge first (fewer files, immediate signal).
- **Curriculum gating**: Easy threshold met at iteration 142. Medium threshold met at iteration 289. Hard tier ran full 100 iterations without convergence (as expected for 36+ file libraries).
- **KL stability**: beta annealing (0.01 -> 0.04) maintained policy within 0.15 nats of SFT reference. No mode collapse.
- **Inference-time deployment**: GRPO-trained policy, deployed in 3-stage agentic loop, shows improved first-draft quality (S1 pass rate increases), which compounds through Stages 2 and 3.
- **Cost**: ~$18,000 total (compute: $14K generation, $2K verification, $2K gradient updates). ROI positive given 12pp improvement on held-out libraries at standard inference budget.

### References

- Shao et al. (2024). DeepSeekMath: Pushing the Limits of Mathematical Reasoning in Open Language Models. arXiv:2402.03300
- Guo et al. (2025). DeepSeek-R1: Incentivizing Reasoning Capability in LLMs via Reinforcement Learning. arXiv:2501.12948
- Le et al. (2022). CodeRL: Mastering Code Generation through Pretrained Models and Deep Reinforcement Learning. NeurIPS 2022.
- Liu et al. (2023). RLTF: Reinforcement Learning from Unit Test Feedback. arXiv:2307.04349
- Li et al. (2026). Exploring Pass-Rate Reward in Reinforcement Learning for Code Generation. arXiv:2605.02944
- Schulman et al. (2017). Proximal Policy Optimization Algorithms. arXiv:1707.06347
