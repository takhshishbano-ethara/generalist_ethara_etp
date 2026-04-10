/** @odoo-module */
import { Component, useState, useRef, onMounted, onWillDestroy, onPatched, markup } from "@odoo/owl";
import { rpc } from "@web/core/network/rpc";

const MARKED_CDN = "https://cdn.jsdelivr.net/npm/marked@11.1.1/marked.min.js";
let _markedReady = false;

function _loadMarked() {
    if (_markedReady || document.querySelector(`script[src="${MARKED_CDN}"]`)) {
        _markedReady = true;
        return Promise.resolve();
    }
    return new Promise((resolve, reject) => {
        const s = document.createElement("script");
        s.src = MARKED_CDN;
        s.onload = () => { _markedReady = true; resolve(); };
        s.onerror = reject;
        document.head.appendChild(s);
    });
}

function renderMarkdown(text) {
    if (!text) return "";
    const lib = window.marked?.default ?? window.marked;
    if (lib && typeof lib.parse === "function") {
        try {
            const html = lib.parse(text);
            return typeof html === "string" ? html : String(html);
        } catch (_) { /* fall through */ }
    }
    return text.replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/\n/g, "<br/>");
}

const LOG_PREFIX = "[talos-chat]";
const STREAM_WORD_THRESHOLD = 5;

let _msgId = 0;
function nextId() {
    return `odoo-${++_msgId}-${Date.now().toString(36)}`;
}

const _sessions = new Map();

function _getSession(sandboxId) {
    if (!_sessions.has(sandboxId)) {
        _sessions.set(sandboxId, {
            ws: null,
            wsConnected: false,
            messages: [],
            streaming: false,
            currentTurnId: null,
            historyLoaded: false,
            _streamBuf: "",
            _lastFlushedWordCount: 0,
            _toolCalls: [],
            _rawEvents: [],
            _pendingRpc: new Map(),
            qcPending: false,
            qcResult: null,
            qcDismissReason: "",
            qcPromptText: "",
        });
    }
    return _sessions.get(sandboxId);
}

export class TalosChatWidget extends Component {
    static template = "talos.ChatWidget";
    static props = {
        sandboxId: Number,
        dockerStatus: String,
        dockerWsUrl: { type: [String, Boolean], optional: true },
        gatewayToken: { type: [String, Boolean], optional: true },
    };

    setup() {
        this.messagesEndRef = useRef("messagesEnd");

        this._currentSandboxId = this.props.sandboxId;
        this._session = _getSession(this.props.sandboxId);

        this.state = useState({
            messages: [],
            inputText: "",
            sending: false,
            streaming: false,
            currentTurnId: null,
            connected: false,
            statusText: "Initializing...",
            activityText: "",
            // QC state
            qcPending: false,
            qcResult: null,       // { severity, summary, checks, ... } or null
            qcDismissReason: "",   // for "high" severity justification
            qcPromptText: "",      // the original prompt text awaiting QC resolution
        });

        this._onWsMessage = (payload) => this._handleWsPayload(payload);

        onMounted(() => {
            console.log(LOG_PREFIX, "Widget mounted. sandboxId:", this.props.sandboxId,
                "sessionCached:", this._session.wsConnected,
                "messages:", this._session.messages.length);

            _loadMarked().catch(e => console.warn(LOG_PREFIX, "marked load failed:", e));
            this._syncFromSession();
        });

        onPatched(() => {
            if (this.props.sandboxId !== this._currentSandboxId) {
                console.log(LOG_PREFIX, "SandboxId changed:", this._currentSandboxId, "->", this.props.sandboxId);
                this._detachFromSession();
                this._currentSandboxId = this.props.sandboxId;
                this._session = _getSession(this.props.sandboxId);
                this._syncFromSession();
            }
        });

        onWillDestroy(() => {
            console.log(LOG_PREFIX, "Widget unmounting. WS stays alive.");
            this._detachFromSession();
        });
    }

