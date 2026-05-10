/** @odoo-module **/
import { Component, useState, useRef, onMounted, onWillUnmount, onWillStart } from "@odoo/owl";
import { loadBundle } from "@web/core/assets";
import { applySmoothing, createCrosshairPlugin, createDragZoomPlugin, CHART_DEFS } from "../chart_utils";

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
        this.logScrollRef = useRef("logScroll");
        this.fullscreenCanvasRef = useRef("fullscreenCanvas");
        this.timeoutId = null;
        this.charts = {};
        this._lastRecordedStep = -1;
        this._chartRegistry = [];
        this._crosshair = { step: null, sourceId: null };
        this._zoomState = {};
        this._fullscreenChart = null;
        this._startTime = null;
        this._durationInterval = null;

        this.metrics = {
            steps: [], loss: [], reward: [], reward_mean: [], reward_std: [],
            best_reward: [], policy_loss: [], value_loss: [], entropy: [],
            kl_divergence: [], clip_fraction: [], advantage_mean: [],
            gradient_norm: [], learning_rate: [], tokens_per_second: [],
            samples_per_second: [], gpu_memory_used: [],
            cpu_percent: [], memory_percent: [], gpu_utilization: [],
        };

        this.logEntries = useState([]);

        this.state = useState({
            status: "training",
            statusLabel: "Training",
            progress: 0,
            currentStep: 0,
            totalSteps: 500,
            currentLoss: "\u2014",
            currentReward: "\u2014",
            bestReward: "\u2014",
            learningRate: "\u2014",
            tokensPerSec: "\u2014",
            gpuMem: "\u2014",
            etaLabel: "\u2014",
            jobId: null,
            policyType: this.props.policyType || "",
            activeTab: "charts",
            sidebarOpen: true,
            duration: "0s",
            smoothingType: "ema",
            smoothingWeight: 0.6,
            ignoreOutliers: false,
            fullscreenChartId: null,
            logFilter: "all",
            logSearch: "",
            logAutoScroll: true,
            logNewCount: 0,
            sections: [
                { title: "Training Overview", open: true, pinned: false },
                { title: "Loss Breakdown", open: true, pinned: false },
                { title: "Reward & Advantage", open: true, pinned: false },
                { title: "Policy Diagnostics", open: true, pinned: false },
                { title: "Learning Rate & Gradient", open: true, pinned: false },
                { title: "System Metrics", open: true, pinned: false },
            ],
        });

        onWillStart(async () => {
            await loadBundle("web.chartjs_lib");
        });

        onMounted(async () => {
            this._startTime = Date.now();
            this._durationInterval = setInterval(() => {
                const elapsed = Math.floor((Date.now() - this._startTime) / 1000);
                const m = Math.floor(elapsed / 60);
                const s = elapsed % 60;
                this.state.duration = `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
            }, 1000);
            await this._createJob();
            requestAnimationFrame(() => {
                this._createAllCharts();
                this._trainStep();
            });
        });

        onWillUnmount(() => {
            if (this.timeoutId) clearTimeout(this.timeoutId);
            this.timeoutId = null;
            if (this._durationInterval) clearInterval(this._durationInterval);
            this._durationInterval = null;
            if (this._fullscreenChart) {
                this._fullscreenChart.destroy();
                this._fullscreenChart = null;
            }
            for (const id in this.charts) {
                if (this.charts[id]) this.charts[id].destroy();
            }
            this.charts = {};
            this._chartRegistry = [];
        });
    }

    setActiveTab(tab) {
        this.state.activeTab = tab;
        if (tab === "charts") {
            this._destroyAllCharts();
            setTimeout(() => {
                this._createAllCharts();
                this._updateAllCharts();
            }, 80);
        }
    }

    toggleSidebar() {
        this.state.sidebarOpen = !this.state.sidebarOpen;
    }

    onSmoothingTypeChange(ev) {
        const prev = this.state.smoothingType;
        this.state.smoothingType = ev.target.value;
        const needsRebuild = (prev === "none") !== (ev.target.value === "none");
        if (needsRebuild) {
            this._destroyAllCharts();
            requestAnimationFrame(() => {
                this._createAllCharts();
                this._updateAllCharts();
            });
        } else {
            this._updateAllCharts();
        }
    }

    onSmoothingWeightChange(ev) {
        this.state.smoothingWeight = parseFloat(ev.target.value);
        this._updateAllCharts();
    }

    toggleOutliers() {
        this.state.ignoreOutliers = !this.state.ignoreOutliers;
        this._updateAllCharts();
    }

    togglePin(idx) {
        this.state.sections[idx].pinned = !this.state.sections[idx].pinned;
    }

    openFullscreen(chartId) {
        this.state.fullscreenChartId = chartId;
        setTimeout(() => {
            requestAnimationFrame(() => this._createFullscreenChart(chartId));
        }, 50);
    }

    closeFullscreen() {
        if (this._fullscreenChart) {
            this._fullscreenChart.destroy();
            this._fullscreenChart = null;
        }
        this.state.fullscreenChartId = null;
    }

    getFullscreenDef() {
        return CHART_DEFS.find(d => d.id === this.state.fullscreenChartId) || null;
    }

    onLogSearch(ev) {
        this.state.logSearch = ev.target.value;
    }

    onLogFilterChange(ev) {
        this.state.logFilter = ev.target.value;
    }

    toggleAutoScroll() {
        this.state.logAutoScroll = !this.state.logAutoScroll;
        if (this.state.logAutoScroll) this.scrollToBottom();
    }

    scrollToBottom() {
        if (this.logScrollRef.el) {
            this.logScrollRef.el.scrollTop = this.logScrollRef.el.scrollHeight;
            this.state.logNewCount = 0;
        }
    }

    onLogScroll() {
        if (!this.logScrollRef.el) return;
        const el = this.logScrollRef.el;
        const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 30;
        if (atBottom) this.state.logNewCount = 0;
    }

    get filteredLogs() {
        let logs = this.logEntries;
        if (this.state.logFilter === "warn") {
            logs = logs.filter(l => l.severity === "warn" || l.severity === "error");
        } else if (this.state.logFilter === "error") {
            logs = logs.filter(l => l.severity === "error");
        }
        if (this.state.logSearch) {
            const q = this.state.logSearch.toLowerCase();
            logs = logs.filter(l =>
                String(l.step).includes(q) || l.loss.toLowerCase().includes(q) ||
                l.reward.toLowerCase().includes(q)
            );
        }
        return logs;
    }

    _randomDelay() {
        return 1500 + Math.floor(Math.random() * 1000);
    }

    _scheduleNextStep() {
        if (this.state.status !== "training") return;
        const delay = this._randomDelay();
        const remaining = this.state.totalSteps - this.state.currentStep;
        const avgDelaySec = 3;
        const etaSec = remaining * avgDelaySec;
        this.state.etaLabel = etaSec > 3600
            ? `~${(etaSec / 3600).toFixed(1)}h`
            : etaSec > 60
                ? `~${Math.round(etaSec / 60)}m`
                : `~${etaSec}s`;
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

            const step = result.current_step || 0;
            this._failCount = 0;
            this.state.currentStep = step;
            this.state.totalSteps = result.total_steps || 500;
            this.state.progress = Math.round(result.progress || 0);
            this.state.currentLoss = this._fmtMetric(result.current_loss);
            this.state.currentReward = this._fmtMetric(result.current_reward);

            if (step > this._lastRecordedStep) {
                this._lastRecordedStep = step;
                this._pushMetrics(result);
                this._updateAllCharts();
                this._updateFullscreenChart();
            }

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
            this._failCount = (this._failCount || 0) + 1;
            if (this._failCount > 20) {
                this.state.status = "failed";
                this.state.statusLabel = "Connection Lost";
                return;
            }
            const backoff = Math.min(30000, 1500 * Math.pow(1.5, this._failCount));
            this.timeoutId = setTimeout(() => this._trainStep(), backoff);
            return;
        }
    }

    _pushMetrics(result) {
        const step = result.current_step || 0;
        this.metrics.steps.push(step);

        const keys = [
            ["loss", "current_loss"], ["reward", "current_reward"],
            ["reward_mean", "reward_mean"], ["reward_std", "reward_std"],
            ["policy_loss", "policy_loss"], ["value_loss", "value_loss"],
            ["entropy", "entropy"], ["kl_divergence", "kl_divergence"],
            ["clip_fraction", "clip_fraction"], ["advantage_mean", "advantage_mean"],
            ["gradient_norm", "gradient_norm"], ["learning_rate", "learning_rate"],
            ["tokens_per_second", "tokens_per_second"],
            ["samples_per_second", "samples_per_second"],
            ["gpu_memory_used", "gpu_memory_used"],
            ["cpu_percent", "cpu_percent"],
            ["memory_percent", "memory_percent"],
            ["gpu_utilization", "gpu_utilization"],
        ];

        for (const [metricKey, resultKey] of keys) {
            const val = result[resultKey];
            this.metrics[metricKey].push(val != null ? val : null);
        }

        const prevBest = this.metrics.best_reward.length > 0
            ? this.metrics.best_reward[this.metrics.best_reward.length - 1]
            : -Infinity;
        const currentReward = result.current_reward != null ? result.current_reward : prevBest;
        this.metrics.best_reward.push(Math.max(prevBest, currentReward));

        this.state.bestReward = this._fmtMetric(this.metrics.best_reward[this.metrics.best_reward.length - 1]);
        this.state.learningRate = result.learning_rate != null ? result.learning_rate.toExponential(2) : "\u2014";
        this.state.tokensPerSec = result.tokens_per_second != null ? Math.round(result.tokens_per_second) : "\u2014";
        this.state.gpuMem = result.gpu_memory_used != null ? `${result.gpu_memory_used.toFixed(1)}%` : "\u2014";

        this._addLogEntry(step, result);
    }

    _addLogEntry(step, result) {
        const elapsed = this._startTime ? Math.floor((Date.now() - this._startTime) / 1000) : 0;
        const m = Math.floor(elapsed / 60);
        const s = elapsed % 60;
        const timeLabel = `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;

        let severity = "info";
        if (result.gradient_norm != null && result.gradient_norm > 50) severity = "warn";
        if (result.current_loss != null && result.current_loss > 5) severity = "error";

        this.logEntries.push({
            step,
            timeLabel,
            severity,
            loss: this._fmtMetric(result.current_loss),
            reward: this._fmtMetric(result.current_reward),
            lr: result.learning_rate != null ? result.learning_rate.toExponential(2) : "\u2014",
            grad: this._fmtMetric(result.gradient_norm),
            entropy: this._fmtMetric(result.entropy),
            kl: this._fmtMetric(result.kl_divergence),
            clip: this._fmtMetric(result.clip_fraction),
        });

        if (this.logEntries.length > 200) {
            this.logEntries.shift();
        }

        if (this.state.logAutoScroll && this.logScrollRef.el) {
            requestAnimationFrame(() => {
                if (this.logScrollRef.el) {
                    this.logScrollRef.el.scrollTop = this.logScrollRef.el.scrollHeight;
                }
            });
        } else {
            this.state.logNewCount++;
        }
    }

    _destroyAllCharts() {
        for (const id in this.charts) {
            try { if (this.charts[id]) this.charts[id].destroy(); } catch (e) {}
        }
        this.charts = {};
        this._chartRegistry = [];
    }

    _createAllCharts() {
        const Chart = globalThis.Chart;
        if (!Chart) return;

        for (const def of CHART_DEFS) {
            if (!this.state.sections[def.section].open) continue;
            this._createSingleChart(def, Chart);
        }
    }

    _createSingleChart(def, Chart) {
        if (!Chart) Chart = globalThis.Chart;
        if (!Chart) return;

        const canvas = this.el
            ? this.el.querySelector(`[data-chart-id="${def.id}"]`)
            : document.querySelector(`[data-chart-id="${def.id}"]`);
        if (!canvas) return;
        if (this.charts[def.id]) {
            this.charts[def.id].destroy();
            const idx = this._chartRegistry.indexOf(this.charts[def.id]);
            if (idx > -1) this._chartRegistry.splice(idx, 1);
            delete this.charts[def.id];
        }

        const datasets = [];
        const useSmoothing = this.state.smoothingType !== "none";

        for (const line of def.lines) {
            datasets.push({
                label: line.label,
                data: [],
                borderColor: line.color,
                backgroundColor: "transparent",
                borderWidth: 1.5,
                pointRadius: 0,
                tension: 0.3,
                spanGaps: true,
                borderDash: line.dash || [],
            });
            if (useSmoothing) {
                datasets.push({
                    label: `${line.label} (raw)`,
                    data: [],
                    borderColor: line.color,
                    backgroundColor: "transparent",
                    borderWidth: 0.5,
                    pointRadius: 0,
                    tension: 0.3,
                    spanGaps: true,
                    borderDash: [],
                    hidden: false,
                    _isGhost: true,
                });
            }
        }

        if (def.stdBand) {
            datasets.push({
                label: "+σ",
                data: [],
                borderColor: "transparent",
                backgroundColor: "transparent",
                borderWidth: 0,
                pointRadius: 0,
                spanGaps: true,
                fill: false,
            });
            datasets.push({
                label: "-σ",
                data: [],
                borderColor: "transparent",
                backgroundColor: `${def.lines[0].color}18`,
                borderWidth: 0,
                pointRadius: 0,
                spanGaps: true,
                fill: "-1",
            });
        }

        if (def.refLine) {
            datasets.push({
                label: def.refLine.label,
                data: [],
                borderColor: def.refLine.color,
                backgroundColor: "transparent",
                borderWidth: 1,
                pointRadius: 0,
                borderDash: [5, 3],
                spanGaps: true,
            });
        }

        const showLegend = datasets.filter(d => d.label !== "+σ" && d.label !== "-σ" && !d._isGhost).length > 1;

        const crosshairPlugin = createCrosshairPlugin(this._chartRegistry, this._crosshair);
        const dragZoomPlugin = createDragZoomPlugin(this._zoomState);

        const config = {
            type: "line",
            data: { labels: [], datasets },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                animation: false,
                interaction: { intersect: false, mode: "index" },
                plugins: {
                    legend: {
                        display: showLegend,
                        labels: {
                            color: "#64748b",
                            boxWidth: 8,
                            font: { size: 10 },
                            filter: (item) => item.text !== "+σ" && item.text !== "-σ" && !item.text.includes("(raw)"),
                        },
                    },
                    decimation: { enabled: true, algorithm: "lttb", samples: 200 },
                },
                scales: {
                    x: {
                        grid: { color: "rgba(148,163,184,0.06)" },
                        ticks: { color: "#64748b", maxTicksLimit: 8, font: { size: 10 } },
                        border: { color: "rgba(148,163,184,0.06)" },
                    },
                    y: {
                        grid: { color: "rgba(148,163,184,0.06)" },
                        ticks: { color: "#64748b", font: { size: 10 }, maxTicksLimit: 6 },
                        border: { color: "rgba(148,163,184,0.06)" },
                    },
                },
            },
            plugins: [crosshairPlugin, dragZoomPlugin],
        };

        const zoom = this._zoomState[def.id];
        if (zoom) {
            config.options.scales.x.min = zoom.min;
            config.options.scales.x.max = zoom.max;
        }

        const chart = new Chart(canvas, config);
        this.charts[def.id] = chart;
        this._chartRegistry.push(chart);
    }

    _updateAllCharts() {
        const steps = this.metrics.steps;
        const type = this.state.smoothingType;
        const alpha = this.state.smoothingWeight;
        const useSmoothing = type !== "none";

        for (const def of CHART_DEFS) {
            const chart = this.charts[def.id];
            if (!chart) continue;

            chart.data.labels = steps;
            let dsIdx = 0;

            for (const line of def.lines) {
                const raw = this.metrics[line.key];
                if (useSmoothing) {
                    chart.data.datasets[dsIdx].data = applySmoothing(steps, raw, type, alpha);
                    dsIdx++;
                    chart.data.datasets[dsIdx].data = [...raw];
                    dsIdx++;
                } else {
                    chart.data.datasets[dsIdx].data = [...raw];
                    dsIdx++;
                }
            }

            if (def.stdBand) {
                const mean = this.metrics[def.lines[0].key];
                const std = this.metrics[def.stdBand];
                const upper = mean.map((v, i) => v != null && std[i] != null ? v + std[i] : null);
                const lower = mean.map((v, i) => v != null && std[i] != null ? v - std[i] : null);
                chart.data.datasets[dsIdx].data = upper;
                dsIdx++;
                chart.data.datasets[dsIdx].data = lower;
                dsIdx++;
            }

            if (def.refLine) {
                chart.data.datasets[dsIdx].data = steps.map(() => def.refLine.value);
                dsIdx++;
            }

            chart.update("none");
        }
    }

    _createFullscreenChart(chartId) {
        const Chart = globalThis.Chart;
        if (!Chart) return;
        const def = CHART_DEFS.find(d => d.id === chartId);
        if (!def) return;

        const canvas = this.fullscreenCanvasRef.el;
        if (!canvas) return;

        if (this._fullscreenChart) {
            this._fullscreenChart.destroy();
            this._fullscreenChart = null;
        }

        const datasets = [];
        const useSmoothing = this.state.smoothingType !== "none";

        for (const line of def.lines) {
            datasets.push({
                label: line.label,
                data: [],
                borderColor: line.color,
                backgroundColor: "transparent",
                borderWidth: 2,
                pointRadius: 0,
                tension: 0.3,
                spanGaps: true,
            });
            if (useSmoothing) {
                datasets.push({
                    label: `${line.label} (raw)`,
                    data: [],
                    borderColor: line.color,
                    backgroundColor: "transparent",
                    borderWidth: 0.5,
                    pointRadius: 0,
                    tension: 0.3,
                    spanGaps: true,
                    _isGhost: true,
                });
            }
        }

        if (def.stdBand) {
            datasets.push({ label: "+σ", data: [], borderColor: "transparent", backgroundColor: "transparent", borderWidth: 0, pointRadius: 0, spanGaps: true, fill: false });
            datasets.push({ label: "-σ", data: [], borderColor: "transparent", backgroundColor: `${def.lines[0].color}18`, borderWidth: 0, pointRadius: 0, spanGaps: true, fill: "-1" });
        }

        if (def.refLine) {
            datasets.push({ label: def.refLine.label, data: [], borderColor: def.refLine.color, backgroundColor: "transparent", borderWidth: 1, pointRadius: 0, borderDash: [5, 3], spanGaps: true });
        }

        this._fullscreenChart = new Chart(canvas, {
            type: "line",
            data: { labels: [], datasets },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                animation: false,
                interaction: { intersect: false, mode: "index" },
                plugins: {
                    legend: { display: true, labels: { color: "#94a3b8", font: { size: 12 }, filter: (item) => !item.text.includes("σ") && !item.text.includes("(raw)") } },
                    decimation: { enabled: true, algorithm: "lttb", samples: 1000 },
                },
                scales: {
                    x: { grid: { color: "rgba(148,163,184,0.08)" }, ticks: { color: "#94a3b8", font: { size: 11 } }, border: { color: "rgba(148,163,184,0.08)" } },
                    y: { grid: { color: "rgba(148,163,184,0.08)" }, ticks: { color: "#94a3b8", font: { size: 11 } }, border: { color: "rgba(148,163,184,0.08)" } },
                },
            },
        });

        this._updateFullscreenChart();
    }

    _updateFullscreenChart() {
        if (!this._fullscreenChart || !this.state.fullscreenChartId) return;
        const def = CHART_DEFS.find(d => d.id === this.state.fullscreenChartId);
        if (!def) return;

        const steps = this.metrics.steps;
        const type = this.state.smoothingType;
        const alpha = this.state.smoothingWeight;
        const useSmoothing = type !== "none";
        const chart = this._fullscreenChart;

        chart.data.labels = steps;
        let dsIdx = 0;

        for (const line of def.lines) {
            const raw = this.metrics[line.key];
            if (useSmoothing) {
                chart.data.datasets[dsIdx].data = applySmoothing(steps, raw, type, alpha);
                dsIdx++;
                chart.data.datasets[dsIdx].data = [...raw];
                dsIdx++;
            } else {
                chart.data.datasets[dsIdx].data = [...raw];
                dsIdx++;
            }
        }

        if (def.stdBand) {
            const mean = this.metrics[def.lines[0].key];
            const std = this.metrics[def.stdBand];
            chart.data.datasets[dsIdx].data = mean.map((v, i) => v != null && std[i] != null ? v + std[i] : null);
            dsIdx++;
            chart.data.datasets[dsIdx].data = mean.map((v, i) => v != null && std[i] != null ? v - std[i] : null);
            dsIdx++;
        }

        if (def.refLine) {
            chart.data.datasets[dsIdx].data = steps.map(() => def.refLine.value);
            dsIdx++;
        }

        chart.update("none");
    }

    toggleSection(idx) {
        this.state.sections[idx].open = !this.state.sections[idx].open;
        if (this.state.sections[idx].open) {
            setTimeout(() => {
                requestAnimationFrame(() => {
                    const Chart = globalThis.Chart;
                    if (!Chart) return;
                    const sectionDefs = CHART_DEFS.filter(d => d.section === idx);
                    for (const def of sectionDefs) {
                        if (!this.charts[def.id]) {
                            this._createSingleChart(def, Chart);
                        }
                    }
                    this._updateAllCharts();
                });
            }, 50);
        }
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

    getChartsForSection(idx) {
        return CHART_DEFS.filter(d => d.section === idx);
    }

    _fmtMetric(val) {
        if (val == null) return "\u2014";
        return typeof val === "number" ? val.toFixed(4) : String(val);
    }

    formatMetric(val) {
        return val;
    }
}
