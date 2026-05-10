# Tesseract

**Ethara.AI RL Environment · Multimodal + Multi-Language Software Engineering Benchmark**

Extends: [SWE-bench Multimodal](https://arxiv.org/abs/2410.03859)

---

## Executive Summary

Tesseract is a **multi-language, execution-validated benchmark and RL environment** for evaluating autonomous coding agents on visually grounded software engineering tasks. It extends the Princeton/Stanford/Meta SWE-bench Multimodal benchmark (originally limited to 17 JavaScript repositories) into **8 languages across 128 real-world repositories** with **10,000 task instances** and fully containerized execution infrastructure.

Each instance is a real GitHub issue bundled with visual context (screenshots, UI renders, data visualisations) that an agent must interpret alongside the codebase to produce a correct patch. Resolution is verified by **execution**, not LLM judgment.

---

## Why Tesseract Matters

### The Problem
Current coding benchmarks either:
- Lack visual context (SWE-bench original), missing the ~30% of issues where images are *part of the specification*
- Are JavaScript-only (SWE-bench Multimodal), not representative of real enterprise codebases
- Use LLM-as-judge, unreliable, non-reproducible evaluation

### The Solution
Tesseract provides:
- **Cross-modal reasoning**: Visual context + code → verified patch
- **Multi-language coverage**: Python, TypeScript, JavaScript, Go, C, C++, Rust, Java
- **Execution-validated evaluation**: fail-to-pass and pass-to-pass test oracles
- **Containerized reproducibility**: Every instance runs in an isolated Docker environment
- **RL-ready reward signal**: Binary execution outcome wired directly into the policy loop

**For frontier labs:** Tesseract is the first execution-verified multimodal coding benchmark spanning multiple languages where images are *necessary* to solve the issue.

---

## Foundation: SWE-bench Multimodal (Original Paper)

**Title:** SWE-bench Multimodal: Do AI Systems Generalize to Visual Software Domains?  
**Authors:** John Yang, Carlos E. Jimenez, Alex L. Zhang, Kilian Lieret, Joyce Yang, Xindi Wu, Ori Press, Niklas Muennighoff, Gabriel Synnaeve, Karthik R. Narasimhan, Diyi Yang, Sida I. Wang, Ofir Press  
**Affiliations:** Princeton / Stanford / Meta  
**arXiv:** [2410.03859](https://arxiv.org/abs/2410.03859) (October 2024)  
**License:** CC-BY-4.0

### Core Contribution

Extends SWE-bench into the visual domain. Each task requires the agent to *look at an image* (UI screenshot, data-viz render, diagram) to understand the problem and produce the correct patch. Evaluation is execution-verified, not subjective.

### Original Benchmark Stats

| Dimension | Value |
|-----------|-------|
| Task instances | 617 |
| Repositories | 17 JavaScript libraries |
| Domains | Web UI, diagramming, data-visualisation, syntax highlighting, mapping |
| Visual content | ≥1 image per task (problem statement or test assets) |
| Test oracle | fail-to-pass + pass-to-pass |
| Top baseline (SWE-agent) | **12% resolution rate** |

### Key Limitation
The original benchmark is **JavaScript-only**, restricted to front-end charting/rendering libraries (Chart.js, react-pdf, etc.). This limits generalisability to real-world multi-language codebases. Tesseract addresses this at **16× scale** (10,000 instances vs 617).

---

## What Tesseract Extends

| Dimension | SWE-bench Multimodal (Original) | Tesseract (Ethara) |
|-----------|----------------------------------|---------------------|
| Languages | JavaScript only (17 front-end libs) | **8 languages**: Python, TypeScript, JavaScript, Go, C, C++, Rust, Java |
| Repositories | 17 JS libraries | **128 repositories** across domains |
| Task instances | 617 | **10,000** |
| Use-mode | Static benchmark | **RL environment** with cross-modal reward |
| Reward signal | Single resolution pass/fail | Execution-validated fail-to-pass + pass-to-pass wired into the policy loop |
| Infrastructure | External setup required | **Fully containerized** (Docker per instance) |
| Difficulty levels | Not categorised | **Easy / Medium / Hard / Expert** |

---

## Dataset Composition

### Language & Repository Coverage

| Language | Repositories | Instances |
|----------|-------------|-----------|
| **Python** | matplotlib, Pillow, seaborn, albumentations, scikit-image, plotly, bokeh, manim, opencv-python, rich + 12 more | 2,100 |
| **TypeScript** | ant-design, vuejs/core, angular, next.js, storybook, recharts, excalidraw, mermaid + 16 more | 2,400 |
| **JavaScript** | apexcharts, p5.js, Chart.js, d3, three.js, fabric.js, konva, leaflet + 10 more | 1,800 |
| **Go** | cli/cli, fyne, lazygit, bubbletea, tview, ebiten, gio + 6 more | 900 |
| **C** | libvips, lvgl, raylib, SDL, cairo, nuklear, stb + 5 more | 700 |
| **C++** | opencv, imgui, filament, magnum, ogre, vtk, dlib + 7 more | 850 |
| **Rust** | egui, iced, bevy, wgpu, resvg, plotters, druid + 5 more | 650 |
| **Java** | JFreeChart, JavaFX, Processing, XChart, batik, plantuml + 6 more | 600 |
| **Total** | **128 repositories** | **10,000 instances** |

### Difficulty Distribution

| Difficulty | Count | Percentage |
|------------|-------|------------|
| Easy | 4,500 | 45% |
| Medium | 2,800 | 28% |
| Hard | 2,000 | 20% |
| Expert | 700 | 7% |

### Instance Schema

Each instance contains:

```json
{
  "repo": "org/repo-name",
  "instance_id": "org__repo-NNNN",
  "base_commit": "sha",
  "patch": "gold-standard fix (diff)",
  "test_patch": "verification tests (diff)",
  "problem_statement": "GitHub issue text with image references",
  "hints_text": "optional developer hints",
  "FAIL_TO_PASS": ["tests that must flip from fail → pass"],
  "PASS_TO_PASS": ["tests that must remain passing"],
  "environment_setup_commit": "sha for reproducible env",
  "image_assets": ["base64-encoded screenshots/diagrams"]
}
```

### What Makes It Multimodal

Every instance includes embedded `image_assets` (base64-encoded screenshots, UI renders, or visualisations) that are **necessary** to understand the problem. For example:

- **fyne-io/fyne#6029 (Go)**: Screenshot shows navigation panel failing to refresh after forward navigation. Without seeing the visual artifact, the developer cannot identify that `nav.Refresh()` is missing from `Forward()`.
- **vuejs/core#7836 (TypeScript)**: Visual rendering bug in component tree requires inspecting browser output screenshots.
- **p5.js#6222 (JavaScript)**: Canvas rendering anomaly visible only in the attached image.

The key research insight is preserved: *images are not decoration; they are part of the specification*.

---

## Baseline Evaluation Results

### Models Evaluated

| Model | Type | Resolution Rate |
|-------|------|-----------------|
| **Kimi K2.5** | Frontier multimodal | **18.4% (1,840/10,000)** |
| **Nova 2 Lite** | Lightweight multimodal | **9.2% (920/10,000)** |
| SWE-agent (original paper) | Code agent | 12% (on JS-only set) |

### Detailed Results: Kimi K2.5 (Best Performer)

| Language | Instances | Resolved | Rate |
|----------|-----------|----------|------|
| Python | 2,100 | 378 | 18.0% |
| TypeScript | 2,400 | 504 | 21.0% |
| JavaScript | 1,800 | 378 | 21.0% |
| Go | 900 | 126 | 14.0% |
| C | 700 | 63 | 9.0% |
| C++ | 850 | 119 | 14.0% |
| Rust | 650 | 78 | 12.0% |
| Java | 600 | 114 | 19.0% |

### Detailed Results: Nova 2 Lite

| Language | Instances | Resolved | Rate |
|----------|-----------|----------|------|
| Python | 2,100 | 189 | 9.0% |
| TypeScript | 2,400 | 264 | 11.0% |
| JavaScript | 1,800 | 198 | 11.0% |
| Go | 900 | 54 | 6.0% |
| C | 700 | 18 | 2.6% |
| C++ | 850 | 51 | 6.0% |
| Rust | 650 | 33 | 5.0% |
| Java | 600 | 60 | 10.0% |

### Operational Metrics

| Metric | Range | Median |
|--------|-------|--------|
| Tool calls per instance | 1-126 | ~15 |
| Completion time | 9.8s-730s | ~120s |
| Files modified | 0-31 | ~3 |

### Key Observations

1. **Multi-language gap**: Both models struggle significantly outside JavaScript/TypeScript. Most Go, C, C++, and Rust instances remain unsolved.
2. **Difficulty paradox**: Both models pass Hard/Expert JavaScript instances (p5.js) but fail many Easy instances in unfamiliar languages, suggesting language familiarity dominates over task complexity.
3. **Headroom**: 80-90% of instances remain unsolved, confirming Tesseract is a challenging, non-saturated benchmark.

---

## RL Training & Results

Tesseract doubles as an **RL environment**. We trained a 7B vision-enabled model using **GRPO** (Group Relative Policy Optimization) with execution-grounded reward, the same algorithm family behind DeepSeek-R1's code reasoning breakthroughs.

**How it works:** For each instance, the policy generates 8 candidate patches in parallel. Each patch runs inside its Docker container against the test oracle. The binary pass/fail outcome (all tests green = reward 1, anything else = 0) feeds directly into group-relative advantage estimation: no learned critic, no soft heuristics, no gameable proxy.

**Key innovations:**
- **Step-level credit assignment**: adaptive branching at high-uncertainty tool calls (e.g., after interpreting a screenshot) gives the policy fine-grained signal across 50+ step trajectories
- **Dynamic sampling**: filters out zero-variance batches where all rollouts fail, ensuring every training step produces gradient
- **Difficulty curriculum**: progressive introduction from Easy to Hard to Expert prevents early collapse

### Training Results

| Metric | Base Model | Post-Training | Δ |
|--------|-----------|---------------|---|
| Resolution rate (Tesseract-10K) | 9.2% | **34.7%** | +25.5pp |
| Pass@8 | 22% | **53%** | +31pp |
| Patch validity | 58% | **89%** | +31pp |
| Cross-language consistency | 19% std | **7% std** | more uniform |
| Tool calls per solve | 52 | **24** | -54% |

| Language | Post-Training | Δ vs Base |
|----------|---------------|-----------|
| Python | 48% | +39pp |
| TypeScript | 37% | +16pp |
| JavaScript | 44% | +23pp |
| Go | 28% | +22pp |
| C | 11% | +8.4pp |
| C++ | 26% | +20pp |
| Rust | 22% | +17pp |
| Java | 38% | +19pp |

**Bottom line:** RL training on Tesseract's execution-grounded signal closed the multi-language gap, eliminated the "difficulty paradox" (models passing Hard JS but failing Easy Go), and halved the number of tool calls needed to solve. This proves that the environment produces a learnable, transferable training signal for multimodal coding agents.

---

## Competitive Positioning

| Feature | SWE-bench | SWE-bench Multimodal | Tesseract |
|---------|-----------|---------------------|-----------|
| Task instances | 2,294 | 617 | **10,000** |
| Visual context | No | Yes | Yes |
| Multi-language | Yes (Python) | No (JS only) | **Yes (8 languages)** |
| Containerized | Partial | No | **Full Docker isolation** |
| RL-ready | No | No | **Yes** |
| Difficulty labels | No | No | **Yes** |
| Agent trajectories | No | No | **Yes (included)** |

---

## Links

- **Dashboard:** https://projects.ethara.ai/tesseract
- **HF Dataset:** https://huggingface.co/datasets/ethara/tesseract
- **Trajectories:** https://github.com/Ethara-Ai/Tesseract/tree/main/Trajectories
- **SWE-bench Multimodal:** https://arxiv.org/abs/2410.03859
- **Ethara.AI:** https://ethara.ai
