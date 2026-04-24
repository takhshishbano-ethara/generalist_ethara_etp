# ATLAS — TRAJECTORY STRUCTURE & META_INFO QC PROMPT

> **Purpose**: This prompt defines the QC checks for validating trajectory JSON files against the Atlas schema. It covers structural integrity (top-level keys, message envelope, content blocks) and `meta_info` field correctness (required fields, valid values, non-empty constraints). These checks are deterministic and scriptable.
>
> **Companion Script**: `check_trajectory_structure.py` — implements these checks programmatically.
>
> **Reference Schema**: `ideal_jsonFile.json` — the canonical template defining the expected structure.
>
> **Severity Tiers**:
> - **BLOCK**: Auto-fail. Trajectory is rejected. Non-negotiable.
> - **WARNING**: Logged. Does not block individually. 5+ accumulated WARNINGs across a batch = BLOCK.
> - **ADVISORY**: Noted for awareness. No action required unless pattern is systemic.

---

## 1. INPUTS

| Input | Description |
|---|---|
| Trajectory JSON files | All `.json` files under the task delivery folders (1P trajectories, 3P trajectories, golden trajectories). Located by walking the directory tree, **excluding** `workspace/`, `workspace_before/`, and `.openclaw/` directories. |
| `ideal_jsonFile.json` | The reference schema file. Defines the expected top-level keys, `meta_info` fields, message envelope keys, and message inner keys. The ideal file's `system_prompt` is intentionally empty (it's a template placeholder) — actual trajectories MUST have it non-empty. |

---

## 2. TOP-LEVEL STRUCTURE VALIDATION — BLOCK

Every trajectory JSON must be a valid JSON object with exactly the keys defined in the ideal file.

### 2.1 — JSON Validity

- [ ] File parses as valid JSON without errors (no truncation, no trailing commas, no comments, no BOM markers)
- [ ] Root element is a JSON **object** (`{}`), not an array or primitive

**BLOCK** if file is not valid JSON or root is not an object.

### 2.2 — Top-Level Key Match

The ideal file defines these top-level keys:

```json
{
  "meta_info": { ... },
  "messages": [ ... ]
}
```

- [ ] All keys from the ideal file are present → **BLOCK** if any are missing
- [ ] No extra keys exist beyond those in the ideal file → **WARNING** if extra keys are found

> **Check**: `set(actual.keys()) == set(ideal.keys())`. Report missing and extra keys separately.

---

## 3. `meta_info` VALIDATION — BLOCK

The `meta_info` object contains task-level metadata. Every field defined in the ideal file must be present, and required fields must have valid, non-empty values.

### 3.1 — Key Completeness

- [ ] `meta_info` exists and is a JSON object → **BLOCK** if missing or not an object
- [ ] All keys from the ideal file's `meta_info` are present → **BLOCK** if any are missing
- [ ] No extra keys beyond the ideal file's `meta_info` → **WARNING** if extra keys found

The ideal file defines these `meta_info` keys:

| Key | Type | Present in Ideal |
|---|---|---|
| `task_type` | string | `"home_and_organization"` |
| `task_description` | string | `"Agent profiles user's home through questions..."` |
| `task_completion_status` | string | `"success"` |
| `system_prompt` | string | `""` (empty — template placeholder) |
| `platform` | string | `"macOS"` |

### 3.2 — `task_type` (Required, Enum)

- [ ] Present and is a non-empty string → **BLOCK** if missing or empty
- [ ] Value is one of EXACTLY these 8 valid values:

```
home_and_organization
customer_service
research_and_analysis
creative_writing
technical_support
education_and_learning
health_and_wellness
finance_and_budgeting
```

- [ ] **BLOCK** if value is not from this list. No variations, no typos, no creative additions.

> **Common mistakes to catch**: `"Home_and_Organization"` (wrong case), `"home-and-organization"` (hyphens), `"home_organization"` (missing `and_`), `"wellness"` (truncated), `"Health and Wellness"` (spaces instead of underscores).

### 3.3 — `task_description` (Required, Non-Empty String)

- [ ] Present and is a string → **BLOCK** if missing
- [ ] Not empty or whitespace-only → **BLOCK** if empty
- [ ] Length ≥ 20 characters → **WARNING** if suspiciously short (under 50 chars)
- [ ] Not a placeholder value (`"TBD"`, `"test"`, `"TODO"`, `"placeholder"`, `"test task"`) → **BLOCK** if placeholder detected
- [ ] Not duplicated across multiple trajectories within the same task → **WARNING** if near-identical descriptions found across different tasks (fuzzy match at 90%+ similarity)

