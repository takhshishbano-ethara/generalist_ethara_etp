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
const INCREMENTAL_SAVE_INTERVAL_MS = 3000;

const LOGIN_URL_PATTERNS = [
    /\/login/i, /\/signin/i, /\/sign-in/i, /\/oauth/i,
    /\/auth\//i, /\/accounts\//i, /\/sso\//i,
    /accounts\.google\.com/i, /login\.microsoftonline/i,
    /github\.com\/login/i, /login\.yahoo\.com/i,
];

function _looksLikeLoginUrl(url) {
    if (!url) return false;
    return LOGIN_URL_PATTERNS.some(p => p.test(url));
}

function _extractUrlsFromText(text) {
    if (!text) return [];
    const re = /https?:\/\/[^\s"'<>]+/gi;
    return text.match(re) || [];
}

let _msgId = 0;
function nextId() {
    return `odoo-${++_msgId}-${Date.now().toString(36)}`;
}

const _sessions = new Map();

export async function clearChatSession(sandboxId) {
    if (_sessions.has(sandboxId)) {
        const session = _sessions.get(sandboxId);
        // Fetch and persist trajectory before tearing down the WS.
        if (session.ws && session.wsConnected) {
            const widget = session.ws._odooWidget;
            const turnId = session.currentTurnId
                || await _resolveLatestTurnId(sandboxId);
            if (turnId && widget) {
                try {
                    await widget._fetchTrajectory(turnId);
                } catch (e) {
                    console.warn("[TalosChatWidget]", "Trajectory save on stop failed:", e);
                }
            }
            if (widget) {
                widget.state.messages.length = 0;
                widget.state.connected = false;
                widget.state.streaming = false;
                widget.state.sending = false;
                widget.state.activityText = "";
                widget.state.statusText = "Session stopped";
                widget._clearQcState();
            }
        }
        if (session.ws) {
            try { session.ws.close(); } catch {}
        }
        // Covers the ws=null case: session.messages is the same reference as
        // widget.state.messages (shared via _syncFromSession).
        session.messages.length = 0;
        _sessions.delete(sandboxId);
    }
}

async function _resolveLatestTurnId(sandboxId) {
    try {
        const history = await rpc("/talos/chat/history", {
            sandbox_id: sandboxId,
        });
        const turns = history?.turns || [];
        if (turns.length > 0) {
            return turns[turns.length - 1].id;
        }
    } catch (e) {
        console.warn("[TalosChatWidget]", "Could not resolve latest turn:", e);
    }
    return null;
}

function _getSession(sandboxId) {
    if (!_sessions.has(sandboxId)) {
        _sessions.set(sandboxId, {
            ws: null,
            wsConnected: false,
            messages: [],
            streaming: false,
            currentTurnId: null,
            currentRunId: null,
            historyLoaded: false,
            _streamBuf: "",
            _lastFlushedWordCount: 0,
            _toolCalls: [],
            _toolCallMap: new Map(),
            _rawEvents: [],
            _pendingRpc: new Map(),
            _incrementalSaveTimer: null,
            _lastSavedText: "",
            _heartbeatTimer: null,
            qcPending: false,
            qcResult: null,
            qcDismissReason: "",
            qcPromptText: "",
        });
    }
    return _sessions.get(sandboxId);
}

function _logToolCall(session, entry) {
    const { toolCallId, name, args, result, isError, phase, source_event } = entry;
    if (!toolCallId) return;
    const model_name = session.modelType || "";

    if (phase === "start") {
        const record = {
            toolCallId,
            name: name || "",
            args: args || null,
            result: null,
            isError: false,
            model_name,
            startedAt: new Date().toISOString(),
            endedAt: null,
            duration_ms: null,
            source_event: source_event || "",
        };
        session._toolCallMap.set(toolCallId, record);
        // Backward compat: push to array
        session._toolCalls.push({
            toolCallId,
            name: name || "",
            args: args || null,
            result: null,
            isError: false,
            model_name,
            startedAt: record.startedAt,
            endedAt: null,
            duration_ms: null,
            source_event: source_event || "",
        });
        console.log(LOG_PREFIX, `🔧 [LOG] start ${name} (${toolCallId}) model=${model_name} source=${source_event}`);
    } else if (phase === "end") {
        const existing = session._toolCallMap.get(toolCallId);
        if (existing) {
            const endedAt = new Date().toISOString();
            existing.result = result !== undefined ? result : null;
            existing.isError = !!isError;
            existing.endedAt = endedAt;
            existing.duration_ms = new Date(endedAt) - new Date(existing.startedAt);
            // Sync to array
            const arrEntry = session._toolCalls.find(t => t.toolCallId === toolCallId);
            if (arrEntry) {
                arrEntry.result = existing.result;
                arrEntry.isError = existing.isError;
                arrEntry.endedAt = existing.endedAt;
                arrEntry.duration_ms = existing.duration_ms;
            }
        }
        console.log(LOG_PREFIX, `🔧 [LOG] end ${name} (${toolCallId}) model=${model_name} source=${source_event}`);
    } else if (phase === "update") {
        const existing = session._toolCallMap.get(toolCallId);
        if (existing && result !== undefined) {
            existing.result = result;
            // Sync to array
            const arrEntry = session._toolCalls.find(t => t.toolCallId === toolCallId);
            if (arrEntry) {
                arrEntry.result = result;
            }
        }
        console.log(LOG_PREFIX, `🔧 [LOG] update ${name} (${toolCallId}) model=${model_name} source=${source_event}`);
    }
}

export class TalosChatWidget extends Component {
    static template = "talos.ChatWidget";
    static props = {
        sandboxId: Number,
        dockerStatus: String,
        dockerWsUrl: { type: [String, Boolean], optional: true },
        gatewayToken: { type: [String, Boolean], optional: true },
        modelType: { type: String, optional: true },
    };

    setup() {
        this.messagesEndRef = useRef("messagesEnd");

        this._currentSandboxId = this.props.sandboxId;
        this._session = _getSession(this.props.sandboxId);
        this._session.modelType = this.props.modelType || "";

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
            // Browser auth state
            browserAuthActive: false,
            browserAuthUrl: "",
            browserAuthDomain: "",
            browserAuthScreenshot: "",
            browserAuthCookieInput: "",
            browserAuthStatus: "",   // "waiting" | "injecting" | "done" | "error"
            browserAuthError: "",
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
            } else if (this.isRunning && !this._session.wsConnected) {
                this._tryConnect();
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

    get isRunning() {
        return this.props.dockerStatus === "running";
    }
    get gatewayToken() { return this.props.gatewayToken; }
    get gatewayWsUrl() { return this.props.dockerWsUrl; }

    _tryConnect() {
        if (!this.isRunning) {
            this.state.statusText = "Sandbox not running";
            return;
        }
        if (this._session.ws) {
            const rs = this._session.ws.readyState;
            if (rs === WebSocket.CONNECTING || rs === WebSocket.OPEN) return;
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
            console.group(`${LOG_PREFIX} ➡️ SEND connect`);
            console.log("Full connect message:", JSON.stringify(msg));
            console.log("caps:", msg.params.caps);
            console.log("role:", msg.params.role);
            console.log("scopes:", msg.params.scopes);
            console.groupEnd();
            return JSON.stringify(msg);
        };

        ws.onopen = () => {
            console.log(LOG_PREFIX, "WS onopen — waiting for challenge");
            this.state.statusText = "Waiting for challenge...";
        };

        ws.onmessage = (event) => {
            let frame;
            try { frame = JSON.parse(event.data); } catch {
                console.warn(LOG_PREFIX, "⬅️ RECV RAW (unparseable):", event.data.substring(0, 500));
                return;
            }

            const widget = ws._odooWidget;
            const rawStr = JSON.stringify(frame);

            console.group(`${LOG_PREFIX} ⬅️ RECV [${frame.type}${frame.event ? '/' + frame.event : ''}${frame.id ? ' id=' + frame.id : ''}]`);
            console.log("Full frame:", rawStr.length > 2000 ? rawStr.substring(0, 2000) + "..." : rawStr);
            if (frame.payload) {
                console.log("Payload keys:", Object.keys(frame.payload));
                console.log("Payload.state:", frame.payload.state);
                console.log("Payload.stream:", frame.payload.stream);
                if (frame.payload.message) {
                    const msgStr = JSON.stringify(frame.payload.message);
                    console.log("Payload.message:", msgStr.length > 1000 ? msgStr.substring(0, 1000) + "..." : msgStr);
                }
            }
            console.groupEnd();

            if (frame.type === "event" && frame.event === "connect.challenge") {
                console.log(LOG_PREFIX, "🔐 Challenge received, sending auth");
                if (widget) widget.state.statusText = "Authenticating...";
                ws.send(connectMsg());
                return;
            }

            if (frame.type === "res" && !this._session.wsConnected) {
                if (frame.ok) {
                    console.log(LOG_PREFIX, "✅ CONNECTED OK — caps:", JSON.stringify(frame.result?.caps || frame.caps || "n/a"));
                    this._session.wsConnected = true;
                    this._startHeartbeat();
                    if (widget) {
                        widget.state.connected = true;
                        widget.state.statusText = "Connected";
                    }
                    // After reconnect, fetch the latest session state from OpenClaw
                    this._restoreSessionFromGateway();
                } else {
                    const msg = frame.error?.message || JSON.stringify(frame.error || {});
                    console.error(LOG_PREFIX, "❌ CONNECT FAILED:", frame.error);
                    if (widget) widget.state.statusText = `Auth failed: ${msg}`;
                }
                return;
            }

            if (frame.type === "event" && frame.event === "chat") {
                const p = frame.payload || {};
                console.group(`${LOG_PREFIX} 💬 CHAT EVENT [state=${p.state || "none"} stream=${p.stream || "none"}]`);
                console.log("sessionKey:", p.sessionKey);
                console.log("runId:", p.runId);
                if (p.message) {
                    const msgStr = JSON.stringify(p.message);
                    console.log("message:", msgStr.length > 1500 ? msgStr.substring(0, 1500) + "..." : msgStr);
                    if (p.message.content && Array.isArray(p.message.content)) {
                        console.log("message.content types:", p.message.content.map(b => b?.type || typeof b));
                    }
                }
                if (p.data) {
                    const dataStr = JSON.stringify(p.data);
                    console.log("data:", dataStr.length > 1500 ? dataStr.substring(0, 1500) + "..." : dataStr);
                }
                console.groupEnd();
                this._handleChatEvent(p, widget);
                return;
            }

            if (frame.type === "event" && (frame.event === "session.tool" || frame.event === "tool")) {
                console.group(`${LOG_PREFIX} 🔧 TOOL EVENT [${frame.event}]`);
                console.log("Full payload:", JSON.stringify(frame.payload).substring(0, 2000));
                console.groupEnd();
                const toolPayload = frame.payload || {};
                this._handleChatEvent({
                    stream: "tool",
                    message: toolPayload,
                    data: toolPayload,
                    ...toolPayload,
                }, widget);
                return;
            }

            if (frame.type === "event" && frame.event === "agent") {
                const p = frame.payload || {};
                const agentStream = p.stream;
                const agentData = p.data || {};

                if (agentStream === "tool") {
                    // Agent sends phase "result" where our handler expects "end"
                    const phase = agentData.phase === "result" ? "end" : agentData.phase;
                    console.log(LOG_PREFIX, `🤖 AGENT TOOL [${agentData.phase}→${phase}] name=${agentData.name} id=${agentData.toolCallId}`);
                    this._handleChatEvent({
                        stream: "tool",
                        message: {
                            phase: phase,
                            toolCallId: agentData.toolCallId || "",
                            name: agentData.name || "",
                            args: agentData.args || null,
                            result: agentData.result ?? agentData.meta ?? null,
                            isError: !!agentData.isError,
                        },
                        data: {
                            phase: phase,
                            toolCallId: agentData.toolCallId || "",
                            name: agentData.name || "",
                            args: agentData.args || null,
                            result: agentData.result ?? agentData.meta ?? null,
                            isError: !!agentData.isError,
                        },
                    }, widget);
                    return;
                }

                if (agentStream === "item") {
                    const phase = agentData.phase;
                    const kind = agentData.kind;
                    if (kind === "tool" && agentData.toolCallId) {
                        console.log(LOG_PREFIX, `🤖 AGENT ITEM [${phase}] name=${agentData.name} status=${agentData.status} id=${agentData.toolCallId}`);
                        if (phase === "start") {
                            const session = this._session;
                            if (!session._toolCallMap.has(agentData.toolCallId)) {
                                this._handleChatEvent({
                                    stream: "tool",
                                    message: { phase: "start", toolCallId: agentData.toolCallId, name: agentData.name || "", args: null, result: null, isError: false },
                                    data: { phase: "start", toolCallId: agentData.toolCallId, name: agentData.name || "", args: null, result: null, isError: false },
                                }, widget);
                            }
                        } else if (phase === "end") {
                            const session = this._session;
                            _logToolCall(session, {
                                toolCallId: agentData.toolCallId,
                                name: agentData.name || "",
                                result: agentData.meta || agentData.title || "(completed)",
                                isError: agentData.status === "error",
                                phase: "end",
                                source_event: "agent.item",
                            });
                        }
                    }
                    return;
                }

                if (agentStream === "assistant") {
                    console.log(LOG_PREFIX, `🤖 AGENT ASSISTANT:`, JSON.stringify(agentData).substring(0, 500));
                    this._handleChatEvent({
                        stream: "assistant",
                        message: agentData,
                        data: agentData,
                    }, widget);
                    return;
                }

                if (agentStream === "lifecycle") {
                    console.log(LOG_PREFIX, `🤖 AGENT LIFECYCLE: phase=${agentData.phase}`);
                    this._handleChatEvent({
                        stream: "lifecycle",
                        message: agentData,
                        data: agentData,
                    }, widget);
                    return;
                }

                console.log(LOG_PREFIX, `🤖 AGENT [${agentStream}]:`, JSON.stringify(agentData).substring(0, 500));
                return;
            }

            if (frame.type === "event" && (frame.event === "tick" || frame.event === "health" || frame.event === "presence")) {
                return;
            }

            if (frame.type === "event") {
                console.log(LOG_PREFIX, "📨 UNHANDLED EVENT:", frame.event, "payload:", JSON.stringify(frame.payload).substring(0, 1000));
            }

            if (frame.type === "res" && frame.id && this._session.wsConnected) {
                console.log(LOG_PREFIX, "📥 RPC RESPONSE:", { id: frame.id, ok: frame.ok, error: frame.error, resultKeys: frame.result ? Object.keys(frame.result) : null });
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
                    let msg = this._session.messages.findLast(m => m.pending);
                    if (!msg) {
                        msg = { role: "assistant", text: "", html: markup(""), pending: false };
                        this._session.messages.push(msg);
                    }
                    msg.pending = false;
                    msg.text = errText;
                    msg.html = markup(renderMarkdown(errText));
                    msg.isError = true;
                    const toolCalls = this._session._toolCallMap.size > 0 ? Array.from(this._session._toolCallMap.values()) : null;
                    if (toolCalls && toolCalls.length > 0) {
                        msg.toolCalls = toolCalls;
                        msg.toolsExpanded = true;
                    }
                    this._session.streaming = false;
                    this._stopIncrementalSave();
                    this._session._streamBuf = "";
                    this._session._lastFlushedWordCount = 0;
                    this._session._toolCalls = [];
                    this._session._toolCallMap = new Map();
                    this._session._rawEvents = [];
                    if (widget) {
                        widget.state.streaming = false;
                        widget.state.sending = false;
                        widget.state.activityText = "";
                        widget._scrollToBottom();
                    }
                }
            }
        };

        ws.onclose = (ev) => {
            const was = this._session.wsConnected;
            console.log(LOG_PREFIX, `WS onclose: code=${ev.code} reason=${ev.reason || "n/a"} wasConnected=${was}`);
            if (this._session.streaming && this._session.currentTurnId) {
                this._stopIncrementalSave();
                const currentText = this._session._streamBuf || "";
                const toolCalls = this._session._toolCallMap.size > 0 ? Array.from(this._session._toolCallMap.values()) : null;
                if (currentText) {
                    console.log(LOG_PREFIX, "💾 WS closed during stream — saving partial response");
                    const params = { turn_id: this._session.currentTurnId, response: currentText, partial: true };
                    if (toolCalls && toolCalls.length > 0) params.tool_calls = JSON.stringify(toolCalls);
                    rpc("/talos/chat/save_response", params).catch(e => console.warn(LOG_PREFIX, "Partial save on disconnect failed:", e));
                }
                let msg = this._session.messages.findLast(m => m.pending);
                if (msg) {
                    msg.pending = false;
                    if (currentText) {
                        msg.text = currentText;
                        msg.html = markup(renderMarkdown(currentText));
                    }
                    if (toolCalls && toolCalls.length > 0) msg.toolCalls = toolCalls;
                }
                this._session.streaming = false;
                this._session._streamBuf = "";
                this._session._lastFlushedWordCount = 0;
                this._session._toolCalls = [];
                this._session._toolCallMap = new Map();
                this._session._rawEvents = [];
            }
            this._session.wsConnected = false;
            this._session.ws = null;
            this._stopHeartbeat();
            const widget = ws._odooWidget;
            if (widget) {
                widget.state.connected = false;
                widget.state.activityText = "";
                if (widget.state.streaming) { widget.state.streaming = false; widget.state.sending = false; }
                widget.state.statusText = was ? "Disconnected — reconnecting…" : `Closed (code=${ev.code} reason=${ev.reason || "n/a"})`;
                if (was && widget.isRunning) {
                    setTimeout(() => widget._tryConnect(), 2000);
                }
            }
        };

        ws.onerror = (ev) => {
            console.error(LOG_PREFIX, "WS onerror:", ev);
        };
    }

    _disconnectGateway() {
        this._stopHeartbeat();
        if (this._session.ws) {
            console.log(LOG_PREFIX, "Disconnecting WS");
            try { this._session.ws.close(); } catch {}
            this._session.ws = null;
        }
        this._session.wsConnected = false;
        this.state.connected = false;
    }

    _startHeartbeat() {
        this._stopHeartbeat();
        this._session._heartbeatTimer = setInterval(() => {
            const ws = this._session.ws;
            if (ws && ws.readyState === WebSocket.OPEN) {
                ws.send(JSON.stringify({ type: "req", id: nextId(), method: "sessions.list", params: {} }));
            }
        }, 30000);
    }

    _stopHeartbeat() {
        if (this._session._heartbeatTimer) {
            clearInterval(this._session._heartbeatTimer);
            this._session._heartbeatTimer = null;
        }
    }

    _handleChatEvent(payload, widget) {
        if (!payload) return;
        const state = payload.state;
        const messages = this._session.messages;
        const session = this._session;
        const stream = payload.stream;
        const data = payload.message || payload.data || payload;

        session._rawEvents.push({
            ts: new Date().toISOString(),
            stream,
            state,
            runId: payload.runId || session.currentRunId || "",
            seq: typeof payload.seq === "number" ? payload.seq : undefined,
            data: JSON.parse(JSON.stringify(data)),
        });

        console.group(`${LOG_PREFIX} 🔄 _handleChatEvent [stream=${stream || "none"} state=${state || "none"}]`);
        console.log("payload keys:", Object.keys(payload));
        console.log("data keys:", data ? Object.keys(data) : null);
        console.log("data.phase:", data?.phase);
        console.log("data.text:", data?.text ? data.text.substring(0, 200) : null);
        console.log("data.toolCallId:", data?.toolCallId);
        console.log("data.name:", data?.name);
        console.log("session._toolCalls count:", session._toolCalls.length);
        console.log("full data:", JSON.stringify(data).substring(0, 1000));
        console.groupEnd();

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
            console.log(LOG_PREFIX, `🔧 TOOL STREAM: phase=${phase} name=${toolName} id=${toolCallId} args=${JSON.stringify(data.args).substring(0, 300)}`);
            if (phase === "start" && toolCallId) {
                _logToolCall(session, { toolCallId, name: toolName, args: data.args, phase: "start", source_event: "chat.tool" });
                if (widget) widget.state.activityText = `Running ${toolName}…`;
                console.log(LOG_PREFIX, `🔧 Tool START: ${toolName} (${toolCallId}) — total tool calls now: ${session._toolCalls.length}`);
            } else if (phase === "end" && toolCallId) {
                _logToolCall(session, { toolCallId, name: toolName, result: data.result ?? data.error ?? data.partialResult, isError: !!(data.isError || data.error), phase: "end", source_event: "chat.tool" });
                console.log(LOG_PREFIX, `🔧 Tool END: ${toolName} (${toolCallId}) isError=${!!data.isError} result=${JSON.stringify(data.result).substring(0, 300)}`);
                if (toolName === "browser" && widget) {
                    this._checkBrowserToolForLogin(data, widget);
                }
            } else if (phase === "update" && toolCallId) {
                _logToolCall(session, { toolCallId, name: toolName, result: data.partialResult, phase: "update", source_event: "chat.tool" });
                console.log(LOG_PREFIX, `🔧 Tool UPDATE: ${toolName} (${toolCallId})`);
            } else {
                console.warn(LOG_PREFIX, `🔧 Tool UNKNOWN phase: ${phase} toolCallId=${toolCallId} name=${toolName}`);
            }
            this._syncLiveToolCalls(session, messages, widget);
        } else if (stream === "lifecycle" && data.phase === "start") {
            console.log(LOG_PREFIX, "🏁 Lifecycle START — Thinking…");
            if (widget) widget.state.activityText = "Thinking…";
        } else if (stream === "lifecycle" && data.phase === "end") {
            console.group(`${LOG_PREFIX} 🏁 Lifecycle END`);
            console.log("Tool calls collected:", session._toolCalls.length, session._toolCalls.map(t => t.name));
            console.log("Raw events collected:", session._rawEvents.length);
            console.log("Stream buffer length:", session._streamBuf?.length);
            console.log("Current turn ID:", session.currentTurnId);
            console.groupEnd();
            const msg = messages.findLast(m => m.pending);
            if (msg) {
                if (session._streamBuf) {
                    msg.text = session._streamBuf;
                    msg.html = markup(renderMarkdown(session._streamBuf));
                }
                msg.pending = false;
            }
            const toolCalls = session._toolCallMap.size > 0 ? Array.from(session._toolCallMap.values()) : null;
            const rawEvents = session._rawEvents.length > 0 ? [...session._rawEvents] : null;
            if (msg && toolCalls && toolCalls.length > 0) {
                msg.toolCalls = toolCalls;
                msg.toolsExpanded = toolCalls.some(tc => tc.isError);
            }
            session._streamBuf = "";
            session._lastFlushedWordCount = 0;
            session._toolCalls = [];
            session._toolCallMap = new Map();
            session._rawEvents = [];
            session.streaming = false;
            this._stopIncrementalSave();
            if (widget) {
                widget.state.streaming = false;
                widget.state.activityText = "";
            }
            const savedTurnId = session.currentTurnId;
            this._saveAndFetchTrajectory(msg ? msg.text : "", toolCalls, rawEvents, savedTurnId);
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
            // Attach accumulated tool calls BEFORE clearing them
            const toolCalls = session._toolCallMap.size > 0 ? Array.from(session._toolCallMap.values()) : null;
            if (toolCalls && toolCalls.length > 0) {
                msg.toolCalls = toolCalls;
                msg.toolsExpanded = true;
            }
            const rawEvents = session._rawEvents.length > 0 ? [...session._rawEvents] : null;
            session._streamBuf = "";
            session._lastFlushedWordCount = 0;
            session._toolCalls = [];
            session._toolCallMap = new Map();
            session._rawEvents = [];
            session.streaming = false;
            this._stopIncrementalSave();
            if (widget) {
                widget.state.streaming = false;
                widget.state.activityText = "";
            }
            this._saveResponse(errText, toolCalls, rawEvents);
        } else if (state === "delta") {
            const text = this._extractText(payload.message);
            const deltaTools = this._extractToolCallsFromMessage(payload.message);
            console.log(LOG_PREFIX, `📝 DELTA: text=${text ? text.substring(0, 100) : "null"} embeddedTools=${deltaTools.length} existingToolCalls=${session._toolCalls.length}`);
            if (deltaTools.length > 0) {
                console.log(LOG_PREFIX, "📝 DELTA embedded tools:", deltaTools.map(t => t.name));
                for (const tc of deltaTools) {
                    if (!session._toolCallMap.has(tc.toolCallId)) {
                        _logToolCall(session, { toolCallId: tc.toolCallId, name: tc.name, args: tc.args, phase: "start", source_event: "delta.embedded" });
                        if (widget) widget.state.activityText = `Running ${tc.name}…`;
                    }
                }
                this._syncLiveToolCalls(session, messages, widget);
            }
            console.log(LOG_PREFIX, `📝 DELTA text applied:`, JSON.stringify(text)?.substring(0, 200));
            if (text) {
                if (widget) widget.state.activityText = "";
                const msg = messages.findLast(m => m.pending);
                if (msg) {
                    msg.text += text;
                    msg.html = markup(renderMarkdown(msg.text));
                }
                if (widget) widget._scrollToBottom();
            }
        } else if (state === "final") {
            const finalText = this._extractText(payload.message);
            const embeddedTools = this._extractToolCallsFromMessage(payload.message);
            console.group(`${LOG_PREFIX} ✅ FINAL`);
            console.log("text length:", finalText?.length);
            console.log("embedded tools from message:", embeddedTools.length, embeddedTools.map(t => t.name));
            console.log("stream _toolCalls:", session._toolCalls.length, session._toolCalls.map(t => t.name));
            console.log("raw events:", session._rawEvents.length);
            console.log("current turn ID:", session.currentTurnId);
            console.log("full message:", JSON.stringify(payload.message).substring(0, 2000));
            console.groupEnd();
            const msg = messages.findLast(m => m.pending);
            if (msg) {
                if (finalText) msg.text = finalText;
                msg.html = markup(renderMarkdown(msg.text));
                msg.pending = false;
            }
            let toolCalls = session._toolCallMap.size > 0 ? Array.from(session._toolCallMap.values()) : [];
            if (embeddedTools.length > 0) {
                for (const tc of embeddedTools) {
                    if (!session._toolCallMap.has(tc.toolCallId)) {
                        _logToolCall(session, { toolCallId: tc.toolCallId, name: tc.name, args: tc.args, phase: "start", source_event: "final.embedded" });
                    }
                }
                toolCalls = Array.from(session._toolCallMap.values());
            }
            const rawEvents = session._rawEvents.length > 0 ? [...session._rawEvents] : null;
            if (msg && toolCalls.length > 0) {
                msg.toolCalls = toolCalls;
            }
            session._toolCalls = [];
            session._toolCallMap = new Map();
            session._rawEvents = [];
            session.streaming = false;
            this._stopIncrementalSave();
            if (widget) {
                widget.state.streaming = false;
                widget.state.activityText = "";
                widget._scrollToBottom();
            }
            const savedTurnId = session.currentTurnId;
            this._saveAndFetchTrajectory(msg ? msg.text : "", toolCalls.length > 0 ? toolCalls : null, rawEvents, savedTurnId);
        } else if (state === "error") {
            const errText = payload.errorMessage || "Chat error";
            console.error(LOG_PREFIX, "Chat ERROR:", errText);
            let msg = messages.findLast(m => m.pending);
            if (!msg) {
                msg = { role: "assistant", text: "", html: markup(""), pending: false };
                messages.push(msg);
            }
            msg.pending = false;
            msg.text = errText;
            msg.html = markup(renderMarkdown(errText));
            msg.isError = true;
            const toolCalls = session._toolCallMap.size > 0 ? Array.from(session._toolCallMap.values()) : null;
            if (toolCalls && toolCalls.length > 0) {
                msg.toolCalls = toolCalls;
                msg.toolsExpanded = true;
            }
            session._toolCalls = [];
            session._toolCallMap = new Map();
            session._rawEvents = [];
            this._session.streaming = false;
            this._stopIncrementalSave();
            if (widget) {
                widget.state.streaming = false;
                widget.state.activityText = "";
            }
            this._saveResponse(errText, toolCalls, null);
        } else if (state === "aborted") {
            let msg = messages.findLast(m => m.pending);
            if (!msg) {
                msg = { role: "assistant", text: "[Aborted]", html: markup("[Aborted]"), pending: false };
                messages.push(msg);
            } else {
                msg.pending = false;
                if (!msg.text) msg.text = "[Aborted]";
                msg.html = markup(renderMarkdown(msg.text));
            }
            const toolCalls = session._toolCallMap.size > 0 ? Array.from(session._toolCallMap.values()) : null;
            if (toolCalls && toolCalls.length > 0) {
                msg.toolCalls = toolCalls;
                msg.toolsExpanded = true;
            }
            session._toolCalls = [];
            session._toolCallMap = new Map();
            session._rawEvents = [];
            this._session.streaming = false;
            this._stopIncrementalSave();
            if (widget) {
                widget.state.streaming = false;
                widget.state.activityText = "";
            }
        }
    }

    _syncLiveToolCalls(session, messages, widget) {
        if (session._toolCallMap.size === 0) return;
        let msg = messages.findLast(m => m.pending);
        if (!msg) {
            msg = { role: "assistant", text: "", html: markup(""), pending: true };
            messages.push(msg);
            session._lastFlushedWordCount = 0;
        }
        msg.toolCalls = Array.from(session._toolCallMap.values());
        msg.toolsExpanded = true;
        if (widget) widget._scrollToBottom();
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

    _extractToolCallsFromMessage(message) {
        if (!message) return [];
        const content = message.content || message.messages || [];
        if (!Array.isArray(content)) {
            if (content && typeof content === "object") {
                console.log(LOG_PREFIX, "🔍 _extractToolCallsFromMessage: content is object, not array. Keys:", Object.keys(content));
            }
            return [];
        }
        const tools = [];
        for (const block of content) {
            if (!block || typeof block !== "object") continue;
            if (block.type === "tool_use" || block.type === "toolCall") {
                tools.push({
                    toolCallId: block.id || block.toolCallId || `msg-tc-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`,
                    name: block.name || "unknown",
                    args: block.input || block.arguments || null,
                    result: null,
                    isError: false,
                });
            }
        }
        if (tools.length > 0) {
            console.log(LOG_PREFIX, `🔍 _extractToolCallsFromMessage: found ${tools.length} embedded tool calls:`, tools.map(t => t.name));
        }
        return tools;
    }

    async _loadHistory() {
        if (!this.props.sandboxId) return;
        if (this._session.historyLoaded) return;
        console.log(LOG_PREFIX, "📖 _loadHistory: loading for sandbox", this.props.sandboxId);
        await _loadMarked().catch(e => console.warn(LOG_PREFIX, "marked load failed:", e));
        try {
            const result = await rpc("/talos/chat/history", { sandbox_id: this.props.sandboxId });
            console.group(`${LOG_PREFIX} 📖 History loaded`);
            console.log("turns:", result.turns?.length);
            if (result.turns) {
                for (const t of result.turns) {
                    console.log(`  turn ${t.id}: prompt=${t.prompt?.substring(0, 50) || "none"} response=${t.response?.substring(0, 50) || "none"} tool_calls=${t.tool_calls ? "YES" : "no"} qc=${t.qc_severity || "none"}`);
                }
            }
            console.groupEnd();
            if (result.turns) {
                this._session.messages.length = 0;
                for (const t of result.turns) {
                    if (t.prompt) {
                        this._session.messages.push({ role: "user", text: t.prompt });
                    }

                    if (t.qc_severity) {
                        let qcChecks = [];
                        let qcSummary = "";
                        if (t.qc_response) {
                            try {
                                const qcData = JSON.parse(t.qc_response);
                                qcChecks = qcData.checks || [];
                                qcSummary = qcData.summary || "";
                            } catch {}
                        }
                        this._session.messages.push({
                            role: "assistant",
                            text: qcSummary || `QC: ${t.qc_severity}`,
                            isQc: true,
                            qcSeverity: t.qc_severity,
                            qcChecks,
                            pending: false,
                        });
                        if (t.qc_dismiss_reason) {
                            this._session.messages.push({
                                role: "assistant",
                                text: `QC dismissed: ${t.qc_dismiss_reason}`,
                                html: markup(renderMarkdown(`*QC dismissed:* ${t.qc_dismiss_reason}`)),
                                pending: false,
                            });
                        }
                    }

                    if (t.response) {
                        const isPartial = t.status === "Streaming";
                        const msg = {
                            role: "assistant",
                            text: t.response,
                            html: markup(renderMarkdown(t.response + (isPartial ? "\n\n*(partial — response was interrupted)*" : ""))),
                            pending: false,
                        };
                        if (t.tool_calls) {
                            try {
                                const calls = JSON.parse(t.tool_calls);
                                if (Array.isArray(calls) && calls.length > 0) {
                                    msg.toolCalls = calls;
                                    console.log(LOG_PREFIX, `📖 Turn ${t.id}: ${calls.length} tool calls restored:`, calls.map(c => c.name));
                                }
                            } catch (e) {
                                console.warn(LOG_PREFIX, `📖 Turn ${t.id}: tool_calls parse error:`, e);
                            }
                        }
                        this._session.messages.push(msg);
                    } else if (t.status === "Pending") {
                        this._session.currentTurnId = t.id;
                    }
                }
            }
            this._session.historyLoaded = true;
        } catch (e) {
            console.error(LOG_PREFIX, "📖 History load failed:", e);
        }
        this._scrollToBottom();
    }

    async _restoreSessionFromGateway() {
        const sessionKey = "odoo:sandbox:" + this.props.sandboxId;
        console.log(LOG_PREFIX, "🔄 _restoreSessionFromGateway: sessionKey=", sessionKey);
        try {
            const res = await this._wsRpc("chat.history", { sessionKey, limit: 200 });
            const messages = res?.result?.messages || res?.messages || [];
            console.log(LOG_PREFIX, `🔄 Gateway returned ${messages.length} messages`);
            if (messages.length === 0) return;

            let lastAssistantText = "";
            const toolCalls = {};
            for (const msg of messages) {
                const inner = msg?.message || msg;
                const role = inner?.role || "";
                const content = inner?.content;
                if (role === "assistant") {
                    if (typeof content === "string") {
                        lastAssistantText = content;
                    } else if (Array.isArray(content)) {
                        let text = "";
                        for (const block of content) {
                            if (!block || typeof block !== "object") continue;
                            if (block.type === "text") text += block.text || "";
                            if (block.type === "tool_use" || block.type === "toolCall") {
                                const tcId = block.id || block.toolCallId || "";
                                toolCalls[tcId] = {
                                    toolCallId: tcId,
                                    name: block.name || "unknown",
                                    args: block.input || block.arguments || null,
                                    result: null,
                                    isError: false,
                                };
                            }
                        }
                        if (text) lastAssistantText = text;
                    }
                } else if (role === "tool" || role === "toolResult") {
                    const tcId = inner.tool_use_id || inner.toolCallId || "";
                    if (tcId && toolCalls[tcId] && Array.isArray(content)) {
                        let resultText = "";
                        for (const block of content) {
                            if (typeof block === "string") resultText += block;
                            else if (block?.type === "text") resultText += block.text || "";
                        }
                        toolCalls[tcId].result = resultText || null;
                        toolCalls[tcId].isError = !!(inner.is_error || inner.isError);
                    }
                }
            }

            const existingAssistant = this._session.messages.findLast(
                m => m.role === "assistant" && !m.isQc && !m.isError
            );
            const existingText = existingAssistant?.text || "";
            if (lastAssistantText && lastAssistantText.length > existingText.length) {
                console.log(LOG_PREFIX, `🔄 Gateway has newer text (${lastAssistantText.length} > ${existingText.length}), updating`);
                if (existingAssistant) {
                    existingAssistant.text = lastAssistantText;
                    existingAssistant.html = markup(renderMarkdown(lastAssistantText));
                    existingAssistant.pending = false;
                    const tcArr = Object.values(toolCalls);
                    if (tcArr.length > 0) existingAssistant.toolCalls = tcArr;
                } else {
                    const newMsg = {
                        role: "assistant",
                        text: lastAssistantText,
                        html: markup(renderMarkdown(lastAssistantText)),
                        pending: false,
                    };
                    const tcArr = Object.values(toolCalls);
                    if (tcArr.length > 0) newMsg.toolCalls = tcArr;
                    this._session.messages.push(newMsg);
                }
                this._scrollToBottom();
            }
        } catch (e) {
            console.warn(LOG_PREFIX, "🔄 _restoreSessionFromGateway failed:", e);
        }
    }

    _wsRpc(method, params) {
        const ws = this._session.ws;
        if (!ws || !this._session.wsConnected) {
            console.warn(LOG_PREFIX, `📡 _wsRpc(${method}): WS not connected — rejecting`);
            return Promise.reject(new Error("WS not connected"));
        }
        const id = nextId();
        const msg = { type: "req", id, method, params };
        console.log(LOG_PREFIX, `📡 _wsRpc SEND: method=${method} id=${id} params=${JSON.stringify(params).substring(0, 500)}`);
        return new Promise((resolve, reject) => {
            this._session._pendingRpc.set(id, { resolve, reject });
            ws.send(JSON.stringify(msg));
            setTimeout(() => {
                if (this._session._pendingRpc.has(id)) {
                    this._session._pendingRpc.delete(id);
                    console.error(LOG_PREFIX, `📡 _wsRpc TIMEOUT: method=${method} id=${id}`);
                    reject(new Error("WS RPC timeout"));
                }
            }, 15000);
        });
    }

    async _saveAndFetchTrajectory(text, toolCalls, rawEvents, turnId) {
        await this._saveResponse(text, toolCalls, rawEvents);
        await new Promise(r => setTimeout(r, 1000));
        await this._fetchTrajectory(turnId);
    }

    async _fetchTrajectory(turnId) {
        if (!turnId) return;
        const sessionKey = "odoo:sandbox:" + this.props.sandboxId;
        const maxAttempts = 3;

        for (let attempt = 1; attempt <= maxAttempts; attempt++) {
            console.log(LOG_PREFIX, `📜 _fetchTrajectory attempt ${attempt}/${maxAttempts}: turnId=${turnId}`);
            try {
                const res = await this._wsRpc("chat.history", { sessionKey, limit: 1000 });
                const messages = res?.result?.messages || res?.messages || [];
                console.log(LOG_PREFIX, `📜 chat.history returned ${messages.length} messages`);

                if (messages.length === 0) {
                    if (attempt < maxAttempts) {
                        await new Promise(r => setTimeout(r, 1500));
                        continue;
                    }
                    console.warn(LOG_PREFIX, "📜 chat.history returned 0 messages after all retries");
                    return;
                }

                const extractedTools = this._extractToolCallsFromTrajectory(messages);
                if (extractedTools.length > 0) {
                    const lastAssistant = this._session.messages.findLast(
                        m => m.role === "assistant" && !m.isQc && !m.isError
                    );
                    if (lastAssistant) {
                        if (!lastAssistant.toolCalls || lastAssistant.toolCalls.length === 0) {
                            lastAssistant.toolCalls = extractedTools;
                        } else {
                            const existingMap = new Map(lastAssistant.toolCalls.map(t => [t.toolCallId, t]));
                            for (const tc of extractedTools) {
                                const existing = existingMap.get(tc.toolCallId);
                                if (!existing) {
                                    lastAssistant.toolCalls.push(tc);
                                } else if (tc.result && !existing.result) {
                                    existing.result = tc.result;
                                    existing.isError = tc.isError;
                                    if (tc.args && !existing.args) existing.args = tc.args;
                                }
                            }
                        }
                    }
                }

                await rpc("/talos/chat/save_trajectory", {
                    turn_id: turnId,
                    trajectory_messages: JSON.stringify(messages),
                });
                console.log(LOG_PREFIX, `📜 save_trajectory done for turn=${turnId}`);
                return;
            } catch (e) {
                console.error(LOG_PREFIX, `📜 Trajectory attempt ${attempt} failed:`, e);
                if (attempt < maxAttempts) {
                    await new Promise(r => setTimeout(r, 1500));
                }
            }
        }
    }

    _extractToolCallsFromTrajectory(messages) {
        const toolCalls = {};
        for (const msg of messages) {
            const inner = msg?.message || msg;
            const role = inner?.role || "";
            const content = inner?.content;
            if (!Array.isArray(content)) continue;

            if (role === "assistant") {
                for (const block of content) {
                    if (!block || typeof block !== "object") continue;
                    if (block.type === "tool_use" || block.type === "toolCall") {
                        const tcId = block.id || block.toolCallId || `tc-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`;
                        toolCalls[tcId] = {
                            toolCallId: tcId,
                            name: block.name || "unknown",
                            args: block.input || block.arguments || null,
                            result: null,
                            isError: false,
                        };
                    }
                }
            } else if (role === "tool" || role === "toolResult") {
                const tcId = inner.tool_use_id || inner.toolCallId || "";
                if (tcId && toolCalls[tcId]) {
                    let resultText = "";
                    for (const block of content) {
                        if (typeof block === "string") resultText += block;
                        else if (block?.type === "text") resultText += block.text || "";
                    }
                    toolCalls[tcId].result = resultText || null;
                    toolCalls[tcId].isError = !!(inner.is_error || inner.isError);
                }
            }
        }
        return Object.values(toolCalls);
    }

    async _saveResponse(text, toolCalls = null, rawEvents = null) {
        const turnId = this._session.currentTurnId;
        const runId = this._session.currentRunId;
        if (!turnId) {
            console.warn(LOG_PREFIX, "💾 _saveResponse: no currentTurnId — skipping");
            return;
        }
        this._session.currentTurnId = null;
        this._session.currentRunId = null;
        console.group(`${LOG_PREFIX} 💾 _saveResponse turn=${turnId}`);
        console.log("text length:", text?.length);
        console.log("toolCalls:", toolCalls ? toolCalls.length + " — " + JSON.stringify(toolCalls.map(t => ({name: t.name, hasResult: !!t.result}))) : "null");
        console.log("rawEvents:", rawEvents ? rawEvents.length : "null");
        console.log("runId:", runId);
        console.groupEnd();
        try {
            const params = {
                turn_id: turnId,
                response: text,
                timestamp: new Date().toISOString(),
            };
            if (runId) {
                params.run_id = runId;
            }
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
    }

    _startIncrementalSave() {
        this._stopIncrementalSave();
        const session = this._session;
        session._lastSavedText = "";
        session._incrementalSaveTimer = setInterval(() => {
            this._saveIncremental();
        }, INCREMENTAL_SAVE_INTERVAL_MS);
    }

    _stopIncrementalSave() {
        const session = this._session;
        if (session._incrementalSaveTimer) {
            clearInterval(session._incrementalSaveTimer);
            session._incrementalSaveTimer = null;
        }
    }

    async _saveIncremental() {
        const session = this._session;
        if (!session.currentTurnId || !session.streaming) return;
        const currentText = session._streamBuf || "";
        if (!currentText || currentText === session._lastSavedText) return;
        session._lastSavedText = currentText;
        const toolCalls = session._toolCallMap.size > 0 ? Array.from(session._toolCallMap.values()) : null;
        try {
            const params = {
                turn_id: session.currentTurnId,
                response: currentText,
                partial: true,
            };
            if (toolCalls && toolCalls.length > 0) {
                params.tool_calls = JSON.stringify(toolCalls);
            }
            await rpc("/talos/chat/save_response", params);
            console.log(LOG_PREFIX, `💾 Incremental save: turn=${session.currentTurnId} text=${currentText.length}chars tools=${toolCalls?.length || 0}`);
        } catch (e) {
            console.warn(LOG_PREFIX, "Incremental save failed:", e);
        }
    }

    async onSend() {
        const text = this.state.inputText.trim();
        if (!text || this.state.sending || this._session.streaming) return;
        if (!this._session.wsConnected) {
            this._session.messages.push({
                role: "assistant",
                text: `Not connected. ${this.state.statusText}`,
                html: markup(renderMarkdown(`Not connected. ${this.state.statusText}`)),
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
            const r = await rpc("/talos/chat/create_turn", {
                sandbox_id: this.props.sandboxId,
                message: text,
                timestamp: new Date().toISOString(),
            });
            turnId = r.turn_id;
            console.log(LOG_PREFIX, "Turn created:", turnId);
        } catch (e) {
            console.error(LOG_PREFIX, "Create turn failed:", e);
        }
        this._session.currentTurnId = turnId;

        let qcResult = null;
        try {
            const qcResponse = await rpc("/talos/qc", { prompt: text });
            console.log(LOG_PREFIX, "QC response:", JSON.stringify(qcResponse));
            if (qcResponse.error) {
                console.warn(LOG_PREFIX, "QC error, passing through:", qcResponse.error);
                this._session.messages.push({
                    role: "assistant",
                    text: `⚠️ QC check failed: ${qcResponse.error}`,
                    pending: false,
                });
                this._scrollToBottom();
            } else if (qcResponse.qc_result) {
                qcResult = qcResponse.qc_result;
                if (turnId) {
                    rpc("/talos/chat/save_qc", {
                        turn_id: turnId,
                        severity: qcResult.severity || "",
                        qc_response: JSON.stringify(qcResult),
                    }).catch(e => console.warn(LOG_PREFIX, "save_qc failed:", e));
                }
            } else {
                console.warn(LOG_PREFIX, "QC response has no qc_result:", qcResponse);
                this._session.messages.push({
                    role: "assistant",
                    text: "⚠️ QC check returned no result. The LLM response may not have been parseable.",
                    pending: false,
                });
                this._scrollToBottom();
            }
        } catch (e) {
            console.warn(LOG_PREFIX, "QC call failed, passing through:", e);
            this._session.messages.push({
                role: "assistant",
                text: `⚠️ QC call failed: ${e.message || e}`,
                pending: false,
            });
            this._scrollToBottom();
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

    async _sendToOpenClaw(text) {
        if (!this._session.ws || !this._session.wsConnected) {
            for (let i = 0; i < 5; i++) {
                await new Promise(r => setTimeout(r, 2000));
                if (this._session.ws && this._session.wsConnected) break;
            }
        }
        if (!this._session.ws || !this._session.wsConnected) {
            this._session.messages.push({
                role: "assistant",
                text: "Connection lost while processing. Please try again.",
                html: markup(renderMarkdown("Connection lost while processing. Please try again.")),
                isError: true,
                pending: false,
            });
            this._scrollToBottom();
            return;
        }
        const runId = crypto.randomUUID();
        const chatSendMsg = {
            type: "req",
            id: nextId(),
            method: "chat.send",
            params: {
                message: text,
                sessionKey: "odoo:sandbox:" + this.props.sandboxId,
                deliver: false,
                idempotencyKey: runId,
            },
        };
        console.group(`${LOG_PREFIX} ➡️ SEND chat.send`);
        console.log("Full message:", JSON.stringify(chatSendMsg));
        console.groupEnd();
        this._session.ws.send(JSON.stringify(chatSendMsg));

        this._session.currentRunId = runId;
        this._session.messages.push({ role: "assistant", text: "", pending: true });
        this.state.activityText = "Waiting for model…";
        this._session.streaming = true;
        this.state.streaming = true;
        this._startIncrementalSave();
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

        const toolCalls = session._toolCallMap.size > 0 ? Array.from(session._toolCallMap.values()) : null;
        const rawEvents = session._rawEvents.length > 0 ? [...session._rawEvents] : null;
        if (msg && toolCalls && toolCalls.length > 0) {
            msg.toolCalls = toolCalls;
        }
        session._streamBuf = "";
        session._lastFlushedWordCount = 0;
        session._toolCalls = [];
        session._toolCallMap = new Map();
        session._rawEvents = [];
        session.streaming = false;
        this._stopIncrementalSave();
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

    onToggleTools(msg) {
        msg.toolsExpanded = !msg.toolsExpanded;
    }

    formatToolResult(result) {
        if (typeof result === "string") return result;
        try { return JSON.stringify(result, null, 2); } catch { return String(result); }
    }

    _checkBrowserToolForLogin(data, widget) {
        const resultStr = typeof data.result === "string"
            ? data.result
            : JSON.stringify(data.result || "");
        const argsStr = typeof data.args === "string"
            ? data.args
            : JSON.stringify(data.args || "");

        const allText = resultStr + " " + argsStr;
        const urls = _extractUrlsFromText(allText);
        const loginUrl = urls.find(u => _looksLikeLoginUrl(u));

        const loginKeywords = /\b(login|sign.?in|authenticat|credential|password|username)\b/i;
        const hasLoginContent = loginKeywords.test(allText);

        if (!loginUrl && !hasLoginContent) return;

        let domain = "";
        try {
            domain = loginUrl ? new URL(loginUrl).hostname : "";
        } catch {}

        console.log(LOG_PREFIX, "🔐 Login page detected:", loginUrl || "(keyword match)", "domain:", domain);

        widget.state.browserAuthActive = true;
        widget.state.browserAuthUrl = loginUrl || "";
        widget.state.browserAuthDomain = domain;
        widget.state.browserAuthStatus = "waiting";
        widget.state.browserAuthError = "";
        widget.state.browserAuthScreenshot = "";
        widget.state.browserAuthCookieInput = "";

        this._fetchBrowserScreenshot(widget);
    }

    async _fetchBrowserScreenshot(widget) {
        try {
            const result = await rpc("/talos/browser/screenshot", {
                sandbox_id: this.props.sandboxId,
            });
            if (result.image) {
                widget.state.browserAuthScreenshot = "data:image/png;base64," + result.image;
            } else if (result.error) {
                console.warn(LOG_PREFIX, "Screenshot error:", result.error);
            }
        } catch (e) {
            console.warn(LOG_PREFIX, "Screenshot fetch failed:", e);
        }
    }

    onRefreshBrowserScreenshot() {
        this._fetchBrowserScreenshot(this);
    }

    async onInjectCookies() {
        const raw = this.state.browserAuthCookieInput.trim();
        if (!raw) return;

        this.state.browserAuthStatus = "injecting";
        this.state.browserAuthError = "";

        try {
            const result = await rpc("/talos/browser/inject_cookies", {
                sandbox_id: this.props.sandboxId,
                cookies: raw,
                url: this.state.browserAuthUrl || undefined,
            });
            if (result.success) {
                this.state.browserAuthStatus = "done";
                this._sendToOpenClaw(
                    "I've injected the authentication cookies. Please refresh the page and continue."
                );
                setTimeout(() => this.onDismissBrowserAuth(), 3000);
            } else {
                this.state.browserAuthStatus = "error";
                this.state.browserAuthError = result.error || "Unknown error";
            }
        } catch (e) {
            this.state.browserAuthStatus = "error";
            this.state.browserAuthError = e.message || "RPC failed";
        }
    }

    onDismissBrowserAuth() {
        this.state.browserAuthActive = false;
        this.state.browserAuthUrl = "";
        this.state.browserAuthDomain = "";
        this.state.browserAuthScreenshot = "";
        this.state.browserAuthCookieInput = "";
        this.state.browserAuthStatus = "";
        this.state.browserAuthError = "";
    }
}
