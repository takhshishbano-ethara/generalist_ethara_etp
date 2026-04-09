/** @odoo-module */
import { Component, useState, useRef, onMounted, onWillDestroy, onPatched } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { standardWidgetProps } from "@web/views/widgets/standard_widget_props";
import { rpc } from "@web/core/network/rpc";

const MODELS = [
    { id: "claude-opus-4.6", label: "Claude Opus 4.6" },
    { id: "kimi-k2.5", label: "Kimi K2.5" },
];

const LOG_PREFIX = "[talos-chat]";

let _msgId = 0;
function nextId() {
    return `odoo-${++_msgId}-${Date.now().toString(36)}`;
}

const _sessions = new Map();

function _getSession(taskId) {
    if (!_sessions.has(taskId)) {
        _sessions.set(taskId, {
            ws: null,
            wsConnected: false,
            messages: [],
            streaming: false,
            currentTurnId: null,
            historyLoaded: false,
        });
    }
    return _sessions.get(taskId);
}

export class TalosChatWidget extends Component {
    static template = "talos.ChatWidget";
    static props = { ...standardWidgetProps };

    setup() {
        this.notification = useService("notification");
        this.messagesEndRef = useRef("messagesEnd");

        const taskId = this.props.record.resId;
        this._session = _getSession(taskId);

        this.state = useState({
            messages: this._session.messages,
            inputText: "",
            sending: false,
            streaming: this._session.streaming,
            currentTurnId: this._session.currentTurnId,
            selectedModel: MODELS[0].id,
            connected: this._session.wsConnected,
            statusText: this._session.wsConnected ? "Connected" : "Initializing...",
        });

        this.models = MODELS;

        this._onWsMessage = (payload) => this._handleWsPayload(payload);

        onMounted(() => {
            console.log(LOG_PREFIX, "Widget mounted. taskId:", taskId,
                "sessionCached:", this._session.wsConnected,
                "messages:", this._session.messages.length);

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
        });

        onPatched(() => {
            if (this.isRunning && !this._session.wsConnected && !this._session.ws) {
                this._tryConnect();
            }
        });

        onWillDestroy(() => {
            console.log(LOG_PREFIX, "Widget unmounting (tab switch). WS stays alive.");
            if (this._session.ws) {
                this._session.ws._odooWidget = null;
            }
        });
    }

    get taskId() { return this.props.record.resId; }

    get isRunning() {
        const s = this.props.record.data.docker_status;
        return Array.isArray(s) ? s[0] === "running" : s === "running";
    }

    get gatewayPort() { return this.props.record.data.docker_port; }
    get gatewayToken() { return this.props.record.data.docker_gateway_token; }
    get gatewayWsUrl() { return this.props.record.data.docker_ws_url; }

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

            if (frame.type === "event" && frame.event === "agent") {
                console.log(LOG_PREFIX, "AGENT EVENT:", {
                    stream: frame.payload?.stream,
                    dataType: frame.payload?.data?.type,
                    phase: frame.payload?.data?.phase,
                    textLen: frame.payload?.data?.text?.length,
                });
                this._handleAgentEvent(frame.payload, widget);
                return;
            }

