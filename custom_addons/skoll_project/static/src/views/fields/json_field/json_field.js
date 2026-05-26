/** @odoo-module */

import { _t } from "@web/core/l10n/translation";
import { registry } from "@web/core/registry";
import { standardFieldProps } from "@web/views/fields/standard_field_props";
import { formatText } from "@web/views/fields/formatters";
import { useRecordObserver } from "@web/model/relational_model/utils";
import { useService } from "@web/core/utils/hooks";
import { rpc } from "@web/core/network/rpc";

import { Component, markup, useRef, useState, onMounted, onPatched, onWillPatch, onWillUnmount } from "@odoo/owl";
import { ConfirmationDialog } from "@web/core/confirmation_dialog/confirmation_dialog";

function escapeHtml(s) {
    const div = document.createElement("div");
    div.textContent = s;
    return div.innerHTML;
}

function syntaxHighlightJsonToHtml(jsonStr) {
    return escapeHtml(jsonStr).replace(
        /("(?:\\.|[^"\\])*")\s*:/g,
        '<span class="json-key">$1</span>:'
    ).replace(
        /:\s*("(?:\\.|[^"\\])*")/g,
        ': <span class="json-string">$1</span>'
    ).replace(
        /:\s*(\d+(?:\.\d+)?)/g,
        ': <span class="json-number">$1</span>'
    ).replace(
        /:\s*(true|false)/g,
        ': <span class="json-boolean">$1</span>'
    ).replace(
        /:\s*(null)/g,
        ': <span class="json-null">$1</span>'
    );
}

function parseEntries(raw) {
    if (!raw) return [];
    const trimmed = raw.trim();
    if (!trimmed) return [];
    try {
        const parsed = JSON.parse(trimmed);
        if (Array.isArray(parsed)) {
            return parsed;
        }
        return [{ session_id: "legacy", timestamp: "", trajectory: parsed }];
    } catch (_e) {
        return [{ session_id: "raw", timestamp: "", trajectory: trimmed }];
    }
}

function _extractAgentType(sessionKey) {
    const match = (sessionKey || "").match(/:subagent:(\w+)/);
    if (match) return match[1];
    const parts = (sessionKey || "").split(":");
    return parts[parts.length - 1] || "unknown";
}

function _findRoleAndContent(msg) {
    if (!msg || typeof msg !== "object") return { role: "", content: [] };
    if (msg.role) return { role: msg.role, content: msg.content || [] };
    if (msg.message) return _findRoleAndContent(msg.message);
    return { role: "", content: [] };
}

function _getFirstText(content) {
    if (!Array.isArray(content)) return "";
    for (const block of content) {
        if (block && block.type === "text" && block.text) return block.text;
    }
    return "";
}

