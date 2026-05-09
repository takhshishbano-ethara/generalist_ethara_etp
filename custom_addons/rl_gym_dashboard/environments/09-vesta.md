# Vesta (Drengr)

**Ethara.AI RL Environment · Open-World Agent Safety**

Extends: [OpenAgentSafety](https://arxiv.org/abs/2507.06134)

---

## What Ethara pitches

Vesta trains foundation models against complex socially-integrated adversarial attacks and tool-use vulnerabilities. Models operate in native operating environments (shell, web browser, file system) alongside simulated secondary actors. These interactions generate training signal across **8 risk domains** to ensure safety under sustained social and technical pressure.

**Pitched capabilities**

- Verifiable reward signals derived from real-world environment state changes
- Dual-layered validation: automated system-state checks + sophisticated behavioural intent analysis

**For frontier labs:** Chat-alignment breaks down when models gain tool access. Vesta closes the agentic safety gap, generating the RL signals needed to train models against social engineering and autonomous tool misuse.

---

## Original paper: OpenAgentSafety

**Title:** OpenAgentSafety: A Comprehensive Framework for Evaluating Real-World AI Agent Safety
**Authors:** Sanidhya Vijayvargiya, Aditya Bharat Soni, Xuhui Zhou, Zora Zhiruo Wang, Nouha Dziri, Graham Neubig, Maarten Sap (CMU, Allen AI)
**arXiv:** [2507.06134](https://arxiv.org/abs/2507.06134) (submitted 8 Jul 2025, v2 Feb 2026)
**Venues:** Accepted at ICLR 2026 and IASEAI 2026
**Category:** cs.AI

### Core contribution

OpenAgentSafety evaluates AI-agent safety in the settings that actually matter: multi-turn, multi-user scenarios with real tools. Agents are confronted with adversarial co-actors and ambiguous instructions, and safety is scored by a **hybrid** of deterministic checks and LLM-as-judge behavioural intent analysis.

### Benchmark stats (original)

| Item | Value |
|------|-------|
| Risk categories | **8** (enumerated below) |
| Tasks | **356** executable (80 seed + 276 GPT-4o-generated, all manually verified) |
| Format | Multi-turn, multi-user |
| Tools | Web browser (OwnCloud + GitLab + Plane, self-hosted), IPython code exec, filesystem, bash, ChatNPC (Sotopia framework + Redis backend) |
| Evaluation | Rule-based system-state check (binary) + LLM-as-judge (GPT-4.1, 4-label) |

### The eight risk categories

Verbatim from the paper:

1. Computer Security Compromise
2. Data Loss / Corruption
3. Privacy Breach
4. Unsafe Code Execution
5. Financial Loss
6. Spreading Malicious Content
7. Legal Violations
8. Harmful Decision-Making

### Dual-evaluation design

Two scorers, in parallel, on every trajectory:

- **Rule-based:** deterministic state-check on real system surfaces (container filesystem, GitLab state, wallet balances, messages sent, etc.). Binary.
- **LLM-as-judge:** GPT-4.1 applied to the full trajectory, emitting one of four labels - `-1 failure`, `0 safe`, `1 incomplete-unsafe`, `2 complete-unsafe`.

The **disagreement between the two scorers** is itself a signal and reveals blind spots of the rule-based surface - e.g. the agent *intended* the unsafe action but the filesystem check missed it because the action was socially mediated.

### Baseline results - unsafe rates on safety-vulnerable trajectories

| Model | LLM-judge % unsafe | Rule-based % unsafe |
|-------|---|---|
| **Claude Sonnet 4** | **49.1** | **49.1** |
| Claude Sonnet 3.7 | 51.2 | 32.9 |
| GPT-5 | 52.6 | 52.2 |
| Deepseek-v3 | 62.2 | 32.4 |
| Deepseek-R1 | 62.8 | 32.8 |
| GPT-4o | 65.8 | 32.6 |
| **o3-mini** | **72.7** | 32.3 |

Even the safest model (Claude Sonnet 4) is unsafe on ~half of vulnerable trajectories. The LLM-judge consistently finds more unsafe behaviour than the rule-based checker, exposing how much of agent-safety evaluation leaks through tool-state-only measurement.

### The benign-intent paradox

Tasks framed with **benign user intent** still produce 50-86% unsafe behaviour across models. Claude 3.7 has the *lowest* unsafe rate under malicious framing but the *highest* (85.7%) under benign framing - an over-helpfulness pattern that cannot be fixed at the chat layer.

- Systemic risks (security, legal, privacy): 72-86% unsafe.
- Browser tasks: 59-75% unsafe.
- Chat-based tasks: 45-70%.
- Dominant failure modes: authentication bypass, over-helpfulness.

---

## What Ethara extends

| Dimension | Original OpenAgentSafety | Vesta |
|-----------|--------------------------|-------|
| Use-mode | Static evaluation framework | RL environment generating training signal |
| Validation | Rule-based + LLM-judge | Same dual-layered engine, surfaced as a **reward** |
| Coverage | 8 risk domains, 350+ tasks | 8 risk domains, expanded simulated-secondary-actor coverage |
| Reward signal | Pass/fail per task | Verifiable reward from real-world environment state changes |

Vesta is OpenAgentSafety turned into an RL loop: the paper's dual-layered validator becomes the reward function, and the simulated secondary actors are persistent co-inhabitants of the training environment so the policy gets exposure to *sustained* social pressure, not one-shot prompt injections.

---

## Ethara scale target

**10,000 tasks per environment** — Ethara scales Vesta from OpenAgentSafety's 356 manually-verified tasks by extending the paper's GPT-4o-assisted generation pipeline across the 8 risk categories, preserving the rule-based + LLM-as-judge dual-evaluation and the Sotopia-style simulated secondary actors.

---

## Links

### Original research
- **Paper (OpenAgentSafety):** https://arxiv.org/abs/2507.06134
- **GitHub:** https://github.com/Open-Agent-Safety/OpenAgentSafety
- **Venues:** ICLR 2026, IASEAI 2026

### Ethara Vesta (Drengr)
- **Ethara project page:** https://projects.ethara.ai/vesta
- **Ethara.AI:** https://ethara.ai
- **GitHub (expected):** https://github.com/ethara-ai/vesta
- **Hugging Face dataset (expected):** https://huggingface.co/datasets/ethara-ai/vesta
- **Dashboard (expected):** https://ethara.ai/dashboard/vesta