    _syncFromSession() {
        this.state.messages.length = 0;
        for (const msg of this._session.messages) {
            this.state.messages.push(msg);
        }
        this._session.messages = this.state.messages;

        this.state.streaming = this._session.streaming;
        this.state.connected = this._session.wsConnected;
        this.state.statusText = this._session.wsConnected ? "Connected" : "Initializing...";
        this.state.qcPending = this._session.qcPending;
        this.state.qcResult = this._session.qcResult;
        this.state.qcDismissReason = this._session.qcDismissReason;
        this.state.qcPromptText = this._session.qcPromptText;

        if (!this._session.historyLoaded) {
            this._loadHistory();
        }

        if (this._session.wsConnected && this._session.ws) {
            this.state.connected = true;
            this.state.statusText = "Connected";
            this._session.ws._odooWidget = this;
        } else {
            this._tryConnect();
        }

        this._scrollToBottom();
    }

    _detachFromSession() {
        if (this._session.ws) {
            this._session.ws._odooWidget = null;
        }
        this._session.messages = [...this.state.messages];
        this._session.qcPending = this.state.qcPending;
        this._session.qcResult = this.state.qcResult;
        this._session.qcDismissReason = this.state.qcDismissReason;
        this._session.qcPromptText = this.state.qcPromptText;
    }

    get isRunning() { return this.props.dockerStatus === "running"; }
    get gatewayToken() { return this.props.gatewayToken; }
    get gatewayWsUrl() { return this.props.dockerWsUrl; }

    _tryConnect() {
        if (!this.isRunning) {
            this.state.statusText = "Sandbox not running";
            return;
        }
        const wsUrl = this.gatewayWsUrl;
        const token = this.gatewayToken;
        if (!wsUrl || !token) {
            this.state.statusText = "Missing gateway URL or token";
            return;
        }
        this.state.statusText = "Connecting...";
        this._connectGateway(wsUrl, token);
    }

