Training Brief: The Goku Harness

1. The 30-second pitch (lead with this)

▎ "Goku is our multimodal agentic evaluation benchmark. It measures how well frontier AI agents — Claude, GPT, Gemini — handle realistic multi-step tasks that involve looking at images, PDFs, and video, not just text. Each task gives the agent a prompt plus attached media; the agent runs inside a sandboxed Docker container, uses tools, produces files and a written answer; then we grade it against a per-task rubric using a mix of deterministic checks and an LLM judge. The headline output is a per-model score we can compare apples-to-apples across providers."

 Everything below is depth to defend it.

2. The mental model — three stages

   TASK                  AGENT RUN (Docker)              GRADING
 ┌────────┐   media on   ┌─────────────────┐   files   ┌──────────────┐
 │prompt  │──turn 1────► │ agent + tools    │──────────►│ 8 rubric     │
 │+ media │              │ iterates, writes │  response │ types        │
 │+ rubric│              │ to /workspace    │──────────►│ → score 0..1 │
 └────────┘              └─────────────────┘           └──────────────┘
  authored by humans      OpenHands SDK runtime         deterministic + LLM judge

Three things make Goku different from a normal eval:
1. Multimodal, symmetric — the agent and the judge see the same media, so visual rubrics are graded against ground truth, not the agent's self-description.
2. Sandboxed + tool-using — it's an agentic benchmark; the agent actually executes shell/tools in a container, not a single Q→A.
3. Hybrid grading — cheap deterministic checks where possible, LLM judge only where semantics are needed.

3. Where Goku sits

Goku is one benchmark in a family in this repo (alongside SWE-bench, GAIA, commit0). It's the multimodal one. They all sit on the OpenHands software-agent-SDK (vendored as a git submodule, untouched). That's a good "we didn't reinvent the agent runtime" point.

4. The pipeline (architecture walkthrough)

Two CLIs do the work, plus a batch runner:

- goku-infer — runs agents on tasks, scores them, writes per-task results
- goku-eval — aggregates across models/runs, exports a clean delivery folder
- goku-rescore — re-judges existing runs after a rubric edit without paying for agent inference again (only the judge re-runs)
- run_batch.sh — overnight runner: many tasks × models × runs, with parallelism, Docker cleanup, retries, resume

The inference path, step by step (benchmarks/goku/run_infer.py):

