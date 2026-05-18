/** @odoo-module */

import { _t } from "@web/core/l10n/translation";
import { registry } from "@web/core/registry";
import { standardFieldProps } from "@web/views/fields/standard_field_props";
import { useRecordObserver } from "@web/model/relational_model/utils";

import { useService } from "@web/core/utils/hooks";
import { rpc } from "@web/core/network/rpc";
import { Component, markup, useState, onWillUnmount } from "@odoo/owl";

const SEVERITY_COLORS = {
    critical: "#f38ba8",
    major: "#fab387",
    minor: "#f9e2af",
};

const SCORE_COLOR = (v) => (v >= 8 ? "#a6e3a1" : v >= 5 ? "#f9e2af" : "#f38ba8");

function scoreBar(label, value) {
    const pct = Math.round((value / 10) * 100);
    const color = SCORE_COLOR(value);
    return `<div class="skoll-qc-score-row">
        <span class="skoll-qc-score-label">${label}</span>
        <div class="skoll-qc-score-track">
            <div class="skoll-qc-score-fill" style="width:${pct}%;background:${color}"></div>
        </div>
        <span class="skoll-qc-score-val" style="color:${color}">${value}</span>
    </div>`;
}

function renderQcHtml(parsed) {
    const parts = [];

    const verdict = (parsed.verdict || "unknown").toLowerCase();
    const verdictColor =
        verdict === "pass" ? "#a6e3a1" : verdict === "fail" ? "#f38ba8" : "#fab387";
    parts.push(
        `<div class="skoll-qc-verdict" style="border-color:${verdictColor}">` +
            `<span class="skoll-qc-verdict-badge" style="background:${verdictColor}">${verdict.toUpperCase()}</span>` +
            (parsed.confidence != null
                ? `<span class="skoll-qc-confidence">Confidence: ${Math.round(parsed.confidence * 100)}%</span>`
                : "") +
            `</div>`
    );

    if (parsed.summary) {
        parts.push(`<div class="skoll-qc-summary">${parsed.summary}</div>`);
    }

    if (parsed.scores && typeof parsed.scores === "object") {
        parts.push(`<div class="skoll-qc-section-title">Scores</div><div class="skoll-qc-scores">`);
        const order = [
            "overall",
            "structural_integrity",
            "persona_alignment",
            "task_type_match",
            "tool_realism",
            "thinking_quality",
            "content_naturalness",
            "safety_compliance",
            "sub_agent_quality",
            "spawn_yield_flow",
        ];
        for (const key of order) {
            if (parsed.scores[key] != null) {
                const label = key.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
                parts.push(scoreBar(label, parsed.scores[key]));
            }
        }
        parts.push(`</div>`);
    }

    if (parsed.issues && parsed.issues.length) {
        parts.push(`<div class="skoll-qc-section-title">Issues (${parsed.issues.length})</div><div class="skoll-qc-issues">`);
        for (const issue of parsed.issues) {
            const color = SEVERITY_COLORS[issue.severity] || "#cdd6f4";
            parts.push(
                `<div class="skoll-qc-issue">` +
                    `<span class="skoll-qc-issue-severity" style="color:${color}">${(issue.severity || "").toUpperCase()}</span>` +
                    (issue.category ? `<span class="skoll-qc-issue-cat">${issue.category}</span>` : "") +
                    `<span class="skoll-qc-issue-desc">${issue.description || ""}</span>` +
                    (issue.location ? `<span class="skoll-qc-issue-loc">${issue.location}</span>` : "") +
                    `</div>`
            );
        }
        parts.push(`</div>`);
    }

    if (parsed.strengths && parsed.strengths.length) {
        parts.push(`<div class="skoll-qc-section-title">Strengths</div><ul class="skoll-qc-list skoll-qc-strengths">`);
        for (const s of parsed.strengths) {
            parts.push(`<li>${s}</li>`);
        }
        parts.push(`</ul>`);
    }

    if (parsed.recommendations && parsed.recommendations.length) {
        parts.push(`<div class="skoll-qc-section-title">Recommendations</div><ul class="skoll-qc-list skoll-qc-recommendations">`);
        for (const r of parsed.recommendations) {
            parts.push(`<li>${r}</li>`);
        }
        parts.push(`</ul>`);
    }

    return parts.join("");
}