    _connectGateway(url, token) {
        this._disconnectGateway();
        let ws;
        try {
            ws = new WebSocket(url);
            console.log(LOG_PREFIX, `WebSocket created for ${url}`);
        } catch (e) {
            console.error(LOG_PREFIX, "WebSocket constructor failed:", e);
            this.state.statusText = `WS error: ${e.message}`;
            return;
        }
        this._session.ws = ws;
        ws._odooWidget = this;

        const connectMsg = () => {
            const msg = {
                type: "req",
                id: nextId(),
                method: "connect",
                params: {
                    minProtocol: 3,
                    maxProtocol: 3,
                    client: { id: "openclaw-control-ui", version: "control-ui", platform: "web", mode: "webchat" },
                    role: "operator",
                    scopes: ["operator.admin", "operator.read", "operator.write", "operator.approvals", "operator.pairing"],
                    caps: ["tool-events"],
                    auth: { token },
                    userAgent: navigator.userAgent,
                    locale: navigator.language,
                },
            };
            console.log(LOG_PREFIX, "SEND connect:", JSON.stringify(msg).substring(0, 300));
            return JSON.stringify(msg);
        };

        ws.onopen = () => {
            console.log(LOG_PREFIX, "WS onopen — waiting for challenge");
            this.state.statusText = "Waiting for challenge...";
        };

        ws.onmessage = (event) => {
            let frame;
            try { frame = JSON.parse(event.data); } catch {
                console.warn(LOG_PREFIX, "RECV unparseable:", event.data.substring(0, 200));
                return;
            }

            const widget = ws._odooWidget;

            console.log(LOG_PREFIX, "RECV:", {
                type: frame.type,
                event: frame.event,
                id: frame.id,
                ok: frame.ok,
                raw: JSON.stringify(frame).substring(0, 500),
            });

            if (frame.type === "event" && frame.event === "connect.challenge") {
                console.log(LOG_PREFIX, "Challenge received, sending auth");
                if (widget) widget.state.statusText = "Authenticating...";
                ws.send(connectMsg());
                return;
            }

            if (frame.type === "res" && !this._session.wsConnected) {
                if (frame.ok) {
                    console.log(LOG_PREFIX, "CONNECTED OK");
                    this._session.wsConnected = true;
                    if (widget) {
                        widget.state.connected = true;
                        widget.state.statusText = "Connected";
                    }
                } else {
                    const msg = frame.error?.message || JSON.stringify(frame.error || {});
                    console.error(LOG_PREFIX, "CONNECT FAILED:", frame.error);
                    if (widget) widget.state.statusText = `Auth failed: ${msg}`;
                }
                return;
            }

            if (frame.type === "event" && frame.event === "chat") {
                console.log(LOG_PREFIX, "CHAT EVENT:", {
                    state: frame.payload?.state,
                    sessionKey: frame.payload?.sessionKey,
                    runId: frame.payload?.runId,
                    hasMessage: !!frame.payload?.message,
                });
                this._handleChatEvent(frame.payload, widget);
                return;
            }

            if (frame.type === "res" && frame.id && this._session.wsConnected) {
                console.log(LOG_PREFIX, "RPC RESPONSE:", { id: frame.id, ok: frame.ok, error: frame.error });
                // Resolve pending RPC promise if any
                const pending = this._session._pendingRpc.get(frame.id);
                if (pending) {
                    this._session._pendingRpc.delete(frame.id);
                    if (frame.ok) {
                        pending.resolve(frame);
                    } else {
                        pending.reject(frame.error || { message: "RPC failed" });
                    }
                    return;
                }
                if (!frame.ok && this._session.streaming) {
                    const errText = frame.error?.message || JSON.stringify(frame.error || {});
                    const msg = this._session.messages.findLast(m => m.pending);
                    if (msg) {
                        msg.pending = false;
                        msg.text = errText;
                        msg.isError = true;
                    }
                    this._session.streaming = false;
                    if (widget) widget.state.streaming = false;
                }
            }
        };

        ws.onclose = (ev) => {
            const was = this._session.wsConnected;
            console.log(LOG_PREFIX, `WS onclose: code=${ev.code} reason=${ev.reason || "n/a"} wasConnected=${was}`);
            this._session.wsConnected = false;
            this._session.ws = null;
            const widget = ws._odooWidget;
            if (widget) {
                widget.state.connected = false;
                if (widget.state.streaming) { widget.state.streaming = false; widget.state.sending = false; }
                widget.state.statusText = was ? "Disconnected" : `Closed (code=${ev.code} reason=${ev.reason || "n/a"})`;
            }
        };

        ws.onerror = (ev) => {
            console.error(LOG_PREFIX, "WS onerror:", ev);
        };
    }

    _disconnectGateway() {
        if (this._session.ws) {
            console.log(LOG_PREFIX, "Disconnecting WS");
            try { this._session.ws.close(); } catch {}
            this._session.ws = null;
        }
        this._session.wsConnected = false;
        this.state.connected = false;
    }