1. Discover tasks from folders → EvalInstance (instruction + rubric + input files).
2. Spin up Docker (arm64/amd64 aware), mkdir /workspace/results, install ffmpeg / PDF deps only if the task needs them.
3. Upload media with a 3-tier fallback: docker cp (fast native copy) → SDK upload → base64-in-bash chunks. (Built because the agent-server's upload endpoint 500s on large files.)
4. Build the agent's first turn as a multimodal message — text prompt + image/PDF/video blocks.
5. Run the conversation — agent iterates with tools; a token-budget-driven condenser compresses history as it approaches the model's context window.
6. Extract the response — scans events in reverse for the agent's final message; retries 10× because the remote conversation can transiently lose events (so we retry instead of scoring a blank as wrong).
7. Download outputs from /workspace, score every rubric item, write scores.jsonl.

5. Multimodal handling — the part that impresses engineers

This is where most of the hard work is. Three media types, each with a real constraint:

┌────────┬───────────────────────────────────────────────────────────────────────────────────────────┬─────────────────────────────────────────────────────────────────┐
│ Media  │                                        What we do                                         │                               Why                               │
├────────┼───────────────────────────────────────────────────────────────────────────────────────────┼─────────────────────────────────────────────────────────────────┤
│ Images │ Native image blocks; auto-resize to stay under Bedrock's 8000px / 5MB caps; detect format │ Re-encoded files lie about their extension                      │
│        │  by magic bytes, not extension                                                            │                                                                 │
├────────┼───────────────────────────────────────────────────────────────────────────────────────────┼─────────────────────────────────────────────────────────────────┤
│ PDFs   │ Default "tool mode": turn-1 gets a tiny text index + 50-DPI thumbnails (~600KB for 55     │ Inline rendering blows Anthropic's 32MB request cap past ~5     │
│        │ pages); agent fetches any page at full res on demand via an in-container pdf_page tool    │ pages. Tool mode is 45× under the cap regardless of PDF size    │
├────────┼───────────────────────────────────────────────────────────────────────────────────────────┼─────────────────────────────────────────────────────────────────┤
│ Video  │ Always extract uniform keyframes via ffmpeg (JPEG), attach as images                      │ Only Gemini supports native video — using it would make scores  │
│        │                                                                                           │ incomparable across providers. Symmetry > fidelity here         │
└────────┴───────────────────────────────────────────────────────────────────────────────────────────┴─────────────────────────────────────────────────────────────────┘

Per-provider routing (media_adapters.py): a PDF becomes a document block for Claude, a file block for GPT/Gemini, or rendered-to-images for judges that can't read PDFs (Kimi). Bedrock ARNs are opaque, so the config carries a model_canonical_name hint to pick the right shape.

The one-liner for the CTO: "We solved the 'big media won't fit in one request' problem by giving the agent an index plus on-demand high-res tools, the same pattern a human uses skimming a document."

6. Scoring — the math (have this cold)

8 rubric types, 6 deterministic + 2 LLM-judged:

- Deterministic: probe_file_exists, probe_file_contains (regex), probe_dir_exists, shell_succeeds_real (runs a bash assertion), response_contains, response_regex_present
- LLM judge: response_criteria (does the answer satisfy a natural-language assertion?), response_not_criteria (hallucination detector — if it matches, it's a penalty)

Per-task score:
awarded   = Σ(passed positive points) − Σ(triggered negative |points|)
max_total = Σ(positive points)
raw_score = awarded / max_total          # can go negative
per_task_score = clip(raw_score, 0, 1)
pass = ALL mandatory positives passed AND no mandatory negative triggered
Note the strict gate: one failed mandatory item fails the whole task even if the numeric score is high.

Benchmark level (across the 3 runs per task):
- mean_per_task_score — the headline metric
- pass_rate — mean pass across runs
- pass@3 ≈ 1−(1−p)³ — lenient, "≥1 of 3 runs passed"
- pass^3 ≈ p³ — strict, "all 3 runs passed" (consistency)

The key invariant to state confidently: passed always means "criterion matched," not "task succeeded." For a hallucination rubric, matched = bad, so it's inverted at write-time.

7. The trust / anti-gaming engineering (a CTO will love this)

These are the details that separate a toy eval from a real one:

- Input vs. output media separation — the judge gets two labeled sections: === INPUT MEDIA === (the fixture) and === OUTPUT MEDIA === (what the agent produced), so "did the output correctly summarize the input?" is gradeable.
- Prompt-injection defense — all agent-controlled text is wrapped in opaque fences with escape rules, so an agent can't "close the fence" and inject judge instructions.
- Confabulation detection — if the judge cites a filename that doesn't exist in the agent's files, or its rationale contradicts its verdict, we re-run the judge 2 more times and take majority vote.
- Council mode — optionally run N different judges in parallel and majority-vote, with an audit sidecar recording every judge's rationale.
- Sandbox safety — symlink-escape checks, regex timeouts (30s, C-level not fork-based), task-loader rejects rubrics with curl/wget/rm -rf /.
- Task validation fails loud — categories are strictly siloed (a pdf task can only ship PDFs), with hard caps (PDF ≤100pg/40MB, video ≤60min). Bad tasks error at load, never silently skip.

8. Where it's heading — Trinity (one slide, don't over-rotate)

The current branch (trinity-bootstrap) is bootstrapping Trinity, a meta-layer that authors and audits hard tasks so the benchmark stays hard as models improve. Three agent "instruments":
- ENGRAM (master) — cross-task memory; tracks which agent weaknesses ("levers") still beat the frontier and expires them the moment the frontier catches up.
- FORGE (author) — generates and proves one new task at a time.
- CRUCIBLE (auditor) — audits the whole project for leakage, reward-hacking, secrets.

The single most important Trinity idea: "difficulty is measured, never claimed — only an externally-signed pilot test counts," and FORGE (author) and CRUCIBLE (auditor) are firewalled from each other so you can't tune tasks to pass your own audit.

Critical honesty point: Trinity is early — Phase 0 done, the rest blocked on humans writing requirements/. It does not touch the Goku pipeline (that's structural, not a promise). Don't present it as shipping.

---
9. Q&A BANK — drill these

Tier 1 — basics (you must nail these instantly)

Q: What does Goku actually measure?
Agent capability on realistic multimodal, multi-step tasks — ending in a mean_per_task_score per model, comparable across providers.

Q: Why Docker?
Sandbox. The agent executes real shell/tools; we isolate that and get reproducibility + cleanup.

Q: Who writes the tasks?
Humans today (each task = instruction.md + rubrics.jsonl + data/input_files/). Trinity is the future automation layer.

Q: How many runs and why 3?
3 runs/model/task to measure consistency — that's what enables pass@3 (lenient) and pass^3 (strict).

Q: Which models?
Anything with a config in .llm_config/ — Claude (Bedrock/Anthropic), GPT, Gemini. Judge is typically Kimi.

Tier 2 — design rationale (the "why" questions)

Q: Why an LLM judge — isn't that unreliable?
We minimize its use: deterministic checks (file exists, regex, shell exit code) do the heavy lifting; the judge only handles genuinely semantic criteria. And we harden it — input/output separation, prompt-injection fences, confabulation re-voting, optional N-judge council. We also goku-rescore to re-judge without re-running agents, so judge quality is independently iterable.

Q: How do you keep model comparisons fair?
Symmetric media handling. Video → keyframes for everyone, even Gemini which could do native, because asymmetry would muddy the comparison. Same fixture goes to agent and judge.

Q: How do you fit a 100-page PDF into a request?
We don't inline it. Turn 1 = text index + tiny thumbnails (~600KB); the agent pulls full-resolution pages on demand via an in-container tool, with auto-DPI-clamping to stay under the API's 2000px limit. Bounded payload regardless of document size.

Q: What stops an agent from gaming the judge?
Opaque escaped fences around all agent-controlled text (can't inject), filename-confabulation detection with re-voting, and the judge sees ground-truth media not the agent's words.

Q: What's the headline metric and can it be negative?
mean_per_task_score. Per-task raw score can go negative (hallucination penalties exceed awards) but it's clipped to [0,1]. The strict pass gate is separate — all mandatory items.

Tier 3 — sharp / adversarial 

Q: How big is the benchmark — is it statistically meaningful?
Be honest: currently ~12 tasks in the dataset, 3 runs each. It's a pilot. Scaling task count is exactly what Trinity is being built to automate. Don't claim broad coverage we don't have.

Q: What happens when models get better and saturate it?
That's the explicit problem Trinity solves — it tracks which weaknesses still beat the frontier and expires the ones that don't, with externally-signed proof that each task is still hard. "Difficulty is perishable."

Q: How do you know the judge is right?
We don't fully trust a single call — that's why confabulation detection + council voting + the audit sidecar exist. Trinity's CRUCIBLE adds a judge-reliability discipline (multiple trials, position randomization). It's defense-in-depth, not a guarantee.

Q: What's the failure mode if infra flakes mid-run?
Distinguished deliberately: missing agent events → raise and retry (don't score a blank as a wrong answer); agent ran but produced no text → legitimate empty response. Batch runner has retries + resume; exits 2 on zero completions so a watcher can tell a silent skip from partial success.

Q: Is Trinity in production?
No — Phase 0 only, blocked on human-authored requirements, and architecturally cannot modify the eval pipeline. It's the roadmap, not the current state.

Q: Could a task author tune tasks to pass their own audit?
No — FORGE (author) and CRUCIBLE (auditor) are firewalled; the auditor's pass criteria never reach the author. That's a deliberate anti-collusion design.

Tier 4 — "gotcha" honesty traps (answer with candor)

- "So the scores are gospel?" → No. Small task set, LLM judge in the loop. It's a directional pilot with serious anti-gaming engineering, scaling next.
- "Why not just use an existing benchmark?" → Existing ones are mostly text or single-turn; Goku is multimodal and agentic and keeps agent/judge media symmetric — and it's built to stay hard via Trinity.
- "What's the most fragile part?" → The LLM judge on response_criteria. That's precisely why deterministic checks are preferred, rescore exists, and council/confabulation guards exist.

---
10. Three things NOT to overclaim

1. Don't say Goku has "lots of tasks" — it's ~12, a pilot.
2. Don't present Trinity as shipped — it's Phase 0, human-blocked.
3. Don't say the LLM judge is "accurate" — say it's "hardened and defense-in-depth," and that deterministic checks are preferred.

---