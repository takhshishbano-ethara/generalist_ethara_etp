/** @odoo-module **/
import { Component, useState, useRef, onMounted, onWillUnmount, onWillStart } from "@odoo/owl";
import { loadBundle } from "@web/core/assets";

export class StepTraining extends Component {
    static template = "rl_gym_dashboard.TrainingWizard.StepTraining";
    static props = {
        trainingState: { type: Object },
        modelId: { type: [Number, { value: null }], optional: true },
        configId: { type: [Number, { value: null }], optional: true },
        jobName: { type: String, optional: true },
        policyType: { type: String, optional: true },
        rpc: { type: Function },
    };

    setup() {
        this.chartRef = useRef("trainingChart");
        this.chart = null;
        this.timeoutId = null;
        this.lossData = [];
        this.rewardData = [];
        this.stepLabels = [];

        this.state = useState({
            status: "training",
            statusLabel: "Training",
            progress: 0,
            currentStep: 0,
            totalSteps: 500,
            currentLoss: "—",
            currentReward: "—",
            bestReward: "—",
            learningRate: "—",
            tokensPerSec: "—",
            etaLabel: "—",
            jobId: null,
        });

        onWillStart(async () => {
            await loadBundle("web.chartjs_lib");
        });

        onMounted(async () => {
            await this._createJob();
            this._createChart();
            this.intervalId = setInterval(() => this._trainStep(), 1);
        });

        onWillUnmount(() => {
            if (this.timeoutId) clearTimeout(this.timeoutId);
            if (this.chart) this.chart.destroy();
        });
    }

    _randomDelay() {
        return Math.floor(Math.random() * (300000 - 10000 + 1)) + 10000;
    }

    _scheduleNextStep() {
        if (this.state.status !== "training") return;
        const delay = this._randomDelay();
        const delaySec = Math.round(delay / 1000);
        const remaining = this.state.totalSteps - this.state.currentStep;
        const avgDelay = 155;
        const etaSec = remaining * avgDelay;
        this.state.etaLabel = etaSec > 3600
            ? `~${(etaSec / 3600).toFixed(1)}h`
            : `~${Math.round(etaSec / 60)}m`;
        this.timeoutId = setTimeout(() => this._trainStep(), delay);
    }

    async _createJob() {
        try {
            const job = await this.props.rpc("/rl_gym/training/create_job", {
                values: { name: this.props.jobName || "training-run" },
                model_id: this.props.modelId,
                config_id: this.props.configId,
            });
            this.state.jobId = job.id;
            this.props.trainingState.jobId = job.id;

            await this.props.rpc("/rl_gym/training/start", { job_id: job.id });
            this.state.status = "training";
            this.state.statusLabel = "Training";
        } catch (e) {
            console.error("Failed to create training job:", e);
            this.state.status = "failed";
            this.state.statusLabel = "Failed";
        }
    }

    async _trainStep() {
        if (this.state.status !== "training") return;
        if (!this.state.jobId) return;

        try {
            const result = await this.props.rpc("/rl_gym/training/step", {
                job_id: this.state.jobId,
            });

            this.state.currentStep = result.current_step || 0;
            this.state.totalSteps = result.total_steps || 500;
            this.state.progress = Math.round(result.progress || 0);
            this.state.currentLoss = this._fmtMetric(result.current_loss);
            this.state.currentReward = this._fmtMetric(result.current_reward);
            this.state.bestReward = this._fmtMetric(result.best_reward);

            if (result.current_loss != null) this.lossData.push(result.current_loss);
            if (result.current_reward != null) this.rewardData.push(result.current_reward);
            this.stepLabels.push(result.current_step || this.stepLabels.length);

            this._updateChart();

            if (result.state === "completed" || result.progress >= 100) {
                this.state.status = "completed";
                this.state.statusLabel = "Completed";
                this.state.progress = 100;
                this.props.trainingState.status = "completed";
            } else {
                this._scheduleNextStep();
            }
        } catch (e) {
            console.error("Training step failed:", e);
            this._scheduleNextStep();
        }
    }

    async _fetchLiveMetrics() {
        if (!this.state.jobId) return;
        try {
            const metrics = await this.props.rpc("/rl_gym/training/metrics", {
                job_id: this.state.jobId,
                since_step: Math.max(0, this.state.currentStep - 1),
            });
            if (metrics && metrics.length) {
                const last = metrics[metrics.length - 1];
                this.state.learningRate = last.learning_rate ? last.learning_rate.toExponential(2) : "—";
                this.state.tokensPerSec = last.tokens_per_second ? Math.round(last.tokens_per_second) : "—";
            }
        } catch (_) {}
    }

    _createChart() {
        const Chart = globalThis.Chart;
        if (!this.chartRef.el) return;

        this.chart = new Chart(this.chartRef.el, {
            type: "line",
            data: {
                labels: [],
                datasets: [
                    {
                        label: "Loss",
                        data: [],
                        borderColor: "#3b82f6",
                        backgroundColor: "rgba(59, 130, 246, 0.08)",
                        borderWidth: 2,
                        pointRadius: 0,
                        tension: 0.3,
                        fill: true,
                        yAxisID: "y",
                    },
                    {
                        label: "Reward",
                        data: [],
                        borderColor: "#10b981",
                        backgroundColor: "rgba(16, 185, 129, 0.08)",
                        borderWidth: 2,
                        pointRadius: 0,
                        tension: 0.3,
                        fill: true,
                        yAxisID: "y1",
                    },
                ],
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                animation: { duration: 300 },
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
                        ticks: { color: "#94a3b8", maxTicksLimit: 10, font: { size: 10 } },
                        border: { color: "rgba(148, 163, 184, 0.15)" },
                    },
                    y: {
                        position: "left",
                        grid: { color: "rgba(148, 163, 184, 0.08)" },
                        ticks: { color: "#3b82f6", font: { size: 10 } },
                        border: { color: "rgba(148, 163, 184, 0.15)" },
                        title: { display: true, text: "Loss", color: "#3b82f6", font: { size: 11 } },
                    },
                    y1: {
                        position: "right",
                        grid: { drawOnChartArea: false },
                        ticks: { color: "#10b981", font: { size: 10 } },
                        border: { color: "rgba(148, 163, 184, 0.15)" },
                        title: { display: true, text: "Reward", color: "#10b981", font: { size: 11 } },
                    },
                },
            },
        });
    }

    _updateChart() {
        if (!this.chart) return;
        this.chart.data.labels = [...this.stepLabels];
        this.chart.data.datasets[0].data = [...this.lossData];
        this.chart.data.datasets[1].data = [...this.rewardData];
        this.chart.update("none");

        if (this.stepLabels.length % 5 === 0) {
            this._fetchLiveMetrics();
        }
    }

    _fmtMetric(val) {
        if (val == null) return "—";
        return typeof val === "number" ? val.toFixed(4) : String(val);
    }

    formatMetric(val) {
        return val;
    }

    togglePause() {
        if (this.state.status === "training") {
            this.state.status = "paused";
            this.state.statusLabel = "Paused";
            if (this.timeoutId) clearTimeout(this.timeoutId);
        } else if (this.state.status === "paused") {
            this.state.status = "training";
            this.state.statusLabel = "Training";
            this._scheduleNextStep();
        }
    }
}
