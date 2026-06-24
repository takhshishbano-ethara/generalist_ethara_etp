/** @odoo-module */

import { registry } from "@web/core/registry";
import { Component, useState, onMounted, onWillUnmount, useRef, onWillStart } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";

const REFRESH_INTERVAL = 60000;

const PALETTE = {
    available: "#6366f1",
    assigned: "#14b8a6",
    used: "#22c55e",
    bad: "#ef4444",
    indigoSoft: "#a5b4fc",
};

export class LynceusDashboard extends Component {
    static template = "lynceus.Dashboard";
    static props = ["*"];

    setup() {
        this.action = useService("action");
        this.orm = useService("orm");

        this.state = useState({
            loaded: false,
            kpis: {
                pool_available: 0,
                pool_status: "muted",
                pool_threshold: 0,
                in_flight: 0,
                in_flight_taskers: 0,
                completed_in_range: 0,
                bad_in_range: 0,
                active_taskers: 0,
                enrolled_taskers: 0,
                last_batch: { id: 0, name: "—", state: "" },
                total_used: 0,
                total_bad: 0,
            },
            pool_breakdown: [],
            per_tasker: [],
            live_batches: [],
            top_submitters: [],
        });

        this.filters = useState({
            date_from: "",
            date_to: "",
        });

        this.poolChart = useRef("poolChart");
        this.workloadChart = useRef("workloadChart");

        this._timer = null;
        this._charts = [];

        onWillStart(async () => {
            const today = new Date().toISOString().slice(0, 10);
            this.filters.date_from = today;
            this.filters.date_to = today;
            await this._loadData();
        });

        onMounted(() => {
            this._renderCharts();
            this._timer = setInterval(() => this._loadData(), REFRESH_INTERVAL);
        });

        onWillUnmount(() => {
            if (this._timer) clearInterval(this._timer);
            this._destroyCharts();
        });
    }

    _buildFilters() {
        const f = {};
        if (this.filters.date_from) f.date_from = this.filters.date_from;
        if (this.filters.date_to) f.date_to = this.filters.date_to;
        return Object.keys(f).length > 0 ? f : null;
    }

    async _loadData() {
        try {
            const data = await this.orm.call(
                "lynceus.batch",
                "get_dashboard_data",
                [this._buildFilters()],
            );
            this.state.kpis = data.kpis || {};
            this.state.pool_breakdown = data.pool_breakdown || [];
            this.state.per_tasker = data.per_tasker || [];
            this.state.live_batches = data.live_batches || [];
            this.state.top_submitters = data.top_submitters || [];
            this.state.loaded = true;
            this._renderCharts();
        } catch (e) {
            console.error("Lynceus dashboard load error:", e);
            this.state.loaded = true;
        }
    }

    onDateFromChange(ev) {
        this.filters.date_from = ev.target.value || "";
        this._loadData();
    }

    onDateToChange(ev) {
        this.filters.date_to = ev.target.value || "";
        this._loadData();
    }

    onResetDates() {
        const today = new Date().toISOString().slice(0, 10);
        this.filters.date_from = today;
        this.filters.date_to = today;
        this._loadData();
    }

    _destroyCharts() {
        for (const c of this._charts) {
            try { c.destroy(); } catch { }
        }
        this._charts = [];
    }

    _renderCharts() {
        this._destroyCharts();
        this._renderPoolChart();
        this._renderWorkloadChart();
    }

    _renderPoolChart() {
        const el = this.poolChart.el;
        if (!el || typeof Chart === "undefined") return;
        const items = (this.state.pool_breakdown || []).filter((d) => d.value > 0);
        if (!items.length) return;
        const ctx = el.getContext("2d");
        const chart = new Chart(ctx, {
            type: "doughnut",
            data: {
                labels: items.map((d) => d.label),
                datasets: [{
                    data: items.map((d) => d.value),
                    backgroundColor: items.map((d) => PALETTE[d.key] || PALETTE.indigoSoft),
                    borderWidth: 0,
                }],
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                cutout: "65%",
                plugins: {
                    legend: { position: "bottom", labels: { padding: 14, usePointStyle: true } },
                },
            },
        });
        this._charts.push(chart);
    }

    _renderWorkloadChart() {
        const el = this.workloadChart.el;
        if (!el || typeof Chart === "undefined") return;
        const items = this.state.per_tasker || [];
        if (!items.length) return;
        const ctx = el.getContext("2d");
        const chart = new Chart(ctx, {
            type: "bar",
            data: {
                labels: items.map((d) => d.label),
                datasets: [{
                    label: "Assigned",
                    data: items.map((d) => d.count),
                    backgroundColor: PALETTE.assigned,
                    borderRadius: 6,
                }],
            },
            options: {
                indexAxis: "y",
                responsive: true,
                maintainAspectRatio: false,
                plugins: { legend: { display: false } },
                scales: {
                    x: { beginAtZero: true, ticks: { stepSize: 1, precision: 0 } },
                    y: { ticks: { autoSkip: false } },
                },
            },
        });
        this._charts.push(chart);
    }

    openGenerateBatch() {
        this.action.doAction("lynceus.action_lynceus_generate_batch_wizard");
    }

    openImportTaskers() {
        this.action.doAction("lynceus.action_lynceus_import_active_taskers_wizard");
    }

    openPool(state) {
        const domain = state ? [["state", "=", state]] : [];
        this.action.doAction({
            type: "ir.actions.act_window",
            name: "Prompt Pool",
            res_model: "lynceus.prompt",
            view_mode: "list,form",
            views: [[false, "list"], [false, "form"]],
            domain,
        });
    }

    openBatches(state) {
        const domain = state ? [["state", "=", state]] : [];
        this.action.doAction({
            type: "ir.actions.act_window",
            name: "Batches",
            res_model: "lynceus.batch",
            view_mode: "list,form",
            views: [[false, "list"], [false, "form"]],
            domain,
        });
    }

    openBatchRecord(id) {
        if (!id) {
            this.openBatches();
            return;
        }
        this.action.doAction({
            type: "ir.actions.act_window",
            res_model: "lynceus.batch",
            res_id: id,
            view_mode: "form",
            views: [[false, "form"]],
        });
    }

    openAllQueues() {
        this.action.doAction("lynceus.action_lynceus_all_queues");
    }

    get poolStatusLabel() {
        const s = this.state.kpis.pool_status;
        if (s === "ok") return "Healthy";
        if (s === "warning") return "Low";
        if (s === "danger") return "Empty";
        return "—";
    }

    get poolStatusDot() {
        const s = this.state.kpis.pool_status;
        if (s === "ok") return "lyn-dot-ok";
        if (s === "warning") return "lyn-dot-warn";
        if (s === "danger") return "lyn-dot-danger";
        return "lyn-dot-muted";
    }

    get batchStatePill() {
        const s = (this.state.kpis.last_batch || {}).state;
        if (s === "done") return "lyn-pill-success";
        if (s === "running") return "lyn-pill-info";
        if (s === "pending") return "lyn-pill-warn";
        if (s === "failed") return "lyn-pill-danger";
        return "lyn-pill-muted";
    }
}

registry.category("actions").add("lynceus.dashboard", LynceusDashboard);