function splitSubAgentMessages(trajectory) {
    if (!trajectory || typeof trajectory !== "object") return trajectory;
    if (
        trajectory.sub_agent_trajectories &&
        Object.keys(trajectory.sub_agent_trajectories).length > 0
    ) {
        return trajectory;
    }

    const messages = trajectory.messages;
    if (!Array.isArray(messages) || messages.length === 0) return trajectory;

    const tcidToArgs = {};
    const tcidToChildKey = {};

    for (const msg of messages) {
        const { role, content } = _findRoleAndContent(msg);
        if (role === "assistant" && Array.isArray(content)) {
            for (const block of content) {
                if (
                    block &&
                    block.type === "toolCall" &&
                    block.name === "sessions_spawn"
                ) {
                    let args = block.arguments;
                    if (typeof args === "string") {
                        try {
                            args = JSON.parse(args);
                        } catch {
                            args = {};
                        }
                    }
                    args = args || {};
                    tcidToArgs[block.id] = {
                        taskName: args.taskName || args.name || "",
                        taskText: args.task || args.prompt || "",
                    };
                }
            }
        }
        if (role === "toolResult") {
            const inner = msg.message || msg;
            const innerMsg = inner.message || inner;
            const toolName = innerMsg.toolName || inner.toolName || "";
            const tcid = innerMsg.toolCallId || inner.toolCallId || "";
            if (toolName === "sessions_spawn" && tcid) {
                const text = _getFirstText(
                    innerMsg.content || inner.content || [],
                );
                if (text) {
                    try {
                        const result = JSON.parse(text);
                        if (result.childSessionKey) {
                            tcidToChildKey[tcid] = result.childSessionKey;
                        }
                    } catch {}
                }
            }
        }
    }

    const childKeyInfo = {};
    const childKeys = [];
    for (const [tcid, childKey] of Object.entries(tcidToChildKey)) {
        const args = tcidToArgs[tcid] || {};
        childKeyInfo[childKey] = args;
        childKeys.push(childKey);
    }

    if (childKeys.length === 0) return trajectory;

    const taskWordSets = {};
    for (const [key, info] of Object.entries(childKeyInfo)) {
        taskWordSets[key] = new Set(
            (info.taskText || "")
                .toLowerCase()
                .split(/\s+/)
                .filter((w) => w.length > 3),
        );
    }

    const mainMessages = [];
    const subGroups = {};
    let currentKey = null;
    const usedKeys = new Set();

    for (const msg of messages) {
        const { role, content } = _findRoleAndContent(msg);

        if (role === "user") {
            const text = _getFirstText(content);
            if (text.includes("[Subagent Context]")) {
                const contextWords = new Set(
                    text
                        .toLowerCase()
                        .split(/\s+/)
                        .filter((w) => w.length > 3),
                );
                let bestKey = null;
                let bestOverlap = 0;
                for (const [key, words] of Object.entries(taskWordSets)) {
                    let overlap = 0;
                    for (const w of words) {
                        if (contextWords.has(w)) overlap++;
                    }
                    if (overlap > bestOverlap) {
                        bestOverlap = overlap;
                        bestKey = key;
                    }
                }
                if (!bestKey) {
                    bestKey =
                        childKeys.find((k) => !usedKeys.has(k)) ||
                        childKeys[0];
                }
                currentKey = bestKey;
                usedKeys.add(currentKey);
                subGroups[currentKey] = subGroups[currentKey] || [];
                subGroups[currentKey].push(msg);
                continue;
            } else if (currentKey) {
                currentKey = null;
            }
        }

        if (currentKey) {
            subGroups[currentKey] = subGroups[currentKey] || [];
            subGroups[currentKey].push(msg);
        } else {
            mainMessages.push(msg);
        }
    }

    if (Object.keys(subGroups).length === 0) return trajectory;

    const result = { ...trajectory, messages: mainMessages };
    const subTrajectories = {};
    for (const [key, msgs] of Object.entries(subGroups)) {
        const info = childKeyInfo[key] || {};
        subTrajectories[key] = {
            meta_info: {
                task_name: info.taskName || "",
                session_key: key,
                message_count: msgs.length,
            },
            messages: msgs,
        };
    }
    result.sub_agent_trajectories = subTrajectories;

    if (result.meta_info && result.meta_info.agents) {
        result.meta_info.agents.spawned = Object.keys(subTrajectories);
    }

    return result;
}