> **What "non-empty" means**: After `.strip()`, the string must have length > 0. Whitespace-only strings like `"   "` or `"\n\t"` are treated as empty.

### 3.4 — `task_completion_status` (Required, Enum)

- [ ] Present and is a non-empty string → **BLOCK** if missing or empty
- [ ] Value is one of EXACTLY these 4 valid values:

```
success
partial_success
incomplete
failure
```

- [ ] **BLOCK** if value is not from this list.

> **Integrity cross-check (manual/non-deterministic)**: If `task_completion_status` is `"success"` but the trajectory content shows obvious failure or incomplete work, this is data fraud → **BLOCK**. This check requires reading the actual conversation content and cannot be fully automated.

### 3.5 — `system_prompt` (Required, MUST Be Non-Empty in Actual Trajectories)

This is the most commonly failed check. The ideal file has `system_prompt` as `""` because it's a **template placeholder**. Actual trajectory files MUST have a populated system prompt.

- [ ] Key exists in `meta_info` → **BLOCK** if missing
- [ ] Value is a string → **BLOCK** if wrong type
- [ ] Value is **NOT** empty after `.strip()` → **BLOCK** if empty

> **Why this matters**: An empty `system_prompt` means the trajectory was generated without persona context — it's structurally valid but semantically useless for training. This is a hard blocker.

> **Golden trajectories**: Golden trajectories MUST also have a non-empty `system_prompt`. There is no exception for golden files.

### 3.6 — `platform` (Required, Non-Empty String)

- [ ] Present and is a non-empty string → **BLOCK** if missing or empty
- [ ] Value is a recognizable platform identifier → **WARNING** if value seems nonsensical

> **Known valid values**: `"macOS"`, `"iOS"`, `"Android"`, `"Windows"`, `"Linux"`, `"web"`. This list is not exhaustive — flag values that don't look like real platforms.

### 3.7 — `session_id` (Optional, UUID Format)

- [ ] If present, must be a valid UUID string (regex: `^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$`) → **WARNING** if present but malformed
- [ ] 1P and 3P trajectories typically include `session_id`; golden trajectories may omit it → **ADVISORY** if golden consistently lacks it
- [ ] If SOME goldens have it and some don't → **WARNING** for inconsistency

---

## 4. `messages` ARRAY VALIDATION — BLOCK

The `messages` array contains the conversation turns. Each element is a message envelope.

### 4.1 — Array-Level Checks

- [ ] `messages` key exists → **BLOCK** if missing
- [ ] Value is a JSON array → **BLOCK** if not an array
- [ ] Array is non-empty (at least 1 message) → **BLOCK** if empty

### 4.2 — Message Envelope Structure (Per Message)

