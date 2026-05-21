# Nested Sub-Agent Chat Implementation Plan

> **Task**: Show sub-agent spawning tree in the chat UI. Each sub-agent appears as a collapsible read-only box within the main chat, similar to OpenCode desktop's nested agent visualization.

## 1. Current State Analysis

### What Already Exists (Backend — COMPLETE)

The backend **already captures and stores** all sub-agent data:

| Component | Location | What It Does |
|-----------|----------|--------------|
| `ws_client.py` | Lines 76-815 | Subscribes to `sessions.changed` events, detects sub-agent spawns, captures messages from child sessions, builds hierarchical spawn tree |
| `consumer.py` | Lines 500-536 | After main response, calls `get_sub_agent_messages()` + `get_spawn_tree()`, saves via `auto_process_save_sub_agent_messages` |
| `skoll.turn` fields | `models/skoll.py:1931-1932` | `sub_agent_messages = fields.Text` (JSON), `spawn_tree = fields.Text` (JSON) |
| `skoll_sandbox.py` | Lines 1244-1293 | `_collect_sub_agent_messages()`, `_collect_spawn_tree()`, `_build_session_key_map()` — used in trajectory export |
| `auto_process_save_sub_agent_messages` | `skoll_sandbox.py:2876-2902` | Merges sub-agent messages into turn record, saves spawn tree |

### What's Missing (Frontend — THIS TASK)

The chat widget (`chat_widget.js`) currently:
- ✅ Displays main agent messages (user/assistant) with tool calls, thinking, QC, feedback
- ✅ Has collapsible UI patterns (thinking section, tools section)
- ❌ Does NOT display sub-agent messages or spawn tree
- ❌ Does NOT fetch sub-agent data from history endpoint
- ❌ Has no UI component for nested agent visualization

### Data Structures Available

**Spawn Tree** (hierarchical, stored per-turn):
```json
[
  {
    "sessionKey": "odoo:sandbox:42:subagent:explore-1",
    "agent": "explore",
    "parentKey": "odoo:sandbox:42",
    "spawned_at": "2025-05-13T10:30:00Z",
    "message_count": 5,
    "children": [
      {
        "sessionKey": "odoo:sandbox:42:subagent:explore-1:subagent:librarian-1",
        "agent": "librarian",
        "parentKey": "odoo:sandbox:42:subagent:explore-1",
        "spawned_at": "2025-05-13T10:30:15Z",
        "message_count": 3,
        "children": []
      }
    ]
  }
]
```

**Sub-Agent Messages** (flat list, stored per-turn):
```json
[
  {
    "sessionKey": "odoo:sandbox:42:subagent:explore-1",
    "role": "user",
    "text": "Find auth implementations in src/",
    "provenance": {"kind": "inter_session"},
    "timestamp": "2025-05-13T10:30:00Z",
    "kind": "inter_session"
  },
  {
    "sessionKey": "odoo:sandbox:42:subagent:explore-1",
    "role": "assistant",
    "text": "Found 3 auth files...",
    "provenance": {},
    "timestamp": "2025-05-13T10:30:05Z",
    "kind": ""
  }
]
```

---

## 2. UX Design (OpenCode Reference)

### OpenCode Desktop Pattern