export class SkollQcResultField extends Component {
    static template = "skoll.QcResultField";
    static props = { ...standardFieldProps };

    setup() {
        this.notification = useService("notification");
        this.state = useState({
            html: null,
            rawFallback: "",
            improving: false,
            qcRunning: false,
            qcStreaming: "",
        });
        this._improveAbort = null;
        this._qcAbort = null;

        useRecordObserver((record) => this._parse(record.data[this.props.name]));
        this._parse(this.props.record.data[this.props.name]);

        onWillUnmount(() => {
            if (this._improveAbort) this._improveAbort.abort();
            if (this._qcAbort) this._qcAbort.abort();
        });
    }

    _extractJson(raw) {
        let s = (raw || "").trim();
        if (!s) return null;

        if (s.startsWith("```")) {
            const nl = s.indexOf("\n");
            if (nl !== -1) s = s.slice(nl + 1);
            if (s.endsWith("```")) s = s.slice(0, -3).trimEnd();
        }

        try {
            return JSON.parse(s);
        } catch {}

        const first = s.indexOf("{");
        const last = s.lastIndexOf("}");
        if (first !== -1 && last > first) {
            try {
                return JSON.parse(s.slice(first, last + 1));
            } catch {}
        }

        return null;
    }

    _parse(raw) {
        if (!raw || !raw.trim()) {
            this.state.html = null;
            this.state.rawFallback = "";
            return;
        }
        const parsed = this._extractJson(raw);
        if (parsed && parsed.verdict) {
            this.state.html = markup(renderQcHtml(parsed));
            this.state.rawFallback = "";
        } else {
            this.state.html = null;
            this.state.rawFallback = raw;
        }
    }

    get hasGoldenTrajectory() {
        const mode = this.props.record.data.golden_input_mode;
        if (mode === "manual") {
            return !!this.props.record.data.golden_trajectory_manual;
        }
        return !!this.props.record.data.golden_trajectory;
    }

    get canRunQc() {
        return (
            !this.state.qcRunning &&
            !this.state.improving &&
            this.hasGoldenTrajectory
        );
    }

    get canImprove() {
        const qcStatus = this.props.record.data.golden_qc_status;
        return (
            !this.state.improving &&
            !this.state.qcRunning &&
            (qcStatus === "fail" || qcStatus === "needs_revision") &&
            !!this.props.record.data.golden_trajectory &&
            !!this.props.record.data.golden_qc_result
        );
    }