            if (frame.type === "res" && frame.id && this._session.wsConnected) {
                console.log(LOG_PREFIX, "RPC RESPONSE:", { id: frame.id, ok: frame.ok, error: frame.error });
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

    _handleAgentEvent(payload, widget) {
        const stream = payload?.stream || "";
        const data = payload?.data || {};
        const messages = this._session.messages;

        if (stream === "assistant" && data.type === "text") {
            const msg = messages.findLast(m => m.pending);
            if (msg) msg.text += data.text || "";
            if (widget) widget._scrollToBottom();
        } else if (stream === "lifecycle" && data.phase === "end") {
            console.log(LOG_PREFIX, "Agent lifecycle END");
            const msg = messages.findLast(m => m.pending);
            if (msg) msg.pending = false;
            this._session.streaming = false;
            if (widget) widget.state.streaming = false;
            this._saveResponse(msg ? msg.text : "");
        } else if (stream === "lifecycle" && data.phase === "error") {
            const errText = data.message || data.error || data.reason || JSON.stringify(data);
            console.error(LOG_PREFIX, "Agent lifecycle ERROR:", errText, "full data:", data);
            const msg = messages.findLast(m => m.pending);
            if (msg) {
                msg.pending = false;
                msg.text = errText;
                msg.isError = true;
            }
            this._session.streaming = false;
            if (widget) widget.state.streaming = false;
        }
    }

    async _loadHistory() {
        if (!this.taskId) return;
        if (this._session.historyLoaded) return;
        console.log(LOG_PREFIX, "Loading history for task", this.taskId);
        try {
            const result = await rpc("/talos/chat/history", { task_id: this.taskId });
            console.log(LOG_PREFIX, "History loaded:", result.turns?.length, "turns");
            if (result.turns) {
                this._session.messages.length = 0;
                for (const t of result.turns) {
                    if (t.prompt) this._session.messages.push({ role: "user", text: t.prompt, model: t.model });
                    if (t.response) this._session.messages.push({ role: "assistant", text: t.response, model: t.model, pending: false });
                }
            }
            this._session.historyLoaded = true;
        } catch (e) {
            console.error(LOG_PREFIX, "History load failed:", e);
        }
        this._scrollToBottom();
    }

    async _saveResponse(text) {
        if (!this._session.currentTurnId) {
            console.warn(LOG_PREFIX, "_saveResponse: no currentTurnId");
            return;
        }
        console.log(LOG_PREFIX, "Saving response for turn", this._session.currentTurnId);
        try {
            await rpc("/talos/chat/save_response", {
                turn_id: this._session.currentTurnId,
                response: text,
            });
        } catch (e) {
            console.error(LOG_PREFIX, "Save response failed:", e);
        }
        this._session.currentTurnId = null;
    }

    async onSend() {
        const text = this.state.inputText.trim();
        if (!text || this.state.sending || this._session.streaming) return;
        if (!this._session.wsConnected) {
            this.notification.add(`Not connected. ${this.state.statusText}`, { type: "warning" });
            return;
        }

        console.log(LOG_PREFIX, "onSend:", { text: text.substring(0, 100), model: this.state.selectedModel });

        this.state.inputText = "";
        this.state.sending = true;
        this._session.messages.push({ role: "user", text, model: this.state.selectedModel });
        this._scrollToBottom();

        console.log(LOG_PREFIX, "Running QC check...");
        let qcPassed = true;
        try {
            const qcResult = await rpc("/talos/qc", { prompt: text });
            console.log(LOG_PREFIX, "QC result:", qcResult);
            if (qcResult.error) {
                console.warn(LOG_PREFIX, "QC error, passing through:", qcResult.error);
            } else if (qcResult.parsed_json) {
                const qc = qcResult.parsed_json;
                if (qc.pass === false || qc.approved === false || qc.allowed === false) {
                    qcPassed = false;
                    const reason = qc.reason || qc.message || qcResult.response || "Prompt rejected by QC";
                    this._session.messages.push({
                        role: "assistant",
                        text: reason,
                        isError: true,
                        pending: false,
                    });
                    this.state.sending = false;
                    this._scrollToBottom();
                    return;
                }
            }
        } catch (e) {
            console.warn(LOG_PREFIX, "QC call failed, passing through:", e);
        }

        let turnId = null;
        try {
            const r = await rpc("/talos/chat/create_turn", { task_id: this.taskId, message: text, model: this.state.selectedModel });
            turnId = r.turn_id;
            console.log(LOG_PREFIX, "Turn created:", turnId);
        } catch (e) {
            console.error(LOG_PREFIX, "Create turn failed:", e);
        }
        this._session.currentTurnId = turnId;

        const chatSendMsg = {
            type: "req",
            id: nextId(),
            method: "chat.send",
            params: {
                message: text,
                sessionKey: "odoo:" + (this.taskId || "0"),
                deliver: false,
                idempotencyKey: crypto.randomUUID(),
            },
        };
        console.log(LOG_PREFIX, "SEND chat.send:", JSON.stringify(chatSendMsg));
        this._session.ws.send(JSON.stringify(chatSendMsg));

        this._session.messages.push({ role: "assistant", text: "", model: this.state.selectedModel, pending: true });
        this._session.streaming = true;
        this.state.streaming = true;
        this.state.sending = false;
        this._scrollToBottom();
    }

    onAbort() {
        if (!this._session.ws || !this._session.wsConnected) return;
        const abortMsg = { type: "req", id: nextId(), method: "chat.abort", params: {} };
        console.log(LOG_PREFIX, "SEND chat.abort:", JSON.stringify(abortMsg));
        this._session.ws.send(JSON.stringify(abortMsg));
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

export const talosChatWidgetDef = { component: TalosChatWidget };
registry.category("view_widgets").add("talos_chat", talosChatWidgetDef);
