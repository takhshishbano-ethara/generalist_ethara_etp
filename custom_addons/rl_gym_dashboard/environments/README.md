# Ethara.AI RL Environments

Ten production-grade reinforcement-learning environments purpose-built for training frontier LLMs. Each environment wraps a peer-reviewed research benchmark into a hermetic, reward-bearing sandbox and extends it - typically by widening language coverage, stiffening verification, or folding the paper's evaluation oracle directly into the training loop.

Every environment is scaled to **10,000 tasks** — two to four orders of magnitude beyond the research benchmark it extends — to deliver the throughput required for full-scale RL post-training.

This directory contains one markdown per environment. Each file profiles the **original paper**, enumerates **what Ethara extends**, states the **10,000-task scale target**, and links both the original research artefacts and Ethara's canonical URLs.

---

## The ten environments

| # | Environment | Domain | Extends | Paper | Scale |
|---|-------------|--------|---------|-------|-------|
| 1 | [**MILO-Bench**](./01-milo-bench.md) | Long-horizon software evolution | SWE-EVO | [2512.18470](https://arxiv.org/abs/2512.18470) | 10,000 tasks |
| 2 | [**Kaiju**](./02-kaiju.md) | Library generation from scratch | Commit0 | [2412.01769](https://arxiv.org/abs/2412.01769) | 10,000 tasks |
| 3 | [**Kraken**](./03-kraken.md) | Repository-level performance optimisation | SWE-fficiency | [2511.06090](https://arxiv.org/abs/2511.06090) | 10,000 tasks |
| 4 | [**Tesseract**](./04-tesseract.md) | Multimodal + multi-language SWE | SWE-bench Multimodal | [2410.03859](https://arxiv.org/abs/2410.03859) | 10,000 tasks |
| 5 | [**Valkyrie**](./05-valkyrie.md) | Security vulnerability remediation | SWE-smith | [2504.21798](https://arxiv.org/abs/2504.21798) | 10,000 tasks |
| 6 | [**Janus**](./06-janus.md) | Process-reward multimodal tool use | Agentic-MME | [2604.03016](https://arxiv.org/abs/2604.03016) | 10,000 tasks |
| 7 | [**Terra** (Nokor)](./07-terra.md) | General AI assistant | GAIA | [2311.12983](https://arxiv.org/abs/2311.12983) | 10,000 tasks |
| 8 | [**Mars** (Huskarl)](./08-mars.md) | CLI / terminal autonomy | Terminal-Bench | [2601.11868](https://arxiv.org/abs/2601.11868) | 10,000 tasks |
| 9 | [**Vesta** (Drengr)](./09-vesta.md) | Open-world agent safety | OpenAgentSafety | [2507.06134](https://arxiv.org/abs/2507.06134) | 10,000 tasks |
| 10 | [**Pax** (Surtur)](./10-pax.md) | Personalised agent threat | From Assistant to Double Agent (PASB) | [2602.08412](https://arxiv.org/abs/2602.08412) | 10,000 tasks |

**Total corpus:** 100,000 reward-verified tasks across the ten environments.

---

## Ethara URL conventions

Each environment file carries both **original-research URLs** (verified from the paper) and **Ethara URLs** (per the pitch deck):

- `projects.ethara.ai/<env-name>` — confirmed public surface for each environment (Kaiju and Janus/Akatsuki are live; the others follow the same convention).
- `github.com/ethara-ai/<env-name>` — canonical GitHub org convention; specific repositories may not all be public yet.
- `huggingface.co/datasets/ethara-ai/<env-name>` — canonical Hugging Face dataset convention. (Terra's dataset is confirmed as `ethara/Nokor` in trajectory metadata.)
- `ethara.ai/dashboard/<env-name>` — the deck's "Dashboard" reference.

Slots explicitly marked *(expected)* in per-environment files should be treated as forward-looking until Ethara publishes the concrete links.

---

## Coverage matrix

```
Coding        : MILO-Bench, Kaiju, Kraken, Tesseract
Security      : Valkyrie (code-level), Vesta (agent-level), Pax (personalised)
Multimodal    : Tesseract (code+image), Janus (tool-chain), Terra (general)
Autonomy      : Mars (terminal), Terra (general AI assistant)
Evolution     : MILO-Bench (milestone-scale)
Performance   : Kraken (speedup under correctness)
```

The pitch deck markets the family as "Five environments, one mission"; the actual catalogue is **ten** and spans coding, security, performance, multimodal reasoning, long-horizon evolution, autonomy, and agent safety.

---

## Consistent design pattern

Across the ten environments, Ethara applies the same transformation to each underlying research benchmark:

1. **Take a peer-reviewed evaluation harness** whose scoring is already execution-verified or process-verified.
2. **Widen the surface** - usually from Python-only or JS-only to a multi-language matrix (Python, Rust, Go, TypeScript, JavaScript, Java, C, C++).
3. **Harden the sandbox** - hermetic Docker, deterministic reset, contamination-audited inputs.
4. **Reuse the paper's verifier as an RL reward** rather than an offline score, so the policy trains directly against the ground truth the authors designed.

This is the right shape: the hard research work (task construction, oracle design) is inherited from published papers; Ethara's contribution is the **environment engineering** that makes that oracle usable as a training signal at scale.

---

## Data-quality notes (flagged in individual files)

- **Pax / Surtur** - the pitch deck reuses OpenAgentSafety's arXiv ID `2507.06134`; the correct reference for *From Assistant to Double Agent* is `2602.08412`. See [10-pax.md](./10-pax.md).
- **Janus / Agentic-MME** - arXiv ID `2604.03016` is future-dated; carried verbatim from the deck and resolves to the Agentic-MME paper.
- **Valkyrie baselines** - "Kimi K2.5" and "Nova 2 Lite" in the deck are Ethara's cross-environment baseline-model convention (`moonshotai.kimi-k2.5` and `amazon.nova-2-lite-v1_0` on Bedrock), used in the Mars / Pax / Terra trajectories. They are not baselines from the SWE-smith paper itself.
- **Deck summary** - the final summary slide says "Five Environments. One Mission." while the body enumerates ten. Treat the ten as authoritative.

---

## Ethara baseline trajectories

The sibling `../trajectories/` directory ships execution traces for three of the ten environments:

| Env | Trajectories | Scaffold | Models | Pass rate |
|-----|---|---|---|---|
| [Mars](./08-mars.md) | 20 tasks × 2 models = 40 runs | `terminus-2` (Terminal-Bench) | `glm-5`, `nova-2-lite` | 58.3% / 45.0% |
| [Pax](./10-pax.md) | 30 attack cases × 2 models = 60 trials (IPI × 10, mem-cred × 10, TRD × 10) | Custom attack harness | `glm`, `nova` | Mean security 2.17 / 1.53 (0-3 scale, higher = safer) |
| [Terra](./07-terra.md) | 20 GAIA-style Qs × 2 models = 40 runs | OpenHands | `moonshotai.kimi-k2.5`, `amazon.nova-2-lite-v1_0` | 50% / 10% |

**Cross-environment model convention:** every env with trajectories pairs a cheap + fast model (`nova-2-lite`) with a stronger + reasoning-heavy model (`glm-5` / `kimi-k2.5`). `nova-2-lite` consistently costs more per run (due to loop budget consumption on failure) while the reasoning-heavy models get higher pass / security scores at lower token counts. See per-environment files for cost tables and attack-family breakdowns.

The remaining seven environments (MILO-Bench, Kaiju, Kraken, Tesseract, Valkyrie, Janus, Vesta) do not ship trajectories in this repository - their per-environment files are paper-grounded only.

---

## Sources

- Ethara.AI pitch deck: `../Ethara.AI OTS.pptx.txt`
- Direct arXiv verification for all resolvable IDs
- Project pages: swefficiency.com, swesmith.com, tbench.ai, huggingface.co/gaia-benchmark
- Ethara.AI: https://ethara.ai