    _handleChatEvent(payload, widget) {
        if (!payload) return;
        const state = payload.state;
        const messages = this._session.messages;
        const session = this._session;
        const stream = payload.stream;
        const data = payload.message || payload.data || payload;

        // Capture raw event for trajectory export
        session._rawEvents.push({
            ts: new Date().toISOString(),
            stream,
            data: JSON.parse(JSON.stringify(data)),
        });

        console.log(LOG_PREFIX, "CHAT EVENT detail:", {
            state,
            stream,
            messageType: typeof payload.message,
            messageKeys: payload.message ? Object.keys(payload.message) : null,
            rawMessage: JSON.stringify(payload.message)?.substring(0, 500),
            sessionKey: payload.sessionKey,
            runId: payload.runId,
        });

        if (stream === "assistant" && data.text) {
            if (widget) widget.state.activityText = "";
            let msg = messages.findLast(m => m.pending);
            if (!msg) {
                msg = { role: "assistant", text: "", html: markup(""), pending: true };
                messages.push(msg);
                session._lastFlushedWordCount = 0;
            }

            session._streamBuf = data.text || "";
            const wordCount = session._streamBuf.split(/\s+/).length;
            if (wordCount - session._lastFlushedWordCount >= STREAM_WORD_THRESHOLD) {
                msg.text = session._streamBuf;
                msg.html = markup(renderMarkdown(session._streamBuf));
                session._lastFlushedWordCount = wordCount;
                if (widget) widget._scrollToBottom();
            }
        } else if (stream === "tool") {
            const phase = data.phase || "";
            const toolCallId = data.toolCallId || "";
            const toolName = data.name || "";
            if (phase === "start" && toolCallId) {
                session._toolCalls.push({
                    toolCallId,
                    name: toolName,
                    args: data.args || null,
                    result: null,
                    isError: false,
                });
                if (widget) widget.state.activityText = `Running ${toolName}…`;
                console.log(LOG_PREFIX, "Tool START:", toolName, toolCallId);
            } else if (phase === "end" && toolCallId) {
                const tc = session._toolCalls.find(t => t.toolCallId === toolCallId);
                if (tc) {
                    tc.result = data.result ?? data.partialResult ?? null;
                    tc.isError = !!data.isError;
                }
                console.log(LOG_PREFIX, "Tool END:", toolName, toolCallId);
            } else if (phase === "update" && toolCallId) {
                const tc = session._toolCalls.find(t => t.toolCallId === toolCallId);
                if (tc && data.partialResult !== undefined) {
                    tc.result = data.partialResult;
                }
            }
        } else if (stream === "lifecycle" && data.phase === "start") {
            if (widget) widget.state.activityText = "Thinking…";
        } else if (stream === "lifecycle" && data.phase === "end") {
            console.log(LOG_PREFIX, "Agent lifecycle END");
            const msg = messages.findLast(m => m.pending);
            if (msg) {
                if (session._streamBuf) {
                    msg.text = session._streamBuf;
                    msg.html = markup(renderMarkdown(session._streamBuf));
                }
                msg.pending = false;
            }
            const toolCalls = session._toolCalls.length > 0 ? [...session._toolCalls] : null;
            const rawEvents = session._rawEvents.length > 0 ? [...session._rawEvents] : null;
            session._streamBuf = "";
            session._lastFlushedWordCount = 0;
            session._toolCalls = [];
            session._rawEvents = [];
            session.streaming = false;
            if (widget) {
                widget.state.streaming = false;
                widget.state.activityText = "";
            }
            const savedTurnId = session.currentTurnId;
            this._saveResponse(msg ? msg.text : "", toolCalls, rawEvents);
            this._fetchTrajectory(savedTurnId);
        } else if (stream === "lifecycle" && data.phase === "error") {
            const errText = data.message || data.error || data.reason || JSON.stringify(data);
            console.error(LOG_PREFIX, "Agent lifecycle ERROR:", errText, "full data:", data);
            let msg = messages.findLast(m => m.pending);
            if (!msg) {
                msg = { role: "assistant", text: "", html: markup(""), pending: false };
                messages.push(msg);
            }
            msg.pending = false;
            msg.text = errText;
            msg.html = markup(renderMarkdown(errText));
            msg.isError = true;
            session._streamBuf = "";
            session._lastFlushedWordCount = 0;
            session._toolCalls = [];
            session._rawEvents = [];
            session.streaming = false;
            if (widget) {
                widget.state.streaming = false;
                widget.state.activityText = "";
            }
        } else if (state === "delta") {
            const text = this._extractText(payload.message);
            console.log(LOG_PREFIX, "DELTA extracted text:", JSON.stringify(text)?.substring(0, 200));
            if (text) {
                const msg = messages.findLast(m => m.pending);
                if (msg) msg.text += text;
                if (widget) widget._scrollToBottom();
            }
        } else if (state === "final") {
            const finalText = this._extractText(payload.message);
            console.log(LOG_PREFIX, "FINAL text length:", finalText?.length);
            const msg = messages.findLast(m => m.pending);
            if (msg) {
                if (finalText) msg.text = finalText;
                msg.pending = false;
            }
            this._session.streaming = false;
            if (widget) {
                widget.state.streaming = false;
                widget._scrollToBottom();
            }
            this._saveResponse(msg ? msg.text : "");
        } else if (state === "error") {
            const errText = payload.errorMessage || "Chat error";
            console.error(LOG_PREFIX, "Chat ERROR:", errText);
            const msg = messages.findLast(m => m.pending);
            if (msg) {
                msg.pending = false;
                msg.text = errText;
                msg.isError = true;
            }
            this._session.streaming = false;
            if (widget) widget.state.streaming = false;
        } else if (state === "aborted") {
            const msg = messages.findLast(m => m.pending);
            if (msg) {
                msg.pending = false;
                if (!msg.text) msg.text = "[Aborted]";
            }
            this._session.streaming = false;
            if (widget) widget.state.streaming = false;
        }
    }

