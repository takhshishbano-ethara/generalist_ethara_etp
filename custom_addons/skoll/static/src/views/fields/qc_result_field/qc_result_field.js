/** @odoo-module */

import { _t } from "@web/core/l10n/translation";
import { registry } from "@web/core/registry";
import { standardFieldProps } from "@web/views/fields/standard_field_props";
import { useRecordObserver } from "@web/model/relational_model/utils";

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
        this.state = useState({
            html: null,
            rawFallback: "",
            improving: false,
        });
        this._improveAbort = null;

        useRecordObserver((record) => this._parse(record.data[this.props.name]));
        this._parse(this.props.record.data[this.props.name]);

        onWillUnmount(() => {
            if (this._improveAbort) this._improveAbort.abort();
        });
    }

    _parse(raw) {
        if (!raw || !raw.trim()) {
            this.state.html = null;
            this.state.rawFallback = "";
            return;
        }
        try {
            const parsed = JSON.parse(raw.trim());
            this.state.html = markup(renderQcHtml(parsed));
            this.state.rawFallback = "";
        } catch {
            this.state.html = null;
            this.state.rawFallback = raw;
        }
    }

    get canImprove() {
        const qcStatus = this.props.record.data.qc_status;
        return (
            !this.state.improving &&
            (qcStatus === "fail" || qcStatus === "needs_revision") &&
            !!this.props.record.data.content &&
            !!this.props.record.data.qc_result
        );
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
            const resp = await fetch("/skoll/improve_stream", {
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
                await this.props.record.update({ content: accumulated });
                await this.props.record.save();
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
}

export const skollQcResultField = {
    component: SkollQcResultField,
    displayName: _t("QC Result"),
    supportedTypes: ["text"],
    extractProps: () => ({}),
};

registry.category("fields").add("skoll_qc_result", skollQcResultField);
