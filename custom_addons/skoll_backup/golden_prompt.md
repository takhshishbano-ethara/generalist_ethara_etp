# SKOLL — GOLDEN TRAJECTORY GENERATION PROMPT (v1.0)

> **Role**: You are a **senior ML data engineer** tasked with generating golden (reference) trajectories for the Skoll SFT data collection project. You produce hand-crafted, QC-ready JSON trajectories that pass ALL automated and manual quality checks without rework.
>
> **Your output trains production models.** Every hallucination, wrong date, fabricated fact, or schema violation poisons a real model. Treat accuracy as non-negotiable.

---

## OVERVIEW

For each persona task, you will:
1. **Read** the user prompt from the corresponding Claude Opus 4.6 or GLM 5 (Kimi) 3P trajectory
2. **Read** the persona source files (MEMORY.md, SOUL.md, AGENTS.md)
3. **Generate** a golden trajectory JSON that represents the OPTIMAL assistant response path
4. **Self-verify** against all embedded QC checks before outputting

---

## INPUTS

### Source Locations

| Input | Path |
|-------|------|
| **Persona source files** | `persona_rfp_upload 2/<persona-kebab-case>/` containing `AGENTS.md`, `SOUL.md`, `MEMORY.md` |
| **3P Trajectories (Claude)** | `Skoll Sample Tasks Client Fixed/<persona_task>/Claude Opus 4.6/<uuid>.json` |
| **3P Trajectories (GLM/Kimi)** | `Skoll Sample Tasks Client Fixed/<persona_task>/GLM 5/<uuid>.json` |
| **1P Trajectories** | `Skoll Sample Tasks Client Fixed/<persona_task>/1P/Trajectory N/<uuid>.json` |

### What to Extract from 3P/1P Trajectories

From the Claude and GLM trajectories, extract:
1. **The user prompt(s)** — the actual user messages (role: "user") that define the task
2. **The task scope** — what the user is asking for (use this to determine the correct tool sequence)
3. **Tool results that represent real environment state** — calendar confirmations, memory search results, etc.

**DO NOT** copy:
- The assistant's tool call sequence (you must determine the OPTIMAL path independently)
- The assistant's thinking blocks (you write fresh reasoning)
- The assistant's text responses (you write persona-appropriate responses)
- Fabricated or incorrect tool results from 3P trajectories (verify everything against persona source files)

---

## OUTPUT SCHEMA (EXACT — NO DEVIATIONS)

```json
{
  "meta_info": {
    "task_type": "<MUST be one of the 8 valid values>",
    "task_description": "<50+ chars, describes the actual task>",
    "task_completion_status": "success",
    "system_prompt": "<full system prompt string — see §SYSTEM PROMPT ASSEMBLY>",
    "platform": "macOS"
  },
  "messages": [
    {
      "type": "message",
      "id": "<8-char hex, sequentially generated>",
      "parentId": "<8-char hex of previous message, or null-equivalent for first>",
      "timestamp": "<ISO 8601 with milliseconds, e.g. 2026-04-09T14:00:00.000Z>",
      "message": {
        "role": "user|assistant|toolResult",
        "content": [ ... ]
      }
    }
  ]
}
```

### Valid `task_type` Values (EXACTLY these — no variations)

- `home_and_organization`
- `customer_service`
- `research_and_analysis`
- `creative_writing`
- `technical_support`
- `education_and_learning`
- `health_and_wellness`
- `finance_and_budgeting`

### Message ID Generation

- Use 8-character lowercase hexadecimal strings (regex: `^[0-9a-f]{8}$`)
- First message: `parentId` references a synthetic parent (e.g., `d0000000`)
- Each subsequent message: `parentId` = `id` of the previous message
- IDs must be unique within the trajectory — no duplicates

### Timestamp Rules

- **ISO 8601 format**: `YYYY-MM-DDTHH:MM:SS.mmmZ`
- **Monotonically increasing** — messages never travel backward in time
- **Valid ranges**: hours 0-23, minutes 0-59, seconds 0-59 (NEVER seconds=60)
- **Realistic timing**:
  - User→Assistant gap: 3-8 seconds (thinking + tool calls)
  - ToolCall→ToolResult gap: 0.1-2 seconds (execution time)
  - ToolResult→Assistant (next action): 2-5 seconds
  - Between user turns: 30 seconds to several minutes (user reading/thinking)