    _extractText(message) {
        if (!message) return "";
        if (typeof message === "string") return message;
        if (typeof message.text === "string") return message.text;
        if (Array.isArray(message.content)) {
            return message.content
                .filter(b => b && typeof b === "object" && b.text)
                .map(b => b.text)
                .join("");
        }
        if (typeof message.content === "string") return message.content;
        if (message.role && message.content) {
            return this._extractText(message.content);
        }
        return JSON.stringify(message);
    }

    async _loadHistory() {
        if (!this.props.sandboxId) return;
        if (this._session.historyLoaded) return;
        console.log(LOG_PREFIX, "Loading history for sandbox", this.props.sandboxId);
        try {
            const result = await rpc("/talos/chat/history", { sandbox_id: this.props.sandboxId });
            console.log(LOG_PREFIX, "History loaded:", result.turns?.length, "turns");
            if (result.turns) {
                this._session.messages.length = 0;
                for (const t of result.turns) {
                    if (t.prompt) this._session.messages.push({ role: "user", text: t.prompt });
                    if (t.response) this._session.messages.push({ role: "assistant", text: t.response, pending: false });
                }
            }
            this._session.historyLoaded = true;
        } catch (e) {
            console.error(LOG_PREFIX, "History load failed:", e);
        }
        this._scrollToBottom();
    }

    _wsRpc(method, params) {
        const ws = this._session.ws;
        if (!ws || !this._session.wsConnected) {
            return Promise.reject(new Error("WS not connected"));
        }
        const id = nextId();
        const msg = { type: "req", id, method, params };
        return new Promise((resolve, reject) => {
            this._session._pendingRpc.set(id, { resolve, reject });
            ws.send(JSON.stringify(msg));
            setTimeout(() => {
                if (this._session._pendingRpc.has(id)) {
                    this._session._pendingRpc.delete(id);
                    reject(new Error("WS RPC timeout"));
                }
            }, 15000);
        });
    }

    async _fetchTrajectory(turnId) {
        if (!turnId || !this._session.wsConnected) return;
        const sessionKey = "odoo:sandbox:" + this.props.sandboxId;
        console.log(LOG_PREFIX, "Fetching trajectory via chat.history for", sessionKey);
        try {
            const res = await this._wsRpc("chat.history", { sessionKey, limit: 1000 });
            const messages = res?.result?.messages || res?.messages || [];
            if (messages.length === 0) {
                console.warn(LOG_PREFIX, "chat.history returned 0 messages");
                return;
            }
            console.log(LOG_PREFIX, "Trajectory fetched:", messages.length, "messages");
            await rpc("/talos/chat/save_trajectory", {
                turn_id: turnId,
                trajectory_messages: JSON.stringify(messages),
            });
        } catch (e) {
            console.error(LOG_PREFIX, "Trajectory fetch failed:", e);
        }
    }

