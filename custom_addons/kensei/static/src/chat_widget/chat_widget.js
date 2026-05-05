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

const LOG_PREFIX = "[kensei-chat]";
const STREAM_WORD_THRESHOLD = 5;
const INCREMENTAL_SAVE_INTERVAL_MS = 3000;
const CHAT_TIMEOUT_MS = 30 * 60 * 1000;

const MAX_PENDING_ATTACHMENTS = 10;
const ALLOWED_IMAGE_TYPES = ["image/jpeg", "image/png", "image/gif", "image/webp"];
const ALLOWED_DOC_TYPES = [
    "application/pdf", "text/plain", "text/markdown",
    "text/html", "text/csv", "application/json",
];
const ALLOWED_VIDEO_TYPES = [];
const ALLOWED_TYPES = [...ALLOWED_IMAGE_TYPES, ...ALLOWED_DOC_TYPES, ...ALLOWED_VIDEO_TYPES];

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

function _wsUrlToHttpUrl(wsUrl) {
    if (!wsUrl) return null;
    let url = wsUrl.replace(/^ws(s?):\/\//, (_, s) => `http${s}://`);
    url = url.replace(/\/+$/, "");
    return url;
}

const HTTP_ONLY_TYPES = new Set([
    "application/pdf", "text/plain", "text/markdown",
    "text/html", "text/csv", "application/json",
    ...ALLOWED_VIDEO_TYPES,
]);

function _extractUrlsFromText(text) {
    if (!text) return [];
    const re = /https?:\/\/[^\s"'<>]+/gi;
    return text.match(re) || [];
}

// ─── MEDIA: token protocol (OpenClaw) ───
const MEDIA_TOKEN_RE = /\bMEDIA:\s*`?([^\s`\n]+)`?/gi;
const BARE_PATH_RE = /(?:^|\s|`)(\/home\/node\/\.openclaw\/(?:workspace|uploads|media)\/[^\s`\n"')]+\.(?:png|jpe?g|gif|webp|bmp|svg|mp4|webm|mov|mp3|wav|ogg|m4a|pdf))(?:\s|`|$|[.,;!?)])/gi;
const BARE_DIR_RE = /(?:^|\s|`)(\/home\/node\/\.openclaw\/(?:workspace|uploads|media)(?:\/[^\s`\n"')]*)?\/)\s*(?:\n|$)/gim;
const MEDIA_EXTENSIONS = /\.(?:png|jpe?g|gif|webp|bmp|svg|mp4|webm|mov|mp3|wav|ogg|m4a|pdf)$/i;
const RELATIVE_FILE_RE = /(?:^|\n)\s*([a-zA-Z0-9][a-zA-Z0-9_\-.]*\.(?:png|jpe?g|gif|webp|bmp|svg|mp4|webm|mov|mp3|wav|ogg|m4a|pdf))\b/g;

function _splitMediaFromText(text) {
    if (!text) return { cleanText: "", mediaUrls: [] };
    const mediaUrls = [];
    let match;
    MEDIA_TOKEN_RE.lastIndex = 0;
    while ((match = MEDIA_TOKEN_RE.exec(text)) !== null) {
        mediaUrls.push(match[1].trim());
    }
    BARE_PATH_RE.lastIndex = 0;
    while ((match = BARE_PATH_RE.exec(text)) !== null) {
        const path = match[1].trim();
        if (!mediaUrls.includes(path)) {
            mediaUrls.push(path);
        }
    }
    // Reconstruct full paths from directory + relative filenames
    BARE_DIR_RE.lastIndex = 0;
    const dirs = [];
    while ((match = BARE_DIR_RE.exec(text)) !== null) {
        dirs.push(match[1].trim());
    }
    const relativeNames = [];
    if (dirs.length > 0) {
        const baseDir = dirs[dirs.length - 1];
        RELATIVE_FILE_RE.lastIndex = 0;
        while ((match = RELATIVE_FILE_RE.exec(text)) !== null) {
            const fileName = match[1];
            const fullPath = baseDir + fileName;
            if (!mediaUrls.includes(fullPath)) {
                mediaUrls.push(fullPath);
                relativeNames.push(fileName);
            }
        }
    }
    let cleanText = text.replace(MEDIA_TOKEN_RE, "");
    if (mediaUrls.length > 0) {
        for (const mediaPath of mediaUrls) {
            const escaped = mediaPath.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
            cleanText = cleanText.replace(new RegExp(escaped, "g"), "");
        }
    }
    for (const name of relativeNames) {
        const escaped = name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
        cleanText = cleanText.replace(new RegExp(escaped, "g"), "");
    }
    for (const dir of dirs) {
        const escaped = dir.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
        cleanText = cleanText.replace(new RegExp(escaped, "g"), "");
    }
    cleanText = cleanText.replace(/\n{3,}/g, "\n\n").trim();
    return { cleanText, mediaUrls };
}

function _inferMediaType(url) {
    const lower = (url || "").toLowerCase();
    if (/\.(png|jpe?g|gif|webp|bmp|svg)(\?|$)/i.test(lower)) return "image";
    if (/\.(mp4|webm|mov|mkv|avi)(\?|$)/i.test(lower)) return "video";
    if (/\.(mp3|wav|ogg|m4a|flac|aac)(\?|$)/i.test(lower)) return "audio";
    if (/\.(pdf|docx?|xlsx?|pptx?|txt|md|csv|json|html?)(\?|$)/i.test(lower)) return "document";
    return "image";
}

function _buildAssistantMediaUrl(sandboxId, source) {
    if (!source) return null;
    if (/^https?:\/\//i.test(source)) return source;
    if (/^data:/i.test(source)) return source;
    if (!sandboxId) return null;
    const params = new URLSearchParams({ sandbox_id: sandboxId, source });
    return `/kensei/chat/media?${params.toString()}`;
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
                    console.warn("[KenseiChatWidget]", "Trajectory save on stop failed:", e);
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
        const history = await rpc("/kensei/chat/history", {
            sandbox_id: sandboxId,
        });
        const turns = history?.turns || [];
        if (turns.length > 0) {
            return turns[turns.length - 1].id;
        }
    } catch (e) {
        console.warn("[KenseiChatWidget]", "Could not resolve latest turn:", e);
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

export class KenseiChatWidget extends Component {
    static template = "kensei.ChatWidget";
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
        this.fileInputRef = useRef("fileInput");

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
            // Auto-hint loop state
            autoHintActive: false,
            autoHintIteration: 0,
            autoHintMaxRetries: 5,
            autoHintStatus: "",      // "evaluating" | "sending_hint" | "streaming" | ""
            autoHintGroupId: "",
            pendingAttachments: [],
            attachmentError: "",
            dragOver: false,
            uploadProgress: {},
        });

        this._onWsMessage = (payload) => this._handleWsPayload(payload);
        this._pendingHint = false;

        // Auto-hint bus listener
        this._onAutoHintResult = (ev) => {
            this._handleAutoHintResult(ev.detail);
        };
        this.env.bus.addEventListener("KENSEI:AUTO_HINT_RESULT", this._onAutoHintResult);

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
            this.env.bus.removeEventListener("KENSEI:AUTO_HINT_RESULT", this._onAutoHintResult);
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
            if (typeof raw === "string" && (raw === "HEARTBEAT_OK" || raw === "HEARTBEAT" || raw === "PONG")) return;

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
                    rpc("/kensei/chat/save_response", params).catch(e => console.warn(LOG_PREFIX, "Partial save on disconnect failed:", e));
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
        if (typeof dataText === "string" && dataText.includes("HEARTBEAT")) return;

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
                if (widget) widget.state.activityText = `Running ${toolName}…`;
                console.log(LOG_PREFIX, `🔧 Tool START: ${toolName} (${toolCallId}) — total tool calls now: ${session._toolCalls.length}`);
            } else if (phase === "end" && toolCallId) {
                _logToolCall(session, { toolCallId, name: toolName, result: data.result ?? data.error ?? data.partialResult, isError: !!(data.isError || data.error), phase: "end", source_event: "chat.tool" });
                console.log(LOG_PREFIX, `🔧 Tool END: ${toolName} (${toolCallId}) isError=${!!data.isError} result=${JSON.stringify(data.result || null).substring(0, 300)}`);
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
        } else if (stream === "thinking") {
            const thinkingText = data.text || data.delta || "";
            console.log(LOG_PREFIX, `🧠 THINKING STREAM: len=${thinkingText.length} delta_len=${(data.delta || "").length}`);
            if (widget) widget.state.activityText = "Thinking…";
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
                const textSource = session._streamBuf || msg.text || "";
                if (textSource) {
                    const { cleanText, mediaUrls } = _splitMediaFromText(textSource);
                    msg.text = cleanText;
                    msg.html = markup(renderMarkdown(cleanText));
                    if (mediaUrls.length > 0) {
                        const sandboxId = widget?.props?.sandboxId;
                        const mediaItems = msg.mediaItems || [];
                        for (const url of mediaUrls) {
                            const resolvedUrl = _buildAssistantMediaUrl(sandboxId, url);
                            if (resolvedUrl) {
                                mediaItems.push({ type: _inferMediaType(url), url: resolvedUrl, alt: url.split("/").pop() || "" });
                            }
                        }
                        if (mediaItems.length > 0) msg.mediaItems = mediaItems;
                        if (sandboxId) {
                            rpc("/kensei/chat/persist_output_media", {
                                sandbox_id: sandboxId,
                                media_paths: mediaUrls,
                            }).catch(e => console.warn(LOG_PREFIX, "persist_output_media failed:", e));
                        }
                    }
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
            session._streamBuf = "";
            session._lastFlushedWordCount = 0;
            session._thinkingBuf = "";
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
                    session._streamBuf = (session._streamBuf || "") + text;
                    msg.text = session._streamBuf;
                    msg.html = markup(renderMarkdown(session._streamBuf));
                }
            }
        } else if (state === "final") {
            const finalText = this._extractText(payload.message);
            const imageBlocks = this._extractImageBlocks(payload.message);
            const embeddedTools = this._extractToolCallsFromMessage(payload.message);
            console.group(`${LOG_PREFIX} ✅ FINAL`);
            console.log("text length:", finalText?.length);
            console.log("image blocks:", imageBlocks.length);
            console.log("embedded tools from message:", embeddedTools.length, embeddedTools.map(t => t.name));
            console.log("stream _toolCalls:", session._toolCalls.length, session._toolCalls.map(t => t.name));
            console.log("raw events:", session._rawEvents.length);
            console.log("current turn ID:", session.currentTurnId);
            console.log("full message:", JSON.stringify(payload.message || null).substring(0, 2000));
            console.groupEnd();
            const msg = messages.findLast(m => m.pending);
            if (msg) {
                let displayText = finalText || msg.text || "";
                const { cleanText, mediaUrls } = _splitMediaFromText(displayText);
                if (cleanText !== displayText) {
                    displayText = cleanText;
                }
                msg.text = displayText;
                session._streamBuf = displayText;
                msg.html = markup(renderMarkdown(displayText));
                msg.pending = false;

                const sandboxId = widget?.props?.sandboxId;
                const mediaItems = [];
                for (const url of mediaUrls) {
                    const resolvedUrl = _buildAssistantMediaUrl(sandboxId, url);
                    if (resolvedUrl) {
                        mediaItems.push({ type: _inferMediaType(url), url: resolvedUrl, alt: url.split("/").pop() || "" });
                    }
                }
                for (const block of imageBlocks) {
                    const src = block.url || (block.source?.data ? `data:${block.source.media_type || block.mimeType || "image/png"};base64,${block.source.data}` : null);
                    const resolvedUrl = src ? _buildAssistantMediaUrl(sandboxId, src) : null;
                    if (resolvedUrl) {
                        mediaItems.push({ type: "image", url: resolvedUrl, alt: block.alt || "", width: block.width, height: block.height });
                    }
                }
                if (mediaItems.length > 0) {
                    msg.mediaItems = mediaItems;
                }
                if (mediaUrls.length > 0 && sandboxId) {
                    rpc("/kensei/chat/persist_output_media", {
                        sandbox_id: sandboxId,
                        media_paths: mediaUrls,
                    }).catch(e => console.warn(LOG_PREFIX, "persist_output_media failed:", e));
                }
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
                .filter(b => b && typeof b === "object" && b.type === "text" && b.text)
                .map(b => b.text)
                .join("");
        }
        if (typeof message.content === "string") return message.content;
        if (message.role && message.content) {
            return this._extractText(message.content);
        }
        return JSON.stringify(message);
    }

    _extractImageBlocks(message) {
        if (!message || !Array.isArray(message.content)) return [];
        const images = [];
        for (const block of message.content) {
            if (!block || typeof block !== "object") continue;
            if (block.type === "image" && (block.url || block.source)) {
                images.push({
                    type: "image",
                    url: block.url || null,
                    source: block.source || null,
                    alt: block.alt || "",
                    mimeType: block.mimeType || "",
                    width: block.width || null,
                    height: block.height || null,
                });
            }
        }
        return images;
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
            const result = await rpc("/kensei/chat/history", { sandbox_id: this.props.sandboxId });
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
                        const { cleanText, mediaUrls } = _splitMediaFromText(t.response);
                        const mediaItems = [];
                        for (const url of mediaUrls) {
                            const resolvedUrl = _buildAssistantMediaUrl(this.props.sandboxId, url);
                            if (resolvedUrl) mediaItems.push({ type: _inferMediaType(url), url: resolvedUrl, alt: url.split("/").pop() || "" });
                        }
                        const msg = {
                            role: "assistant",
                            text: cleanText,
                            html: markup(renderMarkdown(cleanText + (isPartial ? "\n\n*(partial — response was interrupted)*" : ""))),
                            pending: false,
                            isModelResponse: true,
                            turnId: t.id,
                            feedback: t.feedback || null,
                        };
                        if (mediaItems.length > 0) msg.mediaItems = mediaItems;
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

        try {
            const sandboxData = await rpc("/kensei/chat/sandbox_state", { sandbox_id: this.props.sandboxId });
            if (sandboxData.auto_hint_status === "evaluating") {
                this.state.autoHintActive = true;
                this.state.autoHintIteration = sandboxData.auto_hint_iteration || 0;
                this.state.autoHintStatus = "evaluating";
                this.state.autoHintGroupId = sandboxData.auto_hint_group_id || "";
                this.state.sending = true;
            } else if (sandboxData.auto_hint_status && sandboxData.auto_hint_status !== "idle") {
                rpc("/kensei/chat/sandbox_state", { sandbox_id: this.props.sandboxId }).catch(() => {});
            }
        } catch (e) {
            console.warn(LOG_PREFIX, "Failed to read sandbox auto_hint state:", e);
        }

        this._scrollToBottom();
    }

    async _restoreSessionFromGateway() {
        const sessionKey = "odoo:sandbox:" + this.props.sandboxId;
        console.log(LOG_PREFIX, "🔄 _restoreSessionFromGateway: sessionKey=", sessionKey);
        try {
            const res = await this._wsRpc("chat.history", { sessionKey, limit: 200 }, 60000);
            const messages = res?.result?.messages || res?.messages || [];
            console.log(LOG_PREFIX, `🔄 Gateway returned ${messages.length} messages`);
            if (messages.length === 0) return;

            let lastAssistantText = "";
            let lastAssistantImageBlocks = [];
            const toolCalls = {};
            for (const msg of messages) {
                const inner = msg?.message || msg;
                const role = inner?.role || "";
                const content = inner?.content;
                if (role === "assistant") {
                    lastAssistantImageBlocks = [];
                    if (typeof content === "string") {
                        lastAssistantText = content;
                    } else if (Array.isArray(content)) {
                        let text = "";
                        for (const block of content) {
                            if (!block || typeof block !== "object") continue;
                            if (block.type === "text") text += block.text || "";
                            if (block.type === "image" && (block.url || block.source)) {
                                lastAssistantImageBlocks.push(block);
                            }
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
                const { cleanText, mediaUrls } = _splitMediaFromText(lastAssistantText);
                const mediaItems = [];
                for (const url of mediaUrls) {
                    const resolvedUrl = _buildAssistantMediaUrl(this.props.sandboxId, url);
                    if (resolvedUrl) mediaItems.push({ type: _inferMediaType(url), url: resolvedUrl, alt: url.split("/").pop() || "" });
                }
                for (const block of lastAssistantImageBlocks) {
                    const src = block.url || (block.source?.data ? `data:${block.source.media_type || block.mimeType || "image/png"};base64,${block.source.data}` : null);
                    const resolvedUrl = src ? _buildAssistantMediaUrl(this.props.sandboxId, src) : null;
                    if (resolvedUrl) mediaItems.push({ type: "image", url: resolvedUrl, alt: block.alt || "" });
                }

                if (existingAssistant) {
                    existingAssistant.text = cleanText;
                    existingAssistant.html = markup(renderMarkdown(cleanText));
                    existingAssistant.pending = false;
                    const tcArr = Object.values(toolCalls);
                    if (tcArr.length > 0) existingAssistant.toolCalls = tcArr;
                    if (mediaItems.length > 0) existingAssistant.mediaItems = mediaItems;
                } else {
                    const newMsg = {
                        role: "assistant",
                        text: cleanText,
                        html: markup(renderMarkdown(cleanText)),
                        pending: false,
                        isModelResponse: true,
                        turnId: this._session.currentTurnId,
                    };
                    const tcArr = Object.values(toolCalls);
                    if (tcArr.length > 0) newMsg.toolCalls = tcArr;
                    if (mediaItems.length > 0) newMsg.mediaItems = mediaItems;
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

        if (turnId && this._session.wsConnected) {
            if (this.state.autoHintIteration < this.state.autoHintMaxRetries) {
                this._triggerAutoHintEval(turnId);
            } else if (this.state.autoHintActive) {
                this._endAutoHintLoop("max_retries");
            }
        }
    }

    async _fetchTrajectory(turnId) {
        if (!turnId) return;
        const sessionKey = "odoo:sandbox:" + this.props.sandboxId;
        const maxAttempts = 3;

        for (let attempt = 1; attempt <= maxAttempts; attempt++) {
            console.log(LOG_PREFIX, `📜 _fetchTrajectory attempt ${attempt}/${maxAttempts}: turnId=${turnId}`);
            try {
                const res = await this._wsRpc("chat.history", { sessionKey, limit: 1000 }, 60000);
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
                await rpc("/kensei/chat/save_trajectory", {
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
            await rpc("/kensei/chat/save_response", params);
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
            await rpc("/kensei/chat/save_response", params);
            console.log(LOG_PREFIX, `💾 Incremental save: turn=${session.currentTurnId} text=${currentText.length}chars tools=${toolCalls?.length || 0}`);
        } catch (e) {
            console.warn(LOG_PREFIX, "Incremental save failed:", e);
        }
    }

    async _triggerAutoHintEval(turnId) {
        if (!turnId || !this.props.sandboxId) return;
        const msg = this.state.messages.findLast(m => m.isModelResponse && m.turnId === turnId);
        if (msg && msg.feedback) return;

        this.state.autoHintActive = true;
        this.state.autoHintStatus = "evaluating";
        this.state.autoHintIteration++;
        this.state.sending = true;

        if (this._autoHintTimeout) clearTimeout(this._autoHintTimeout);
        this._autoHintTimeout = setTimeout(() => {
            if (this.state.autoHintActive && this.state.autoHintStatus === "evaluating") {
                console.warn(LOG_PREFIX, "Auto hint eval timed out after 10 minutes");
                this._endAutoHintLoop("error");
            }
        }, 600000);

        try {
            const result = await rpc("/kensei/auto_hint_eval", {
                turn_id: turnId,
                sandbox_id: this.props.sandboxId,
            });
            if (result && result.error) {
                console.error(LOG_PREFIX, "Auto hint eval returned error:", result.error);
                this._endAutoHintLoop("error");
            } else if (result && result.status === "max_retries") {
                console.warn(LOG_PREFIX, "Auto hint eval: max retries reached");
                this._endAutoHintLoop("max_retries");
            }
        } catch (e) {
            console.error(LOG_PREFIX, "Auto hint eval request failed:", e);
            this._endAutoHintLoop("error");
        }
    }

    _handleAutoHintResult(payload) {
        if (!payload || payload.sandbox_id !== this.props.sandboxId) return;

        if (payload.status === "satisfied") {
            const msg = this.state.messages.findLast(m => m.isModelResponse && !m.feedback);
            if (msg) {
                msg.feedback = "satisfied";
                if (msg.turnId) {
                    rpc("/kensei/chat/save_feedback", { turn_id: msg.turnId, feedback: "satisfied" })
                        .catch(e => console.warn(LOG_PREFIX, "auto-hint save_feedback satisfied failed:", e));
                }
            }
            if (payload.reasoning) {
                this._session.messages.push({
                    role: "assistant",
                    text: `Auto-review passed: ${payload.reasoning}`,
                    isAutoHint: true,
                    isAutoHintVerdict: true,
                    autoHintIteration: payload.iteration,
                    pending: false,
                });
            }
            this._endAutoHintLoop("satisfied");
        } else if (payload.status === "unsatisfied") {
            if (payload.reasoning) {
                this._session.messages.push({
                    role: "assistant",
                    text: `Auto-review (${payload.iteration}/5): ${payload.reasoning}`,
                    isAutoHint: true,
                    isAutoHintVerdict: true,
                    autoHintIteration: payload.iteration,
                    pending: false,
                });
            }
            this._scrollToBottom();
            this._sendAutoHint(payload.hint, payload.turn_id, payload.group_id, payload.iteration);
        } else if (payload.status === "max_retries") {
            this._session.messages.push({
                role: "assistant",
                text: `Auto-review reached maximum attempts (${payload.iteration}/5). Needs human review.`,
                isAutoHint: true,
                isAutoHintVerdict: true,
                pending: false,
            });
            this._endAutoHintLoop("max_retries");
        } else if (payload.status === "error") {
            this._endAutoHintLoop("error");
        }
    }

    async _sendAutoHint(hint, evalTurnId, groupId, iteration) {
        if (!hint || !this._session.wsConnected) {
            this._endAutoHintLoop("error");
            return;
        }

        this.state.autoHintStatus = "sending_hint";

        this._session.messages.push({ role: "user", text: hint, isHint: true, isAutoHint: true });
        this._scrollToBottom();

        let turnId = null;
        try {
            const r = await rpc("/kensei/chat/create_turn", {
                sandbox_id: this.props.sandboxId,
                message: hint,
                timestamp: new Date().toISOString(),
                is_hint: true,
                is_auto_hint: true,
                auto_hint_iteration: iteration + 1,
                auto_hint_group_id: groupId,
            });
            turnId = r.turn_id;
        } catch (e) {
            console.error(LOG_PREFIX, "Auto hint create_turn failed:", e);
            this._endAutoHintLoop("error");
            return;
        }

        this._session.currentTurnId = turnId;

        rpc("/kensei/chat/save_feedback", {
            turn_id: evalTurnId,
            feedback: "unsatisfied",
            hint_text: hint,
        }).catch(e => console.warn(LOG_PREFIX, "Auto hint save_feedback failed:", e));

        this.state.autoHintStatus = "streaming";
        this._sendToOpenClaw(hint);
    }

    _endAutoHintLoop(reason) {
        if (this._autoHintTimeout) {
            clearTimeout(this._autoHintTimeout);
            this._autoHintTimeout = null;
        }
        this.state.autoHintActive = false;
        this.state.autoHintIteration = 0;
        this.state.autoHintStatus = "";
        this.state.autoHintGroupId = "";
        this.state.sending = false;

        if (reason === "error") {
            this._session.messages.push({
                role: "assistant",
                text: "Auto-review encountered an error. Please review manually.",
                isAutoHint: true,
                isError: true,
                pending: false,
            });
        }
        this._scrollToBottom();
    }

    get acceptedFileTypes() {
        return ALLOWED_TYPES.join(",");
    }

    formatFileSize(bytes) {
        if (bytes < 1024) return bytes + " B";
        if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KB";
        return (bytes / (1024 * 1024)).toFixed(1) + " MB";
    }

    onAttachClick() {
        const input = this.fileInputRef.el;
        if (input) { input.value = ""; input.click(); }
    }

    onFileSelected(event) {
        const files = event.target.files;
        if (files && files.length) this._processFiles(files);
    }

    onDragOver(event) {
        event.preventDefault();
        event.stopPropagation();
        this.state.dragOver = true;
    }

    onDragLeave(event) {
        event.preventDefault();
        event.stopPropagation();
        this.state.dragOver = false;
    }

    onDrop(event) {
        event.preventDefault();
        event.stopPropagation();
        this.state.dragOver = false;
        const files = event.dataTransfer?.files;
        if (files && files.length) this._processFiles(files);
    }

    onPaste(event) {
        const items = event.clipboardData?.items;
        if (!items) return;
        const imageFiles = [];
        for (const item of items) {
            if (item.kind === "file" && ALLOWED_IMAGE_TYPES.includes(item.type)) {
                const file = item.getAsFile();
                if (file) imageFiles.push(file);
            }
        }
        if (imageFiles.length > 0) {
            event.preventDefault();
            this._processFiles(imageFiles);
        }
    }

    onRemoveAttachment(index) {
        const att = this.state.pendingAttachments[index];
        if (att && att.previewUrl) URL.revokeObjectURL(att.previewUrl);
        this.state.pendingAttachments.splice(index, 1);
        this.state.attachmentError = "";
    }

    _processFiles(fileList) {
        this.state.attachmentError = "";
        const files = Array.from(fileList);

        if (this.state.pendingAttachments.length + files.length > MAX_PENDING_ATTACHMENTS) {
            this.state.attachmentError = `Maximum ${MAX_PENDING_ATTACHMENTS} attachments allowed.`;
            return;
        }

        for (const file of files) {
            if (!ALLOWED_TYPES.includes(file.type)) {
                this.state.attachmentError = `"${file.name}" has unsupported type: ${file.type || "unknown"}`;
                continue;
            }
            const previewUrl = ALLOWED_IMAGE_TYPES.includes(file.type)
                ? URL.createObjectURL(file) : null;
            this.state.pendingAttachments.push({
                id: crypto.randomUUID(),
                file,
                name: file.name,
                mimeType: file.type,
                size: file.size,
                previewUrl,
            });
        }
    }

    _fileToRawBase64(file) {
        return new Promise((resolve, reject) => {
            const reader = new FileReader();
            reader.onload = () => {
                // OpenClaw expects raw base64 (strip "data:<mime>;base64," prefix)
                const dataUri = reader.result;
                const commaIdx = dataUri.indexOf(",");
                resolve(commaIdx >= 0 ? dataUri.slice(commaIdx + 1) : dataUri);
            };
            reader.onerror = reject;
            reader.readAsDataURL(file);
        });
    }

    async _buildAttachmentsPayload() {
        const attachments = [];
        for (const att of this.state.pendingAttachments) {
            const rawBase64 = await this._fileToRawBase64(att.file);
            attachments.push({
                type: att.mimeType.startsWith("image/") ? "image" : "file",
                fileName: att.name,
                mimeType: att.mimeType,
                content: rawBase64,
            });
        }
        return attachments;
    }

    async onSend() {
        const text = this.state.inputText.trim();
        const hasAttachments = this.state.pendingAttachments.length > 0;
        if ((!text && !hasAttachments) || this.state.sending || this._session.streaming) return;

        if (this.state.autoHintActive) {
            this._endAutoHintLoop("manual_override");
        }
        this.state.autoHintIteration = 0;
        this.state.autoHintGroupId = "";

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

        let attachmentsPayload = [];
        let attachmentsMeta = [];
        if (hasAttachments) {
            this.state.sending = true;
            try {
                attachmentsPayload = await this._buildAttachmentsPayload();
                attachmentsMeta = this.state.pendingAttachments.map(a => ({
                    name: a.name, mimeType: a.mimeType, size: a.size
                }));
            } catch (e) {
                this.state.attachmentError = `Failed to encode attachments: ${e.message}`;
                this.state.sending = false;
                return;
            }
        }

        const isHint = !!this._pendingHint;
        this._pendingHint = false;

        console.log(LOG_PREFIX, "onSend:", { text: text.substring(0, 100), isHint, attachments: attachmentsMeta.length });

        this.state.inputText = "";
        this.state.sending = true;
        const userMsg = { role: "user", text, isHint };
        if (this.state.pendingAttachments.length > 0) {
            userMsg.attachments = this.state.pendingAttachments.map(a => ({
                name: a.name, mimeType: a.mimeType, size: a.size, previewUrl: a.previewUrl,
            }));
        }
        this._session.messages.push(userMsg);
        this.state.pendingAttachments = [];
        this.state.attachmentError = "";
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
            if (attachmentsMeta.length > 0) {
                createParams.attachments_meta = JSON.stringify(attachmentsMeta);
            }
            const r = await rpc("/kensei/chat/create_turn", createParams);
            turnId = r.turn_id;
            console.log(LOG_PREFIX, "Turn created:", turnId);
        } catch (e) {
            console.error(LOG_PREFIX, "Create turn failed:", e);
        }
        this._session.currentTurnId = turnId;

        if (attachmentsPayload.length > 0) {
            rpc("/kensei/chat/persist_attachments", {
                sandbox_id: this.props.sandboxId,
                attachments: attachmentsPayload,
            }).catch(e => console.warn(LOG_PREFIX, "persist_attachments failed:", e));
        }

        let qcResult = null;
        try {
            const qcResponse = await rpc("/kensei/qc", { prompt: text });
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
                    rpc("/kensei/chat/save_qc", saveParams).catch(e => console.warn(LOG_PREFIX, "save_qc failed:", e));
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

        this._sendToOpenClaw(text, attachmentsPayload);
    }

    onQcDismiss() {
        const severity = this.state.qcResult?.severity;
        if (severity === "critical") return;
        if (severity === "high" && !this.state.qcDismissReason.trim()) return;

        const turnId = this._session.currentTurnId;
        const reason = this.state.qcDismissReason.trim();

        if (turnId && reason) {
            rpc("/kensei/chat/save_qc", {
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

    async _sendToOpenClaw(text, attachments = []) {
        const hasNonImageAttachment = attachments.some(a => HTTP_ONLY_TYPES.has(a.mimeType));
        if (hasNonImageAttachment) {
            return this._sendViaHttpResponses(text, attachments);
        }

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
        if (attachments.length > 0) {
            chatSendMsg.params.attachments = attachments;
        }
        console.group(`${LOG_PREFIX} ➡️ SEND chat.send`);
        console.log("Message:", text.substring(0, 200), "| Attachments:", attachments.length);
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

    async _sendViaHttpResponses(text, attachmentsPayload) {
        const httpBase = _wsUrlToHttpUrl(this.gatewayWsUrl);
        const token = this.gatewayToken;
        if (!httpBase || !token) {
            this._session.messages.push({
                role: "assistant",
                text: "Cannot send documents: missing gateway URL or token.",
                html: markup(renderMarkdown("Cannot send documents: missing gateway URL or token.")),
                isError: true, pending: false,
            });
            this._scrollToBottom();
            return;
        }

        const sessionKey = "odoo:sandbox:" + this.props.sandboxId;
        const content = [];
        if (text) {
            content.push({ type: "input_text", text });
        }
        for (const att of attachmentsPayload) {
            if (att.mimeType.startsWith("image/")) {
                content.push({
                    type: "input_image",
                    source: { type: "base64", media_type: att.mimeType, data: att.content },
                });
            } else {
                content.push({
                    type: "input_file",
                    source: { type: "base64", media_type: att.mimeType, data: att.content, filename: att.fileName },
                });
            }
        }

        const body = {
            input: [{ type: "message", role: "user", content }],
            model: "openclaw",
            stream: true,
        };

        console.group(`${LOG_PREFIX} ➡️ HTTP /v1/responses`);
        console.log("URL:", `${httpBase}/v1/responses`);
        console.log("Content parts:", content.length, content.map(c => c.type));
        console.groupEnd();

        this._session.messages.push({ role: "assistant", text: "", pending: true, isModelResponse: true, turnId: this._session.currentTurnId });
        this.state.activityText = "Waiting for model…";
        this._session.streaming = true;
        this.state.streaming = true;
        this._session._streamBuf = "";
        this._startChatTimeout();
        this._scrollToBottom();

        try {
            const resp = await fetch(`${httpBase}/v1/responses`, {
                method: "POST",
                headers: {
                    "Content-Type": "application/json",
                    "Authorization": `Bearer ${token}`,
                    "X-OpenClaw-Session-Key": sessionKey,
                },
                body: JSON.stringify(body),
            });

            if (!resp.ok) {
                const errBody = await resp.text();
                throw new Error(`HTTP ${resp.status}: ${errBody.substring(0, 500)}`);
            }

            const reader = resp.body.getReader();
            const decoder = new TextDecoder();
            let sseBuffer = "";
            const session = this._session;
            const messages = session.messages;

            while (true) {
                const { done, value } = await reader.read();
                if (done) break;
                sseBuffer += decoder.decode(value, { stream: true });

                const lines = sseBuffer.split("\n");
                sseBuffer = lines.pop();

                for (const line of lines) {
                    if (!line.startsWith("data: ")) continue;
                    const jsonStr = line.slice(6);
                    if (jsonStr === "[DONE]") continue;

                    let event;
                    try { event = JSON.parse(jsonStr); } catch { continue; }

                    this._handleHttpResponseEvent(event, session, messages);
                }
            }

            if (sseBuffer.startsWith("data: ")) {
                const jsonStr = sseBuffer.slice(6);
                if (jsonStr !== "[DONE]") {
                    try {
                        const event = JSON.parse(jsonStr);
                        this._handleHttpResponseEvent(event, session, messages);
                    } catch {}
                }
            }

            const msg = messages.findLast(m => m.pending);
            if (msg) {
                if (session._streamBuf) {
                    msg.text = session._streamBuf;
                    msg.html = markup(renderMarkdown(session._streamBuf));
                }
                msg.pending = false;
            }
        } catch (e) {
            console.error(LOG_PREFIX, "HTTP /v1/responses failed:", e);
            const msg = this._session.messages.findLast(m => m.pending);
            if (msg) {
                msg.pending = false;
                msg.text = `Error: ${e.message}`;
                msg.html = markup(renderMarkdown(`Error: ${e.message}`));
                msg.isError = true;
            } else {
                this._session.messages.push({
                    role: "assistant", text: `Error: ${e.message}`,
                    html: markup(renderMarkdown(`Error: ${e.message}`)),
                    isError: true, pending: false,
                });
            }
        } finally {
            this._session.streaming = false;
            this._session._streamBuf = "";
            this._session._lastFlushedWordCount = 0;
            this._clearChatTimeout();
            this._stopIncrementalSave();
            this.state.streaming = false;
            this.state.sending = false;
            this.state.activityText = "";
            this._scrollToBottom();

            const savedTurnId = this._session.currentTurnId;
            const finalMsg = this._session.messages.findLast(m => m.isModelResponse && m.turnId === savedTurnId);
            if (finalMsg && finalMsg.text && savedTurnId) {
                rpc("/kensei/chat/save_response", {
                    turn_id: savedTurnId,
                    response: finalMsg.text,
                    tool_calls: "[]",
                    raw_events: "[]",
                }).catch(e => console.warn(LOG_PREFIX, "save_response after HTTP failed:", e));
            }
        }
    }

    _handleHttpResponseEvent(event, session, messages) {
        const msg = messages.findLast(m => m.pending);
        if (!msg) return;

        if (event.type === "response.output_text.delta" && event.delta) {
            session._streamBuf = (session._streamBuf || "") + event.delta;
            msg.text = session._streamBuf;
            msg.html = markup(renderMarkdown(session._streamBuf));
            this.state.activityText = "";
            this._scrollToBottom();
        } else if (event.type === "response.output_text.done" && event.text) {
            session._streamBuf = event.text;
            msg.text = session._streamBuf;
            msg.html = markup(renderMarkdown(session._streamBuf));
        } else if (event.type === "response.completed" || event.type === "response.done") {
            const output = event.response?.output;
            if (Array.isArray(output)) {
                for (const item of output) {
                    if (item.type === "message" && Array.isArray(item.content)) {
                        for (const block of item.content) {
                            if (block.type === "output_text" && block.text) {
                                session._streamBuf = block.text;
                                msg.text = session._streamBuf;
                                msg.html = markup(renderMarkdown(session._streamBuf));
                            }
                        }
                    }
                }
            }
        }
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
        const timeoutNote = "⏱️ Response timed out after 30 minutes. Please try again.";
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
            rpc("/kensei/chat/mark_timeout", {
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
            const result = await rpc("/kensei/browser/screenshot", {
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
            const result = await rpc("/kensei/browser/inject_cookies", {
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

    onFeedbackSatisfied(msgIndex) {
        if (this.state.autoHintActive) {
            this._endAutoHintLoop("manual_override");
        }
        const msg = this.state.messages[msgIndex];
        if (!msg || msg.feedback) return;
        msg.feedback = "satisfied";
        if (msg.turnId) {
            rpc("/kensei/chat/save_feedback", { turn_id: msg.turnId, feedback: "satisfied" })
                .catch(e => console.warn(LOG_PREFIX, "save_feedback failed:", e));
        }
    }

    onFeedbackUnsatisfied(msgIndex) {
        if (this.state.autoHintActive) {
            this._endAutoHintLoop("manual_override");
        }
        const msg = this.state.messages[msgIndex];
        if (!msg || msg.feedback) return;
        msg.feedback = "unsatisfied";
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
        const msgIndex = this.state.hintTargetMsgIndex;
        const msg = msgIndex >= 0 ? this.state.messages[msgIndex] : null;
        if (msg && msg.turnId) {
            rpc("/kensei/chat/save_feedback", { turn_id: msg.turnId, feedback: "unsatisfied", hint_text: hint })
                .catch(e => console.warn(LOG_PREFIX, "save_feedback failed:", e));
        }
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