function renderTrajectoryHtml(trajectory) {
    if (typeof trajectory === "string") {
        return `<pre class="skoll-json-block"><code>${escapeHtml(trajectory)}</code></pre>`;
    }

    const traj = splitSubAgentMessages(trajectory);

    const subAgentTrajectories = traj.sub_agent_trajectories;
    const hasSubAgents =
        subAgentTrajectories &&
        typeof subAgentTrajectories === "object" &&
        Object.keys(subAgentTrajectories).length > 0;

    let mainObj = traj;
    if (hasSubAgents) {
        const { sub_agent_trajectories: _ignored, ...rest } = traj;
        mainObj = rest;
    }
    const mainMsgCount = Array.isArray(mainObj.messages)
        ? mainObj.messages.length
        : 0;
    const mainPretty = JSON.stringify(mainObj, null, 2);

    let html = `<details open class="skoll-sa-traj-entry skoll-main-traj-entry">`;
    html += `<summary class="skoll-sa-traj-summary">`;
    html += `<span class="skoll-sa-traj-badge skoll-sa-traj-badge-main">Main Trajectory</span>`;
    html += `<span class="skoll-sa-traj-msg-count">${mainMsgCount} msg${mainMsgCount !== 1 ? "s" : ""}</span>`;
    html += `</summary>`;
    html += `<pre class="skoll-json-block skoll-sa-traj-body"><code>${syntaxHighlightJsonToHtml(mainPretty)}</code></pre>`;
    html += `</details>`;

    if (!hasSubAgents) return html;

    const sessionKeys = Object.keys(subAgentTrajectories);

    for (const sk of sessionKeys) {
        const entry = subAgentTrajectories[sk];
        const isWrapped = entry && !Array.isArray(entry) && entry.messages;
        const msgs = isWrapped
            ? entry.messages
            : Array.isArray(entry)
              ? entry
              : [];
        const meta = isWrapped ? entry.meta_info || {} : {};

        const taskName = meta.task_name || "";
        const agent = _extractAgentType(sk);
        const msgCount = Array.isArray(msgs) ? msgs.length : 0;
        const displayObj = isWrapped ? entry : msgs;
        const sessionPretty = JSON.stringify(displayObj, null, 2);

        html += `<details class="skoll-sa-traj-entry">`;
        html += `<summary class="skoll-sa-traj-summary">`;
        if (taskName) {
            html += `<span class="skoll-sa-traj-badge skoll-sa-traj-badge-task">${escapeHtml(taskName)}</span>`;
        } else {
            html += `<span class="skoll-sa-traj-badge skoll-sa-traj-badge-${agent}">${agent}</span>`;
        }
        html += `<span class="skoll-sa-traj-key" title="${escapeHtml(sk)}">${escapeHtml(sk)}</span>`;
        html += `<span class="skoll-sa-traj-msg-count">${msgCount} msg${msgCount !== 1 ? "s" : ""}</span>`;
        html += `</summary>`;
        html += `<pre class="skoll-json-block skoll-sa-traj-body"><code>${syntaxHighlightJsonToHtml(sessionPretty)}</code></pre>`;
        html += `</details>`;
    }

    html += `</div>`;
    return html;
}

export class SkollJsonField extends Component {
    static template = "skoll.JsonField";
    static props = {
        ...standardFieldProps,
        placeholder: { type: String, optional: true },
    };

    setup() {
        this.state = useState({
            entries: [],
            deleting: -1,
            editingIndex: -1,
            editBuffer: "",
            editError: "",
            qcRunning: -1,
            qcResults: {},
            qcExpandedChecks: {},
            qcCollapsed: {},
            taskDescGenerating: -1,
        });
        this._qcPollingTimers = {};
        this._taskDescPollingTimers = {};
        this.orm = useService("orm");
        this.action = useService("action");
        this.dialog = useService("dialog");
        this.notification = useService("notification");
        this.editTextareaRef = useRef("editTextarea");
        this.rootRef = useRef("root");
        this._savedScrollPositions = null;
        this._lastRawValue = null;

        useRecordObserver((record) => {
            this._updateEntries(formatText(record.data[this.props.name]));
        });
        this._updateEntries(formatText(this.props.record.data[this.props.name]));

        this._onGoldenStatusChanged = (ev) => {
            this._handleGoldenStatusChanged(ev.detail);
        };
        if (this.isGoldenField) {
            this.env.bus.addEventListener(
                "SKOLL:GOLDEN_STATUS_CHANGED",
                this._onGoldenStatusChanged,
            );
        }

        this._onTaskDescTriggered = (ev) => {
            this._handleTaskDescTriggered(ev.detail);
        };
        this.env.bus.addEventListener(
            "SKOLL:TASK_DESC_TRIGGERED",
            this._onTaskDescTriggered,
        );

        this._onQcTriggered = (ev) => {
            this._handleQcTriggered(ev.detail);
        };
        this.env.bus.addEventListener(
            "SKOLL:QC_TRIGGERED",
            this._onQcTriggered,
        );

        onMounted(() => {
            this._autoResizeEditTextarea();
            this._resumePendingQc();
        });
        onWillPatch(() => this._saveScrollPositions());
        onPatched(() => {
            this._restoreScrollPositions();
            this._autoResizeEditTextarea();
        });
        onWillUnmount(() => {
            this._clearAllPolling();
            if (this.isGoldenField) {
                this.env.bus.removeEventListener(
                    "SKOLL:GOLDEN_STATUS_CHANGED",
                    this._onGoldenStatusChanged,
                );
            }
            this.env.bus.removeEventListener(
                "SKOLL:TASK_DESC_TRIGGERED",
                this._onTaskDescTriggered,
            );
            this.env.bus.removeEventListener(
                "SKOLL:QC_TRIGGERED",
                this._onQcTriggered,
            );
        });
    }