    async _saveResponse(text, toolCalls = null, rawEvents = null) {
        if (!this._session.currentTurnId) {
            console.warn(LOG_PREFIX, "_saveResponse: no currentTurnId");
            return;
        }
        console.log(LOG_PREFIX, "Saving response for turn", this._session.currentTurnId,
            toolCalls ? `with ${toolCalls.length} tool call(s)` : "",
            rawEvents ? `with ${rawEvents.length} raw event(s)` : "");
        try {
            const params = {
                turn_id: this._session.currentTurnId,
                response: text,
            };
            if (toolCalls && toolCalls.length > 0) {
                params.tool_calls = JSON.stringify(toolCalls);
            }
            if (rawEvents && rawEvents.length > 0) {
                params.raw_events = JSON.stringify(rawEvents);
            }
            await rpc("/talos/chat/save_response", params);
        } catch (e) {
            console.error(LOG_PREFIX, "Save response failed:", e);
        }
        this._session.currentTurnId = null;
    }

    async onSend() {
        const text = this.state.inputText.trim();
        if (!text || this.state.sending || this._session.streaming) return;
        if (!this._session.wsConnected) {
            this._session.messages.push({
                role: "assistant",
                text: `Not connected. ${this.state.statusText}`,
                isError: true,
                pending: false,
            });
            this._scrollToBottom();
            return;
        }

        console.log(LOG_PREFIX, "onSend:", { text: text.substring(0, 100) });

        this.state.inputText = "";
        this.state.sending = true;
        this._session.messages.push({ role: "user", text });
        this._scrollToBottom();

        this.state.activityText = "Running QC check…";
        let turnId = null;
        try {
            const r = await rpc("/talos/chat/create_turn", { sandbox_id: this.props.sandboxId, message: text });
            turnId = r.turn_id;
            console.log(LOG_PREFIX, "Turn created:", turnId);
        } catch (e) {
            console.error(LOG_PREFIX, "Create turn failed:", e);
        }
        this._session.currentTurnId = turnId;

        let qcResult = null;
        try {
            const qcResponse = await rpc("/talos/qc", { prompt: text });
            console.log(LOG_PREFIX, "QC response:", qcResponse);
            if (qcResponse.error) {
                console.warn(LOG_PREFIX, "QC error, passing through:", qcResponse.error);
            } else if (qcResponse.qc_result) {
                qcResult = qcResponse.qc_result;
                if (turnId) {
                    rpc("/talos/chat/save_qc", {
                        turn_id: turnId,
                        severity: qcResult.severity || "",
                        qc_response: JSON.stringify(qcResult),
                    }).catch(e => console.warn(LOG_PREFIX, "save_qc failed:", e));
                }
            }
        } catch (e) {
            console.warn(LOG_PREFIX, "QC call failed, passing through:", e);
        }

        this.state.activityText = "";
        this.state.sending = false;

        if (qcResult && qcResult.severity) {
            this._session.messages.push({
                role: "assistant",
                text: qcResult.summary || "QC check completed.",
                isQc: true,
                qcSeverity: qcResult.severity,
                qcChecks: qcResult.checks || [],
                pending: false,
            });
            this._scrollToBottom();

            if (qcResult.severity === "critical") {
                this.state.qcResult = qcResult;
                this.state.qcPending = true;
                this.state.qcPromptText = text;
                return;
            }
            if (qcResult.severity === "high") {
                this.state.qcResult = qcResult;
                this.state.qcPending = true;
                this.state.qcPromptText = text;
                this.state.qcDismissReason = "";
                return;
            }
            if (qcResult.severity === "medium" || qcResult.severity === "low") {
                this.state.qcResult = qcResult;
                this.state.qcPending = true;
                this.state.qcPromptText = text;
                return;
            }
        }

        this._sendToOpenClaw(text);
    }

