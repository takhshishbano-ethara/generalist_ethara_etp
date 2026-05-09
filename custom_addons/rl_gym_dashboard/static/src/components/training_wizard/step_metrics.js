/** @odoo-module **/
import { Component, useState, useRef, onMounted, onPatched, onWillUnmount, onWillStart } from "@odoo/owl";
import { loadBundle } from "@web/core/assets";

const CHART_DEFS = [
    {
        id: "reward",
        title: "Reward (Raw + EMA)",
        datasets: [
            { key: "reward", label: "Raw Reward", color: "rgba(16, 185, 129, 0.3)", fill: true, width: 1 },
            { key: "reward_ema", label: "Smoothed (EMA)", color: "#10b981", fill: true, width: 2.5 },
        ],
        yTitle: "Reward",
    },
    {
        id: "losses",
        title: "Loss Decomposition",
        datasets: [
            { key: "policy_loss", label: "Policy Loss", color: "#3b82f6", width: 1.8 },
            { key: "value_loss", label: "Value Loss", color: "#f59e0b", width: 1.8 },
            { key: "loss", label: "Total Loss", color: "#ef4444", width: 1.2 },
        ],
        yTitle: "Loss",
    },
    {
        id: "gradient",
        title: "Gradient Norm",
        datasets: [
            { key: "gradient_norm", label: "Grad Norm", color: "#f43f5e", width: 2 },
        ],
        yTitle: "Norm",
    },
    {
        id: "kl_entropy",
        title: "KL Divergence & Entropy",
        datasets: [
            { key: "entropy", label: "Entropy", color: "#8b5cf6", width: 1.8 },
            { key: "kl_divergence", label: "KL Divergence", color: "#06b6d4", width: 1.8 },
            { key: "clip_fraction", label: "Clip Fraction", color: "#f97316", width: 1.2 },
        ],
        yTitle: "Value",
    },
    {
        id: "throughput",
        title: "Throughput",
        datasets: [
            { key: "tokens_per_second", label: "Tokens/sec", color: "#22c55e", width: 2 },
            { key: "samples_per_second", label: "Samples/sec", color: "#a3e635", width: 1.5 },
        ],
        yTitle: "Rate",
    },
    {
        id: "gpu",
        title: "GPU Memory Usage",
        datasets: [
            { key: "gpu_memory_used", label: "GPU Memory (GB)", color: "#e879f9", fill: true, width: 2 },
        ],
        yTitle: "GB",
    },
];

export class StepMetrics extends Component {
    static template = "rl_gym_dashboard.TrainingWizard.StepMetrics";
    static props = {
        jobId: { type: [Number, { value: null }], optional: true },
        rpc: { type: Function },
    };

    setup() {
        this.charts = {};
        this.rootRef = useRef("metricsRoot");

        this.state = useState({
            loading: true,
            metricsData: [],
            finalLoss: "—",
            bestReward: "—",
            totalSteps: "—",
            avgTokensSec: "—",
            avgGpuMem: "—",
            policyLossEnd: "—",
        });

        onWillStart(async () => {
            await loadBundle("web.chartjs_lib");
        });

        onMounted(async () => {
            await this._loadMetrics();
        });

        onPatched(() => {
            if (Object.keys(this.charts).length === 0 && this.state.metricsData.length && this.rootRef.el) {
                const canvases = this.rootRef.el.querySelectorAll("canvas[data-chart-id]");
                if (canvases.length === CHART_DEFS.length) this._createCharts();
            }
        });

        onWillUnmount(() => {
            Object.values(this.charts).forEach((c) => c.destroy());
            this.charts = {};
        });
    }

    get chartDefs() {
        return CHART_DEFS;
    }

    async _loadMetrics() {
        this.state.loading = true;
        try {
            const data = await this.props.rpc("/rl_gym/training/metrics", {
                job_id: this.props.jobId,
                since_step: 0,
            });
            this.state.metricsData = data || [];
            this._computeEMA();
            this._computeSummary();
        } catch (e) {
            console.error("Failed to load metrics:", e);
            this.state.metricsData = [];
        }
        this.state.loading = false;
    }

    _computeEMA() {
        const alpha = 0.1;
        let ema = null;
        for (const m of this.state.metricsData) {
            if (ema === null) {
                ema = m.reward;
            } else {
                ema = alpha * m.reward + (1 - alpha) * ema;
            }
            m.reward_ema = ema;
        }
    }

    _computeSummary() {
        const d = this.state.metricsData;
        if (!d.length) return;

        const last = d[d.length - 1];
        this.state.finalLoss = this.fmtNum(last.loss);
        this.state.totalSteps = last.step;
        this.state.policyLossEnd = this.fmtNum(last.policy_loss);

        let best = -Infinity;
        let totalTok = 0;
        let totalGpu = 0;
        for (const m of d) {
            if (m.reward > best) best = m.reward;
            totalTok += m.tokens_per_second || 0;
            totalGpu += m.gpu_memory_used || 0;
        }
        this.state.bestReward = this.fmtNum(best);
        this.state.avgTokensSec = Math.round(totalTok / d.length);
        this.state.avgGpuMem = (totalGpu / d.length).toFixed(1) + " GB";
    }

    _createCharts() {
        const Chart = globalThis.Chart;
        if (!Chart || !this.rootRef.el) return;

        const labels = this.state.metricsData.map((m) => m.step);

        for (const def of CHART_DEFS) {
            const canvas = this.rootRef.el.querySelector(`canvas[data-chart-id="${def.id}"]`);
            if (!canvas) continue;

            const datasets = def.datasets.map((ds) => ({
                label: ds.label,
                data: this.state.metricsData.map((m) => m[ds.key]),
                borderColor: ds.color,
                backgroundColor: ds.fill ? ds.color.replace(")", ", 0.08)").replace("rgb", "rgba") : undefined,
                borderWidth: ds.width || 1.8,
                pointRadius: 0,
                tension: 0.3,
                fill: ds.fill || false,
            }));

            this.charts[def.id] = new Chart(canvas, {
                type: "line",
                data: { labels, datasets },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    animation: { duration: 400 },
                    interaction: { intersect: false, mode: "index" },
                    plugins: {
                        legend: {
                            display: true,
                            labels: { color: "#94a3b8", boxWidth: 10, font: { size: 10 } },
                        },
                    },
                    scales: {
                        x: {
                            grid: { color: "rgba(148, 163, 184, 0.08)" },
                            ticks: { color: "#94a3b8", maxTicksLimit: 8, font: { size: 9 } },
                            border: { color: "rgba(148, 163, 184, 0.15)" },
                            title: { display: true, text: "Step", color: "#64748b", font: { size: 10 } },
                        },
                        y: {
                            grid: { color: "rgba(148, 163, 184, 0.08)" },
                            ticks: { color: "#94a3b8", font: { size: 9 } },
                            border: { color: "rgba(148, 163, 184, 0.15)" },
                            title: { display: true, text: def.yTitle, color: "#64748b", font: { size: 10 } },
                        },
                    },
                },
            });
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
