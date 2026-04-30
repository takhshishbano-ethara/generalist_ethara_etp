/** @odoo-module */
import { Component, useState, onWillStart, onWillUpdateProps } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";
import { rpc } from "@web/core/network/rpc";
import { _t } from "@web/core/l10n/translation";

// ============================================================================
// SANDBOX CHAT WIDGET (COMMENTED OUT - replaced by manual input flow)
// To restore, uncomment this import + `static components = { AtlasChatWidget }`
// below, and revert the template change in sandbox_card.xml.
//
// import { AtlasChatWidget } from "../../chat_widget/chat_widget";
// ============================================================================

const SEVERITY_COLOR = {
    low: "success",
    medium: "warning",
    high: "danger",
    critical: "danger",
};

export class SandboxCard extends Component {
    static template = "atlas.SandboxCard";
    // SANDBOX: static components = { AtlasChatWidget };
    static components = {};
    static props = {
        sandboxId: Number,
        taskId: Number,
        taskEmail: { type: [String, Boolean], optional: true },
        modelType: String,
        modelLabel: String,
        dockerStatus: String,
        sessionStatus: String,
        dockerWsUrl: { type: [String, Boolean], optional: true },
        gatewayToken: { type: [String, Boolean], optional: true },
        dockerError: { type: [String, Boolean], optional: true },
        disabled: Boolean,
        loading: Boolean,
        record: { type: Object, optional: true },
        onStart: Function,
        onStop: Function,
    };

    setup() {
        this.notification = useService("notification");
        this.orm = useService("orm");

        this.state = useState({
            turns: [],
            historyLoading: false,
            sessionEnsured: false,

            promptText: "",
            qcRunning: false,
            qcError: "",
            qcResult: null,
            currentTurnId: null,

            gateMode: "",
            rewriteText: "",
            justificationText: "",
            gateResolved: false,

            responseText: "",
            responseSaving: false,
            responseSaved: false,
            responseError: "",

            goalRubricRunning: false,
            goalRubricError: "",
        });

        onWillStart(async () => {
            await this.loadHistory();
        });

        onWillUpdateProps(async (nextProps) => {
            const prevId = this.props.sandboxId;
            const nextId = nextProps.sandboxId;
            if (nextId && nextId !== prevId) {
                this.state.sessionEnsured = false;
                this.state.currentTurnId = null;
                this.state.qcResult = null;
                this.state.qcError = "";
                this.state.gateMode = "";
                this.state.rewriteText = "";
                this.state.justificationText = "";
                this.state.gateResolved = false;
                this.state.responseText = "";
                this.state.responseSaved = false;
                this.state.responseError = "";
                try {
                    const res = await rpc("/atlas/chat/history", { sandbox_id: nextId });
                    if (res && !res.error && Array.isArray(res.turns)) {
                        this.state.turns = res.turns;
                    } else {
                        this.state.turns = [];
                    }
                } catch (err) {
                    console.warn("loadHistory (prop update) failed:", err);
                    this.state.turns = [];
                }
            }
        });
    }

    get isRunning() {
        return this.props.dockerStatus === "running";
    }
    get isStarting() {
        return this.props.dockerStatus === "starting";
    }
    get isStopped() {
        return this.props.dockerStatus === "stopped";
    }
    get statusColor() {
        const map = { running: "success", starting: "info", stopped: "secondary", error: "danger" };
        return map[this.props.dockerStatus] || "secondary";
    }

    onStartClick() {
        this.props.onStart(this.props.sandboxId);
    }
    onStopClick() {
        this.props.onStop(this.props.sandboxId);
    }
    onExportClick() {
        if (this.props.sandboxId) {
            window.open(`/atlas/chat/export_session?sandbox_id=${this.props.sandboxId}`, "_blank");
        }
    }