    _autoResizeEditTextarea() {
        const el = this.editTextareaRef.el;
        if (el) {
            el.style.height = "auto";
            el.style.height = Math.min(el.scrollHeight, 800) + "px";
        }
    }

    _saveScrollPositions() {
        const root = this.rootRef.el;
        if (!root) return;
        const positions = [];
        const bodies = root.querySelectorAll(".skoll-json-entry-body");
        for (const body of bodies) {
            positions.push(body.scrollTop);
        }
        const scrollParent = root.closest(".o_field_widget") || root;
        this._savedScrollPositions = {
            bodies: positions,
            rootTop: scrollParent.scrollTop,
        };
    }

    _restoreScrollPositions() {
        const saved = this._savedScrollPositions;
        if (!saved) return;
        this._savedScrollPositions = null;

        const root = this.rootRef.el;
        if (!root) return;

        const bodies = root.querySelectorAll(".skoll-json-entry-body");
        for (let i = 0; i < bodies.length && i < saved.bodies.length; i++) {
            if (saved.bodies[i]) {
                bodies[i].scrollTop = saved.bodies[i];
            }
        }
        const scrollParent = root.closest(".o_field_widget") || root;
        if (saved.rootTop) {
            scrollParent.scrollTop = saved.rootTop;
        }
    }

    _resumePendingQc() {
        for (const entry of this.state.entries) {
            if (entry.qcStatus === "pending") {
                this._startQcPolling(entry.index);
            }
            if (entry.taskDescriptionStatus === "pending") {
                this.state.taskDescGenerating = entry.index;
                this._startTaskDescPolling(entry.index);
            }
        }
    }

    _clearAllPolling() {
        for (const key of Object.keys(this._qcPollingTimers)) {
            clearInterval(this._qcPollingTimers[key]);
        }
        this._qcPollingTimers = {};
        for (const key of Object.keys(this._taskDescPollingTimers)) {
            clearInterval(this._taskDescPollingTimers[key]);
        }
        this._taskDescPollingTimers = {};
    }

    async _handleGoldenStatusChanged(payload) {
        if (!payload || !payload.task_id) return;
        if (payload.task_id !== this.props.record.resId) return;
        try {
            await this.props.record.load();
        } catch (e) {
            console.warn("[SkollJsonField] Failed to reload after golden status change:", e);
        }
    }

    _handleTaskDescTriggered(payload) {
        if (!payload) return;
        const { field_name, entry_index } = payload;
        if (field_name !== this.props.name) return;
        if (this.state.taskDescGenerating >= 0) return;
        this.state.taskDescGenerating = entry_index;
        this._startTaskDescPolling(entry_index);
    }