    async onRunQc() {
        if (!this.canRunQc) return;

        if (this.props.record.isDirty) {
            await this.props.record.save();
        }

        const recordId = this.props.record.resId;
        if (!recordId) return;

        this._qcAbort = new AbortController();
        this.state.qcRunning = true;
        this.state.qcStreaming = "";

        let accumulated = "";
        try {
            const resp = await fetch("/skoll/golden/qc_stream", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ record_id: recordId }),
                credentials: "same-origin",
                signal: this._qcAbort.signal,
            });

            if (!resp.ok) {
                this.notification.add(
                    _t("QC request failed: HTTP ") + resp.status,
                    { type: "danger" },
                );
                return;
            }

            const reader = resp.body.getReader();
            const decoder = new TextDecoder();
            let sseBuffer = "";

            while (true) {
                const { done, value } = await reader.read();
                if (done) break;
                sseBuffer += decoder.decode(value, { stream: true });
                const lines = sseBuffer.split("\n");
                sseBuffer = lines.pop() || "";
                for (const line of lines) {
                    if (!line.startsWith("data: ")) continue;
                    try {
                        const payload = JSON.parse(line.slice(6));
                        if (payload.type === "delta" && payload.text) {
                            accumulated += payload.text;
                            this.state.qcStreaming = accumulated;
                        } else if (payload.type === "error") {
                            this.notification.add(
                                _t("QC error: ") + payload.message,
                                { type: "danger" },
                            );
                        }
                    } catch (_e) {}
                }
            }
        } catch (err) {
            if (err.name !== "AbortError") {
                this.notification.add(
                    _t("QC stream failed: ") + (err.message || err),
                    { type: "danger" },
                );
            }
        } finally {
            this.state.qcRunning = false;
            this.state.qcStreaming = "";
            this._qcAbort = null;
            await this.props.record.load();
        }
    }

    onStopQc() {
        if (this._qcAbort) {
            this._qcAbort.abort();
        }
    }

    async onImprove() {
        if (!this.canImprove) return;

        if (this.props.record.isDirty) {
            await this.props.record.save();
        }

        const recordId = this.props.record.resId;
        if (!recordId) return;

        this._improveAbort = new AbortController();
        this.state.improving = true;

        this.env.bus.trigger("SKOLL_STREAM_START");

        let accumulated = "";
        try {
            const resp = await fetch("/skoll/golden/improve_stream", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ record_id: recordId }),
                credentials: "same-origin",
                signal: this._improveAbort.signal,
            });

            if (!resp.ok) {
                console.warn("Improve request failed:", resp.status);
                return;
            }

            const reader = resp.body.getReader();
            const decoder = new TextDecoder();
            let sseBuffer = "";

            while (true) {
                const { done, value } = await reader.read();
                if (done) break;
                sseBuffer += decoder.decode(value, { stream: true });
                const lines = sseBuffer.split("\n");
                sseBuffer = lines.pop() || "";
                for (const line of lines) {
                    if (!line.startsWith("data: ")) continue;
                    try {
                        const payload = JSON.parse(line.slice(6));
                        if (payload.type === "delta" && payload.text) {
                            accumulated += payload.text;
                            this.env.bus.trigger("SKOLL_STREAM_CHUNK", { text: accumulated });
                        } else if (payload.type === "stop") {
                            const truncated = payload.stopReason === "max_tokens";
                            this.env.bus.trigger("SKOLL_STREAM_END", { text: accumulated, truncated });
                        } else if (payload.type === "error") {
                            console.warn("Improve stream error:", payload.message);
                        }
                    } catch (_e) {}
                }
            }

            if (accumulated.trim()) {
                let cleaned = accumulated.trim();
                if (cleaned.startsWith("```")) {
                    const nl = cleaned.indexOf("\n");
                    if (nl !== -1) cleaned = cleaned.slice(nl + 1);
                    if (cleaned.endsWith("```")) cleaned = cleaned.slice(0, -3).trimEnd();
                }
                await this.props.record.update({ golden_trajectory: cleaned });
                await this.props.record.save();
                this._buildSpawnTree();
            }
        } catch (err) {
            if (err.name !== "AbortError") {
                console.warn("Improve stream failed:", err);
            }
        } finally {
            this.state.improving = false;
            this._improveAbort = null;
            await this.props.record.load();
        }
    }

    async _buildSpawnTree() {
        const recordId = this.props.record.resId;
        if (!recordId) return;
        try {
            const result = await rpc("/skoll/golden/spawn_tree", { record_id: recordId });
            if (result.status === "success") {
                this.env.bus.trigger("SKOLL_SPAWN_TREE_READY", { spawn_tree: result.spawn_tree });
            }
        } catch (_e) {}
    }
}

export const skollQcResultField = {
    component: SkollQcResultField,
    displayName: _t("QC Result"),
    supportedTypes: ["text"],
    extractProps: () => ({}),
};

registry.category("fields").add("skoll_qc_result", skollQcResultField);