In OpenCode, when the main agent spawns a sub-agent:
1. An **inline collapsed box** appears in the chat stream with:
   - Agent type badge (e.g., "Explore", "Librarian", "Oracle")
   - Brief description/prompt (first line of the sub-agent's task)
   - Status indicator (spinner while running, checkmark when done)
   - Message count badge
2. **Clicking the box** expands it inline, revealing:
   - The full prompt sent to the sub-agent
   - The sub-agent's response/trajectory (read-only)
   - Any tool calls the sub-agent made
   - Nested sub-agents (recursively — same pattern)
3. **Read-only** — no interaction, just viewing
4. Nesting is visually indicated by indentation + left border color

### Proposed Skoll Adaptation

Since Skoll data is loaded **post-hoc** (after the turn completes, from stored JSON), we adapt:

- Sub-agent boxes appear **after the main assistant response** (not during streaming — data arrives only after completion)
- Each root-level spawn gets its own collapsible card
- Children render recursively inside their parent's expanded view
- During auto-process (no live WebSocket), data is fully available from history load

---

## 3. Implementation Plan

### Phase 1: Backend API Changes (Controller)

**File**: `controllers/chat.py` — modify `/skoll/chat/history` endpoint

**Change**: Include `sub_agent_messages` and `spawn_tree` in turn data returned to frontend.

```python
# In chat_history() method, add to the turn dict:
turns.append({
    # ...existing fields...
    "sub_agent_messages": t.sub_agent_messages or "",
    "spawn_tree": t.spawn_tree or "",
})
```

**Effort**: ~5 lines changed. Trivial.

---

### Phase 2: Frontend Data Layer (chat_widget.js)

**File**: `static/src/chat_widget/chat_widget.js`

**Changes in `_loadHistory()`** (around line 1116-1178):

After reconstructing assistant messages from turns, attach sub-agent data:

```javascript
if (t.response) {
    const msg = { /* ...existing msg construction... */ };
    
    // NEW: Attach sub-agent data
    if (t.spawn_tree) {
        try {
            const tree = JSON.parse(t.spawn_tree);
            if (Array.isArray(tree) && tree.length > 0) {
                msg.spawnTree = tree;
                msg.subAgentMessages = {};
                // Group messages by sessionKey
                if (t.sub_agent_messages) {
                    const allMsgs = JSON.parse(t.sub_agent_messages);
                    for (const m of allMsgs) {
                        const key = m.sessionKey || "";
                        if (!msg.subAgentMessages[key]) msg.subAgentMessages[key] = [];
                        msg.subAgentMessages[key].push(m);
                    }
                }
            }
        } catch (e) {
            console.warn(LOG_PREFIX, "spawn_tree parse error:", e);
        }
    }
    
    this._session.messages.push(msg);
}
```

**Effort**: ~25 lines. Low complexity.

---

### Phase 3: Frontend UI Component (XML Template)

**File**: `static/src/chat_widget/chat_widget.xml`

**Add after the `o_skoll_tools_section`** (after line 125), inside the assistant message bubble:

```xml
<!-- Sub-agent spawn tree -->
<t t-if="msg.spawnTree and msg.spawnTree.length">
    <div class="o_skoll_subagents_section">
        <div class="o_skoll_subagents_header">
            <i class="fa fa-sitemap me-1"/>
            <span><t t-out="msg.spawnTree.length"/> sub-agent(s) spawned</span>
        </div>
        <t t-foreach="msg.spawnTree" t-as="agent" t-key="agent.sessionKey">
            <t t-call="skoll.SubAgentCard">
                <t t-set="agent" t-value="agent"/>
                <t t-set="subAgentMessages" t-value="msg.subAgentMessages"/>
                <t t-set="depth" t-value="0"/>
            </t>
        </t>
    </div>
</t>
```

**New sub-template** `skoll.SubAgentCard` (in same file):

```xml
<t t-name="skoll.SubAgentCard">
    <div t-attf-class="o_skoll_subagent_card o_skoll_subagent_depth_#{depth}">
        <button class="o_skoll_subagent_toggle"
                t-on-click="() => this.onToggleSubAgent(agent)">
            <i t-attf-class="fa fa-chevron-#{agent._expanded ? 'down' : 'right'} me-1"/>
            <span t-attf-class="o_skoll_subagent_badge o_skoll_subagent_badge_#{agent.agent || 'unknown'}">
                <t t-out="(agent.agent || 'sub-agent').charAt(0).toUpperCase() + (agent.agent || 'sub-agent').slice(1)"/>
            </span>
            <span class="o_skoll_subagent_prompt_preview">
                <t t-out="this.getSubAgentPreview(agent, subAgentMessages)"/>
            </span>
            <span class="o_skoll_subagent_meta">
                <t t-if="agent.message_count">
                    <span class="badge bg-secondary"><t t-out="agent.message_count"/> msgs</span>
                </t>
            </span>
        </button>
        <t t-if="agent._expanded">
            <div class="o_skoll_subagent_body">
                <!-- Messages for this sub-agent -->
                <t t-set="agentMsgs" t-value="subAgentMessages[agent.sessionKey] || []"/>
                <t t-foreach="agentMsgs" t-as="sam" t-key="sam_index">
                    <div t-attf-class="o_skoll_subagent_msg o_skoll_subagent_msg_#{sam.role}">
                        <span class="o_skoll_subagent_msg_role">
                            <t t-if="sam.role === 'user'">Prompt</t>
                            <t t-else="">Response</t>
                        </span>
                        <div class="o_skoll_subagent_msg_text">
                            <t t-out="sam.text"/>
                        </div>
                    </div>
                </t>
                <!-- Recursive children -->
                <t t-if="agent.children and agent.children.length">
                    <div class="o_skoll_subagent_children">
                        <t t-foreach="agent.children" t-as="child" t-key="child.sessionKey">
                            <t t-call="skoll.SubAgentCard">
                                <t t-set="agent" t-value="child"/>
                                <t t-set="subAgentMessages" t-value="subAgentMessages"/>
                                <t t-set="depth" t-value="depth + 1"/>
                            </t>
                        </t>
                    </div>
                </t>
            </div>
        </t>
    </div>
</t>
```

**Effort**: ~60 lines XML. Medium complexity.

---

### Phase 4: Frontend Logic (JS Methods)

**File**: `static/src/chat_widget/chat_widget.js`

Add methods to `SkollChatWidget` class:

```javascript
onToggleSubAgent(agent) {
    agent._expanded = !agent._expanded;
}

getSubAgentPreview(agent, subAgentMessages) {
    const msgs = subAgentMessages?.[agent.sessionKey] || [];
    const firstUser = msgs.find(m => m.role === "user");
    if (firstUser && firstUser.text) {
        const text = firstUser.text;
        return text.length > 80 ? text.substring(0, 77) + "…" : text;
    }
    return agent.agent || "Sub-agent task";
}
```

**Effort**: ~15 lines. Trivial.

---

### Phase 5: Styling (SCSS)

**File**: `static/src/chat_widget/chat_widget.scss`

```scss
/* Sub-agent spawn tree */
.o_skoll_subagents_section {
    margin-top: 10px;
    padding-top: 8px;
    border-top: 1px solid rgba(0, 0, 0, 0.08);
}

.o_skoll_subagents_header {
    font-size: 11px;
    color: var(--o-text-color-muted, #6c757d);
    font-weight: 600;
    margin-bottom: 6px;
    text-transform: uppercase;
    letter-spacing: 0.3px;
}

.o_skoll_subagent_card {
    margin-bottom: 4px;
    border-radius: 8px;
    border: 1px solid var(--o-border-color, #dee2e6);
    overflow: hidden;
    background: var(--o-view-background-color, #fff);
}

.o_skoll_subagent_depth_0 { border-left: 3px solid #6366f1; }
.o_skoll_subagent_depth_1 { border-left: 3px solid #8b5cf6; margin-left: 12px; }
.o_skoll_subagent_depth_2 { border-left: 3px solid #a78bfa; margin-left: 24px; }

.o_skoll_subagent_toggle {
    display: flex;
    align-items: center;
    gap: 6px;
    width: 100%;
    padding: 8px 10px;
    background: none;
    border: none;
    cursor: pointer;
    font-size: 12px;
    text-align: left;
    transition: background 0.15s;

    &:hover {
        background: rgba(0, 0, 0, 0.03);
    }
}

.o_skoll_subagent_badge {
    display: inline-block;
    padding: 1px 6px;
    border-radius: 4px;
    font-size: 10px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.3px;
    flex-shrink: 0;
}

.o_skoll_subagent_badge_explore { background: #dbeafe; color: #1e40af; }
.o_skoll_subagent_badge_librarian { background: #fef3c7; color: #92400e; }
.o_skoll_subagent_badge_oracle { background: #ede9fe; color: #5b21b6; }
.o_skoll_subagent_badge_unknown { background: #f3f4f6; color: #374151; }

.o_skoll_subagent_prompt_preview {
    flex: 1;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    color: var(--o-text-color-muted, #6c757d);
    font-size: 11px;
}

.o_skoll_subagent_meta {
    flex-shrink: 0;
    .badge { font-size: 9px; font-weight: 500; }
}

.o_skoll_subagent_body {
    padding: 8px 12px;
    border-top: 1px solid var(--o-border-color, #dee2e6);
    background: rgba(0, 0, 0, 0.015);
    max-height: 400px;
    overflow-y: auto;
}

.o_skoll_subagent_msg {
    margin-bottom: 6px;
    padding: 6px 8px;
    border-radius: 6px;
    font-size: 12px;
    line-height: 1.4;
}

.o_skoll_subagent_msg_role {
    font-size: 10px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.3px;
    display: block;
    margin-bottom: 2px;
}

.o_skoll_subagent_msg_user {
    background: rgba(99, 102, 241, 0.06);
    border-left: 2px solid #6366f1;
    .o_skoll_subagent_msg_role { color: #4338ca; }
}

.o_skoll_subagent_msg_assistant {
    background: rgba(0, 0, 0, 0.03);
    .o_skoll_subagent_msg_role { color: var(--o-text-color-muted, #6c757d); }
}

.o_skoll_subagent_msg_text {
    white-space: pre-wrap;
    word-break: break-word;
    color: var(--o-text-color, #212529);
}

.o_skoll_subagent_children {
    margin-top: 8px;
    padding-top: 6px;
    border-top: 1px dashed rgba(0, 0, 0, 0.1);
}
```

**Effort**: ~100 lines SCSS. Mechanical styling.

---

## 4. Implementation Sequence

| Step | Files Modified | Lines Added | Risk |
|------|---------------|-------------|------|
| 1. API change | `controllers/chat.py` | ~5 | None |
| 2. Data layer | `chat_widget.js` (_loadHistory) | ~25 | Low |
| 3. JS methods | `chat_widget.js` (new methods) | ~15 | None |
| 4. XML template | `chat_widget.xml` | ~60 | Low |
| 5. SCSS styling | `chat_widget.scss` | ~100 | None |

**Total**: ~205 lines across 3 files. No new files, no model changes, no migrations.

---

## 5. Key Design Decisions

### Why Inline Collapsible Cards (Not Modal/Drawer)?

1. **Consistency**: Existing patterns (thinking section, tools section) use inline collapsible—same UX muscle memory
2. **Context**: User sees sub-agents in the context of the response that spawned them
3. **Recursive**: Easy to nest infinitely without modal stacking nightmares
4. **Read-only**: No interaction needed, just expand/collapse to view

### Why Post-Hoc (Not Real-Time)?

1. Sub-agent data arrives via `consumer.py` (background process) → stored in turn → loaded via history
2. The live WebSocket path in `chat_widget.js` handles `sessions.changed` events but does NOT yet store them client-side. Adding real-time would require significant additional work (tracking child sessions in browser WS)
3. **Post-hoc is the pragmatic first step** — the data is always available after turn completion
4. Real-time sub-agent streaming can be a follow-up Phase 2 enhancement

### Why No New OWL Component (Sub-Component)?

1. The sub-agent card is **template-only** (uses `t-call` recursion) — no separate state management needed
2. State lives on the message object (`msg.spawnTree[i]._expanded`) — reactive via OWL's `useState`
3. A separate component would add overhead (registration, props interface, lifecycle) for what is essentially a template fragment
4. If complexity grows later, extraction to `components/subagent_card/` is straightforward

### Why Group Messages by sessionKey in Frontend?

1. Backend stores messages as flat list (simpler storage, easier merge)
2. Frontend groups by `sessionKey` to display per-agent — O(n) transform at load time
3. Keeps the API contract simple: return raw data, let UI handle presentation

---

## 6. Edge Cases & Considerations

| Case | Handling |
|------|----------|
| No sub-agents spawned | `msg.spawnTree` is null/undefined → section not rendered |
| Sub-agent with 0 messages | Card shows but body is empty ("No messages captured") |
| Very deep nesting (3+) | CSS caps at depth_2 styling; deeper levels reuse depth_2 |
| Large sub-agent response | `max-height: 400px; overflow-y: auto` on body |
| Spawn tree without messages | Card renders with agent name, "0 msgs" badge, no body content |
| Multiple turns with spawn data | Each turn's assistant message shows its own spawn tree |
| Markdown in sub-agent text | Not rendered as markdown (plain text) — sub-agent responses are typically short/tool-like. Can add markdown rendering later if needed |

---

## 7. Testing Strategy

1. **Manual test with existing data**: Tasks that ran via `consumer.py` already have `sub_agent_messages` and `spawn_tree` stored. Load their chat history → verify cards appear.
2. **Empty state**: Load a sandbox with no sub-agent data → verify no visual regression.
3. **Expand/collapse**: Click cards → verify toggle works, nested cards render.
4. **Overflow**: Find a turn with many sub-agents or long messages → verify scrolling works.

---

## 8. Future Enhancements (Out of Scope)

- **Real-time sub-agent streaming**: Subscribe to `sessions.changed` and `session.message` in the browser WebSocket to show sub-agents appearing live during streaming
- **Sub-agent tool calls**: Parse sub-agent messages for tool_use blocks, render them with the same collapsible tool UI
- **Sub-agent thinking**: If sub-agents have thinking blocks, show them like main agent thinking
- **Search/filter**: Allow filtering spawn tree by agent type
- **Timing info**: Show duration per sub-agent (spawned_at → last message timestamp)