    _handleQcTriggered(payload) {
        if (!payload) return;
        const { field_name, entry_index } = payload;
        if (field_name !== this.props.name) return;
        if (this.state.qcRunning >= 0) return;
        this.state.qcRunning = entry_index;
        this.state.qcCollapsed[entry_index] = false;
        delete this.state.qcResults[entry_index];
        this._startQcPolling(entry_index);
    }

    _startQcPolling(index) {
        if (this._qcPollingTimers[index]) return;
        this._qcPollingTimers[index] = setInterval(async () => {
            try {
                await this.props.record.load();
                const raw = formatText(this.props.record.data[this.props.name]);
                const entries = parseEntries(raw);
                if (index < entries.length) {
                    const status = entries[index].qc_status;
                    if (status && status !== "pending") {
                        clearInterval(this._qcPollingTimers[index]);
                        delete this._qcPollingTimers[index];
                        if (status === "done" && entries[index].qc_result) {
                            this.state.qcResults[index] = entries[index].qc_result;
                            this.state.qcCollapsed[index] = false;
                        }
                        this.state.qcRunning = -1;
                    }
                }
            } catch (_e) {
                // network hiccup — keep polling
            }
        }, 5000);
    }

    _startTaskDescPolling(index) {
        if (this._taskDescPollingTimers[index]) return;
        this._taskDescPollingTimers[index] = setInterval(async () => {
            try {
                await this.props.record.load();
                const raw = formatText(this.props.record.data[this.props.name]);
                const entries = parseEntries(raw);
                if (index < entries.length) {
                    const status = entries[index].task_description_status;
                    if (status && status !== "pending") {
                        clearInterval(this._taskDescPollingTimers[index]);
                        delete this._taskDescPollingTimers[index];
                        this.state.taskDescGenerating = -1;
                        if (status === "done") {
                            this.onQcEntry(index);
                        }
                    }
                }
            } catch (_e) {
            }
        }, 5000);
    }

    _updateEntries(raw) {
        if (raw === this._lastRawValue) return;
        this._lastRawValue = raw;
        const parsed = parseEntries(raw);
        this.state.entries = parsed.map((entry, idx) => {
            const tokensIn = Number.isFinite(entry.tokens_in) ? entry.tokens_in : null;
            const tokensOut = Number.isFinite(entry.tokens_out) ? entry.tokens_out : null;
            const hasTokens = tokensIn !== null || tokensOut !== null;
            return {
                index: idx,
                sessionId: entry.session_id || `session-${idx + 1}`,
                timestamp: entry.timestamp || "",
                trajectory: entry.trajectory,
                html: entry.deleted
                    ? markup("")
                    : markup(renderTrajectoryHtml(entry.trajectory)),
                deleted: !!entry.deleted,
                deletedReason: entry.deleted_reason || "",
                qcStatus: entry.qc_status || null,
                qcResult: entry.qc_result || null,
                taskDescriptionStatus: entry.task_description_status || null,
                hasTokens,
                tokensIn: tokensIn || 0,
                tokensOut: tokensOut || 0,
                tokensTotal: (tokensIn || 0) + (tokensOut || 0),
            };
        });
    }

    formatNumber(n) {
        return new Intl.NumberFormat().format(n);
    }

    get hasEntries() {
        return this.state.entries.length > 0;
    }

    get isEditable() {
        return !this.props.readonly;
    }

    get isAnyActionRunning() {
        return this.state.qcRunning >= 0 || this.state.taskDescGenerating >= 0;
    }

    get isGoldenField() {
        return this.props.name === "golden_trajectory";
    }

    get isSkollAdmin() {
        return !!this.props.record.data.is_skoll_admin;
    }

    get isQlOrPl() {
        return !!this.props.record.data.is_ql_or_pl;
    }