Each message MUST be a JSON object with these envelope keys (derived from the ideal file's first message):

```json
{
  "type": "message",
  "id": "<string>",
  "parentId": "<string or null>",
  "timestamp": "<ISO 8601 string>",
  "message": {
    "role": "<user|assistant|toolResult>",
    "content": [ ... ]
  }
}
```

For every `messages[i]`:

- [ ] Is a JSON object → **BLOCK** if not
- [ ] Has all expected envelope keys: `type`, `id`, `parentId`, `timestamp`, `message` → **BLOCK** if any missing
- [ ] `type` is literally `"message"` → **BLOCK** if any other value
- [ ] `id` is a non-empty string (expected format: 8-character hex `^[0-9a-f]{8}$`) → **BLOCK** if missing; **WARNING** if format deviates
- [ ] `parentId` is a string or `null`. Must be `null` only for the first message. Every other message should have a valid `parentId` → **WARNING** if chain is broken
- [ ] `timestamp` is a valid ISO 8601 datetime string → **BLOCK** if missing or invalid
- [ ] **Timestamp component ranges**: hours 0–23, minutes 0–59, seconds 0–59. Watch for `seconds=60` or higher — arithmetic overflow produces these but they are INVALID ISO-8601 → **BLOCK**
- [ ] Timestamps are monotonically non-decreasing (no time travel) → **WARNING** if ordering violated

### 4.3 — Inner `message` Object

For every `messages[i].message`:

- [ ] Exists and is a JSON object → **BLOCK** if missing or not an object
- [ ] Has `role` and `content` keys → **BLOCK** if either missing

### 4.4 — `role` Validation

- [ ] `role` is one of EXACTLY: `user`, `assistant`, `toolResult` → **BLOCK** if invalid

> **Note**: The valid roles are case-sensitive. `"User"`, `"ASSISTANT"`, `"tool_result"` are all invalid.

### 4.5 — `content` Array Validation

- [ ] `content` is a JSON array → **BLOCK** if not an array
- [ ] `content` is non-empty (at least 1 block) → **BLOCK** if empty

### 4.6 — Content Block Type Validation

Each element in `content` must be a JSON object with a `type` field. Valid content types:

```
text
thinking
toolCall
toolResult
```

For every `messages[i].message.content[j]`:

- [ ] Is a JSON object → **BLOCK** if not
- [ ] Has a `type` field → **BLOCK** if missing
- [ ] `type` is one of the 4 valid values above → **BLOCK** if unrecognized

> **Content block type is case-sensitive**: `"Text"`, `"THINKING"`, `"tool_call"` are all invalid.

### 4.7 — Content Block Structure (Per Type)

**`text` block:**
```json
{ "type": "text", "text": "<string>" }
```
- [ ] `text` field exists and is a non-empty string → **WARNING** if empty/whitespace-only

**`thinking` block:**
```json
{ "type": "thinking", "thinking": "<string>", "thinkingSignature": "<string>" }
```
- [ ] `thinking` field exists and is a non-empty string → **WARNING** if empty
- [ ] `thinkingSignature` field exists → **WARNING** if missing

**`toolCall` block:**
```json
{ "type": "toolCall", "toolCall": { "id": "<string>", "name": "<string>", "arguments": "<string>" } }
```
- [ ] Inner `toolCall` object exists → **BLOCK** if missing
- [ ] `toolCall.toolCall.id` exists and is non-empty → **BLOCK** if missing
- [ ] `toolCall.toolCall.name` exists and is a non-empty string → **BLOCK** if missing. **Note: the key is `name`, NOT `tool_name`.**
- [ ] `toolCall.toolCall.name` matches a valid tool from the Atlas tool registry (see Appendix A) → **BLOCK** if unknown tool
- [ ] `toolCall.toolCall.arguments` is a valid JSON-encoded string (can be parsed as JSON) → **BLOCK** if malformed

**`toolResult` block (in message envelope):**

`toolResult` may appear as a content block type OR as a message role. When it appears as a content block:

```json
{ "type": "toolResult", "toolResult": { "id": "<string>", "content": "<string>", "isError": <boolean> } }
```
- [ ] Inner `toolResult` object exists → **BLOCK** if missing
- [ ] `id` matches a previously-seen `toolCall.toolCall.id` → **WARNING** if orphaned
- [ ] `isError` is a boolean → **WARNING** if missing or wrong type

When `toolResult` appears as a **message role** (i.e., `message.role === "toolResult"`), the content blocks may include `toolCallId`, `toolName`, `isError`, and `content` at the message level. Validate these are present and consistent.

---

## 5. CONVERSATION-LEVEL INTEGRITY CHECKS — WARNING

These checks validate the conversation as a whole, beyond individual message structure.

### 5.1 — First Message Role

- [ ] The first message (`messages[0]`) has `role: "user"` → **WARNING** if conversation starts with assistant/toolResult

### 5.2 — parentId Chain

- [ ] `messages[0].parentId` is `null` or a valid string (first message may reference an external parent)
- [ ] Every subsequent message's `parentId` matches the `id` of a preceding message → **WARNING** if chain is broken (orphaned messages)
- [ ] No `id` duplicates exist → **BLOCK** if duplicate message IDs found

### 5.3 — toolCall ↔ toolResult Pairing

- [ ] Every `toolCall` block's `id` has a corresponding `toolResult` with matching `id` → **WARNING** if unanswered tool calls exist
- [ ] Every `toolResult` references a previously-seen `toolCall` → **WARNING** if orphaned results exist

### 5.4 — Minimum Conversation Depth

- [ ] Trajectory has at least 3 messages (a realistic conversation has user → assistant → user minimum) → **WARNING** if fewer than 3 messages

---

## 6. GOLDEN TRAJECTORY SPECIFIC CHECKS

Golden trajectory files (`golden_trajectory_v1.json`) are validated with the same structural rules as all other trajectories, PLUS:

- [ ] `system_prompt` MUST be non-empty (same rule — no exception for goldens)
- [ ] All `meta_info` fields must be present and valid
- [ ] Structure matches the same envelope/content block schema

> **Golden trajectories are NOT exempt from any structural check.** They are the most important deliverable and must meet the highest structural bar.

---

## 7. REPORTING FORMAT — MACHINE-READABLE JSON

> **CRITICAL**: You MUST output your final result as a single JSON code block (```json ... ```). The system parses this JSON programmatically. Any other format will cause the QC to fail silently.

Output exactly ONE JSON code block with this structure:

```json
{
  "severity": "<low|medium|high|critical>",
  "summary": "<1-3 sentence overall assessment>",
  "total_fails": <integer>,
  "total_warns": <integer>,
  "total_passes": <integer>,
  "checks": [
    {
      "check": <integer starting from 1>,
      "name": "<check name, e.g. 'Top-Level Keys', 'meta_info.task_type'>",
      "verdict": "<PASS|WARN|FAIL>",
      "reason": "<explanation of what was found or why it failed>",
      "fix": "<optional: suggested fix if verdict is WARN or FAIL, omit if PASS>"
    }
  ]
}
```

### Severity Mapping

| Condition | Severity |
|---|---|
| 0 FAILs, 0 WARNs | `low` |
| 0 FAILs, 1-4 WARNs | `medium` |
| 0 FAILs, 5+ WARNs | `high` |
| Any FAIL (BLOCK-level) | `critical` |

### Field Rules

- **`severity`**: One of `low`, `medium`, `high`, `critical`. Lowercase only.
- **`summary`**: Brief human-readable assessment. Include the most important finding.
- **`total_fails`**: Count of checks with verdict `FAIL`.
- **`total_warns`**: Count of checks with verdict `WARN`.
- **`total_passes`**: Count of checks with verdict `PASS`.
- **`checks`**: Array of individual check results. Each check object MUST have:
  - `check`: Sequential integer starting from 1.
  - `name`: Short check name (e.g. `"JSON Validity"`, `"meta_info.task_type"`).
  - `verdict`: Exactly one of `"PASS"`, `"WARN"`, or `"FAIL"` — **uppercase only**.
  - `reason`: Explanation. For PASS, briefly confirm what was found. For WARN/FAIL, explain what's wrong and what the actual value was.
  - `fix` *(optional)*: Suggested fix. Include only for WARN/FAIL verdicts.

### Example Output (passing trajectory)

```json
{
  "severity": "low",
  "summary": "Trajectory is structurally valid. All required keys present, meta_info complete, message chain intact.",
  "total_fails": 0,
  "total_warns": 0,
  "total_passes": 14,
  "checks": [
    {"check": 1, "name": "JSON Validity", "verdict": "PASS", "reason": "Valid JSON object, root is {}"},
    {"check": 2, "name": "Top-Level Keys", "verdict": "PASS", "reason": "meta_info and messages present, no extra keys"},
    {"check": 3, "name": "meta_info.task_type", "verdict": "PASS", "reason": "Value 'health_and_wellness' is in the valid enum list"},
    {"check": 4, "name": "meta_info.task_description", "verdict": "PASS", "reason": "Non-empty, 87 characters, not a placeholder"},
    {"check": 5, "name": "meta_info.task_completion_status", "verdict": "PASS", "reason": "Value 'success' is valid"},
    {"check": 6, "name": "meta_info.system_prompt", "verdict": "PASS", "reason": "Non-empty system prompt present (2341 chars)"},
    {"check": 7, "name": "meta_info.platform", "verdict": "PASS", "reason": "Value 'macOS' is a known platform"},
    {"check": 8, "name": "Message Envelope Structure", "verdict": "PASS", "reason": "All 24 messages have type, id, parentId, timestamp, message keys"},
    {"check": 9, "name": "First Message Role", "verdict": "PASS", "reason": "First message has role 'user'"},
    {"check": 10, "name": "parentId Chain", "verdict": "PASS", "reason": "All parentId references resolve to preceding message ids"},
    {"check": 11, "name": "Message Roles", "verdict": "PASS", "reason": "All roles are user/assistant/toolResult"},
    {"check": 12, "name": "Content Block Types", "verdict": "PASS", "reason": "All content blocks have valid types (text/thinking/toolCall/toolResult)"},
    {"check": 13, "name": "Timestamp Ordering", "verdict": "PASS", "reason": "Monotonically non-decreasing across all 24 messages"},
    {"check": 14, "name": "Conversation Depth", "verdict": "PASS", "reason": "24 messages (minimum 3 required)"}
  ]
}
```

### Example Output (failing trajectory)

```json
{
  "severity": "critical",
  "summary": "Empty system_prompt and broken parentId chain. 2 BLOCK-level failures.",
  "total_fails": 2,
  "total_warns": 1,
  "total_passes": 11,
  "checks": [
    {"check": 1, "name": "JSON Validity", "verdict": "PASS", "reason": "Valid JSON object"},
    {"check": 2, "name": "Top-Level Keys", "verdict": "PASS", "reason": "meta_info and messages present"},
    {"check": 3, "name": "meta_info.task_type", "verdict": "PASS", "reason": "Value 'customer_service' is valid"},
    {"check": 4, "name": "meta_info.task_description", "verdict": "WARN", "reason": "Only 18 characters — suspiciously short", "fix": "Provide a more descriptive task_description (50+ chars recommended)"},
    {"check": 5, "name": "meta_info.system_prompt", "verdict": "FAIL", "reason": "system_prompt is empty string after strip()", "fix": "Populate system_prompt with the persona/agent instructions used during the conversation"},
    {"check": 6, "name": "parentId Chain", "verdict": "FAIL", "reason": "messages[3].parentId='abc123' does not match any preceding message id", "fix": "Fix parentId of message at index 3 to reference the correct preceding message id"}
  ]
}
```

> **Do NOT include any text outside the JSON code block.** No preamble, no explanation, no markdown outside the fence. The JSON block is the ONLY output.

---

## 8. EXIT CRITERIA

| Condition | Verdict |
|---|---|
| 0 BLOCK errors across all files | **PASS** |
| Any BLOCK error in any file | **FAIL** — file is rejected |
| 5+ WARNING errors across batch | **FAIL** — batch-level concern |
| < 5 WARNING errors, 0 BLOCK | **CONDITIONAL PASS** — warnings noted |

---

## APPENDIX A: VALID TOOL NAMES

### Core Tools (lowercase, case-sensitive)

```
web_search, web_fetch, zeitgeist, read, write, edit, exec, process,
memory_search, memory_get, cron, subagents, message, nodes,
session_status, browser
```

### Skill Tools (Title Case, case-sensitive)

```
Artifacts, Spaces, Imagine, Slides, Skill Creator, Gmail,
Outlook Mail, Apple Email, Google Calendar, Outlook Calendar,
Apple Calendar, Google Contacts, Outlook Contacts, Apple Contacts,
WhatsApp, Telegram, Facebook Search, Instagram Search,
Threads Search, Polymarket, Oura, Withings, Strava, Tessie,
Shopping, Viator, Eventbrite, Printify, Google Drive, Browser,
Wide Research, Documents, Feed, User Context, Self-Awareness, Calendly
```

> **Note**: `browser` (lowercase) is a core tool. `Browser` (Title Case) is a Skill tool. Both are valid but they are different tools. Match is **case-sensitive**.

### Sub-Agent Tools (as seen in ideal file)

```
sessions_spawn, sessions_yield
```

> These may appear as `toolCall.toolCall.name` or as `toolName` in `toolResult` role messages. Both forms are valid.

---

## APPENDIX B: VALID `task_type` VALUES

```
home_and_organization
customer_service
research_and_analysis
creative_writing
technical_support
education_and_learning
health_and_wellness
finance_and_budgeting
```

## APPENDIX C: VALID `task_completion_status` VALUES

```
success
partial_success
incomplete
failure
```

## APPENDIX D: VALID `role` VALUES

```
user
assistant
toolResult
```

## APPENDIX E: VALID CONTENT BLOCK `type` VALUES

```
text
thinking
toolCall
toolResult
```

---

*This QC prompt is aligned to `check_trajectory_structure.py` and `ideal_jsonFile.json`. Every check traces to a specific validation in the script or a lesson learned from Batch 2 QC. If a trajectory passes all checks in this document, it is structurally sound for training pipeline ingestion.*
