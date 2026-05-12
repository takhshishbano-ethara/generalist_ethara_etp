/** @odoo-module */
import { Component, useState, onMounted } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { rpc } from "@web/core/network/rpc";

function formatNumber(n) {
    if (!n && n !== 0) return "0";
    return n.toLocaleString("en-US");
}

function formatDuration(seconds) {
    if (!seconds) return "0s";
    if (seconds < 60) return `${seconds.toFixed(1)}s`;
    const mins = Math.floor(seconds / 60);
    const secs = Math.round(seconds % 60);
    return `${mins}m ${secs}s`;
}

export class ArcQcDashboard extends Component {
    static template = "arc_qc.Dashboard";
    static props = { action: { type: Object, optional: true }, "*": true };

    setup() {
        this.action = useService("action");
        this.notification = useService("notification");
        this.formatNumber = formatNumber;
        this.formatDuration = formatDuration;

        this.state = useState({
            loading: true,
            kpis: null,
            sessions: [],
            game_verdicts: [],
            verdict_breakdown: [],
            severity_totals: { critical: 0, high: 0, medium: 0, low: 0 },
            top_findings: [],
            model_health: [],
            session_trend: [],
            phase_findings: [],
        });

        onMounted(() => this._loadData());
    }

    async _loadData() {
        this.state.loading = true;
        try {
            const res = await rpc("/arc_qc/dashboard/data", {});
            this.state.kpis = res.kpis || {};
            this.state.sessions = res.sessions || [];
            this.state.game_verdicts = res.game_verdicts || [];
            this.state.verdict_breakdown = res.verdict_breakdown || [];
            this.state.severity_totals = res.severity_totals || { critical: 0, high: 0, medium: 0, low: 0 };
            this.state.top_findings = res.top_findings || [];
            this.state.model_health = res.model_health || [];
            this.state.session_trend = res.session_trend || [];
            this.state.phase_findings = res.phase_findings || [];
        } catch (e) {
            this.notification.add("Failed to load QC dashboard data", { type: "danger" });
            console.error("[arc-qc-dashboard]", e);
        } finally {
            this.state.loading = false;
        }
    }

    onRefresh() {
        this._loadData();
    }

    onNewSession() {
        this.action.doAction({
            type: "ir.actions.act_window",
            res_model: "arc.qc.session",
            views: [[false, "form"]],
            target: "current",
        });
    }

    onOpenSession(sessionId) {
        this.action.doAction({
            type: "ir.actions.act_window",
            res_model: "arc.qc.session",
            res_id: sessionId,
            views: [[false, "form"]],
            target: "current",
        });
    }

    onViewAllSessions() {
        this.action.doAction({
            type: "ir.actions.act_window",
            res_model: "arc.qc.session",
            views: [[false, "list"], [false, "form"]],
            target: "current",
        });
    }

    onViewFindings() {
        this.action.doAction({
            type: "ir.actions.act_window",
            res_model: "arc.qc.finding",
            views: [[false, "list"], [false, "form"]],
            target: "current",
            context: { search_default_groupby_code: 1 },
        });
    }

    // --- Verdict / State helpers ---

    stateLabel(state) {
        const map = {
            draft: "Draft", running: "Running",
            ship: "SHIP", conditional_ship: "CONDITIONAL",
            blocked: "BLOCKED", error: "Error",
        };
        return map[state] || state;
    }

    stateClass(state) {
        if (state === "ship") return "o_arc_qc_state_ship";
        if (state === "conditional_ship") return "o_arc_qc_state_conditional";
        if (state === "blocked") return "o_arc_qc_state_blocked";
        if (state === "running") return "o_arc_qc_state_running";
        if (state === "error") return "o_arc_qc_state_error";
        return "o_arc_qc_state_draft";
    }

    verdictColor(verdict) {
        if (verdict === "ship") return "#28a745";
        if (verdict === "conditional_ship") return "#fd7e14";
        if (verdict === "block" || verdict === "blocked") return "#dc3545";
        return "#6c757d";
    }

    severityColor(severity) {
        if (severity === "critical") return "#dc3545";
        if (severity === "high") return "#fd7e14";
        if (severity === "medium") return "#ffc107";
        return "#6c757d";
    }

    // --- Computed properties ---

    get passRateColor() {
        const pct = this.state.kpis?.latest_game_pass_rate || 0;
        if (pct >= 80) return "#28a745";
        if (pct >= 50) return "#fd7e14";
        return "#dc3545";
    }

    get severityTotal() {
        const s = this.state.severity_totals;
        return (s.critical || 0) + (s.high || 0) + (s.medium || 0) + (s.low || 0);
    }

    severityPct(severity) {
        const total = this.severityTotal;
        if (!total) return 0;
        return Math.round(100 * (this.state.severity_totals[severity] || 0) / total);
    }

    get verdictTotal() {
        return this.state.verdict_breakdown.reduce((sum, v) => sum + v.value, 0);
    }

    verdictPct(value) {
        const total = this.verdictTotal;
        if (!total) return 0;
        return Math.round(100 * value / total);
    }

    get hasData() {
        return this.state.kpis && this.state.kpis.total_sessions > 0;
    }

    get problemGames() {
        return [...this.state.game_verdicts]
            .filter((g) => g.verdict !== "ship")
            .sort((a, b) => b.critical - a.critical || b.high - a.high || b.medium - a.medium)
            .slice(0, 10);
    }

    get phaseMax() {
        return Math.max(1, ...this.state.phase_findings.map((p) => p.total));
    }

    phasePct(total) {
        return Math.round(100 * total / this.phaseMax);
    }

    phaseLabel(phase) {
        const map = {
            discovery: "Discovery",
            structural: "Structural",
            runs: "Runs Validation",
            steps: "Steps Validation",
            cross_run: "Cross-Run",
            content_safety: "Content Safety",
            smell_tests: "Smell Tests",
        };
        return map[phase] || phase;
    }

    trendDotClass(state) {
        if (state === "ship") return "o_arc_qc_trend_ship";
        if (state === "conditional_ship") return "o_arc_qc_trend_conditional";
        return "o_arc_qc_trend_blocked";
    }
}

registry.category("actions").add("arc_qc_dashboard", ArcQcDashboard);