    get canGenerateQc() {
        return (
            !this.state.qcRunning &&
            !this.state.responseSaving &&
            !!this.state.promptText.trim()
        );
    }
    get canSaveResponse() {
        return (
            !this.state.responseSaving &&
            !!this.state.currentTurnId &&
            !!this.state.responseText.trim() &&
            this.state.gateResolved
        );
    }
    get qcSeverityColor() {
        if (!this.state.qcResult) return "secondary";
        const sev = String(this.state.qcResult.severity || "").toLowerCase();
        return SEVERITY_COLOR[sev] || "secondary";
    }

    get showRewriteBox() {
        return this.state.gateMode === "rewrite" || this.state.gateMode === "forced_rewrite";
    }
    get showJustificationBox() {
        return this.state.gateMode === "justify";
    }
    get rewriteIsMandatory() {
        return this.state.gateMode === "forced_rewrite";
    }
    get canSelectGate() {
        const sev = String(this.state.qcResult && this.state.qcResult.severity || "").toLowerCase();
        return sev === "medium" && !this.state.gateResolved;
    }
    get canRerunQc() {
        return !this.state.qcRunning && !!this.state.rewriteText.trim() && this.showRewriteBox;
    }
    get canSubmitJustification() {
        return (
            this.state.gateMode === "justify" &&
            !!this.state.justificationText.trim() &&
            !this.state.qcRunning
        );
    }

    severityColor(sev) {
        return SEVERITY_COLOR[String(sev || "").toLowerCase()] || "secondary";
    }

    async loadHistory() {
        if (!this.props.sandboxId) {
            this.state.turns = [];
            return;
        }
        this.state.historyLoading = true;
        try {
            const res = await rpc("/atlas/chat/history", {
                sandbox_id: this.props.sandboxId,
            });
            if (res && !res.error && Array.isArray(res.turns)) {
                this.state.turns = res.turns;
            }
        } catch (err) {
            console.warn("loadHistory failed:", err);
        } finally {
            this.state.historyLoading = false;
        }
    }

    async _ensureSession(force = false) {
        if (!this.props.sandboxId) return null;
        if (this.state.sessionEnsured && !force) return null;
        const res = await rpc("/atlas/chat/new_session", {
            sandbox_id: this.props.sandboxId,
            force,
        });
        if (res && res.error) throw new Error(res.error);
        this.state.sessionEnsured = true;
        return res && res.session_id ? res.session_id : null;
    }

    async onNewSession() {
        if (this.state.qcRunning || this.state.responseSaving) return;
        try {
            await this._ensureSession(true);
            this.state.promptText = "";
            this.state.qcError = "";
            this.state.qcResult = null;
            this.state.currentTurnId = null;
            this.state.gateMode = "";
            this.state.rewriteText = "";
            this.state.justificationText = "";
            this.state.gateResolved = false;
            this.state.responseText = "";
            this.state.responseSaved = false;
            this.state.responseError = "";
            await this.loadHistory();
            this.notification.add(_t("New session started."), { type: "success" });
        } catch (err) {
            this.notification.add(
                err && err.message ? err.message : String(err),
                { type: "danger" }
            );
        }
    }

