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

const LOG_PREFIX = "[skoll-chat]";
const STREAM_WORD_THRESHOLD = 5;
const INCREMENTAL_SAVE_INTERVAL_MS = 3000;
const CHAT_TIMEOUT_MS = 30 * 60 * 1000;

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
                    console.warn("[SkollChatWidget]", "Trajectory save on stop failed:", e);
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
        const history = await rpc("/skoll/chat/history", {
            sandbox_id: sandboxId,
        });
        const turns = history?.turns || [];
        if (turns.length > 0) {
            return turns[turns.length - 1].id;
        }
    } catch (e) {
        console.warn("[SkollChatWidget]", "Could not resolve latest turn:", e);
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
            _reconnectAttempts: 0,
            _reconnectTimer: null,
            _thinkingBuf: "",
            qcPending: false,
            qcResult: null,
            qcDismissReason: "",
            qcPromptText: "",
            // Sub-agent tracking (live WS)
            _childSessions: {},
            _childSubscriptions: new Set(),
            _activeChildKey: null,
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

export class SkollChatWidget extends Component {
    static template = "skoll.ChatWidget";
    static props = {
        sandboxId: Number,
        dockerStatus: String,
        dockerWsUrl: { type: [String, Boolean], optional: true },
        gatewayToken: { type: [String, Boolean], optional: true },
        modelType: { type: String, optional: true },
    };

    setup() {
        this.messagesEndRef = useRef("messagesEnd");
        this.mainTextareaRef = useRef("mainTextarea");
        this.hintTextareaRef = useRef("hintTextarea");

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
            hintPopupVisible: false,
            hintText: "",
            hintTargetMsgIndex: -1,
            subChatView: null,
        });

        this._onWsMessage = (payload) => this._handleWsPayload(payload);
        this._pendingHint = false;

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
                if (this._session.historyLoaded && !this._session.ws) {
                    this._resetSessionMessages();
                }
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
                    maxProtocol: 4,
                    client: { id: "openclaw-control-ui", version: "control-ui", platform: "web", mode: "webchat" },
                    role: "operator",
                    scopes: ["operator.admin", "operator.read", "operator.write", "operator.approvals", "operator.pairing"],
                    caps: ["tool-events", "thinking-events"],
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
            const raw = event.data;
            if (typeof raw === "string" && (raw.toUpperCase() === "HEARTBEAT_OK" || raw.toUpperCase() === "HEARTBEAT" || raw.toUpperCase() === "PONG")) return;

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
                    this._session._reconnectAttempts = 0;
                    this._startHeartbeat();
                    if (widget) {
                        widget.state.connected = true;
                        widget.state.statusText = "Connected";
                    }
                    // After reconnect, fetch the latest session state from OpenClaw
                    this._restoreSessionFromGateway();
                    this._subscribeToSessions();
                    this._detectChildSessions();
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
                console.log("Full payload:", JSON.stringify(frame.payload || null).substring(0, 2000));
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
                // If the server stamps a sessionKey and it belongs to a subagent session,
                // skip main-widget processing — subagent content arrives via session.message.
                const evtKey = p.sessionKey || "";
                if (evtKey.includes(":subagent:")) {
                    return;
                }
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

                if (agentStream === "thinking") {
                    console.log(LOG_PREFIX, `🤖 AGENT THINKING: len=${(agentData.text || "").length} delta_len=${(agentData.delta || "").length}`);
                    this._handleChatEvent({
                        stream: "thinking",
                        message: agentData,
                        data: agentData,
                    }, widget);
                    return;
                }

                console.log(LOG_PREFIX, `🤖 AGENT [${agentStream}]:`, JSON.stringify(agentData).substring(0, 500));
                return;
            }

            if (frame.type === "event" && (frame.event === "tick" || frame.event === "health" || frame.event === "presence" || frame.event === "heartbeat")) {
                return;
            }

            if (frame.type === "event" && frame.event === "sessions.changed") {
                const p = frame.payload || {};
                this._handleSessionsChanged(p, widget);
                return;
            }

            if (frame.type === "event" && frame.event === "session.message") {
                const p = frame.payload || {};
                this._handleSessionMessage(p, widget);
                return;
            }

            if (frame.type === "event") {
                console.log(LOG_PREFIX, "📨 UNHANDLED EVENT:", frame.event, "payload:", JSON.stringify(frame.payload || null).substring(0, 1000));
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
                    rpc("/skoll/chat/save_response", params).catch(e => console.warn(LOG_PREFIX, "Partial save on disconnect failed:", e));
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

                const MAX_RECONNECT = 8;
                const attempts = this._session._reconnectAttempts || 0;

                if (was && widget.isRunning && attempts < MAX_RECONNECT) {
                    const delay = Math.min(2000 * Math.pow(2, attempts), 60000);
                    this._session._reconnectAttempts = attempts + 1;
                    widget.state.statusText = `Disconnected — reconnecting in ${Math.round(delay / 1000)}s (attempt ${attempts + 1}/${MAX_RECONNECT})…`;
                    if (this._session._reconnectTimer) clearTimeout(this._session._reconnectTimer);
                    this._session._reconnectTimer = setTimeout(() => {
                        this._session._reconnectTimer = null;
                        widget._tryConnect();
                    }, delay);
                } else if (was && widget.isRunning) {
                    widget.state.statusText = "Connection failed after multiple attempts. Please reload.";
                } else {
                    widget.state.statusText = `Closed (code=${ev.code} reason=${ev.reason || "n/a"})`;
                }
            }
        };

        ws.onerror = (ev) => {
            console.error(LOG_PREFIX, "WS onerror:", ev);
        };
    }

    _disconnectGateway() {
        this._stopHeartbeat();
        if (this._session._reconnectTimer) {
            clearTimeout(this._session._reconnectTimer);
            this._session._reconnectTimer = null;
        }
        if (this._session.ws) {
            console.log(LOG_PREFIX, "Disconnecting WS");
            try { this._session.ws.close(); } catch {}
            this._session.ws = null;
        }
        this._session.wsConnected = false;
        this.state.connected = false;
    }

    _resetSessionMessages() {
        console.log(LOG_PREFIX, "Resetting session messages for sandbox", this.props.sandboxId);
        this._session.messages.length = 0;
        this._session.historyLoaded = false;
        this._session.currentTurnId = null;
        this._session.currentRunId = null;
        this._session.streaming = false;
        this._session._streamBuf = "";
        this._session._lastFlushedWordCount = 0;
        this._session._toolCalls = [];
        this._session._toolCallMap = new Map();
        this._session._rawEvents = [];
        this._session._reconnectAttempts = 0;
        this._session._childSessions = {};
        this._session._childSubscriptions = new Set();
        this._session._activeChildKey = null;
        this.state.messages.length = 0;
        this.state.streaming = false;
        this.state.sending = false;
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

        const dataText = payload.data?.text || payload.message?.text || "";
        if (typeof dataText === "string" && dataText.toUpperCase().includes("HEARTBEAT")) return;

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
                msg = { role: "assistant", text: "", html: markup(""), pending: true, isModelResponse: true, turnId: session.currentTurnId };
                messages.push(msg);
                session._lastFlushedWordCount = 0;
            }

            session._streamBuf = data.text;
            msg.text = session._streamBuf;
            msg.html = markup(renderMarkdown(session._streamBuf));
            session._lastFlushedWordCount = session._streamBuf.split(/\s+/).length;
        } else if (stream === "tool") {
            const phase = data.phase || "";
            const toolCallId = data.toolCallId || "";
            const toolName = data.name || "";
            console.log(LOG_PREFIX, `🔧 TOOL STREAM: phase=${phase} name=${toolName} id=${toolCallId} args=${JSON.stringify(data.args || null).substring(0, 300)}`);
            if (phase === "start" && toolCallId) {
                _logToolCall(session, { toolCallId, name: toolName, args: data.args, phase: "start", source_event: "chat.tool" });
                if (widget && session.streaming && !session._activeChildKey) widget.state.activityText = `Running ${toolName}…`;
                console.log(LOG_PREFIX, `🔧 Tool START: ${toolName} (${toolCallId}) — total tool calls now: ${session._toolCalls.length}`);
                this._routeToolEventToChild(toolName, "start", data.args, null, false);
            } else if (phase === "end" && toolCallId) {
                _logToolCall(session, { toolCallId, name: toolName, result: data.result ?? data.error ?? data.partialResult, isError: !!(data.isError || data.error), phase: "end", source_event: "chat.tool" });
                console.log(LOG_PREFIX, `🔧 Tool END: ${toolName} (${toolCallId}) isError=${!!data.isError} result=${JSON.stringify(data.result || null).substring(0, 300)}`);
                if (toolName === "browser" && widget) {
                    this._checkBrowserToolForLogin(data, widget);
                }
                this._checkForSubAgentSpawn(data, toolName);
                this._routeToolEventToChild(toolName, "end", data.args, data.result ?? data.error ?? data.partialResult, !!(data.isError || data.error));
            } else if (phase === "update" && toolCallId) {
                _logToolCall(session, { toolCallId, name: toolName, result: data.partialResult, phase: "update", source_event: "chat.tool" });
                console.log(LOG_PREFIX, `🔧 Tool UPDATE: ${toolName} (${toolCallId})`);
            } else {
                console.warn(LOG_PREFIX, `🔧 Tool UNKNOWN phase: ${phase} toolCallId=${toolCallId} name=${toolName}`);
            }
            this._syncLiveToolCalls(session, messages, widget);
        } else if (stream === "thinking") {
            const thinkingText = data.text || data.delta || "";
            console.log(LOG_PREFIX, `🧠 THINKING STREAM: len=${thinkingText.length} delta_len=${(data.delta || "").length}`);
            if (widget && session.streaming && !session._activeChildKey) widget.state.activityText = "Thinking…";
            if (!session._thinkingBuf) session._thinkingBuf = "";
            if (data.text) {
                session._thinkingBuf = data.text;
            } else if (data.delta) {
                session._thinkingBuf += data.delta;
            }
            let msg = messages.findLast(m => m.pending);
            if (!msg) {
                msg = { role: "assistant", text: "", html: markup(""), pending: true, isModelResponse: true, turnId: session.currentTurnId };
                messages.push(msg);
            }
            msg.thinkingText = session._thinkingBuf;
            if (msg.thinkingExpanded === undefined) {
                msg.thinkingExpanded = true;
            }
        } else if (stream === "lifecycle" && data.phase === "start") {
            console.log(LOG_PREFIX, "🏁 Lifecycle START — Thinking…");
            if (widget && session.streaming && !session._activeChildKey) widget.state.activityText = "Thinking…";
        } else if (stream === "lifecycle" && data.phase === "end") {
            console.group(`${LOG_PREFIX} 🏁 Lifecycle END`);
            console.log("Tool calls collected:", session._toolCalls.length, session._toolCalls.map(t => t.name));
            console.log("Raw events collected:", session._rawEvents.length);
            console.log("Stream buffer length:", session._streamBuf?.length);
            console.log("Current turn ID:", session.currentTurnId);
            console.groupEnd();
            const msg = messages.findLast(m => m.pending);
            if (msg) {
                if (session._streamBuf && session._streamBuf.length > (msg.text || "").length) {
                    msg.text = session._streamBuf;
                    msg.html = markup(renderMarkdown(session._streamBuf));
                }
                if (session._thinkingBuf) {
            msg.thinkingText = session._thinkingBuf;
            msg.thinkingExpanded = false;
                }
                msg.pending = false;
            }
            const toolCalls = session._toolCallMap.size > 0 ? Array.from(session._toolCallMap.values()) : null;
            const rawEvents = session._rawEvents.length > 0 ? [...session._rawEvents] : null;
            if (msg && toolCalls && toolCalls.length > 0) {
                msg.toolCalls = toolCalls;
                msg.toolsExpanded = toolCalls.some(tc => tc.isError);
            }
            // Discard pending messages that ended up with no text and no tool calls —
            // these are tool-only turns where no assistant text was produced and would
            // render as blank grey bubbles.
            if (msg && !msg.text && !(msg.toolCalls && msg.toolCalls.length > 0)) {
                const idx = messages.lastIndexOf(msg);
                if (idx !== -1) messages.splice(idx, 1);
            }
            session._streamBuf = "";
            session._lastFlushedWordCount = 0;
            session._thinkingBuf = "";
            session._toolCalls = [];
            session._toolCallMap = new Map();
            session._rawEvents = [];
            session.streaming = false;
            this._stopIncrementalSave();
            if (session._activeChildKey) {
                this._markSubAgentCompleted(session._activeChildKey);
                session._activeChildKey = null;
            }
            if (widget) {
                widget.state.streaming = false;
                widget.state.activityText = "";
            }
            const savedTurnId = session.currentTurnId;
            this._saveAndFetchTrajectory(msg ? msg.text : "", toolCalls, rawEvents, savedTurnId);
            this._detectChildSessions();
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
                        if (widget && session.streaming && !session._activeChildKey) widget.state.activityText = `Running ${tc.name}…`;
                    }
                }
                this._syncLiveToolCalls(session, messages, widget);
            }
            console.log(LOG_PREFIX, `📝 DELTA text applied:`, JSON.stringify(text)?.substring(0, 200));
            if (text) {
                if (widget) widget.state.activityText = "";
                const msg = messages.findLast(m => m.pending);
                if (msg) {
                    session._streamBuf = (session._streamBuf || "") + text;
                    msg.text = session._streamBuf;
                    msg.html = markup(renderMarkdown(session._streamBuf));
                }
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
            console.log("full message:", JSON.stringify(payload.message || null).substring(0, 2000));
            console.groupEnd();
            const msg = messages.findLast(m => m.pending);
            if (msg) {
                if (finalText) {
                    msg.text = finalText;
                    session._streamBuf = finalText;
                }
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
            this._detectChildSessions();
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
            msg = { role: "assistant", text: "", html: markup(""), pending: true, isModelResponse: true, turnId: session.currentTurnId };
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
            const result = await rpc("/skoll/chat/history", { sandbox_id: this.props.sandboxId });
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
                    const userText = t.prompt || t.hints;
                    if (userText) {
                        this._session.messages.push({ role: "user", text: userText, isHint: !!t.is_hint_turn });
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
                            isModelResponse: true,
                            turnId: t.id,
                            feedback: t.feedback || null,
                        };
                        this._session.messages.push(msg);

                        if (t.spawn_tree) {
                            try {
                                const tree = JSON.parse(t.spawn_tree);
                                let subMsgsByKey = {};
                                if (t.sub_agent_messages) {
                                    const allMsgs = JSON.parse(t.sub_agent_messages);
                                    for (const m of allMsgs) {
                                        const key = m.sessionKey || "";
                                        if (!subMsgsByKey[key]) subMsgsByKey[key] = [];
                                        subMsgsByKey[key].push(m);
                                    }
                                }
                                if (Array.isArray(tree)) {
                                    for (const node of tree) {
                                        const childKey = node.sessionKey || "";
                                        this._session._childSessions[childKey] = {
                                            agent: node.agent || "",
                                            parentKey: node.parentKey || "",
                                            description: node.description || "",
                                            status: "completed",
                                            messages: subMsgsByKey[childKey] || [],
                                        };
                                        this._session.messages.push({
                                            type: "subagent_card",
                                            sessionKey: childKey,
                                            agent: node.agent || "",
                                            description: node.description || node.agent || "Sub-agent task",
                                            status: "completed",
                                        });
                                    }
                                }
                            } catch (e) {
                                console.warn(LOG_PREFIX, `📖 Turn ${t.id}: spawn_tree parse error:`, e);
                            }
                        }
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
            const res = await this._wsRpc("chat.history", { sessionKey, limit: 200, includeThinking: true }, 60000);
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
                        isModelResponse: true,
                        turnId: this._session.currentTurnId,
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

    _wsRpc(method, params, timeout = 15000) {
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
                    console.error(LOG_PREFIX, `📡 _wsRpc TIMEOUT: method=${method} id=${id} timeout=${timeout}ms`);
                    reject(new Error("WS RPC timeout"));
                }
            }, timeout);
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
                const res = await this._wsRpc("chat.history", { sessionKey, limit: 1000, includeThinking: true }, 60000);
                const messages = res?.result?.messages || res?.messages || [];
                console.log(LOG_PREFIX, `📜 chat.history returned ${messages.length} messages`);

                let thinkingBlockCount = 0;
                for (const msg of messages) {
                    const inner = msg?.message || msg;
                    const content = inner?.content;
                    if (Array.isArray(content)) {
                        for (const block of content) {
                            if (block?.type === "thinking") {
                                thinkingBlockCount++;
                                console.log(LOG_PREFIX, `🧠 [THINKING-DEBUG] Found thinking block in chat.history:`,
                                    `thinking_len=${(block.thinking || "").length}`,
                                    `has_thinkingSignature=${!!block.thinkingSignature}`,
                                    `has_signature=${!!block.signature}`,
                                    `keys=${Object.keys(block).join(",")}`);
                            }
                        }
                    }
                }
                console.log(LOG_PREFIX, `🧠 [THINKING-DEBUG] chat.history total thinking blocks: ${thinkingBlockCount}`);

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

                const trajectoryStr = JSON.stringify(messages);
                console.log(LOG_PREFIX, `🧠 [THINKING-DEBUG] save_trajectory: ${thinkingBlockCount} thinking blocks, payload_len=${trajectoryStr.length}`);
                await rpc("/skoll/chat/save_trajectory", {
                    turn_id: turnId,
                    trajectory_messages: trajectoryStr,
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
            await rpc("/skoll/chat/save_response", params);
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
        this._clearChatTimeout();
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
            await rpc("/skoll/chat/save_response", params);
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

        const isHint = !!this._pendingHint;
        this._pendingHint = false;

        console.log(LOG_PREFIX, "onSend:", { text: text.substring(0, 100), isHint });

        this.state.inputText = "";
        this.state.sending = true;
        this._session.messages.push({ role: "user", text, isHint });
        this._scrollToBottom();
        requestAnimationFrame(() => {
            const el = this.mainTextareaRef.el;
            if (el) el.style.height = "auto";
        });

        this.state.activityText = "Running QC check…";
        let turnId = null;
        try {
            const createParams = {
                sandbox_id: this.props.sandboxId,
                message: text,
                timestamp: new Date().toISOString(),
            };
            if (isHint) createParams.is_hint = true;
            const r = await rpc("/skoll/chat/create_turn", createParams);
            turnId = r.turn_id;
            console.log(LOG_PREFIX, "Turn created:", turnId);
        } catch (e) {
            console.error(LOG_PREFIX, "Create turn failed:", e);
        }
        this._session.currentTurnId = turnId;

        let qcResult = null;
        try {
            const qcResponse = await rpc("/skoll/qc", { prompt: text });
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
                    const saveParams = {
                        turn_id: turnId,
                        severity: qcResult.severity || "",
                        qc_response: JSON.stringify(qcResult),
                    };
                    if (qcResponse.usage) {
                        saveParams.bedrock_input_tokens = qcResponse.usage.input_tokens || 0;
                        saveParams.bedrock_output_tokens = qcResponse.usage.output_tokens || 0;
                    }
                    rpc("/skoll/chat/save_qc", saveParams).catch(e => console.warn(LOG_PREFIX, "save_qc failed:", e));
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
            rpc("/skoll/chat/save_qc", {
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
        const messages = this.state.messages;
        const rejectedIdx = messages.findLastIndex(
            m => m.role === "user" && m.text === promptText
        );
        if (rejectedIdx >= 0) {
            messages.splice(rejectedIdx);
        }
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
        this._session.messages.push({ role: "assistant", text: "", pending: true, isModelResponse: true, turnId: this._session.currentTurnId });
        this.state.activityText = "Waiting for model…";
        this._session.streaming = true;
        this.state.streaming = true;
        this._startIncrementalSave();
        this._startChatTimeout();
        this._scrollToBottom();
    }

    _startChatTimeout() {
        this._clearChatTimeout();
        const turnId = this._session.currentTurnId;
        this._session.chatTimeoutHandle = setTimeout(() => {
            this._handleChatTimeout(turnId);
        }, CHAT_TIMEOUT_MS);
    }

    _clearChatTimeout() {
        if (this._session.chatTimeoutHandle) {
            clearTimeout(this._session.chatTimeoutHandle);
            this._session.chatTimeoutHandle = null;
        }
    }

    _handleChatTimeout(turnId) {
        if (!this._session.streaming) return;
        if (turnId && turnId !== this._session.currentTurnId) return;

        console.warn(LOG_PREFIX, "⏱️ chat timeout after", CHAT_TIMEOUT_MS, "ms turn=", turnId);

        try {
            if (this._session.ws && this._session.wsConnected) {
                const abortMsg = {
                    type: "req",
                    id: nextId(),
                    method: "chat.abort",
                    params: { sessionKey: "odoo:sandbox:" + this.props.sandboxId },
                };
                this._session.ws.send(JSON.stringify(abortMsg));
            }
        } catch (e) {
            console.warn(LOG_PREFIX, "chat.abort on timeout failed:", e);
        }

        const messages = this._session.messages;
        const session = this._session;
        const timeoutNote = "⏱️ Response timed out after 15 minutes. Please try again.";
        let msg = messages.findLast(m => m.pending);
        const partial = session._streamBuf || (msg ? msg.text : "");
        const finalText = partial ? partial + "\n\n" + timeoutNote : timeoutNote;
        if (msg) {
            msg.pending = false;
            msg.isError = true;
            msg.isTimeout = true;
            msg.text = finalText;
            msg.html = markup(renderMarkdown(finalText));
        } else {
            messages.push({
                role: "assistant",
                text: finalText,
                html: markup(renderMarkdown(finalText)),
                isError: true,
                isTimeout: true,
                pending: false,
            });
        }

        session._streamBuf = "";
        session._lastFlushedWordCount = 0;
        session.streaming = false;
        this._stopIncrementalSave();
        this.state.streaming = false;
        this.state.sending = false;
        this.state.activityText = "";
        this._clearChatTimeout();

        if (turnId) {
            rpc("/skoll/chat/mark_timeout", {
                turn_id: turnId,
                timeout_seconds: Math.floor(CHAT_TIMEOUT_MS / 1000),
            }).catch(e => console.warn(LOG_PREFIX, "mark_timeout RPC failed:", e));
        }

        session.currentTurnId = null;
        session.currentRunId = null;
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
        // Auto-scroll disabled — users reported it interferes with reading
        // while streaming. The chat container retains its scroll position.
    }

    onToggleTools(msg) {
        msg.toolsExpanded = !msg.toolsExpanded;
    }

    onToggleThinking(msg) {
        msg.thinkingExpanded = !msg.thinkingExpanded;
    }

    onToggleSubAgent(agent) {
        agent._expanded = !agent._expanded;
    }

    onOpenSubChat(msg) {
        const sessionKey = msg.sessionKey;
        const childData = this._session._childSessions[sessionKey] || {};
        this.state.subChatView = {
            sessionKey,
            agent: msg.agent || "",
            description: msg.description || "",
            messages: childData.messages || [],
        };
    }

    onBackFromSubChat() {
        this.state.subChatView = null;
    }

    getSubAgentPreview(agent, subAgentMessages) {
        if (agent.description) {
            const d = agent.description;
            return d.length > 100 ? d.substring(0, 97) + "…" : d;
        }
        const msgs = subAgentMessages?.[agent.sessionKey] || [];
        const firstUser = msgs.find(m => m.role === "user");
        if (firstUser && firstUser.text) {
            const text = firstUser.text;
            return text.length > 100 ? text.substring(0, 97) + "…" : text;
        }
        return agent.agent || "Sub-agent task";
    }

    _subscribeToSessions() {
        const ws = this._session.ws;
        if (!ws || !this._session.wsConnected) return;
        const msg = { type: "req", id: nextId(), method: "sessions.subscribe", params: {} };
        console.log(LOG_PREFIX, "📡 Subscribing to session lifecycle events");
        ws.send(JSON.stringify(msg));
    }

    _subscribeToChildSession(childKey) {
        const session = this._session;
        if (session._childSubscriptions.has(childKey)) return;
        const ws = session.ws;
        if (!ws || !session.wsConnected) return;
        session._childSubscriptions.add(childKey);
        const msg = { type: "req", id: nextId(), method: "sessions.messages.subscribe", params: { key: childKey } };
        console.log(LOG_PREFIX, `📡 Subscribing to child session: ${childKey}`);
        ws.send(JSON.stringify(msg));
    }

    _handleSessionsChanged(payload, widget) {
        const reason = payload.reason || payload.phase || "";
        const sessionData = payload.session || {};
        const sessionKey = payload.sessionKey || sessionData.key || "";

        console.log(LOG_PREFIX, `🔀 sessions.changed: reason=${reason} key=${sessionKey} kind=${sessionData.kind || ""}`);

        if (reason === "create" || (sessionKey.includes(":subagent:") && !this._session._childSessions[sessionKey])) {
            const childKey = sessionKey || sessionData.key || "";
            const isSubagent = childKey.includes(":subagent:") || sessionData.kind === "subagent";
            if (!isSubagent) return;
            if (this._session._childSessions[childKey]) return;

            const agent = sessionData.agent || "";
            const parentKey = sessionData.parentKey || "";
            const description = sessionData.title || sessionData.description || sessionData.displayName || "";

            console.log(LOG_PREFIX, `🔀 Sub-agent spawned: key=${childKey} agent=${agent}`);

            this._session._childSessions[childKey] = {
                agent,
                parentKey,
                description,
                status: "running",
                spawned_at: sessionData.createdAt || new Date().toISOString(),
                messages: [],
            };

            this._subscribeToChildSession(childKey);

            this._session.messages.push({
                type: "subagent_card",
                sessionKey: childKey,
                agent: agent || "general",
                description: description || agent || childKey.split(":subagent:").pop() || "Sub-agent task",
                status: "running",
            });
        } else if (reason === "update" || reason === "complete") {
            const childKey = sessionKey || sessionData.key || "";
            if (this._session._childSessions[childKey]) {
                this._markSubAgentCompleted(childKey);
            }
        }
    }

    _handleSessionMessage(payload, widget) {
        const sessionKey = payload.sessionKey || "";
        const message = payload.message || {};
        const role = typeof message === "object" ? (message.role || "unknown") : "unknown";

        let text = "";
        if (typeof message === "string") {
            text = message;
        } else if (typeof message.text === "string") {
            text = message.text;
        } else if (Array.isArray(message.content)) {
            const parts = [];
            for (const b of message.content) {
                if (!b || typeof b !== "object") continue;
                if (b.type === "text" && b.text) parts.push(b.text);
                else if (b.type === "tool_use" || b.type === "toolCall") parts.push(`[Tool: ${b.name || "unknown"}]`);
                else if (b.type === "tool_result" || b.type === "toolResult") {
                    const rt = b.text || (Array.isArray(b.content) ? b.content.filter(c => c?.text).map(c => c.text).join("") : "");
                    parts.push(rt || "[Tool result]");
                }
            }
            text = parts.join("\n");
        } else if (typeof message.content === "string") {
            text = message.content;
        }
        if (!text && role === "tool") {
            text = `[Tool response]`;
        }

        const childData = this._session._childSessions[sessionKey];
        if (!childData) return;
        if (!text) return;

        const entry = { sessionKey, role, text, timestamp: payload.timestamp || "" };
        childData.messages.push(entry);

        if (this.state.subChatView && this.state.subChatView.sessionKey === sessionKey) {
            this.state.subChatView.messages = [...childData.messages];
        }

        console.log(LOG_PREFIX, `🔀 Sub-agent msg: key=${sessionKey} role=${role} len=${text.length}`);
    }

    _markSubAgentCompleted(sessionKey) {
        const messages = this._session.messages;
        for (let i = messages.length - 1; i >= 0; i--) {
            const msg = messages[i];
            if (msg.type === "subagent_card" && msg.sessionKey === sessionKey) {
                msg.status = "completed";
                break;
            }
        }
        const childData = this._session._childSessions[sessionKey];
        if (childData) childData.status = "completed";
    }

    _checkForSubAgentSpawn(data, toolName) {
        let resultObj = data.result;
        if (typeof resultObj === "string") {
            try { resultObj = JSON.parse(resultObj); } catch { return; }
        }
        if (!resultObj || typeof resultObj !== "object") return;

        const childKey = resultObj.childSessionKey || resultObj.sessionKey || "";
        if (!childKey || !childKey.includes(":subagent:")) return;
        if (this._session._childSessions[childKey]) return;

        const taskName = resultObj.taskName || resultObj.description || toolName || "";
        const agent = resultObj.agent || toolName || "general";

        console.log(LOG_PREFIX, `🔀 Sub-agent detected from tool result: key=${childKey} task=${taskName}`);

        this._session._childSessions[childKey] = {
            agent,
            parentKey: "agent:main:odoo:sandbox:" + this.props.sandboxId,
            description: taskName,
            status: "running",
            spawned_at: new Date().toISOString(),
            messages: [],
        };

        this._session.messages.push({
            type: "subagent_card",
            sessionKey: childKey,
            agent,
            description: taskName || "Sub-agent task",
            status: "running",
        });

        this._subscribeToChildSession(childKey);
        this._session._activeChildKey = childKey;
    }

    _routeToolEventToChild(toolName, phase, args, result, isError) {
        const activeKey = this._session._activeChildKey;
        if (!activeKey) return;
        const childData = this._session._childSessions[activeKey];
        if (!childData || childData.status === "completed") return;

        let text = "";
        if (phase === "start") {
            text = `🔧 Running: ${toolName}`;
            if (args) {
                const argsStr = typeof args === "string" ? args : JSON.stringify(args);
                if (argsStr.length < 200) text += `\n${argsStr}`;
            }
        } else if (phase === "end") {
            const resultStr = typeof result === "string" ? result : JSON.stringify(result || "");
            text = `✅ ${toolName} completed`;
            if (resultStr && resultStr.length < 1000) {
                text += `:\n${resultStr}`;
            } else if (resultStr) {
                text += `:\n${resultStr.substring(0, 500)}…`;
            }
            if (isError) text = `❌ ${toolName} failed:\n${resultStr.substring(0, 500)}`;
        }

        if (!text) return;

        const entry = { sessionKey: activeKey, role: "assistant", text, timestamp: new Date().toISOString() };
        childData.messages.push(entry);

        if (this.state.subChatView && this.state.subChatView.sessionKey === activeKey) {
            this.state.subChatView.messages = [...childData.messages];
        }
    }

    async _detectChildSessions() {
        if (!this._session.wsConnected) return;
        const sandboxId = this.props.sandboxId;
        const mainKeyFragment = "odoo:sandbox:" + sandboxId;
        try {
            const res = await this._wsRpc("sessions.list", {}, 15000);
            const rawResult = res?.result || {};
            let sessions = [];
            if (Array.isArray(rawResult)) {
                sessions = rawResult;
            } else if (Array.isArray(rawResult.sessions)) {
                sessions = rawResult.sessions;
            } else if (typeof rawResult === "object") {
                sessions = Object.values(rawResult).filter(v => v && typeof v === "object" && (v.key || v.id));
            }

            console.log(LOG_PREFIX, `🔀 sessions.list returned ${sessions.length} sessions`);

            for (const s of sessions) {
                const key = s.key || s.id || "";
                if (!key) continue;
                if (key.includes(mainKeyFragment)) continue;
                if (!key.includes(":subagent:")) continue;
                if (this._session._childSessions[key]) continue;

                const agent = s.agent || "";
                const label = s.label || s.title || s.description || "";
                const description = label || agent || key.split(":subagent:").pop() || "Sub-agent";

                this._session._childSessions[key] = {
                    agent,
                    parentKey: mainKeyFragment,
                    description,
                    status: "completed",
                    messages: [],
                };

                this._session.messages.push({
                    type: "subagent_card",
                    sessionKey: key,
                    agent: agent || "general",
                    description,
                    status: "completed",
                });

                this._fetchChildHistory(key);
                console.log(LOG_PREFIX, `🔀 Detected child session: key=${key} agent=${agent} desc=${description}`);
            }
        } catch (e) {
            console.warn(LOG_PREFIX, "Failed to detect child sessions:", e);
        }
    }

    async _fetchChildHistory(sessionKey) {
        if (!this._session.wsConnected) return;
        try {
            const res = await this._wsRpc("chat.history", { sessionKey, limit: 500 }, 30000);
            const messages = res?.result?.messages || res?.messages || [];
            if (!Array.isArray(messages) || messages.length === 0) return;

            const childData = this._session._childSessions[sessionKey];
            if (!childData) return;

            for (const msg of messages) {
                const inner = msg?.message || msg;
                const role = inner?.role || "";
                if (!role || role === "system") continue;

                let text = "";
                if (typeof inner.text === "string") {
                    text = inner.text;
                } else if (Array.isArray(inner.content)) {
                    const parts = [];
                    for (const b of inner.content) {
                        if (!b || typeof b !== "object") continue;
                        if (b.type === "text" && b.text) {
                            parts.push(b.text);
                        } else if (b.type === "tool_use" || b.type === "toolCall") {
                            parts.push(`[Tool: ${b.name || "unknown"}]`);
                        } else if (b.type === "tool_result" || b.type === "toolResult") {
                            const resultText = b.text || (Array.isArray(b.content) ? b.content.filter(c => c?.text).map(c => c.text).join("") : "");
                            parts.push(resultText || `[Tool result]`);
                        } else if (b.type === "thinking" && b.thinking) {
                            parts.push(`[Thinking: ${b.thinking.substring(0, 100)}…]`);
                        }
                    }
                    text = parts.join("\n");
                } else if (typeof inner.content === "string") {
                    text = inner.content;
                }
                if (!text && role === "tool") {
                    text = `[Tool response for ${inner.tool_use_id || inner.toolCallId || "unknown"}]`;
                }
                if (!text) continue;

                childData.messages.push({ sessionKey, role, text, timestamp: msg.timestamp || "" });
            }

            if (this.state.subChatView && this.state.subChatView.sessionKey === sessionKey) {
                this.state.subChatView.messages = [...childData.messages];
            }

            console.log(LOG_PREFIX, `🔀 Fetched ${childData.messages.length} messages for child session: ${sessionKey}`);
        } catch (e) {
            console.warn(LOG_PREFIX, `Failed to fetch child history for ${sessionKey}:`, e);
        }
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
            const result = await rpc("/skoll/browser/screenshot", {
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
            const result = await rpc("/skoll/browser/inject_cookies", {
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

    onFeedbackUnsatisfied(msgIndex) {
        const msg = this.state.messages[msgIndex];
        if (!msg || msg.feedback) return;
        this.state.hintPopupVisible = true;
        this.state.hintText = "HINT: ";
        this.state.hintTargetMsgIndex = msgIndex;
        requestAnimationFrame(() => {
            const el = this.hintTextareaRef.el;
            if (el) {
                el.focus();
                el.setSelectionRange(el.value.length, el.value.length);
                this._autoResizeEl(el);
            }
        });
    }

    onHintSend() {
        let hint = this.state.hintText.trim();
        if (!hint) return;
        if (hint.toUpperCase().startsWith("HINT:")) {
            hint = hint.substring(5).trim();
        }
        if (!hint) return;
        this.state.hintPopupVisible = false;
        this.state.hintTargetMsgIndex = -1;
        this.state.hintText = "";

        this._pendingHint = true;
        this.state.inputText = hint;
        this._autoResizeMainTextarea();
        this.onSend();
    }

    onHintCancel() {
        const idx = this.state.hintTargetMsgIndex;
        if (idx >= 0 && this.state.messages[idx]) {
            this.state.messages[idx].feedback = undefined;
        }
        this.state.hintPopupVisible = false;
        this.state.hintText = "";
        this.state.hintTargetMsgIndex = -1;
    }

    onHintOverlayClick() {
        this.onHintCancel();
    }

    onHintKeydown(ev) {
        if (ev.key === "Enter" && !ev.shiftKey) {
            ev.preventDefault();
            this.onHintSend();
        }
        if (ev.key === "Escape") {
            this.onHintCancel();
        }
    }

    onHintTextareaInput(ev) {
        this._autoResizeEl(ev.target);
    }

    onMainTextareaInput(ev) {
        this._autoResizeEl(ev.target);
    }

    _autoResizeEl(el) {
        if (!el) return;
        el.style.height = "auto";
        el.style.height = Math.min(el.scrollHeight, 150) + "px";
    }

    _autoResizeMainTextarea() {
        requestAnimationFrame(() => {
            const el = this.mainTextareaRef.el;
            if (el) this._autoResizeEl(el);
        });
    }
}
