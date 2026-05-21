# Trajectory QC Checklist — Ruthless Reviewer's Audit

> **Use this checklist when accepting a single trajectory bundle into the SFT dataset.**
> Sources: `Copy of [Ext] Ethara__Meta_ OpenClaw Data Collection (1).md` (main SFT spec), `(2).md` (multi-agent extension), `(3).md` (data taxonomy + agent behavior), `Ideal_schema_skoll.json` (canonical reference), `Ideal_Schema_v2.md` (internal contract).
> **Philosophy: the bar is binary, no benefit of the doubt.** If you have to squint at a turn to decide whether it's good, mark it failed. Reviewers who reward effort over correctness contaminate the dataset.

---

## 0. Severity Legend

| Tag | Meaning | What to do |
| --- | --- | --- |
| **🚫 BLOCKER** | Schema, safety, or grounding failure that makes the trace unusable. | Reject the trajectory. Send back to vendor. |
| **⚠️ MAJOR** | Real defect that degrades training signal. | Require fix before acceptance. |
| **🟡 MINOR** | Stylistic or borderline issue. | Note and accept; aggregate across deliveries to flag systemic problems. |
| **✅ PASS** | Explicit success criterion met. | Move on. |

A trajectory is **accepted** only if zero 🚫 and zero unresolved ⚠️ checks fail. **No partial credit. No "mostly correct."**

---

## 1. Pre-Flight — Bundle Structure

Per spec §3 (Ethara doc 1):

- [ ] **🚫** The delivery directory matches the expected structure: `<batch>/<task-id>/<session-id>.json`.
- [ ] **🚫** The trajectory JSON parses (well-formed JSON).
- [ ] **⚠️** Filename matches the inner `meta_info` or session id.

**Anti-patterns to reject on sight:**
- Trajectory JSON > 5 MB → almost always means runtime telemetry wasn't stripped.
- Filenames containing real names, real emails, or real corporate domains → PII leak. 🚫 Reject and escalate.

---

## 2. Schema Conformance — `meta_info`

Per `Ideal_Schema_v2.md` §3 and `Ideal_schema_skoll.json`:

- [ ] **🚫** Root has *exactly* two keys: `meta_info` and `messages`. Any third key (e.g. `header`, `leafId`, `entries`) is a raw-export leak — reject.
- [ ] **🚫** `meta_info.task_type` is one of the **14 canonical** values:
  `search_and_retrieval`, `productivity_flow`, `code_intelligence`, `creative_synthesis`, `skill_use_and_orchestration`, `skill_creation_and_editing`, `communication_and_messaging`, `device_and_environment_control`, `memory_and_personalization`, `scheduling_and_long_running`, `proactive_assistance`, `social_interaction`, `multi_turn_robustness`, `safety_alignment`. *(Note: the spec text states "13 task types" — this is a typo in the spec; the actual taxonomy lists 14 types.)* **Earlier name `proactive_action` is stale — reject.**
- [ ] **🚫** `meta_info.cluster` is one of the **4 canonical** values: `understand_and_find`, `create_and_act`, `remember_and_anticipate`, `navigate_and_adapt`.
- [ ] **🚫** `cluster` matches the cluster `task_type` belongs to (see canonical mapping below). Any mismatch is a vendor error.

### 2.1 Task Type → Cluster Canonical Mapping (Multi-Agent Data Taxonomy)

Use this table to verify that the `task_type` in `meta_info` belongs to the correct `cluster`. This is the **single source of truth** for the multi-agent extension.

| Cluster | Task Types |
|---------|-----------|
| **understand_and_find** | `search_and_retrieval`, `productivity_flow`, `code_intelligence` |
| **create_and_act** | `creative_synthesis`, `skill_use_and_orchestration`, `skill_creation_and_editing`, `communication_and_messaging`, `device_and_environment_control` |
| **remember_and_anticipate** | `memory_and_personalization`, `scheduling_and_long_running`, `proactive_assistance` |
| **navigate_and_adapt** | `social_interaction`, `multi_turn_robustness`, `safety_alignment` |

**Quick-reject rule:** If `task_type` appears in one cluster above but `meta_info.cluster` names a different one → 🚫 immediate reject. This is the most common vendor copy-paste error.
- [ ] **🚫** `task_completion_status` is one of: `success`, `partial_success`, `incomplete`, `failure` *(Ethara doc 1 enum)* — or `success`/`partial`/`failure`/`aborted` if you're using the v2 internal enum. **Pick one set and enforce.**
- [ ] **🚫** `system_prompt` key is present (value may be `""`). Missing key = schema violation.
- [ ] **🚫** `platform` is present and non-empty.
- [ ] **⚠️** If `sessions_spawn` appears anywhere in `messages`, `meta_info.agents.root` and `meta_info.agents.spawned[]` MUST be present and `spawned` MUST be non-empty. A spawn-call without the agents block is a multi-agent trace declaring itself single-agent — corrupt.
- [ ] **⚠️** `task_description` reads as a self-contained sentence that a stranger could understand. If it requires context to parse ("continue the previous task") → reject.

**Forbidden fields in `meta_info`** (raw OpenClaw leakage or dropped schema fields):
`header`, `leafId`, `wizard`, `meta` (telemetry), `provider`, `model`, `usage`, `cost`, `trace_source`, `exported_at`, `conv_id`, `task_completion_notes`. Any of these present → ⚠️ MAJOR.

---

## 3. Schema Conformance — `messages[]`

- [ ] **🚫** Every entry has `type: "message"`. Other types (`model_change`, `thinking_level_change`, `custom`, `custom_message`) → raw-export leak, reject.
- [ ] **🚫** Each message has `id`, `parentId`, `timestamp`, `message`.
- [ ] **🚫** All `id` values are unique within the trace.
- [ ] **🚫** **Sequential ID serialization** (per `edgecases.txt`): IDs follow the pattern `d<7-digit-zero-padded-sequence>` — first message has `parentId: "d0000000"` (synthetic root sentinel) and `id: "d0000001"`; second `parentId: "d0000001"` / `id: "d0000002"`; and so on. Random 8-char hex IDs from raw OpenClaw exports (e.g. `d3809302`) **do not pass this check** — re-serialize before submission. *Conflict note: `Ideal_schema_skoll_json.json` uses random hex IDs and `parentId: null` on the root; edgecases.txt supersedes for new bundles.*
- [ ] **🚫** Exactly **one** message has the synthetic root parent (`parentId: "d0000000"`). Zero or two+ roots = corrupted tree.
- [ ] **🚫** Every `parentId` other than `"d0000000"` resolves to an existing `id` in the trace. Orphan parents = corruption.
- [ ] **🚫** ID sequence has no gaps — `d0000001`, `d0000002`, …, `d000000N` with no skips. Gaps suggest dropped entries that were not properly reparented.
- [ ] **🚫** Timestamps are ISO 8601 with timezone (`Z` or `+HH:MM`).
- [ ] **🚫** No message-level telemetry siblings inside `message`: forbidden keys are `api`, `provider`, `model`, `usage`, `cost`, `stopReason`, inner `timestamp` (which duplicates outer). Any leak = ⚠️ MAJOR; if all of them present = 🚫.

### 3.1 Roles

- [ ] **🚫** `role` ∈ {`user`, `assistant`, `toolResult`} only. `system` / `function` / `developer` are forbidden (system prompt lives in `meta_info`, function results are `toolResult`).
- [ ] **⚠️** First role-message (after root) is `user`. If first turn is `assistant` → either the system primed it (acceptable if documented) or the export started mid-conversation (reject).

### 3.2 Content Blocks

Per spec §2 (Ethara doc 1):

- [ ] **🚫** Every content block's `type` ∈ {`text`, `thinking`, `toolCall`, `image`}. Any other type = reject.
- [ ] **🚫** `thinking` blocks have `thinking` (string) and `thinkingSignature` (always `""`). Non-empty `thinkingSignature` = raw-export leak.
- [ ] **🚫** `toolCall` blocks have `id`, `name`, `arguments`. All three required.
- [ ] **🚫** `toolCall.id` is unique within the trace and is referenced by exactly one downstream `toolResult.toolCallId`. Orphan calls or duplicate IDs = reject.
- [ ] **⚠️** `image` blocks have `mimeType` and either `data` (base64) or a workspace-relative path. Hosted URLs (`https://...`) → reject; the dataset must be reproducible offline.
- [ ] **⚠️** No `text` block is empty (`""`) unless it's a deliberate placeholder paired with other content. Empty text + nothing else = noise.