    async onGenerateQc() {
        if (!this.canGenerateQc) return;
        const prompt = this.state.promptText.trim();
        if (!prompt) return;
        if (!this.props.sandboxId) {
            this.state.qcError = _t("Sandbox record is missing. Cannot create turn.");
            return;
        }

        this.state.qcRunning = true;
        this.state.qcError = "";
        this.state.qcResult = null;
        this.state.currentTurnId = null;
        this.state.gateMode = "";
        this.state.rewriteText = "";
        this.state.justificationText = "";
        this.state.gateResolved = false;
        this.state.responseText = "";
        this.state.responseSaved = false;
        this.state.responseError = "";

        try {
            await this._ensureSession(false);

            const previousTurns = this.state.turns.map((t) => ({
                prompt: t.prompt || "",
                response: t.response || "",
            }));

            const createRes = await rpc("/atlas/chat/create_turn", {
                sandbox_id: this.props.sandboxId,
                message: prompt,
                timestamp: new Date().toISOString(),
            });
            if (!createRes || createRes.error) {
                throw new Error((createRes && createRes.error) || _t("Failed to create turn."));
            }
            const turnId = createRes.turn_id;
            this.state.currentTurnId = turnId;

            const qcRes = await rpc("/atlas/qc", { prompt, previous_turns: previousTurns });
            if (!qcRes || qcRes.error) {
                throw new Error((qcRes && qcRes.error) || _t("QC generation failed."));
            }

            const qcResult = qcRes.qc_result || null;
            this.state.qcResult = qcResult;

            if (qcResult && qcResult.severity) {
                this._applySeverityGate(String(qcResult.severity).toLowerCase(), prompt);
                const usage = qcRes.usage || {};
                try {
                    await rpc("/atlas/chat/save_qc", {
                        turn_id: turnId,
                        severity: String(qcResult.severity).toLowerCase(),
                        qc_response: JSON.stringify(qcResult),
                        bedrock_input_tokens: usage.input_tokens || 0,
                        bedrock_output_tokens: usage.output_tokens || 0,
                    });
                } catch (saveErr) {
                    console.warn("save_qc failed:", saveErr);
                }
            } else {
                this.state.qcError = _t(
                    "QC response could not be parsed into a verdict. Raw model output may not match the expected JSON shape."
                );
            }

            await this.loadHistory();
        } catch (err) {
            this.state.qcError = err && err.message ? err.message : String(err);
            this.notification.add(this.state.qcError, { type: "danger" });
        } finally {
            this.state.qcRunning = false;
        }
    }

    async onSaveResponse() {
        if (!this.canSaveResponse) return;
        const responseText = this.state.responseText.trim();
        if (!responseText) return;

        this.state.responseSaving = true;
        this.state.responseError = "";

        try {
            const res = await rpc("/atlas/chat/save_response", {
                turn_id: this.state.currentTurnId,
                response: responseText,
                timestamp: new Date().toISOString(),
                partial: false,
            });
            if (!res || res.error) {
                throw new Error((res && res.error) || _t("Failed to save response."));
            }
            this.state.responseSaved = true;
            this.notification.add(_t("Response saved."), { type: "success" });
            await this.loadHistory();
        } catch (err) {
            this.state.responseError = err && err.message ? err.message : String(err);
            this.notification.add(this.state.responseError, { type: "danger" });
        } finally {
            this.state.responseSaving = false;
        }
    }

    _applySeverityGate(severity, currentPrompt) {
        if (severity === "low") {
            this.state.gateMode = "";
            this.state.gateResolved = true;
            this.state.rewriteText = "";
            this.state.justificationText = "";
            return;
        }
        if (severity === "high" || severity === "critical") {
            this.state.gateMode = "forced_rewrite";
            this.state.gateResolved = false;
            this.state.rewriteText = currentPrompt || "";
            this.state.justificationText = "";
            return;
        }
        this.state.gateMode = "";
        this.state.gateResolved = false;
        this.state.rewriteText = currentPrompt || "";
        this.state.justificationText = "";
    }

    onChooseRewrite() {
        if (!this.canSelectGate) return;
        this.state.gateMode = "rewrite";
        this.state.justificationText = "";
        if (!this.state.rewriteText) {
            this.state.rewriteText = this.state.promptText || "";
        }
    }

    onChooseJustify() {
        if (!this.canSelectGate) return;
        this.state.gateMode = "justify";
        this.state.rewriteText = "";
    }

    async onSubmitJustification() {
        if (!this.canSubmitJustification) return;
        const justification = this.state.justificationText.trim();
        const severity = String(this.state.qcResult && this.state.qcResult.severity || "").toLowerCase();
        try {
            const res = await rpc("/atlas/chat/save_qc", {
                turn_id: this.state.currentTurnId,
                severity,
                qc_response: JSON.stringify(this.state.qcResult || {}),
                justification,
            });
            if (res && res.error) throw new Error(res.error);
            this.state.gateResolved = true;
            this.notification.add(_t("Justification saved."), { type: "success" });
            await this.loadHistory();
        } catch (err) {
            const msg = err && err.message ? err.message : String(err);
            this.state.qcError = msg;
            this.notification.add(msg, { type: "danger" });
        }
    }