    onDeleteTrajectory() {
        if (!this.isSkollAdmin) return;
        const recordId = this.props.record.resId;
        const fieldName = this.props.name;
        if (!recordId) {
            this.notification.add(_t("Save the record first"), { type: "warning" });
            return;
        }

        this.dialog.add(ConfirmationDialog, {
            title: _t("Delete Trajectory"),
            body: _t(
                "Delete this trajectory and all of its recorded turns? The sandbox must be stopped. This cannot be undone.",
            ),
            confirmLabel: _t("Delete"),
            confirmClass: "btn-danger",
            cancelLabel: _t("Cancel"),
            confirm: async () => {
                try {
                    await this.orm.call(
                        "skoll.skoll",
                        "action_delete_trajectory_by_field",
                        [[recordId]],
                        { field_name: fieldName },
                    );
                    await this.props.record.load();
                    this.notification.add(_t("Trajectory deleted."), { type: "success" });
                } catch (e) {
                    this.notification.add(
                        e.data?.message || e.message || _t("Failed to delete trajectory"),
                        { type: "danger" },
                    );
                }
            },
        });
    }

    onDeleteEntry(index) {
        this.dialog.add(ConfirmationDialog, {
            title: _t("Delete Trajectory Session"),
            body: _t("Are you sure you want to delete this generated trajectory session? This action cannot be undone."),
            confirmLabel: _t("Delete"),
            confirmClass: "btn-danger",
            cancelLabel: _t("Cancel"),
            confirm: async () => {
                this.state.deleting = index;
                try {
                    await this.orm.call(
                        "skoll.skoll",
                        "action_delete_trajectory_entry",
                        [this.props.record.resId],
                        { field_name: this.props.name, entry_index: index }
                    );
                    await this.props.record.load();
                } finally {
                    this.state.deleting = -1;
                }
            },
        });
    }

    onEditEntry(index) {
        const entry = this.state.entries[index];
        let text;
        if (typeof entry.trajectory === "string") {
            text = entry.trajectory;
        } else {
            text = JSON.stringify(entry.trajectory, null, 2);
        }
        this.state.editingIndex = index;
        this.state.editBuffer = text;
        this.state.editError = "";
    }

    onEditInput(ev) {
        this.state.editBuffer = ev.target.value;
        this.state.editError = "";
        ev.target.style.height = "auto";
        ev.target.style.height = Math.min(ev.target.scrollHeight, 800) + "px";
    }

    onSaveEntry() {
        const idx = this.state.editingIndex;
        if (idx < 0) return;

        let newTrajectory;
        try {
            newTrajectory = JSON.parse(this.state.editBuffer);
        } catch (e) {
            this.state.editError = "Invalid JSON: " + e.message;
            return;
        }

        const raw = this.props.record.data[this.props.name] || "";
        let entries = parseEntries(raw);
        if (idx < entries.length) {
            entries[idx].trajectory = newTrajectory;
        }

        const newValue = JSON.stringify(entries, null, 2);
        this.props.record.update({ [this.props.name]: newValue });

        this.state.editingIndex = -1;
        this.state.editBuffer = "";
        this.state.editError = "";
    }

    onCancelEdit() {
        this.state.editingIndex = -1;
        this.state.editBuffer = "";
        this.state.editError = "";
    }

    async onQcEntry(index) {
        if (this.state.qcRunning >= 0 || this.state.taskDescGenerating >= 0) return;

        const recordId = this.props.record.resId;
        const fieldName = this.props.name;
        if (!recordId) {
            this.notification.add(_t("Save the record first"), { type: "warning" });
            return;
        }

        this.state.qcRunning = index;
        this.state.qcCollapsed[index] = false;
        delete this.state.qcResults[index];

        try {
            const resp = await rpc("/skoll/trajectory_qc", {
                record_id: recordId,
                field_name: fieldName,
                entry_index: index,
            });
            if (resp.error) {
                this.notification.add(resp.error, { type: "danger", sticky: false });
                this.state.qcRunning = -1;
                return;
            }
            this._startQcPolling(index);
        } catch (e) {
            this.notification.add(
                _t("QC failed: ") + (e.message || String(e)),
                { type: "danger", sticky: false },
            );
            this.state.qcRunning = -1;
        }
    }