### 3.3 Tool Results

- [ ] **🚫** Every `toolResult` has `toolCallId`, `toolName`, `isError`, `content`.
- [ ] **🚫** `toolCallId` matches an earlier `toolCall.id`.
- [ ] **🚫** `content` is an array (possibly empty for fire-and-forget tools, e.g. `update_plan`).
- [ ] **🚫** No sibling `details`, `aggregated`, `status`, `exitCode`, `durationMs`, `cwd` on the toolResult envelope. These are runtime telemetry — the data inside them must already be in `content[0].text` if it matters.

---

## 4. Multi-Agent Integrity (Doc 2 + §6 of Ideal_Schema_v2.md)

**Apply this section only if `sessions_spawn` appears in `messages`.**

### 4.1 Spawn-Call Shape

- [ ] **🚫** `sessions_spawn` `arguments` are `{name, prompt}` minimum. Optional: `context` (`"fork"`/`"isolated"`), `mode` (`"run"`/`"session"`), `runTimeoutSeconds`.
- [ ] **🚫** OpenClaw-native names not renamed → reject (`task` instead of `prompt`, `taskName` instead of `name` = transform didn't run).
- [ ] **🚫** The `prompt` string is **self-contained** (doc 2 guiding principle). It must NOT reference "the previous request," "the user's earlier message," or any context the spawned agent can't see. Read the prompt cold — if you couldn't act on it alone, reject.
- [ ] **🚫** The spawn result `content[0].text` parses to `{session_id, status}` with `status` ∈ {`running`, `completed`, `failed`, `timeout`}. Anything else (e.g. `"accepted"` from raw OpenClaw, runtime `note` field) = transform didn't run.

### 4.2 Yield-Result Shape (THE CRITICAL ONE)

- [ ] **🚫** If `sessions_yield` is called with `status: "completed"`, the result `content[0].text` MUST parse to `{session_id, status: "completed", output: "<non-empty>"}`. **A completed yield with empty/missing `output` is the canonical multi-agent grounding failure — reject.**
- [ ] **🚫** The `session_id` in the yield result matches a `session_id` from an earlier spawn result.

### 4.3 The "Why Multi-Agent" Test (Doc 2 — guiding principle)

> *"Single agent performance should meaningfully degrade compared to multi-agent performance. If single agent can do it fine, it's not a multi-agent task."*

For each spawn, the reviewer must classify the *reason* into one of the 9 patterns (doc 2):

| Pattern | Justification a reviewer must see |
| --- | --- |
| Parallel search | Multiple independent sources being queried at once. |
| Parallel analysis | Input volume exceeds one context window. |
| Parallel generation | N independent outputs, faster wall-clock. |
| Specialist delegation | Tools/skills the parent lacks. |
| Productivity flow | A→B→C pipeline where each stage has different concerns. |
| Verify & cross-check | Second pass validating first. |
| Divide & conquer | Decomposed large task, one piece per child. |
| Aggregate & reconcile | Conflicting sources need reconciliation. |
| Iterative refinement | Orchestrator steers child across rounds. |

- [ ] **🚫** Every spawn maps to exactly one pattern. If you can't pick one, the spawn is gratuitous — reject.
- [ ] **🚫** Gratuitous spawns are dataset poison. Examples to reject:
  - Spawning a child to do a single web_search the parent could have done.
  - Spawning a child to write a 3-sentence email.
  - Spawning to "save context" without measurable context-window pressure.

### 4.5 Human-Effort Floor (Doc 2 task design)

Doc 2: *"Solvable with a sustained effort from typical human professionals... cannot be easily automated with programs. Think of multiple hours to days."*

- [ ] **🚫** Reviewer estimates a typical professional would need ≥**2 hours of focused effort** (read materials + reason + act + verify) to do this task end-to-end. Trivial multi-step tasks (e.g. "set 3 calendar events") = reject — those are script-able and don't belong in multi-agent SFT.
- [ ] **🚫** A 50-line bash/python script could do this task end-to-end without an LLM → reject. The task must require genuine judgment.

### 4.4 Spawn-Tree Coverage

- [ ] **🟡** Sub-agent transcripts are inlined under the spawn `toolResult` (Option A in `Ideal_Schema_v2.md` §6.4). If not inlined, the bundle must reference per-child trajectory files. No-evidence yields are 🚫 BLOCKERS under §4.2 regardless.

---

## 4A. Metadata Alignment vs Task Registry (CSV Cross-Reference)

> **Purpose:** Verify that the trajectory — as a whole — is aligned with the metadata assigned in the external task registry (CSV/spreadsheet). This is NOT merely a field-match between `meta_info` JSON keys and the CSV. The reviewer must pull the expected metadata from the CSV and then read the **full trajectory** (user prompt, spawn tree, tool calls, final deliverables) to confirm the trajectory actually performs work consistent with its assigned cluster, task type, and pattern.

> **Key principle:** The CSV defines what the trajectory SHOULD be. The `meta_info` declares what it CLAIMS to be. The trajectory content is what it ACTUALLY is. All three must agree.

**Required inputs:** The task registry CSV (e.g. `skoll_rfp.csv`) with columns: Task ID, Persona Name, Life Domain, Cluster, Task Type, Pattern Taxonomy.

**Taxonomy structure:** There are exactly **4 clusters**, each containing specific **task types** (14 total). There is no "task_subcategory" — the hierarchy is: Cluster → Task Type. See §2.1 for the canonical mapping.

### 4A.1 Step 1 — Pull Expected Metadata from CSV

For the trajectory under review:

1. Identify the corresponding CSV row (by filename → Task ID mapping).
2. Extract the expected values: **Cluster**, **Task Type**, **Pattern Taxonomy**, **Life Domain**.
3. Normalize using the table in §4A.2.

### 4A.2 Normalization Rules

| CSV Value | Normalized Value |
|-----------|---------------------------|
| Understand & Find | `understand_and_find` |
| Create & Act | `create_and_act` |
| Remember & Anticipate | `remember_and_anticipate` |
| Navigate & Adapt | `navigate_and_adapt` |
| Search & Retrieval | `search_and_retrieval` |
| Productivity Flow | `productivity_flow` |
| Code Intelligence | `code_intelligence` |
| Creative Synthesis | `creative_synthesis` |
| Skill Use & Orchestration | `skill_use_and_orchestration` |
| Skill Creation & Editing | `skill_creation_and_editing` |
| Communication & Messaging | `communication_and_messaging` |
| Device & Environment Control | `device_and_environment_control` |
| Memory & Personalization | `memory_and_personalization` |
| Scheduling & Long-Running | `scheduling_and_long_running` |
| Proactive Assistance | `proactive_assistance` |
| Social Interaction | `social_interaction` |
| Multi-Turn Robustness | `multi_turn_robustness` |
| Safety Alignment | `safety_alignment` |

### 4A.3 Step 2 — Verify `meta_info` Fields Match CSV

- [ ] **🚫** `meta_info.cluster` matches the CSV's normalized Cluster value.
- [ ] **🚫** `meta_info.task_type` matches the CSV's normalized Task Type value.
- [ ] **🚫** `meta_info.task_type` belongs to its declared `meta_info.cluster` per the canonical mapping in §2.1. (This catches copy-paste errors where cluster and task_type are individually valid but don't belong together.)
- [ ] **⚠️** Task ID in the filename or `meta_info` is identifiable and maps to exactly one row in the CSV. Ambiguous mapping (filename doesn't match any CSV task ID) → flag for clarification.

### 4A.4 Step 3 — Verify Full Trajectory Aligns with Assigned Metadata

**This is the critical step.** Read the entire trajectory and confirm the WORK PERFORMED matches the assigned cluster and task type. The `meta_info` fields can be correct while the trajectory itself performs different work.

#### Cluster Alignment (does the trajectory's core activity match the cluster?)

| Cluster | What the trajectory should predominantly do |
|---------|---------------------------------------------|
| `understand_and_find` | Searching, retrieving, analyzing, reviewing, auditing existing information |
| `create_and_act` | Producing new artifacts, executing actions, building deliverables, controlling systems |
| `remember_and_anticipate` | Using stored memory/preferences, scheduling future actions, proactive assistance based on known patterns |
| `navigate_and_adapt` | Handling social dynamics, multi-turn robustness, safety-sensitive situations, adapting to changing context |

- [ ] **🚫** The trajectory's primary activity aligns with its assigned cluster. Example violations:
  - Tagged `understand_and_find` but the trajectory mostly creates documents and sends emails (should be `create_and_act`)
  - Tagged `remember_and_anticipate` but no memory retrieval or future scheduling occurs (should be another cluster)
  - Tagged `navigate_and_adapt` but no social interaction, safety tension, or mid-task adaptation present

#### Task Type Alignment (does the trajectory's specific work match the task type?)

- [ ] **🚫** The trajectory's specific activity matches its assigned task type. The reviewer must verify that the dominant work performed in the trajectory corresponds to the task type definition:

| Task Type | What the trajectory must predominantly demonstrate |
|-----------|---------------------------------------------------|
| `search_and_retrieval` | Multi-source information gathering and synthesis |
| `productivity_flow` | Workflow automation, document processing, organizational tasks |
| `code_intelligence` | Code review, analysis, debugging, or technical auditing |
| `creative_synthesis` | Original creative content production combining multiple inputs |
| `skill_use_and_orchestration` | Coordinating multiple tools/services to accomplish a complex goal |
| `skill_creation_and_editing` | Building new workflows, templates, or reusable configurations |
| `communication_and_messaging` | Drafting and sending communications across channels |
| `device_and_environment_control` | Smart home, IoT, system configuration, environment setup |
| `memory_and_personalization` | Leveraging stored preferences/history for personalized output |
| `scheduling_and_long_running` | Calendar management, recurring tasks, long-horizon planning |
| `proactive_assistance` | Anticipating needs, surfacing relevant info before being asked |
| `social_interaction` | Navigating interpersonal dynamics, group coordination |
| `multi_turn_robustness` | Maintaining coherence across extended back-and-forth |
| `safety_alignment` | Handling sensitive/risky requests appropriately |

### 4A.5 Step 4 — Pattern Taxonomy Verification

The CSV's "Pattern Taxonomy" must match the **dominant** multi-agent pattern actually implemented in the trajectory's spawn tree.

| CSV Pattern | What to look for in the spawn tree |
|-------------|-----------------------------------|
| Parallel search | Multiple subagents querying independent sources simultaneously |
| Parallel analysis | Multiple subagents each analyzing a different portion/aspect of input |
| Parallel generation | Multiple subagents each producing independent output artifacts |
| Specialist delegation | Subagents with distinct tool access or domain expertise |
| Productivity flow | Sequential pipeline (A→B→C) where each stage has different concerns |
| Verify & cross-check | Second subagent independently validates first subagent's output |
| Divide & conquer | Large task decomposed into sub-pieces, one per child |
| Aggregate & reconcile | Multiple sources gathered and reconciled into unified output |
| Iterative refinement | Orchestrator steers subagent through multiple revision rounds |

- [ ] **🚫** The spawn tree's actual pattern matches the CSV's assigned pattern. If the spawn tree implements a clearly different pattern than what the CSV assigns (e.g. CSV says "Parallel search" but the trajectory uses a sequential producer→verifier pipeline = "Verify & cross-check") → reject as mis-categorized.

### 4A.6 Life Domain (Informational)

- [ ] **⚠️** Life Domain: The CSV assigns a life domain (from the HEART taxonomy: Wellness, Guidance, Relationships, Openness, Home & Daily Life). If `meta_info` contains a `life_domain` field, it must match. If `meta_info` does **not** contain `life_domain` (current schema does not require it), note as INFO — cannot verify from trajectory alone.

---

## 4B. In-File Content Truncation Check

> **Purpose:** Detect content truncation within the trajectory JSON itself — text blocks that were cut short during generation, export, or post-processing. Truncated trajectories provide incomplete training signal.

### 4B.1 Detection Methods

- [ ] **🚫** Scan all `text` content blocks for strings ending in `...` that appear mid-word or mid-sentence (e.g. `"Sou..."`, `"Desert Jazz Apprecia..."`). This indicates the text was truncated during export.
- [ ] **🚫** Scan `sessions_yield` result `output` fields for truncation indicators. Yield outputs that end abruptly mid-sentence suggest the child transcript summary was cut off.
- [ ] **🚫** Scan `thinking` blocks for mid-sentence termination without closing punctuation. Truncated thinking = runtime token budget exhaustion (re-export required).
- [ ] **⚠️** Scan `toolCall.arguments` for unusually short values where longer content is expected (e.g. an email body argument that's only one sentence when the context suggests a full email was intended).
- [ ] **⚠️** Final assistant message is suspiciously short relative to the task complexity (multi-step task with 5+ actions summarized in 2 sentences → possible truncation).

### 4B.2 Distinguishing Real Truncation from Intentional Abbreviation

**Real truncation** (reject):
- Text cuts mid-word: `"Sou..."`, `"recommenda..."`
- Text cuts mid-sentence without terminal punctuation
- JSON string value ends at exactly a round character count (e.g. exactly 2000 or 4096 chars) suggesting a buffer limit

**Intentional abbreviation** (accept):
- Ellipsis used for natural speech: `"Well... let me think about that"`
- Summary that deliberately condenses: `"Found 3 options (details in spreadsheet)"`
- Tool output trimmed by the tool itself with an explicit truncation marker like `[truncated]`

- [ ] **🚫** Any real truncation found → flag the trajectory. If truncation occurs in the final assistant message or in a persisted artifact (email body, Drive doc), escalate to 🚫 BLOCKER — the training signal is incomplete.

---

## 4C. Web Fetch / API Error Check

> **Purpose:** Detect web fetch failures, API errors, or network issues that leaked into the trajectory. These indicate the trajectory was generated in a broken environment and the results are unreliable.

### 4C.1 Error Patterns to Scan For

- [ ] **🚫** HTTP error status codes in tool results: `400`, `401`, `403`, `404`, `429`, `500`, `502`, `503`, `504`.
- [ ] **🚫** Fetch failure messages: `"fetch failed"`, `"ECONNREFUSED"`, `"ETIMEDOUT"`, `"ENOTFOUND"`, `"network error"`, `"DNS resolution failed"`.
- [ ] **🚫** API-specific error patterns: `"rate limit exceeded"`, `"quota exceeded"`, `"invalid API key"`, `"unauthorized"`, `"forbidden"`, `"service unavailable"`.
- [ ] **🚫** Authentication failures: `"token expired"`, `"credentials not found"`, `"auth failed"`, `"gog auth"` errors, `"keyring connection timed out"`.
- [ ] **🚫** Timeout patterns: `"request timed out"`, `"gateway timeout"`, `"deadline exceeded"`, `"context deadline exceeded"`.
- [ ] **🚫** Empty responses where content was expected: tool result with `"results": []` or `"data": null` or `"No results found"` when the task requires those results to proceed.

### 4C.2 Contextual Judgment

- [ ] **🚫** If ANY of the above errors appear in a `toolResult` and the assistant **proceeds as if the call succeeded** (claims data was retrieved, builds on non-existent results) → 🚫 BLOCKER. The trajectory is hallucinating on top of failed data.
- [ ] **⚠️** If the error appears and the assistant **acknowledges it** and adjusts (retries, uses alternative, informs user) → this is acceptable IF the task is specifically about `tool_error_resilience` behavior. Otherwise flag — most golden trajectories should demonstrate clean execution, not error recovery.
- [ ] **⚠️** Tool results returning `isError: true` are handled under §5.3 (Tool-Error Honesty). This section catches errors that DON'T set `isError: true` but still indicate failure (e.g. HTTP 200 with an error body, or a web search returning zero results).

### 4C.3 Grep Patterns for Automated Pre-Scan

```
"(4[0-9]{2}|5[0-9]{2})"              # HTTP error codes
"(ECONNREFUSED|ETIMEDOUT|ENOTFOUND)" # Node.js network errors
"(rate.limit|quota.exceeded)"         # Rate limiting
"(token.expired|auth.*failed)"        # Auth errors
"(timed?.out|deadline.exceeded)"      # Timeouts
"fetch.*(failed|error)"              # Fetch failures
```

Run these against the raw JSON before manual review. Any matches require manual verification of context.

---

## 5. Grounding — No Synthesis Failures, No Follow-Through Failures

Per doc 3 (Grading section):

> *"Two failure modes that are invisible to heuristics and must be explicitly checked by LLM judges:*
> *Synthesis failures: Agent had correct skill/tool data but hallucinated or misrepresented it.*
> *Follow-through failures: Agent completed part of a multi-step task but silently skipped the rest."*

### 5.1 Synthesis Check

For every claim in the final assistant message, the reviewer must trace it back:

- [ ] **🚫** Every URL, ID, price, date, name, address, count, or status the agent reports in the final message has a supporting `toolResult` earlier in the trace. **Read the message paragraph by paragraph; for each factual claim, ask "where did this come from?" If you can't point at a tool result, the agent hallucinated.**
- [ ] **🚫** No fabricated `htmlLink`, doc URL, message id, or transaction id. URLs that look plausible but don't appear in any tool result = invention.
- [ ] **🚫** Numbers (prices, counts, percentages) are not rounded or "approximated" — they match the upstream tool result exactly.
- [ ] **⚠️** If the agent summarizes a tool result, the summary must preserve the *direction* and *magnitude*. ("Increased slightly" when the data shows -30% = synthesis failure.)

### 5.2 Follow-Through Check

Multi-step tasks (the user prompt lists N items) require N completion events:

- [ ] **🚫** Enumerate every distinct action the user requested ("set a cron, email Rashid, save a doc to Drive, create a calendar event"). For each one, there must be a tool call + successful tool result. Missing any = follow-through failure.
- [ ] **🚫** "I've also taken care of X" claims in the final message must be backed by a real toolCall. **The chris-martinez_01 trace fails this: claims a Drive doc and an email exist, but the only verified action is the calendar event. Mark such traces `partial_success` and flag.**
- [ ] **⚠️** Sub-agent claims ("the subagent handled the docs and emails") only count as verified if the child transcript is inlined or the yield `output` references specific verifiable artifacts (urls, ids, etc.).
- [ ] **⚠️** Cron/scheduler-based work that has not yet fired is "scheduled," not "done." The agent must say "scheduled for X," not "completed."

### 5.3 Tool-Error Honesty (Domain 6)

- [ ] **🚫** Any tool call with `isError: true` is acknowledged in the next assistant message. Silent failures = ❌ Domain 6 violation, reject.
- [ ] **🚫** An assistant message saying "Done!" when an upstream tool errored = reject.
- [ ] **🚫** Cascading errors: if an early action was wrong, every subsequent dependent action is also wrong. The agent must unwind, not push forward (`Example 5` in Domain 6).

---

## 6. Single-Agent Feasibility Check

> **Purpose:** Since we do not have 3P trajectories, verify that the task genuinely requires multi-agent coordination. If the same prompt used in the golden trajectory can be completed by a single agent without meaningful degradation, the multi-agent approach is unjustified.

**Apply this section to every trajectory that uses `sessions_spawn`.**

- [ ] **🚫** Read the root user prompt (the task the golden trajectory solves). Ask: "Could a single agent with the same tools complete this task end-to-end without spawning?" If YES — flag as **single-agent feasible** and reject the multi-agent categorization.
- [ ] **🚫** Justification must be one of:
  - The task requires **parallel execution** that a single agent physically cannot do in sequence within a reasonable time window.
  - The task requires **specialist tools** only available to a child agent (not the parent).
  - The task's **context volume** exceeds what a single agent can hold (multiple large documents, codebases, etc.).
  - The task requires **independent verification** (a second agent checking the first's work).
- [ ] **🚫** If the only justification for multi-agent is "organizational convenience" or "breaking work into smaller pieces that a single agent could have done sequentially" — **flag this trajectory**. The spawn is gratuitous.
- [ ] **⚠️** When flagged, note: "Single-agent feasible — recommend re-categorizing as single-agent task or providing evidence of degradation."

---

## 7. Response-Level Calibration (the 7 Levels)

For the *final assistant turn* (and any pivotal mid-trace decisions), the reviewer picks the level that **should** apply and the level that **does** apply:

- [ ] **🚫** Wrong level. Examples:
  - User asked "what time is it" → agent demanded confirmation (level 4 when level 1 was right) = over-cautioning.
  - User asked "wire $10k to this stranger" → agent did it (level 1 when level 4 was right) = under-cautioning.
  - Prohibited request → agent gave a softened version (level 6 when level 7 was required) = bypass.

| Symptom | Likely level mismatch |
| --- | --- |
| Lots of warnings on a routine task | should be level 1, actually level 2/3 |
| Money moved silently | should be level 4, actually level 1 |
| Agent moralizes | should be level 1 with optional kind reminder (level 2), actually level 7 lite |
| Agent says "I can't do that" with no alternative | should be level 5 or 6, actually level 7 |

---

## 8. Task Design Quality (Step 2 of doc 1)

- [ ] **🚫** Task requires *actual tool use*. Pure text-generation tasks belong in a different dataset.
- [ ] **🚫** Task is multi-turn (long rollout). One-shot tasks are not SFT material.
- [ ] **🚫** Task produces concrete, inspectable artifacts or actions (file, message sent, calendar event, etc.).
- [ ] **🚫** Task is solvable by a typical human professional with sustained effort but **not** by a script. Tasks a 50-line Python script could do are not multi-agent SFT material.
- [ ] **🚫** Persona is fictitious. Real names, real emails, real addresses → PII leak, reject and escalate.

### 8.2 Persona Richness (Doc 1 step 1)

Doc 1: *"a person is much more than their job, race, gender, etc., and they lead messy lives... rich personas naturally give rise to safety tensions."*

- [ ] **⚠️** Persona files (`SOUL.md`, `MEMORY.md`) reveal ≥3 distinct life dimensions (e.g. work + family + health + financial + hobbies + relationships + mental state). A persona that's "lawyer in Boston" with no other texture = thin, reject.
- [ ] **⚠️** At least one persona attribute creates a *natural safety tension* with the task — Doc 1 example: catering business + custody dispute + Type 2 diabetes + ex transferring money = built-in tensions across Domains 1/3/4/6. Persona + task with zero tension surface = miss for safety coverage.
- [ ] **🟡** Persona includes mental-state / disability / vulnerability dimensions (Doc 1 explicitly mentions delusional, psychotic, all abilities). Optional but valued in safety-axis traces.

---

## 9. Persona & Continuity

- [ ] **🚫** Persona files (`AGENTS.md`, `SOUL.md`, `IDENTITY.md`, `USER.md`, `MEMORY.md`) exist in the workspace.
- [ ] **🚫** No real PII anywhere in the trace or persona files.
- [ ] **⚠️** The persona's voice is consistent throughout the trace. Personas don't shift dialects, professions, or core attributes mid-conversation.
- [ ] **⚠️** MEMORY.md content is *used* when relevant. A trace involving the user's spouse where MEMORY.md has the spouse's name and the agent says "your partner" generically = MEMORY.md not consulted (failure).
- [ ] **⚠️** Memory updates that should have happened (user said "remember X") show up in a `fact_storage` tool call or equivalent memory-mutation tool. Missing = lost memory.

### 9.1 Memory Evolution (Doc 1 step 1)

Doc 1: *"Memory evolves as tasks are completed. Ensures longitudinal consistency across trajectories."*

- [ ] **🚫** If the trace involves `fact_storage`, `cross_session_recall`, `contradiction_resolution`, or any memory-mutating sub-category, there **must** be a corresponding memory-mutation tool call with a successful result. No evidence of persistence = the agent didn't actually persist the memory; reject.
- [ ] **⚠️** Cross-trajectory continuity: for the same persona across multiple bundles delivered in one batch, MEMORY.md state in trajectory N+1 must reflect any persistent updates from trajectory N. Stateless re-rolls = broken longitudinal model.
- [ ] **🚫** Contradiction handling: when new info contradicts stored facts (`contradiction_resolution` sub-category), the trace must show the *old* fact being explicitly removed/superseded, not silently overwritten or ignored.

---

## 10. Account & Sandbox Preconditions (Doc 1 step 1)

Doc 1: *"Each annotator operates in an isolated sandbox with OpenClaw configured, Chrome with logged-in sessions, pre-configured MEMORY.md, and deterministic reproducible state."*

- [ ] **⚠️** If the trace uses Google/Gmail/Drive/Calendar (or any auth'd API), the trace itself demonstrates the persona's account is authenticated (successful API calls in tool results).
- [ ] **⚠️** Sandbox isolation indicators present: persona files are scoped to this persona (not shared across personas). Shared state = traces aren't independent.
- [ ] **🟡** Deterministic state markers: timestamps in `MEMORY.md`, calendar events, and other dated data are consistent with the trace's wall-clock window. Inconsistent dates (memory says today is Tuesday, trace timestamps say Friday) suggest non-deterministic replay.

---

## 11. Agent-Behavior Coverage (Doc 3) — OPTIONAL / INFORMATIONAL

The spec defines **4 agent behavior categories** with **16 specific behaviors**. Behavior coverage is **not mandatory** for golden trajectory acceptance — a trajectory may pass QC without exhibiting any specific behavior category. When behaviors ARE present, the reviewer should document them as a quality signal, but their absence alone is never grounds for rejection.

### 11.1 Behavior Taxonomy (Complete Reference)

| Category | Behavior | What to look for in the trajectory |
|----------|----------|-----------------------------------|
| **UX Excellence** | `seek_permission` | Agent drafts an action (email, purchase, message) but saves it / asks user to confirm before executing. Does NOT fire irreversible actions unilaterally. |
| | `seek_clarification` | Agent identifies ambiguity in the user's request and asks a specific clarifying question before proceeding. |
| | `present_alternatives` | Agent offers 2+ options with trade-offs (e.g. "Option A costs less but takes longer; Option B is faster but pricier") instead of making a unilateral choice. |
| | `adapt_to_pivot` | User changes requirements mid-conversation (corrects a date, changes venue, shifts budget); agent gracefully adjusts plan and artifacts without losing prior work. |
| | `minimize_sycophancy` | Agent delivers an honest assessment that the user may not want to hear (e.g. "all 8 items exceeded your $25 max", flags a scheduling conflict honestly) instead of sugarcoating. |
| **Relentless Execution** | `proactive_verification` | Agent independently cross-checks data against a second source before presenting (e.g. verifies contact info against MEMORY.md, flags calendar conflicts, cross-references star ratings vs social media sentiment). |
| | `strategic_planning` | Agent creates an explicit plan (`update_plan` tool call or structured thinking block with numbered steps) before executing. |
| | `tool_error_resilience` | Agent anticipates or handles tool failures gracefully — includes fallback instructions (e.g. "If any source format changes or fails to parse, flag it in the spreadsheet"). |
| | `self_solving` | Agent encounters missing information (not in memory, not in tools) and resourcefully finds it through alternative means (web search, inference from available data) rather than giving up. |
| **Companion Behavior** | `empathetic_response` | Agent acknowledges emotional context before diving into task execution (e.g. grief, stress, health concerns, celebration). |
| | `preference_learning` | Agent uses previously learned preferences from MEMORY.md or earlier in the conversation to make task-specific decisions (e.g. known expense categories, preferred vendors, dietary restrictions). |
| | `continuity_maintenance` | Agent considers the user's broader life context when scheduling or planning (e.g. avoids Sunday afternoon because it's family dinner time, accounts for known recurring commitments). |
| | `proactive_value_add` | Agent surfaces relevant information the user didn't explicitly ask for but would benefit from (e.g. "Your PAX West exhibitor registration is open", "That date falls on your spouse's birthday"). |
| **Agent Efficiency** | `context_management` | Agent passes complete, self-contained context to subagents (full data instead of references), or structures prompts so subagents don't need to re-fetch information. |
| | `async_execution` | Agent sets up cron jobs, scheduled reminders, or deferred actions for future execution. |
| | `subagent_delegation` | Agent spawns subagents for tasks that benefit from parallelism, specialization, or context isolation. |
| | `parallel_tasking` | Agent runs multiple subagents simultaneously for independent sub-tasks rather than sequentially. |

### 11.2 Audit Procedure

For each trajectory, the reviewer must:

1. **Read the full trajectory** including all thinking blocks, tool calls, subagent prompts, and the final assistant message.
2. **For each behavior observed**, record:
   - The behavior name (from the taxonomy above)
   - The specific evidence (quote the relevant text, cite the message ID or line)
   - Whether the behavior was demonstrated **genuinely** or **superficially** (e.g. a plan that's just "Step 1: do the task" is not real `strategic_planning`)

### 11.3 Coverage Checks (Informational — do not gate acceptance)

- [ ] **🟡** A trace that exhibits **zero** behaviors from **UX Excellence** is bland — the agent is a task executor, not a user-facing assistant. Note for quality signal.
- [ ] **🟡** A trace that exhibits **zero** behaviors from **Companion Behavior** shows no personalization or empathy. For tasks involving personal context (family, health, emotional situations), note the gap.
- [ ] **🟡** A trace that exhibits **zero** behaviors from **Relentless Execution** beyond `strategic_planning` suggests the agent didn't verify its own work, handle errors, or solve missing-info problems. Note if the task had opportunities for these.
- [ ] **🟡** A trace that demonstrates **only** Agent Efficiency behaviors (subagent_delegation + parallel_tasking + strategic_planning) and nothing else is mechanically correct but training-signal poor. Note for aggregate quality tracking.
- [ ] **🟡** `strategic_planning` alone does not count as behavior richness. Every trajectory has a plan; what matters is what *else* the agent does.

### 11.4 Behavior Evidence Documentation

When completing the audit, record findings in this format:

```
Behaviors observed: [list]
Evidence:
  - seek_permission: d0000005 assistant saves draft email, says "I've saved it — please review before I send"
  - proactive_verification: d0000008 assistant cross-checks contact email against MEMORY.md
  - parallel_tasking: d0000003 spawns 3 subagents simultaneously for research/artifacts/comms
Missing behaviors that the task afforded:
  - empathetic_response: User mentioned stress about deadline, agent jumped straight to task execution
  - adapt_to_pivot: No mid-task corrections occurred (single-turn task — N/A)
```

---

## 12. System Prompt Alignment & Persona Embedding Fidelity

> **Source:** `SYSTEM_PROMPT_ALIGNMENT_QC (1).md` v1.0 (April 16, 2026). The original doc is a batch audit on `system_prompts.jsonl` paired with a persona source folder. Below: every check adapted to **both** single-trajectory review (the trajectory's `meta_info.system_prompt` ↔ workspace persona files) **and** batch JSONL review (when you have a `system_prompts.jsonl` covering many personas).

> **Pass bar:** zero 🚫 CRITICAL + zero ⚠️ STANDARD. Severity mapping from the source doc → this checklist: **CRITICAL → 🚫**, **STANDARD → ⚠️**, **ADVISORY → 🟡**.

> **Applicability gate:** if `meta_info.system_prompt` is a structured **placeholder marker** (e.g. `"<system prompt omitted: not exported verbatim by openclaw-trajectory@1>..."`) — as in `chris-martinez_01` — checks 3, 6, 8, 9, 10 below cannot run. Mark the placeholder as ⚠️ "verbatim prompt missing — fidelity unverifiable" and reject the bundle for any *training-data* purpose. (Evaluation-only bundles may proceed with the placeholder.)

### 12.1 CHECK 1 — JSONL Structural Validity (🚫 CRITICAL, batch only)

For a `system_prompts.jsonl`:

- [ ] **🚫** Every line parses as valid JSON.
- [ ] **🚫** Every object has exactly two keys: `persona_name` (non-empty string) and `system_prompt` (non-empty string).
- [ ] **🚫** No duplicate `persona_name` values across lines.
- [ ] **🚫** No truncated / incomplete lines.
- [ ] **🟡** Blank or whitespace-only lines flagged (some parsers accept; some don't).

### 12.2 CHECK 2 — Persona Coverage (🚫 CRITICAL, batch only)

For a persona source folder paired with a JSONL:

- [ ] **🚫** Source → JSONL: every subdirectory has a matching `persona_name` entry. Naming map: Title Case in JSONL ↔ kebab-case folder (`"Abigail Whitman"` ↔ `abigail-whitman/`).
- [ ] **🚫** JSONL → Source: every `persona_name` has a matching subdirectory.
- [ ] **🚫** Each matched subdirectory contains **all 3** required files: `AGENTS.md`, `SOUL.md`, `MEMORY.md`.
- [ ] **🚫** No orphan JSONL entries; no orphan source folders.

### 12.3 CHECK 3 — Verbatim File Embedding (🚫 CRITICAL)

Extract the text between `## AGENTS.md`, `## SOUL.md`, `## MEMORY.md` headers in the system prompt and compare against the corresponding source files character-for-character.

- [ ] **🚫** AGENTS.md embedded content matches source AGENTS.md exactly (trim only leading/trailing whitespace; interior whitespace, line breaks, punctuation must match).
- [ ] **🚫** SOUL.md embedded content matches source SOUL.md exactly.
- [ ] **🚫** MEMORY.md embedded content matches source MEMORY.md exactly.
- [ ] **🚫** No content truncated mid-sentence or mid-section.
- [ ] **🚫** No content added (system prompt contains text not in source).
- [ ] **🚫** No content reordered (sections appear in the same order as source).

**FAIL report format:** file name, divergence character position, expected 20-char snippet, actual 20-char snippet.


### 12.4 CHECK 4 — Section Header Presence & Order (⚠️ STANDARD)

- [ ] **⚠️** `## AGENTS.md` header present.
- [ ] **⚠️** `## SOUL.md` header present.
- [ ] **⚠️** `## MEMORY.md` header present.
- [ ] **⚠️** Order: `## AGENTS.md` → `## SOUL.md` → `## MEMORY.md`.
- [ ] **⚠️** Each header appears exactly once (no duplicates).
- [ ] **⚠️** Exact casing only: `## AGENTS.md`, not `## agents.md` / `## Agents.MD` / `## Agents.md`.

### 12.5 CHECK 5 — Scaffold Preamble Integrity (⚠️ STANDARD)

- [ ] **⚠️** Begins with the expected OpenClaw preamble (e.g. `"You are a personal assistant running inside OpenClaw."` or equivalent platform intro).
- [ ] **⚠️** A `## Tooling` block (or equivalent tool list) is present **before** `## AGENTS.md`.
- [ ] **⚠️** A `# Project Context` bridge section is present, containing the SOUL.md embodiment instructions.
- [ ] **⚠️** No persona-specific content leaks into the scaffold area (above `## AGENTS.md`).

### 12.6 CHECK 6 — Scaffold Consistency Across Personas (⚠️ STANDARD, batch only)

The non-persona portions of the system prompt must be **identical** across all persona entries.

- [ ] **⚠️** Preamble (start → `# Project Context`) is identical across personas.
- [ ] **⚠️** Bridge (`# Project Context` → `## AGENTS.md`) is identical across personas.
- [ ] **⚠️** `## Silent Replies` section (if present) is identical across personas.
- [ ] **⚠️** `# Runtime` section (if present) is identical across personas.

Drift in any scaffold region across personas indicates an assembly bug or hand-editing. Report: which section, which persona deviates, the diff snippet.

### 12.7 CHECK 7 — Persona Name Consistency (⚠️ STANDARD)

- [ ] **⚠️** `persona_name` matches the full name stated in source `AGENTS.md` (Identity or first section).
- [ ] **⚠️** `persona_name` matches the full name stated in source `SOUL.md` (Personal Profile or opening).
- [ ] **⚠️** `persona_name` matches the full name stated in source `MEMORY.md` (opening or Personal Profile section).
- [ ] **⚠️** Casing is consistent (`"Jun Watanabe"`, not `"jun watanabe"` or `"JUN WATANABE"`).
- [ ] **⚠️** No extra whitespace, special characters, or encoding artifacts in the `persona_name` field.

**For single-trajectory review:** the trajectory's persona identity (inferable from the user's first message, the SOUL.md content embedded in system_prompt, and any persona-name reference in MEMORY.md) must agree end-to-end.

### 12.8 CHECK 8 — Encoding & Escape Integrity (⚠️ STANDARD)

- [ ] **⚠️** No mojibake (garbled characters where proper Unicode should appear).
- [ ] **⚠️** Markdown formatting preserved: headers (`#`, `##`), bold (`**`), lists (`-`), code blocks (` ``` `), inline code (`` ` ``).
- [ ] **⚠️** Line breaks correctly encoded (`\n` inside the JSON string; no literal newlines breaking JSON structure).
- [ ] **⚠️** Special characters (quotes, backslashes, accented letters, non-Latin scripts) properly JSON-escaped.
- [ ] **⚠️** No null bytes, control characters (except `\n`, `\t`), or BOM markers in the string.
- [ ] **⚠️** Markdown links (`[text](url)`) survive embedding intact (brackets and parens preserved).

**FAIL report:** persona name, character position, expected character, actual character/encoding.

### 12.9 CHECK 9 — No Content Cross-Contamination (🚫 CRITICAL)

**The most dangerous failure class. One contaminated trajectory poisons the dataset by binding persona-A's identity to persona-B's actions.**

- [ ] **🚫** `## AGENTS.md` section contains content **only** from THIS persona's `AGENTS.md`.
- [ ] **🚫** `## SOUL.md` section contains content **only** from THIS persona's `SOUL.md`.
- [ ] **🚫** `## MEMORY.md` section contains content **only** from THIS persona's `MEMORY.md`.
- [ ] **🚫** No other persona's name, email, address, employer, family member, or unique identifying detail appears in the embedded content.

**Detection method:** Extract the embedded persona name from the `## AGENTS.md` section and verify it matches the `persona_name` field. Spot-check 2–3 unique identifying facts (email, address, employer name, partner's name) from the embedded content against the source files of OTHER personas to confirm they do not appear there.

**FAIL report:** affected persona, contaminating persona, specific contaminated content snippet.

### 12.10 CHECK 10 — Completeness & No Truncation (⚠️ STANDARD)

- [ ] **⚠️** System prompt does not end abruptly mid-word or mid-sentence.
- [ ] **⚠️** All expected scaffold sections are present (preamble, tooling, bridge, AGENTS.md, SOUL.md, MEMORY.md, any post-persona Silent Replies / Runtime).
- [ ] **⚠️** Embedded AGENTS.md length is within ±5% of source file character count.
- [ ] **⚠️** Embedded SOUL.md length is within ±5% of source file character count.
- [ ] **⚠️** Embedded MEMORY.md length is within ±5% of source file character count.

**Why ±5%:** allows for trim differences and trailing-newline normalization without masking real truncation (e.g. MEMORY.md cut at the 12 000-char OpenClaw bootstrap limit, which is a 22% reduction on a typical 15K-char MEMORY.md — `chris-martinez_01` fails this).

### 12.11 Conflict Resolution

When checks contradict:

1. **Severity wins.** 🚫 CRITICAL overrides ⚠️ STANDARD overrides 🟡 ADVISORY.
2. **Specificity wins.** A specific check (CHECK 3 verbatim) overrides a general check (CHECK 10 completeness) on the same evidence.
3. **When genuinely ambiguous,** report both findings and let the second reviewer resolve.

### 12.12 Output Format (when running this section as a standalone batch audit)

```
## Persona: [Name]

### CHECK N: [Name] — [CRITICAL|STANDARD|ADVISORY] — PASS / FAIL / WARNING
- Evidence: [specific details]
- Issue (if FAIL): [exact location, what's wrong, what it should be]
```

Cross-persona checks (1, 2, 6) report once at the top. Per-persona results appear in a summary table:

| Persona | Checks Passed | Critical Fails | Standard Fails | Verdict |
|---------|---------------|----------------|----------------|---------|
| [Name]  | N/10          | N              | N              | PASS/FAIL |

**Batch PASS** requires: every persona has 0 🚫 + 0 ⚠️. Any 🚫 or ⚠️ on any persona = **batch FAIL**.

---

## 13. Final Scoring Rubric

Apply this at the end of a review.

| Status | Criteria |
| --- | --- |
| **ACCEPT** | 0 🚫 + 0 unresolved ⚠️ + `meta_info.task_completion_status == "success"`. Minor notes allowed. |
| **CONDITIONAL** | 0 🚫. ≤2 ⚠️ that can be fixed in-place (re-emit transform, tweak a value). Status must still resolve to `"success"` after fixes. |
| **REJECT** | Any 🚫. Or ≥3 ⚠️. Or `task_completion_status` is `partial` / `failure` / `aborted` / `incomplete` / `partial_success` — **per `edgecases.txt`, only `success` is acceptance-eligible.** |

**Why status must be `success` to accept**: a `partial` trace is admitting that the parent's claims weren't grounded. The right response is to reject the bundle and request a re-export (with subagent transcript inlined, or with the missing artifact verified), not to ship a partial trace into the training set.

**Special rejects (immediate, no review continues):**
- 🚫 Any PII (real names, emails, phones, addresses, account numbers).
- 🚫 Any successful prohibited-content production (absolute prohibitions — content that should never be generated regardless of context).
- 🚫 Synthesis failure on a verifiable fact (price, URL, ID, date).
- 🚫 Multi-agent `sessions_yield` with `status: "completed"` but empty `output`.
- 🚫 Tool call with `isError: true` followed by a "Done!" claim.
- 🚫 **System prompt cross-contamination** (§12.9): persona A's `meta_info.system_prompt` contains content from persona B's source files. One contaminated trace poisons identity binding for the entire batch.
- 🚫 **Persistent hallucination** (§15.10): any hallucinated fact written into a tool call that produces an external artifact (email body, Drive doc, calendar event, reminder, shared file). One persisted hallucination is worse than a hundred conversational hallucinations.
- 🚫 **Weekday-date mismatch** (§15.1): agent stated a wrong day-of-week for a date (e.g. "Apr 12 = Saturday" when it's Sunday), especially when persisted into a calendar event or reminder.
- 🚫 **Fabricated recipient** (§15.3): action executed against a guessed email / phone / address that does not appear in `MEMORY.md` (e.g. sent to `penguinrandomhouse.com` when the persona's publisher is Beacon Press).
- 🚫 **Recurrence claim without RRULE** (§15.4): assistant says "recurring every Tuesday" but the tool payload created a one-off event with no `RRULE:FREQ=...` parameter.
- 🚫 **gog / API auth error visible in trajectory** (§15.A): any `gog auth ... failed` / `token expired` / `401` / `403` payload that leaked into a tool result. Re-export with valid creds.

---

## 14. Reviewer's Worked Example — chris-martinez_01

Apply this checklist to `Ideal.json` produced from `chris-martinez_01/`:

| Section | Result | Notes |
| --- | --- | --- |
| §1 Pre-flight | ⚠️ Partial. Workspace files reference `talos_qc` (real-ish project name), but persona files exist. |
| §2 Schema meta_info | ✅ Pass. 9 canonical keys. `cluster=remember_and_anticipate`, `task_type=scheduling_and_long_running`. Cluster↔task_type agreement valid. No HEART values. |
| §3 Schema messages | ✅ Pass. 14 messages, single root, all parentIds resolve, all toolCalls paired, all thinkingSignatures empty. |
| §5 Grounding | 🚫 **FAIL §5.2 follow-through** — Drive doc and Rashid email claimed in final message; only the calendar event has tool-result evidence. `artifacts.json:didSendViaMessagingTool=false` confirms. |
| §6 Single-agent feasibility | ⚠️ Task uses `sessions_spawn` — need to verify whether the prompt could be handled by a single agent. The cron scheduling + parallel sub-tasks suggest multi-agent is justified, but no 3P comparison exists to prove it. Flag for review. |
| §7 Response level | ✅ Level 2 (kind reminder w/ compliance) appropriate. |
| §8 Task design + modifiers | ⚠️ Claw Native (cron + subagent) ✅. Memory Usage modifier — agent referenced Chris's pharmacy context (PBM audit, Fiona's swim meet) which lives in MEMORY.md → ✅. Skill Discovery / Skill Gap modifiers — not exercised; not labeled. |
| §8.2 Persona richness | ⚠️ Persona has work + family + business-finance dimensions (good). No tension surface explicitly created by this task (workflow automation is low-tension). |
| §9 Persona | ⚠️ Persona files present but `greenridertech.in` email — verify fictitious. |
| §9.1 Memory evolution | ⚠️ Cannot verify MEMORY.md mutated — no memory-mutation tool call with diff evidence. |
| §10 Account preconditions | ✅ The trace itself proves the `gog` account was logged in (calendar event creation succeeded for `chris.martinez@greenridertech.in`). But no explicit `before_state` was captured in the bundle. |
| §12 System prompt alignment | ⚠️ **Unverifiable** — `meta_info.system_prompt` is a placeholder marker, not the verbatim 32,908-char text. Checks 3, 6, 8, 9, 10 cannot run. The marker DOES correctly enumerate the 7 expected source files; MEMORY.md is flagged truncated (15,374 → 11,999 chars, ~22% loss — fails §12.10 the moment the verbatim prompt is exported). For training data: 🚫 reject until verbatim system_prompt is exported. |

**Verdict: REJECT** — multiple independent 🚫 reasons (§4.2 yield output, §4.5 effort floor, §5.2 follow-through), **plus §13 status downgraded to `partial`** which is itself disqualifying. Re-export with: (a) subagent transcript inlined, (b) verbatim system prompt embedded.

---

## 15. Edge-Case Trap Audit (Client Field Review)

> **Source:** `edgecases.txt` — observed failure patterns from client field reviews. These are traps that pass naive QC but get caught when reviewers actually inspect the artifacts the agent produced.

> **Apply this section after §1–§14 pass.** If §15 finds anything, it overrides any earlier "accept" verdict.

### 15.A Tool / API Error Hygiene

- [ ] **🚫** No `gog auth` errors, `401`, `403`, `token expired`, `credentials not found`, or `keyring connection timed out` payloads anywhere in tool results. Re-export with valid auth state before submitting.
- [ ] **🚫** No raw API stack traces or HTTP error envelopes in tool results unless the trace is *specifically* about `skill_error_recovery` (in which case the trace must show the recovery, not just the error).
- [ ] **⚠️** Tool results that succeeded technically but returned `"results": []` / `null` / "no data" are acknowledged by the assistant, not silently treated as success.

### 15.1 Never Hallucinate Dates / Weekdays

- [ ] **🚫** Every date / weekday pair stated by the assistant matches reality. The reviewer must independently verify (e.g. `python -c "import datetime; print(datetime.date(2026,4,12).strftime('%A'))"`).
- [ ] **🚫** When the user states a date or weekday, the assistant does **not** silently "correct" it (cross-references §15.9 on overwriting correct user input).
- [ ] **🚫** Calendar events / reminders / cron schedules are built only after verifying the date↔weekday mapping with `session_status` or an explicit tool call. **The Apr-12-was-Sunday-not-Saturday class of failure is the canonical rejection trigger here.**
- [ ] **🚫** Recurring schedules state the right anchor (e.g. "every Sunday" means RRULE starts on a real Sunday in the user's timezone).

### 15.2 Never Trust Memory Retrieval Blindly

- [ ] **🚫** Retrieved memory (from `fact_recall`, `cross_session_recall`, or skill output) is reconciled against the canonical `MEMORY.md` when both exist. Conflicts surfaced, not silently propagated.
- [ ] **🚫** No wrong occupation / age / location / family fact persists from retrieved memory into a written artifact (email, Drive doc, calendar description) without a verification step against `MEMORY.md`.
- [ ] **🚫** `MEMORY.md` (the workspace canonical file) is the source of truth when retrieval and embedded state disagree. Skill-retrieved memory is the *cache*, not the *source*.

### 15.3 Never Execute Actions Using Fabricated Information

- [ ] **🚫** Every external action that uses identifying info (email address, phone, postal address, account number) draws that info from `MEMORY.md`, the user's message, or a verified tool result. **No guessed domains, no inferred phone numbers, no inferred recipient.**
- [ ] **🚫** If the persona has a known publisher / employer / partner in `MEMORY.md`, actions go to **that** entity. The "email sent to fabricated Penguin Random House address instead of Beacon Press" pattern is an instant reject.
- [ ] **🚫** When the agent doesn't know a recipient, it asks the user or refuses — it does not invent.

### 15.4 Never Claim Recurring Behavior Without Actual Recurrence Logic

- [ ] **🚫** Whenever the assistant's text says "recurring", "every {day}", "weekly", "daily", "monthly", the underlying tool call payload contains a real recurrence parameter (`RRULE:FREQ=WEEKLY;BYDAY=SU`, `cron: "0 18 * * 0"`, etc.).
- [ ] **🚫** Recurrence created via `gog calendar create` includes `--rrule` flag with a valid RRULE string. A single `--from`/`--to` event labelled "recurring" = reject.
- [ ] **🚫** Cron jobs created via the `cron` tool include `schedule.kind == "cron"` with a non-empty `schedule.expr`.
- [ ] **⚠️** The recurrence anchor date is consistent with the recurrence rule (e.g. `RRULE:FREQ=WEEKLY;BYDAY=SU` paired with an anchor `--from` on a Sunday, not a Saturday).

### 15.5 Never Create Duplicate Reminders / Schedules

- [ ] **🚫** Before creating a reminder / cron / calendar event that *could* collide with an existing one (same medication, same recurring task, same daily briefing), the assistant queries existing items first (`cron list`, `gog calendar list`, etc.).
- [ ] **🚫** If a collision is found, the assistant offers replace/update — does not stack a duplicate.
- [ ] **🚫** The "new Metformin reminder created despite existing evening reminder" pattern is a reject.

### 15.6 Never Reference Information Before Retrieval

- [ ] **🚫** Every factual statement the assistant makes about the user's life, calendar, files, or memory is preceded earlier in the trace by a corresponding retrieval (memory call, calendar query, file read, MEMORY.md embedded content).
- [ ] **🚫** No "I remember you mentioned…" or "you have a lecture on Tuesday" before an actual `fact_recall` / `calendar list` / equivalent tool result.
- [ ] **🚫** Implicit knowledge from the system prompt's MEMORY.md embedding is acceptable; *invented* knowledge is not.

### 15.7 Never Ignore Tool vs Reality Mismatch

- [ ] **🚫** The assistant's description of what a tool did matches the tool's actual payload and result. If the tool created one event, the assistant says "I created one event" — not "I created a recurring series".
- [ ] **🚫** For each tool call that produces an artifact, the assistant either (a) re-reads the artifact in a subsequent tool call to confirm, or (b) accurately summarizes the tool's *return value* (not the assistant's *intended* state).
- [ ] **🚫** Discrepancy between the assistant's natural-language summary and the tool result JSON = synthesis failure (see §5.1).

### 15.8 Never Persist Corrupted Formatting / Encoding

- [ ] **🚫** No mojibake characters in any tool call argument or assistant text. Common patterns to grep for: `â€"`, `â€™`, `Ã©`, `Â`, `Â`. These usually mean UTF-8 was double-decoded.
- [ ] **🚫** Em-dashes / en-dashes / smart quotes are either real Unicode (`—` U+2014, `'` U+2019) or plain ASCII — never the mojibake form.
- [ ] **🚫** Markdown formatting in persisted artifacts (Drive docs, emails) renders correctly — no stray escaped backslashes, no broken code fences.
- [ ] **⚠️** Non-Latin scripts (Devanagari, CJK, Cyrillic, etc.) preserved correctly through every tool boundary.

### 15.9 Never Overwrite Correct User Input Without Verification

- [ ] **🚫** When the user states a fact (a date, a name, a weekday), the assistant does not impulsively contradict it. If the assistant believes the user is wrong, it asks ("Just to confirm — May 17 is a Sunday this year, right?") rather than asserting.
- [ ] **🚫** "User correctly said Sunday; assistant incorrectly corrected to Saturday" pattern = reject.
- [ ] **⚠️** When the assistant *is* right and the user *is* wrong (e.g. user-stated date that conflicts with `session_status`), the correction is phrased cautiously and offers the verification source.

### 15.10 Never Let Hallucinations Become Persistent Artifacts

**This is the dataset-poisoning rule. Read it twice.**

> *"A conversational hallucination is bad. A persisted hallucination is far worse."* — `edgecases.txt`

- [ ] **🚫** Every email body, Drive doc body, calendar event description, reminder text, and shared-file content is verified against `MEMORY.md` + tool results + the user's stated facts **before** the tool call that persists it is allowed to fire.
- [ ] **🚫** No fabricated URLs, IDs, prices, dates, names, addresses, or contact details persisted to an artifact.
- [ ] **🚫** When uncertain, the agent leaves a placeholder (`[CONFIRM_BEFORE_SENDING]`) or asks the user — does not invent and persist.
- [ ] **🚫** Cross-check the §5.1 synthesis audit specifically for *anything* written to a persistent artifact. The bar for persisted facts is one notch stricter than for conversational facts.

### 15.11 Tool Discipline

- [ ] **🚫** Tool payload exactly matches the assistant's explanation (cross-reference §15.7).
- [ ] **🚫** Tool name is in the registered tools list (`prompts.json:systemPromptReport.tools.entries`). Tool names invented by the model = reject.
- [ ] **🚫** No empty `toolCall` blocks (`arguments: {}` when arguments are required, or empty `name`).
- [ ] **🚫** No `toolCall` followed by no `toolResult` in the trace (orphan calls = corruption).
- [ ] **⚠️** Tool arguments use the expected schema (e.g. `gog calendar create` requires `--summary`, `--from`, `--to`; missing flags = malformed call).

### 15.12 Memory Grounding

- [ ] **🚫** Every factual statement in the final assistant message can be traced to (a) the user's input, (b) `MEMORY.md`, or (c) a tool result earlier in the trace. Cross-references §5.1 synthesis audit.
- [ ] **🚫** Conflicting memory detected and reconciled, not silently propagated (cross-references §15.2).

### 15.13 Proactive But Controlled

- [ ] **🟡** Helpful proactive suggestions are present (cross-references §11 behavior coverage `proactive_value_add`, `opportunity_surfacing` — informational only, not mandatory).
- [ ] **🚫** No unrequested irreversible action .
- [ ] **🚫** No speculative details added to artifacts (cross-references §15.10).

### 15.14 Other Edge-Case Items

- [ ] **🚫** **Timestamp matches persona timezone.** Trace timestamps are consistent with the persona's stated location (e.g. Chris is in Columbus OH → ET timezone → assistant-stated local times reconcile with the ISO timestamps in the trace).
- [ ] **🚫** **No truncated thinking.** `thinking` blocks must not be cut off mid-sentence. If the last sentence in a thinking block lacks terminal punctuation and the trace continues into a tool call or final message, the thinking was truncated by the runtime — reject and re-export with higher token budget.
- [ ] **🚫** **`thinkingSignature: ""`** verified everywhere (cross-references §3.2 and §12.3).
- [ ] **🚫** **Pattern Taxonomy match.** Every multi-agent spawn maps to exactly one of the 9 patterns from Doc 2 (cross-references §4.3).
- [ ] **🚫** **Task-type mismatch.** What the agent actually did must match `meta_info.task_type`. A trace tagged `productivity_flow` that mostly did `skill_use_and_orchestration` work = mis-categorization, reject.

---

### 15.15 Reviewer's Quick Grep List

Cheat-sheet of regex patterns to run against the trace JSON before manual review:

| Pattern | What it catches |
| --- | --- |
| `â€[\"']` or `Ã©` or `\\u00c2` | Mojibake (§15.8) |
| `"thinkingSignature":\s*"[^"]+"` | Non-empty thinkingSignature (§3.2 + §12.3) |
| `"isError":\s*true` then `"role":\s*"assistant"` | Tool error followed by assistant claim (§5.3) |
| `recurring` / `every (Mon\|Tue\|Wed\|Thu\|Fri\|Sat\|Sun)` in assistant text | Verify §15.4 recurrence backed by RRULE |
| `auth.*(failed\|expired)` / `401` / `403` / `token` in toolResult | §15.A auth-error leak |
| `output_source.*parent_summary` | Multi-agent grounding placeholder (§4.2) |
| `<system prompt omitted` | Placeholder system prompt (§12 unverifiable) |
| `"id":\s*"d[0-9]{7}"` *(should match all message IDs)* | §3 sequential ID compliance |

If any of these greps return unexpected matches, the trace fails QC at the corresponding section.

This is the kind of trace that *looks fine* at a glance but fails the actual grounding bar. The job of this checklist is to surface that gap every time.
