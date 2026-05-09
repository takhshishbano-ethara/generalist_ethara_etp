/** @odoo-module **/
import { Component, useState, useRef, onMounted, onWillUnmount, onWillStart } from "@odoo/owl";
import { loadBundle } from "@web/core/assets";

const METRIC_LINES = [
    { key: "loss", label: "Loss", color: "#3b82f6" },
    { key: "reward", label: "Reward", color: "#10b981" },
    { key: "gradient_norm", label: "Gradient Norm", color: "#f43f5e" },
    { key: "entropy", label: "Entropy", color: "#8b5cf6" },
];

export class StepMetrics extends Component {
    static template = "rl_gym_dashboard.TrainingWizard.StepMetrics";
    static props = {
        jobId: { type: [Number, { value: null }], optional: true },
        rpc: { type: Function },
    };

    setup() {
        this.chartRef = useRef("metricsChart");
        this.chart = null;

        this.state = useState({
            loading: true,
            metricsData: [],
            finalLoss: "—",
            bestReward: "—",
            totalSteps: "—",
            avgTokensSec: "—",
            visibleLines: {
                loss: true,
                reward: true,
                gradient_norm: true,
                entropy: true,
            },
        });

        onWillStart(async () => {
            await loadBundle("web.chartjs_lib");
        });

        onMounted(async () => {
            await this._loadMetrics();
            this._createChart();
        });

        onWillUnmount(() => {
            if (this.chart) this.chart.destroy();
        });
    }

    get metricLines() {
        return METRIC_LINES;
    }

    async _loadMetrics() {
        this.state.loading = true;
        try {
            const data = await this.props.rpc("/rl_gym/training/metrics", {
                job_id: this.props.jobId,
                since_step: 0,
            });
            this.state.metricsData = data || [];
            this._computeSummary();
        } catch (e) {
            console.error("Failed to load metrics:", e);
            this.state.metricsData = [];
        }
        this.state.loading = false;
    }

    _computeSummary() {
        const d = this.state.metricsData;
        if (!d.length) return;

        const last = d[d.length - 1];
        this.state.finalLoss = this.fmtNum(last.loss);
        this.state.totalSteps = last.step;

        let best = -Infinity;
        let totalTok = 0;
        for (const m of d) {
            if (m.reward > best) best = m.reward;
            totalTok += m.tokens_per_second || 0;
        }
        this.state.bestReward = this.fmtNum(best);
        this.state.avgTokensSec = Math.round(totalTok / d.length);
    }

    _createChart() {
        const Chart = globalThis.Chart;
        if (!this.chartRef.el || !this.state.metricsData.length) return;

        const labels = this.state.metricsData.map((m) => m.step);

        this.chart = new Chart(this.chartRef.el, {
            type: "line",
            data: {
                labels,
                datasets: METRIC_LINES.map((ml) => ({
                    label: ml.label,
                    data: this.state.metricsData.map((m) => m[ml.key]),
                    borderColor: ml.color,
                    borderWidth: 1.8,
                    pointRadius: 0,
                    tension: 0.3,
                    hidden: !this.state.visibleLines[ml.key],
                })),
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                animation: { duration: 400 },
                interaction: { intersect: false, mode: "index" },
                plugins: {
                    legend: {
                        display: true,
                        labels: { color: "#94a3b8", boxWidth: 10, font: { size: 11 } },
                    },
                },
                scales: {
                    x: {
                        grid: { color: "rgba(148, 163, 184, 0.08)" },
                        ticks: { color: "#94a3b8", maxTicksLimit: 12, font: { size: 10 } },
                        border: { color: "rgba(148, 163, 184, 0.15)" },
                        title: { display: true, text: "Step", color: "#64748b", font: { size: 11 } },
                    },
                    y: {
                        grid: { color: "rgba(148, 163, 184, 0.08)" },
                        ticks: { color: "#94a3b8", font: { size: 10 } },
                        border: { color: "rgba(148, 163, 184, 0.15)" },
                    },
                },
            },
        });
    }

    toggleLine(key) {
        this.state.visibleLines[key] = !this.state.visibleLines[key];
        if (this.chart) {
            const idx = METRIC_LINES.findIndex((ml) => ml.key === key);
            if (idx >= 0) {
                this.chart.data.datasets[idx].hidden = !this.state.visibleLines[key];
                this.chart.update();
            }
        }
    }

    fmtNum(val) {
        if (val == null || val === undefined) return "—";
        return typeof val === "number" ? val.toFixed(4) : String(val);
    }

    fmtSci(val) {
        if (val == null) return "—";
        return typeof val === "number" ? val.toExponential(2) : String(val);
    }
}