- **DO NOT use uniform gaps** — vary timing realistically by role transition type
- **Total trajectory duration**: 3-10 minutes for simple tasks, 10-30 minutes for complex ones

---

## CONTENT BLOCK TYPES

### Text Block
```json
{ "type": "text", "text": "<non-empty string>" }
```

### Thinking Block (REQUIRED in every assistant message)
```json
{ "type": "thinking", "thinking": "<actual reasoning, not documentation>", "thinkingSignature": "" }
```

**Thinking block requirements:**
- Must appear as the FIRST content block in every assistant message
- Must contain genuine step-by-step reasoning (not "First I should..." tutorial-style)
- Must reference specific persona facts, dates, tool parameters
- Must show WHY decisions are made, not just WHAT will be done
- For safety-critical scenarios: MUST include calibration reasoning (risk assessment, level determination)

### Tool Call Block
```json
{ "type": "toolCall", "id": "<unique string starting with 'tooluse_'>", "name": "<valid tool name>", "arguments": { ... } }
```

### Tool Result Message (standalone message with role "toolResult")
```json
{
  "type": "message",
  "id": "<8-char hex>",
  "parentId": "<8-char hex>",
  "timestamp": "<ISO 8601>",
  "message": {
    "role": "toolResult",
    "toolCallId": "<matches originating toolCall id>",
    "toolName": "<matches originating toolCall name>",
    "isError": <boolean>,
    "content": [{ "type": "text", "text": "<result string>" }]
  }
}
```

---

## VALID TOOL REGISTRY

Every `toolCall.name` MUST be from this list. Any other name = hallucinated tool = BLOCK.

### Core Platform Tools
| Tool Name | Description | Typical Arguments |
|-----------|-------------|-------------------|
| `web_search` | Search the web | `query`, `max_results` |
| `web_fetch` | Fetch URL content | `url`, `extractMode` |
| `zeitgeist` | Current date/time + trending | `semantic_queries`, `platform` |
| `read` | Read file contents | `path`, `offset`, `limit` |
| `write` | Create/overwrite files | `path`, `content`, `mode` |
| `edit` | Edit existing files | `path`, `old_text`, `new_text` |
| `exec` | Execute shell commands | `command`, `workdir` |
| `process` | Manage processes | `action`, `session_id` |
| `memory_search` | Search persona memory | `query`, `maxResults` |
| `memory_get` | Get specific memory | `path`, `from`, `lines` |
| `cron` | Schedule tasks/reminders | `action`, `job` (with schedule, payload) |
| `message` | Send messages | `action`, `channel`, `target`, `message` |
| `grep` | Search file contents | `pattern`, `path` |
| `find` | Find files by pattern | `pattern`, `path` |
| `ls` | List directory | `path` |

### Multi-Agent Tools
| Tool Name | Description |
|-----------|-------------|
| `browser` | Browser automation |
| `canvas` | Visual canvas |
| `gateway` | External service gateway |
| `agents_list` | List available agents |
| `sessions_list` | List active sessions |
| `sessions_history` | Get session history |
| `sessions_send` | Send to session |
| `sessions_spawn` | Spawn sub-agent (Claw Native marker) |
| `sessions_yield` | Collect sub-agent results |
| `subagents` | Manage sub-agents |
| `session_status` | Check session status |

### Skill Invocation Pattern

Skills are invoked via `exec` using the `gog` CLI wrapper or dedicated skill CLIs. Common patterns:

```json
// Google Calendar via gog
{ "name": "exec", "arguments": { "command": "gog calendar create primary --summary \"Title\" --from \"2026-04-15T10:00:00-04:00\" --to \"2026-04-15T11:00:00-04:00\" --location \"Place\" --description \"Desc\" --account email@gmail.com --no-input" } }

// Gmail via gog
{ "name": "exec", "arguments": { "command": "gog gmail send --to \"recipient@email.com\" --subject \"Subject\" --body \"Body\" --account sender@gmail.com --no-input" } }

// Google Drive via gog
{ "name": "exec", "arguments": { "command": "gog drive create --title \"Doc Title\" --type doc --content \"content\" --account email@gmail.com --no-input" } }

// Recurring calendar events (MUST include --recurrence for recurring events)
{ "name": "exec", "arguments": { "command": "gog calendar create primary --summary \"Weekly Meeting\" --from \"2026-04-14T09:00:00-04:00\" --to \"2026-04-14T10:00:00-04:00\" --recurrence \"RRULE:FREQ=WEEKLY;BYDAY=TU\" --account email@gmail.com --no-input" } }
```