    async onGenerateTaskDesc(index) {
        if (!this.isQlOrPl) return;
        if (this.state.taskDescGenerating >= 0 || this.state.qcRunning >= 0) return;

        const recordId = this.props.record.resId;
        const fieldName = this.props.name;
        if (!recordId) {
            this.notification.add(_t("Save the record first"), { type: "warning" });
            return;
        }

        this.state.taskDescGenerating = index;

        try {
            const resp = await rpc("/skoll/generate_task_description", {
                record_id: recordId,
                field_name: fieldName,
                entry_index: index,
            });
            if (resp.error) {
                this.notification.add(resp.error, { type: "danger", sticky: false });
                this.state.taskDescGenerating = -1;
                return;
            }
            this._startTaskDescPolling(index);
        } catch (e) {
            this.notification.add(
                _t("Description generation failed: ") + (e.message || String(e)),
                { type: "danger", sticky: false },
            );
            this.state.taskDescGenerating = -1;
        }
    }

    async onAbortQc(index) {
        const recordId = this.props.record.resId;
        const fieldName = this.props.name;
        if (!recordId) return;

        try {
            await rpc("/skoll/abort_trajectory_action", {
                record_id: recordId,
                field_name: fieldName,
                entry_index: index,
                action_type: "qc",
            });
        } catch (_e) { /* best-effort */ }

        if (this._qcPollingTimers[index]) {
            clearInterval(this._qcPollingTimers[index]);
            delete this._qcPollingTimers[index];
        }
        this.state.qcRunning = -1;
        // Reset local entry status so buttons return to initial state immediately
        if (this.state.entries[index]) {
            this.state.entries[index].qcStatus = "aborted";
        }
        this.notification.add(_t("QC aborted"), { type: "info" });
        // Reload record to sync DB state
        try { await this.props.record.load(); } catch (_e) { /* ignore */ }
    }

    async onAbortTaskDesc(index) {
        const recordId = this.props.record.resId;
        const fieldName = this.props.name;
        if (!recordId) return;

        try {
            await rpc("/skoll/abort_trajectory_action", {
                record_id: recordId,
                field_name: fieldName,
                entry_index: index,
                action_type: "task_description",
            });
        } catch (_e) { /* best-effort */ }

        if (this._taskDescPollingTimers[index]) {
            clearInterval(this._taskDescPollingTimers[index]);
            delete this._taskDescPollingTimers[index];
        }
        this.state.taskDescGenerating = -1;
        // Reset local entry status so buttons return to initial state immediately
        if (this.state.entries[index]) {
            this.state.entries[index].taskDescriptionStatus = "aborted";
        }
        this.notification.add(_t("Description generation aborted"), { type: "info" });
        // Reload record to sync DB state
        try { await this.props.record.load(); } catch (_e) { /* ignore */ }
    }

    onToggleQcChecks(index) {
        this.state.qcExpandedChecks[index] = !this.state.qcExpandedChecks[index];
    }

    onShowQc(index) {
        this.state.qcCollapsed[index] = false;
    }

    onDismissQc(index) {
        // Just collapse the panel — don't clear persisted QC state
        this.state.qcCollapsed = this.state.qcCollapsed || {};
        this.state.qcCollapsed[index] = true;
        this.state.qcExpandedChecks[index] = false;
    }

    severityClass(severity) {
        const map = { low: "success", medium: "warning", high: "danger", critical: "danger" };
        return map[severity] || "secondary";
    }

    severityIcon(severity) {
        const map = {
            low: "fa-check-circle",
            medium: "fa-exclamation-circle",
            high: "fa-exclamation-triangle",
            critical: "fa-ban",
        };
        return map[severity] || "fa-question-circle";
    }
}

export const skollJsonField = {
    component: SkollJsonField,
    displayName: _t("JSON Viewer"),
    supportedTypes: ["text"],
    extractProps: ({ attrs }) => ({
        placeholder: attrs.placeholder,
    }),
};

registry.category("fields").add("skoll_json", skollJsonField);