    onQcDismiss() {
        const severity = this.state.qcResult?.severity;
        if (severity === "critical") return;
        if (severity === "high" && !this.state.qcDismissReason.trim()) return;

        const turnId = this._session.currentTurnId;
        const reason = this.state.qcDismissReason.trim();

        if (turnId && reason) {
            rpc("/talos/chat/save_qc", {
                turn_id: turnId,
                severity: severity,
                dismiss_reason: reason,
            }).catch(e => console.warn(LOG_PREFIX, "save_qc dismiss failed:", e));
        }

        const promptText = this.state.qcPromptText;
        this._clearQcState();
        this._sendToOpenClaw(promptText);
    }

    onQcRewrite() {
        const promptText = this.state.qcPromptText;
        this._clearQcState();
        this.state.inputText = promptText;
    }

    _clearQcState() {
        this.state.qcResult = null;
        this.state.qcPending = false;
        this.state.qcDismissReason = "";
        this.state.qcPromptText = "";
        this._session.qcResult = null;
        this._session.qcPending = false;
        this._session.qcDismissReason = "";
        this._session.qcPromptText = "";
    }

    _sendToOpenClaw(text) {
        const chatSendMsg = {
            type: "req",
            id: nextId(),
            method: "chat.send",
            params: {
                message: text,
                sessionKey: "odoo:sandbox:" + this.props.sandboxId,
                deliver: false,
                idempotencyKey: crypto.randomUUID(),
            },
        };
        console.log(LOG_PREFIX, "SEND chat.send:", JSON.stringify(chatSendMsg));
        this._session.ws.send(JSON.stringify(chatSendMsg));

        this._session.messages.push({ role: "assistant", text: "", pending: true });
        this.state.activityText = "Waiting for model…";
        this._session.streaming = true;
        this.state.streaming = true;
        this._scrollToBottom();
    }

    onAbort() {
        if (!this._session.ws || !this._session.wsConnected) return;
        const abortMsg = {
            type: "req",
            id: nextId(),
            method: "chat.abort",
            params: { sessionKey: "odoo:sandbox:" + this.props.sandboxId },
        };
        console.log(LOG_PREFIX, "SEND chat.abort:", JSON.stringify(abortMsg));
        this._session.ws.send(JSON.stringify(abortMsg));

        const messages = this._session.messages;
        const session = this._session;
        let msg = messages.findLast(m => m.pending);
        const finalText = session._streamBuf || (msg ? msg.text : "");
        const abortNote = "[Response stopped by user]";
        const fullText = finalText ? finalText + "\n\n" + abortNote : abortNote;
        if (msg) {
            msg.pending = false;
            msg.text = fullText;
            msg.html = markup(renderMarkdown(fullText));
        } else {
            msg = { role: "assistant", text: fullText, html: markup(renderMarkdown(fullText)), pending: false };
            messages.push(msg);
        }

        const toolCalls = session._toolCalls.length > 0 ? [...session._toolCalls] : null;
        const rawEvents = session._rawEvents.length > 0 ? [...session._rawEvents] : null;
        session._streamBuf = "";
        session._lastFlushedWordCount = 0;
        session._toolCalls = [];
        session._rawEvents = [];
        session.streaming = false;
        this.state.streaming = false;
        this.state.activityText = "";
        this._saveResponse(msg.text, toolCalls, rawEvents);
        this._scrollToBottom();
    }

    onKeydown(ev) {
        if (ev.key === "Enter" && !ev.shiftKey) { ev.preventDefault(); this.onSend(); }
    }

    _scrollToBottom() {
        Promise.resolve().then(() => {
            const el = this.messagesEndRef.el;
            if (el) el.scrollIntoView({ behavior: "smooth" });
        });
    }
}