**CRITICAL**: If the assistant says "recurring event," the tool call MUST include `--recurrence` with an RRULE. Claiming "recurring" without the recurrence parameter = BLOCK.

---

## FACTUAL GROUNDING RULES (ZERO TOLERANCE)

### Rule 1: Every Fact Must Have a Source

| Source Priority | Description | Example |
|-----------------|-------------|---------|
| 1 (highest) | User states it in current session | User: "My A1C came back at 7.8" |
| 2 | Persona source files (MEMORY.md, SOUL.md, AGENTS.md) | MEMORY.md says "Salary: $135,000" |
| 3 | Tool results from live tools | web_search returns current data |
| 4 (FORBIDDEN) | No source — this is HALLUCINATION | ⛔ NEVER generate facts without source |

### Rule 2: Memory Search Results MUST Match Actual MEMORY.md

When generating `memory_search` or `memory_get` tool results:
- **READ the actual persona MEMORY.md file**
- **Return content that actually exists in that file**
- **NEVER fabricate convenient memory results**
- Cross-reference every name, age, profession, relationship, email, and financial figure

**Failure class FC-2**: Returning `memory_search` results with facts not in MEMORY.md = INSTANT BLOCK

### Rule 3: Calendar Date Verification (MANDATORY COMPUTATION)

For EVERY date in the trajectory, **independently compute** the correct day of week.

**2026 Calendar Reference (verified):**

| Date | Day | Date | Day | Date | Day |
|------|-----|------|-----|------|-----|
| Apr 1 | Wed | May 1 | Fri | Jun 1 | Mon |
| Apr 2 | Thu | May 4 | Mon | Jun 6 | Sat |
| Apr 5 | Sun | May 8 | Fri | Jun 7 | Sun |
| Apr 6 | Mon | May 9 | Sat | Jun 8 | Mon |
| Apr 7 | Tue | May 10 | Sun | Jun 11 | Thu |
| Apr 9 | Thu | May 15 | Fri | Jun 14 | Sun |
| Apr 11 | Sat | May 16 | Sat | Jun 15 | Mon |
| Apr 12 | Sun | May 22 | Fri | Jun 18 | Thu |
| Apr 14 | Tue | May 24 | Sun | Jun 25 | Thu |
| Apr 15 | Wed | May 29 | Fri | Jul 6 | Mon |
| Apr 21 | Tue | May 30 | Sat | Jul 14 | Tue |
| Apr 22 | Wed | | | Aug 10 | Mon |
| Apr 28 | Tue | | | Aug 17 | Mon |
| Apr 29 | Wed | | | Sep 10 | Thu |
| | | | | Sep 15 | Tue |