    async onRerunQc() {
        if (!this.canRerunQc) return;
        const newPrompt = this.state.rewriteText.trim();
        if (!newPrompt) return;
        if (!this.state.currentTurnId) return;

        this.state.qcRunning = true;
        this.state.qcError = "";

        try {
            const previousTurns = this.state.turns
                .filter((t) => t.id !== this.state.currentTurnId)
                .map((t) => ({ prompt: t.prompt || "", response: t.response || "" }));

            const qcRes = await rpc("/atlas/qc", {
                prompt: newPrompt,
                previous_turns: previousTurns,
            });
            if (!qcRes || qcRes.error) {
                throw new Error((qcRes && qcRes.error) || _t("QC re-run failed."));
            }
            const qcResult = qcRes.qc_result || null;
            this.state.qcResult = qcResult;

            if (!qcResult || !qcResult.severity) {
                this.state.qcError = _t(
                    "QC response could not be parsed into a verdict on re-run."
                );
                return;
            }

            this.state.promptText = newPrompt;
            this._applySeverityGate(String(qcResult.severity).toLowerCase(), newPrompt);

            const usage = qcRes.usage || {};
            try {
                await rpc("/atlas/chat/save_qc", {
                    turn_id: this.state.currentTurnId,
                    severity: String(qcResult.severity).toLowerCase(),
                    qc_response: JSON.stringify(qcResult),
                    new_prompt: newPrompt,
                    bedrock_input_tokens: usage.input_tokens || 0,
                    bedrock_output_tokens: usage.output_tokens || 0,
                });
            } catch (saveErr) {
                console.warn("save_qc (re-run) failed:", saveErr);
            }

            await this.loadHistory();
        } catch (err) {
            const msg = err && err.message ? err.message : String(err);
            this.state.qcError = msg;
            this.notification.add(msg, { type: "danger" });
        } finally {
            this.state.qcRunning = false;
        }
    }

    onResetTurn() {
        this.state.promptText = "";
        this.state.qcError = "";
        this.state.qcResult = null;
        this.state.currentTurnId = null;
        this.state.gateMode = "";
        this.state.rewriteText = "";
        this.state.justificationText = "";
        this.state.gateResolved = false;
        this.state.responseText = "";
        this.state.responseSaved = false;
        this.state.responseError = "";
    }

    get isGenerationRunning() {
        const rec = this.props.record && this.props.record.data;
        if (!rec) return false;
        return rec.goal_generation_status === "running" || rec.rubric_generation_status === "running";
    }

    get canGenerateGoalRubric() {
        return (
            !!this.props.taskId &&
            !this.state.goalRubricRunning &&
            !this.isGenerationRunning &&
            !this.state.qcRunning &&
            !this.state.responseSaving &&
            this.state.turns.length > 0
        );
    }

    async onGenerateGoalRubric() {
        if (!this.canGenerateGoalRubric) return;
        this.state.goalRubricRunning = true;
        this.state.goalRubricError = "";

        try {
            // Combined server action: runs goal + rubric serially in ONE
            // background worker / cursor. Do NOT split into separate
            // action_regenerate_goal + action_regenerate_rubric calls — two
            // parallel workers race on the same atlas_atlas row and fail
            // with psycopg2 SerializationFailure, stranding status='running'.
            await this.orm.call(
                "atlas.atlas",
                "action_regenerate_description",
                [[this.props.taskId]]
            );

            if (this.props.record) {
                try {
                    await this.props.record.load();
                } catch (_e) {}
            }
        } catch (err) {
            this.state.goalRubricError = err && err.message ? err.message : String(err);
            this.notification.add(this.state.goalRubricError, { type: "danger" });
        } finally {
            this.state.goalRubricRunning = false;
        }
    }
}