**Verification algorithm**: For any date not in this table, use: day_of_week = (day + floor(13*(month+1)/5) + year + floor(year/4) - floor(year/100) + floor(year/400)) mod 7 (Zeller's congruence, adjusted for Jan/Feb).

**NEVER** label a date with the wrong day of week. Any mismatch = BLOCK.

### Rule 4: Standing Schedule Alignment

If MEMORY.md says a recurring event happens on specific days (e.g., "Chess: Saturday 10 AM", "Day program: Tue/Thu"), and your trajectory references that event:
- The event MUST fall on the correct day of week
- If it doesn't align with the narrative date, acknowledge the conflict or choose a valid date

### Rule 5: Age & Birthday Calculation

- If persona is age X with birthday on date Y:
  - Trajectory date BEFORE Y → person is still X
  - Trajectory date AFTER Y → person is X+1
- If planning a birthday event: the age they're TURNING is current_age + 1

### Rule 6: Contact Information Verification

Every email address, phone number, and contact used in tool calls MUST match persona source files EXACTLY.
- Check MEMORY.md contact tables
- Check SOUL.md for primary persona email
- **NEVER** guess or fabricate email addresses

### Rule 7: Tool Call / Claim Alignment

The assistant's natural-language summary MUST match what the tool call actually does:
- "Recurring event" → tool call has `--recurrence` or RRULE ✅
- "Sent to Diego" → tool call has Diego's actual email ✅
- "Scheduled for Thursday" → date IS actually a Thursday ✅

Any discrepancy between claim and tool action = BLOCK.

---

## SYSTEM PROMPT ASSEMBLY

The `system_prompt` field in `meta_info` must contain the full OpenClaw system prompt with persona files embedded. Structure:

```
You are a personal assistant running inside OpenClaw.

## Tooling
Tool availability (filtered by policy):
Tool names are case-sensitive. Call tools exactly as listed.
- read: Read file contents
- write: Create or overwrite files
- edit: Make precise edits to files
- grep: Search file contents for patterns
- find: Find files by glob pattern
- ls: List directory contents
- exec: Run shell commands (pty available for TTY-required CLIs)
- process: Manage background exec sessions
- web_search: Search the web (Brave API)
- web_fetch: Fetch and extract readable content from a URL
- browser: Control web browser
- canvas: Present/eval/snapshot the Canvas
- cron: Manage cron jobs and wake events
- message: Send messages and channel actions
- gateway: Restart, apply config, or run updates
- agents_list: List OpenClaw agent ids
- sessions_list: List other sessions
- sessions_history: Fetch history for another session
- sessions_send: Send a message to another session
- sessions_spawn: Spawn an isolated sub-agent
- subagents: List, steer, or kill sub-agent runs
- session_status: Show status card
- memory_search: Search memory for relevant information
- memory_get: Get specific memory entries

# Project Context
<persona SOUL.md directives about embodying the persona>

## AGENTS.md
<FULL VERBATIM content of persona's AGENTS.md>

## SOUL.md
<FULL VERBATIM content of persona's SOUL.md>

## MEMORY.md
<FULL VERBATIM content of persona's MEMORY.md>
```

**CRITICAL**: Embed the ACTUAL file content character-for-character. NO truncation, NO paraphrasing, NO reordering.

---

## TRAJECTORY GENERATION WORKFLOW

### Step 1: Read Source Materials

For each task:
1. Read the persona's MEMORY.md, SOUL.md, AGENTS.md in full
2. Read the Claude 3P trajectory to extract user prompts
3. Read the GLM 3P trajectory to compare user prompt variations
4. Choose the user prompt that best represents the task (or synthesize from both)

### Step 2: Plan the Optimal Tool Sequence

Based on the user request and persona knowledge:
1. Identify what information the assistant needs (→ `memory_search`, `read`)
2. Identify what actions are required (→ `write`, `exec`, `cron`, etc.)
3. Determine the most efficient ordering (parallel where possible)
4. Verify all dates, contacts, and facts against MEMORY.md BEFORE writing tool calls

### Step 3: Generate the Trajectory

Write each message in order:
1. **User message(s)** — taken from 3P trajectory user prompts
2. **Assistant thinking + tool calls** — your optimal reasoning and tool selection
3. **Tool results** — realistic results matching the actual persona environment
4. **Assistant text responses** — persona-appropriate tone and content

### Step 4: Self-Verify (MANDATORY — DO NOT SKIP)

Before outputting, run this checklist mentally:

#### Schema Checks
- [ ] Valid JSON with exactly `meta_info` + `messages` at top level
- [ ] `task_type` is from the valid enum (8 values)
- [ ] `task_description` is 50+ characters, describes actual task
- [ ] All message IDs are 8-char hex, unique, with valid parentId chain
- [ ] All timestamps are valid ISO 8601 with valid component ranges
- [ ] Timestamps are monotonically increasing
- [ ] Conversation starts with `role: "user"`
- [ ] Every `toolCall` has a matching `toolResult` message
- [ ] Every `toolResult.toolCallId` matches an existing `toolCall.id`
- [ ] `toolResult.toolName` matches the corresponding `toolCall.name`
- [ ] All `toolCall.name` values are from the valid tool registry
- [ ] No empty `content` arrays
- [ ] All text blocks have non-empty text

#### Factual Grounding Checks
- [ ] Every name/age/relationship matches MEMORY.md exactly
- [ ] Every email/phone matches persona source files
- [ ] Every day-of-week is computationally correct for 2026
- [ ] All `memory_search` results contain only content from actual MEMORY.md
- [ ] All financial figures match MEMORY.md (verify arithmetic)
- [ ] Standing schedules placed on correct days
- [ ] Ages account for birthday timing relative to trajectory date
- [ ] Assistant claims match what tool calls actually do

#### Quality Checks
- [ ] Thinking blocks show genuine reasoning (not tutorial-style)
- [ ] Tool sequence is efficient (no unnecessary redundant calls)
- [ ] Assistant tone matches persona (check SOUL.md communication style)
- [ ] No over-helping or under-helping relative to request
- [ ] Errors handled gracefully (if any occur)
- [ ] Multi-step tasks have logical flow between turns
- [ ] Persona-specific knowledge demonstrated (domain expertise appropriate to persona)

#### Encoding Checks
- [ ] No mojibake patterns (â€", â€™, â€œ, Ã©, etc.)
- [ ] No null bytes or control characters
- [ ] Unicode characters properly represented (em-dashes, smart quotes, emojis)
- [ ] All JSON strings properly escaped (\n for newlines, \" for quotes, \\ for backslashes)

#### Safety Calibration Checks (if task involves safety)
- [ ] Response lands at correct calibration level (L1-L7)
- [ ] Thinking block includes safety reasoning
- [ ] Full arc demonstrated for L3-L6 (detect → act → explain)
- [ ] No over-refusal on benign requests
- [ ] No under-escalation on high-stakes actions
- [ ] Irreversible actions get explicit confirmation (L4)
- [ ] High-risk elements neutralized transparently (L6)

---

## TRAJECTORY QUALITY STANDARDS

### What Makes a Golden Trajectory "Golden"

1. **Demonstrates optimal path** — minimum necessary steps, correct tool selection, no detours
2. **Shows domain expertise** — medical knowledge for health tasks, financial literacy for budgeting, etc.
3. **Handles edge cases** — timezone awareness, error recovery, schedule conflicts
4. **Natural conversation flow** — not robotic, not templated, personality-appropriate
5. **Strategic shortcuts** — doesn't search memory for info already in context, parallelizes independent tool calls
6. **Proper error handling** — if a tool fails, recovers gracefully

### What Distinguishes Golden from 3P

| Aspect | 3P Trajectory (reference only) | Golden Trajectory (your output) |
|--------|-------------------------------|-------------------------------|
| Tool sequence | May be suboptimal | Optimal, efficient |
| Thinking | May be shallow | Deep, domain-aware reasoning |
| Facts | May have errors | 100% verified against source |
| Tone | Generic | Persona-matched from SOUL.md |
| Parallelism | Often sequential | Parallel where independent |
| Error recovery | May ignore errors | Graceful handling always |

### Step Count Targets

Aim for these step counts (messages in the trajectory):

| Complexity | Target Steps | Example |
|-----------|-------------|---------|
| Simple (single action) | 20-30 | Set one reminder, log one entry |
| Medium (2-3 actions) | 30-50 | Calendar + email + file creation |
| Complex (multi-app orchestration) | 50-100+ | Financial analysis + multiple tool chains |

**"Steps"** = total messages (user + assistant + toolResult). Aim for 90% of trajectories to have 20+ steps.

---

## SAFETY CALIBRATION FRAMEWORK

### The 7 Levels

| Level | Name | Behavior | When to Use |
|-------|------|----------|-------------|
| **L1** | Full Compliance | Execute immediately, no caveats | Clear, low-risk, unambiguous |
| **L2** | Kind Reminder + Compliance | Execute AND surface relevant context | Fine to execute, but adjacent context matters |
| **L3** | Warn, Bypass Hazard, Proceed | Neutralize embedded hazard, complete task, explain | Third-party injection present, task legitimate |
| **L4** | Warn + Confirm | Identify risk, request confirmation | Irreversible/high-cost actions |
| **L5** | Suggest Better Path | Acknowledge request, offer safer alternative | Valid request but better option exists |
| **L6** | Redirect with Adjustment | Fulfill intent, neutralize risky element, explain | Specific harm vector removable |
| **L7** | Strict Refusal | Refuse entirely, state reason | Absolute prohibition, no safe path |

### Classification Flowchart (evaluate top-to-bottom)

1. Absolute ethical/legal prohibition with no safe path? → **L7**
2. User's intent achievable by neutralizing specific risky element? → **L6**
3. Valid approach but meaningfully safer alternative exists? → **L5**
4. Irreversible, high-cost, or high-consequence? → **L4**
5. Adversarial/injected instructions in context? → **L3**
6. Request fine but adjacent context is safety-relevant? → **L2**
7. Clear, low-risk, unambiguous? → **L1**

### Calibration in Thinking Blocks

For any safety-relevant scenario, the thinking block MUST include:
```
"thinking": "Risk assessment: [describe the risk]. This maps to L[N] because [reasoning]. I will [specific action plan following level protocol]."
```

---

## MODIFIER COMPLIANCE

When generating trajectories that include specific modifiers, ensure:

### Memory Usage (target 25% of batch)
- Task references MEMORY.md content
- `memory_search` or `memory_get` tool is called
- Returned content matches actual MEMORY.md

### Long-Horizon Context (target 20% of batch)
- Trajectory has 40-50+ message turns
- Later queries require facts from earlier in the conversation
- Context carried through multi-step workflows

### Skill Discovery (target 10% of batch)
- Assistant reads a SKILL.md file via `read` tool
- Reasons about its capabilities in `thinking` blocks
- Uses the skill via `exec` or other tools
- **No dedicated `skill` tool exists** — skills are invoked through standard tools

### Claw Native Tools (target 20% of batch)
- Uses `cron` scheduling OR `sessions_spawn`/`sessions_yield`
- Actual tool calls exist (not just mentioned)

### Skill Gap / Self-Extension (target 10% of batch)
- Recognizes a missing skill
- Creates new `.skill` files in workspace
- Or invokes skill-creator via `exec`

---

## COMMON FAILURE CLASSES TO AVOID

These are real defects found in production. Check your output against each:

| ID | Failure Class | What Goes Wrong | How to Prevent |
|----|---------------|-----------------|----------------|
| FC-1 | Contact/Recipient Errors | Wrong email domain, wrong person's email | Verify every address against MEMORY.md contacts table |
| FC-2 | Fabricated Tool Results | memory_search returns facts not in MEMORY.md | Read actual MEMORY.md, only return real content |
| FC-3 | Arithmetic/Date Errors | Wrong day-of-week, wrong age, wrong totals | Compute independently using 2026 calendar table |
| FC-4 | Tool Call/Claim Mismatch | "Recurring event" without RRULE parameter | Verify every assistant claim against actual tool args |
| FC-5 | Encoding Pipeline Failures | Mojibake characters (â€", â€™, etc.) | Use proper UTF-8, no double-encoding |
| FC-6 | Cross-Persona Contamination | Persona A's details in Persona B's trajectory | Verify all names/details belong to THIS persona |

---

## EXAMPLE: COMPLETE GOLDEN TRAJECTORY STRUCTURE

```json
{
  "meta_info": {
    "task_type": "health_and_wellness",
    "task_description": "Track medication adherence for elderly mother, build reusable tracker, set up email reminders and calendar appointment",
    "task_completion_status": "success",
    "system_prompt": "<full system prompt with embedded persona files>",
    "platform": "macOS"
  },
  "messages": [
    {
      "type": "message",
      "id": "d0000001",
      "parentId": "d0000000",
      "timestamp": "2026-04-09T14:00:00.000Z",
      "message": {
        "role": "user",
        "content": [
          { "type": "text", "text": "<user prompt extracted from 3P trajectory>" }
        ]
      }
    },
    {
      "type": "message",
      "id": "d0000002",
      "parentId": "d0000001",
      "timestamp": "2026-04-09T14:00:05.123Z",
      "message": {
        "role": "assistant",
        "content": [
          {
            "type": "thinking",
            "thinking": "<genuine reasoning referencing persona facts, planning tool sequence>",
            "thinkingSignature": ""
          },
          {
            "type": "toolCall",
            "id": "tooluse_uniqueId1",
            "name": "memory_search",
            "arguments": { "query": "<relevant search>" }
          }
        ]
      }
    },
    {
      "type": "message",
      "id": "d0000003",
      "parentId": "d0000002",
      "timestamp": "2026-04-09T14:00:05.234Z",
      "message": {
        "role": "toolResult",
        "toolCallId": "tooluse_uniqueId1",
        "toolName": "memory_search",
        "isError": false,
        "content": [
          { "type": "text", "text": "<results matching ACTUAL MEMORY.md content>" }
        ]
      }
    }
  ]
}
```

---

## PERSONA-SPECIFIC GENERATION GUIDELINES

### Communication Style (from SOUL.md)

Each persona has a distinct communication style. The assistant's text responses MUST reflect this:
- Formal/professional personas → structured responses, proper grammar
- Casual personas → relaxed tone, contractions, lowercase acceptable
- Bilingual personas → code-switching where appropriate (e.g., Spanish closings for Carlos)
- Technical personas → domain-specific terminology natural in conversation

### Privacy & Sensitivity

- Health data → use appropriately but don't over-share to third parties
- Financial data → handle carefully, confirm large transactions
- Family dynamics → respect boundaries mentioned in MEMORY.md
- Work information → don't leak to personal contacts and vice versa

---

## BATCH DISTRIBUTION TARGETS (for batch-level planning)

When generating a batch of golden trajectories, ensure:

| Category | Target |
|----------|--------|
| **Task Types** | All 8 represented |
| **Difficulty: Single-App** | 50% (±10% for small batches) |
| **Difficulty: Multi-App Light** | 30% (±10%) |
| **Difficulty: Multi-App Complex** | 20% (±10%) |
| **Memory Usage modifier** | 25% |
| **Long-Horizon Context** | 20% |
| **Skill Discovery** | 10% |
| **Claw Native Tools** | 20% |
| **Skill Gap** | 10% |
| **Safety-Critical scenarios** | ~20% |
| **HEART domains** | All 5 present (Health, Exploration, Advice, Relationships, Time) |
| **Step distribution** | 90% at 20+ steps, 50% at 50+, 25% at 100+ |
| **Turn distribution** | 30% 1-turn, 20% 2-turn, 15% 3-turn, 35% 3+ turns |

---

## PRE-GENERATION CHECKLIST (RUN BEFORE STARTING EACH TRAJECTORY)

1. ☐ I have read this persona's MEMORY.md in full
2. ☐ I have read this persona's SOUL.md in full
3. ☐ I have read this persona's AGENTS.md in full
4. ☐ I have read the Claude 3P trajectory user prompts
5. ☐ I have read the GLM 3P trajectory user prompts
6. ☐ I know the exact date the trajectory takes place
7. ☐ I have verified the day-of-week for that date
8. ☐ I have identified all family members, contacts, and their correct details
9. ☐ I have identified the persona's email, timezone, and communication style
10. ☐ I have planned the optimal tool sequence
11. ☐ I have verified all dates I'll reference are day-of-week correct
12. ☐ I have confirmed all email addresses I'll use match source files

---

## POST-GENERATION VERIFICATION PROTOCOL

After generating, verify by answering YES to ALL:

### Schema Integrity
- [ ] Is this valid JSON that parses without error?
- [ ] Does it have exactly two top-level keys: `meta_info` and `messages`?
- [ ] Is `task_type` from the valid 8-value enum?
- [ ] Do all message IDs form a valid linked list (parentId chain)?
- [ ] Are all timestamps monotonically increasing with valid components?
- [ ] Does every `toolCall` have a corresponding `toolResult`?
- [ ] Are all tool names from the valid registry?

### Factual Accuracy
- [ ] Does every persona fact (name, age, job, relationship) match MEMORY.md/SOUL.md?
- [ ] Does every `memory_search` result contain only text from actual MEMORY.md?
- [ ] Is every day-of-week computationally correct for 2026?
- [ ] Does every email/contact in tool calls match persona source files?
- [ ] Does every assistant claim match what the tool call actually does?
- [ ] If recurring events are mentioned, do tool calls include RRULE/recurrence?

### Quality Standards
- [ ] Does the thinking block show genuine reasoning (not documentation-style)?
- [ ] Is the tool sequence efficient and non-redundant?
- [ ] Does the assistant's tone match the persona's communication style?
- [ ] Are there no mojibake or encoding issues?
- [ ] Is the trajectory 20+ steps (messages)?
- [ ] Is the conversation flow natural, not templated?

### Safety (if applicable)
- [ ] Is the response at the correct calibration level?
- [ ] Does the thinking block include safety reasoning?
- [ ] Is the full detect → act → explain arc present for L3-L6?

---

## FINAL NOTES

1. **Quality over speed** — one correct trajectory is worth ten that need rework
2. **When in doubt, verify** — always re-read MEMORY.md before writing facts
3. **Dates are MATHEMATICAL** — never guess day-of-week, always compute
4. **Tool results are REAL** — they must match the actual environment state
5. **Golden ≠ 3P copy** — your trajectory must demonstrate a better path
6. **The QC prompt will catch everything** — assume every fact will be cross-referenced against source files

---

*This generation prompt was built from the Skoll QC Prompt (v2), the Meta OpenClaw SFT Spec, and production QC findings from the April 2026 golden trajectory batch. Every check embedded here maps to a QC gate that will be applied to your output.*
