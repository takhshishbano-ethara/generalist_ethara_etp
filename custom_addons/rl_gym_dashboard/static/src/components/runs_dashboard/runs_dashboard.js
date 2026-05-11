/** @odoo-module **/
import { Component, useState, onWillStart, onMounted, onWillUnmount } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { rpc } from "@web/core/network/rpc";
import { useService } from "@web/core/utils/hooks";
import { loadBundle } from "@web/core/assets";
import { applySmoothing, createCrosshairPlugin, createDragZoomPlugin, CHART_DEFS } from "../chart_utils";

export class RunsDashboard extends Component {
    static template = "rl_gym_dashboard.RunsDashboard";
    static props = ["*"];

    setup() {
        this.action = useService("action");

        this.state = useState({
            view: "list",
            runs: [],
            loading: true,
            searchQuery: "",
            stateFilter: "all",
            sortBy: "recent",
            selectedRun: null,
            smoothingType: "ema",
            smoothingWeight: 0.6,
            ignoreOutliers: false,
            sections: [
                { title: "Training Overview", open: true, pinned: false },
                { title: "Loss Breakdown", open: true, pinned: false },
                { title: "Reward & Advantage", open: true, pinned: false },
                { title: "Policy Diagnostics", open: true, pinned: false },
                { title: "LR & Gradient", open: true, pinned: false },
                { title: "System", open: true, pinned: false },
            ],
            fullscreenChartId: null,
            detailMetrics: null,
            detailLoading: false,
            kpiTick: 0,
            weightsOpen: true,
            inferenceOpen: true,
            weightId: null,
            weightUploading: false,
            weightS3Url: null,
            weightError: null,
            inferencePrompt: "",
            inferenceMaxTokens: 256,
            inferenceTemperature: 0.7,
            inferenceRunning: false,
            inferenceResponse: null,
        });

        this.sparklineCharts = {};
        this.detailCharts = {};
        this._chartRegistry = [];
        this._crosshair = { step: null, sourceId: null };
        this._zoomState = {};
        this._fullscreenChart = null;
        this._pollInterval = null;
        this.metrics = null;

        onWillStart(async () => {
            await loadBundle("web.chartjs_lib");
        });

        onMounted(() => {
            this._loadRuns();
        });

        onWillUnmount(() => {
            this._destroySparklines();
            this._destroyDetailCharts();
            this._stopPolling();
        });
    }

    async _loadRuns() {
        this.state.loading = true;
        try {
            const result = await rpc("/rl_gym/dashboard/runs", {});
            this.state.runs = result.runs || result || [];
        } catch (e) {
            console.error("Failed to load runs:", e);
            this.state.runs = [];
        }
        this.state.loading = false;
        requestAnimationFrame(() => this._renderSparklines());
    }

    get filteredRuns() {
        let runs = [...this.state.runs];
        if (this.state.searchQuery) {
            const q = this.state.searchQuery.toLowerCase();
            runs = runs.filter(r =>
                (r.name || "").toLowerCase().includes(q) ||
                (r.model_name || "").toLowerCase().includes(q)
            );
        }
        if (this.state.stateFilter !== "all") {
            runs = runs.filter(r => r.state === this.state.stateFilter);
        }
        if (this.state.sortBy === "recent") {
            runs.sort((a, b) => (b.id || 0) - (a.id || 0));
        } else if (this.state.sortBy === "reward") {
            runs.sort((a, b) => (b.best_reward || 0) - (a.best_reward || 0));
        } else if (this.state.sortBy === "name") {
            runs.sort((a, b) => (a.name || "").localeCompare(b.name || ""));
        }
        return runs;
    }

    async _renderSparklines() {
        const Chart = globalThis.Chart;
        if (!Chart) return;
        this._destroySparklines();

        const runs = this.filteredRuns;
        const results = await Promise.all(
            runs.map(run =>
                rpc("/rl_gym/dashboard/sparkline", { job_id: run.id })
                    .then(data => ({ runId: run.id, data }))
                    .catch(() => ({ runId: run.id, data: {} }))
            )
        );
        for (const { runId, data } of results) {
            this._createSparkline(runId, "loss", data.loss || [], "#3b82f6", Chart);
            this._createSparkline(runId, "reward", data.reward || [], "#10b981", Chart);
        }
    }

    _createSparkline(runId, type, data, color, Chart) {
        const canvas = document.querySelector(`[data-spark-id="${type}_${runId}"]`);
        if (!canvas || !data.length) return;

        const chart = new Chart(canvas, {
            type: "line",
            data: {
                labels: data.map((_, i) => i),
                datasets: [{
                    data,
                    borderColor: color,
                    backgroundColor: "transparent",
                    borderWidth: 1.5,
                    pointRadius: 0,
                    tension: 0.3,
                    spanGaps: true,
                }],
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                animation: false,
                plugins: {
                    legend: { display: false },
                    tooltip: { enabled: false },
                },
                scales: {
                    x: { display: false },
                    y: { display: false },
                },
                elements: {
                    point: { radius: 0 },
                    line: { borderWidth: 1.5 },
                },
            },
        });

        if (!this.sparklineCharts[runId]) {
            this.sparklineCharts[runId] = {};
        }
        this.sparklineCharts[runId][type] = chart;
    }

    _destroySparklines() {
        for (const runId in this.sparklineCharts) {
            const charts = this.sparklineCharts[runId];
            if (charts.loss) charts.loss.destroy();
            if (charts.reward) charts.reward.destroy();
        }
        this.sparklineCharts = {};
    }

    startNewTraining() {
        this.action.doAction("rl_gym_dashboard.action_training_wizard");
    }

    async openRunDetail(run) {
        this.state.view = "detail";
        this.state.selectedRun = run;
        this.state.detailLoading = true;
        this._destroySparklines();

        this.metrics = {
            steps: [], loss: [], reward: [], reward_mean: [], reward_std: [],
            best_reward: [], policy_loss: [], value_loss: [], entropy: [],
            kl_divergence: [], clip_fraction: [], advantage_mean: [],
            gradient_norm: [], learning_rate: [], tokens_per_second: [],
            samples_per_second: [], gpu_memory_used: [],
            cpu_percent: [], memory_percent: [], gpu_utilization: [],
        };

        try {
            const result = await rpc("/rl_gym/training/metrics", { job_id: run.id, since_step: 0 });
            this._processMetricsResult(result);
            this.state.detailMetrics = this.metrics;
        } catch (e) {
            console.error("Failed to load run metrics:", e);
        }

        this.state.detailLoading = false;
        // Retry until OWL has committed chart canvases to DOM
        this._waitForChartsAndCreate();

        if (run.state === "training") {
            this._startPolling(run.id);
        }
    }

    _processMetricsResult(result) {
        const records = result.metrics || result.records || result || [];
        if (!Array.isArray(records)) return;

        for (const rec of records) {
            const step = rec.step || rec.current_step || 0;
            this.metrics.steps.push(step);

            const keys = [
                ["loss", "loss"], ["loss", "current_loss"],
                ["reward", "reward"], ["reward", "current_reward"],
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
                if (rec[resultKey] != null && this.metrics[metricKey].length < this.metrics.steps.length) {
                    this.metrics[metricKey].push(rec[resultKey]);
                }
            }

            for (const metricKey of Object.keys(this.metrics)) {
                if (metricKey === "steps") continue;
                while (this.metrics[metricKey].length < this.metrics.steps.length) {
                    this.metrics[metricKey].push(null);
                }
            }

            const prevBest = this.metrics.best_reward.length > 1
                ? this.metrics.best_reward[this.metrics.best_reward.length - 2]
                : -Infinity;
            const curReward = this.metrics.reward[this.metrics.reward.length - 1];
            this.metrics.best_reward[this.metrics.best_reward.length - 1] =
                curReward != null ? Math.max(prevBest, curReward) : prevBest === -Infinity ? null : prevBest;
        }
    }

    backToList() {
        this._destroyDetailCharts();
        this._stopPolling();
        this.state.view = "list";
        this.state.selectedRun = null;
        this.state.detailMetrics = null;
        this.state.fullscreenChartId = null;
        this.metrics = null;
        requestAnimationFrame(() => this._renderSparklines());
    }

    _waitForChartsAndCreate(attempt = 0) {
        if (attempt > 20) return;
        const firstDef = CHART_DEFS[0];
        const canvas = document.querySelector(`[data-chart-id="${firstDef.id}"]`);
        if (canvas) {
            this._createDetailCharts();
        } else {
            setTimeout(() => this._waitForChartsAndCreate(attempt + 1), 50);
        }
    }

    _createDetailCharts() {
        const Chart = globalThis.Chart;
        if (!Chart || !this.metrics) return;

        for (const def of CHART_DEFS) {
            if (!this.state.sections[def.section].open) continue;
            this._createSingleDetailChart(def, Chart);
        }
        this._updateDetailCharts();
    }

    _createSingleDetailChart(def, Chart) {
        if (!Chart) Chart = globalThis.Chart;
        if (!Chart) return;

        const canvas = document.querySelector(`[data-chart-id="${def.id}"]`);
        if (!canvas) return;

        if (this.detailCharts[def.id]) {
            this.detailCharts[def.id].destroy();
            const idx = this._chartRegistry.indexOf(this.detailCharts[def.id]);
            if (idx > -1) this._chartRegistry.splice(idx, 1);
            delete this.detailCharts[def.id];
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
        this.detailCharts[def.id] = chart;
        this._chartRegistry.push(chart);
    }

    _updateDetailCharts() {
        if (!this.metrics) return;
        const steps = this.metrics.steps;
        const type = this.state.smoothingType;
        const alpha = this.state.smoothingWeight;
        const useSmoothing = type !== "none";

        for (const def of CHART_DEFS) {
            const chart = this.detailCharts[def.id];
            if (!chart) continue;

            chart.data.labels = steps;
            let dsIdx = 0;

            for (const line of def.lines) {
                const raw = this.metrics[line.key] || [];
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
                const mean = this.metrics[def.lines[0].key] || [];
                const std = this.metrics[def.stdBand] || [];
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

    _destroyDetailCharts() {
        if (this._fullscreenChart) {
            this._fullscreenChart.destroy();
            this._fullscreenChart = null;
        }
        for (const id in this.detailCharts) {
            if (this.detailCharts[id]) this.detailCharts[id].destroy();
        }
        this.detailCharts = {};
        this._chartRegistry = [];
    }

    _startPolling(jobId) {
        this._stopPolling();

        this._pollInterval = setInterval(async () => {
            try {
                const sinceStep = this.metrics && this.metrics.steps.length > 0
                    ? this.metrics.steps[this.metrics.steps.length - 1]
                    : 0;
                const result = await rpc("/rl_gym/training/metrics", { job_id: jobId, since_step: sinceStep });
                const records = result.metrics || result.records || result || [];
                if (Array.isArray(records) && records.length > 0) {
                    this._processMetricsResult({ metrics: records });
                    this._updateDetailCharts();
                    this._updateFullscreenChart();
                }
                const status = await rpc("/rl_gym/training/status", { job_id: jobId });
                if (status) {
                    if (this.state.selectedRun) {
                        this.state.selectedRun.current_step = status.current_step || this.state.selectedRun.current_step;
                        this.state.selectedRun.progress = status.progress || this.state.selectedRun.progress;
                    }
                    this.state.kpiTick = (this.state.kpiTick || 0) + 1;
                    if (status.state !== "training") {
                        this._stopPolling();
                        if (this.state.selectedRun) {
                            this.state.selectedRun.state = status.state;
                        }
                    }
                }
            } catch (e) {
                // polling error, continue
            }
        }, 1500 + Math.floor(Math.random() * 1000));
    }

    _stopPolling() {
        if (this._pollInterval) {
            clearInterval(this._pollInterval);
            this._pollInterval = null;
        }
    }

    toggleSection(idx) {
        this.state.sections[idx].open = !this.state.sections[idx].open;
        if (this.state.sections[idx].open) {
            requestAnimationFrame(() => {
                const Chart = globalThis.Chart;
                if (!Chart) return;
                const sectionDefs = CHART_DEFS.filter(d => d.section === idx);
                for (const def of sectionDefs) {
                    if (!this.detailCharts[def.id]) {
                        this._createSingleDetailChart(def, Chart);
                    }
                }
                this._updateDetailCharts();
            });
        }
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

    _createFullscreenChart(chartId) {
        const Chart = globalThis.Chart;
        if (!Chart || !this.metrics) return;
        const def = CHART_DEFS.find(d => d.id === chartId);
        if (!def) return;

        const canvas = document.querySelector(".rl-wb__fullscreen-body canvas");
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
        if (!this._fullscreenChart || !this.state.fullscreenChartId || !this.metrics) return;
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
            const raw = this.metrics[line.key] || [];
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
            const mean = this.metrics[def.lines[0].key] || [];
            const std = this.metrics[def.stdBand] || [];
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

    getFullscreenDef() {
        return CHART_DEFS.find(d => d.id === this.state.fullscreenChartId) || null;
    }

    getChartsForSection(sectionIdx) {
        return CHART_DEFS.filter(d => d.section === sectionIdx);
    }

    onSmoothingTypeChange(ev) {
        const prev = this.state.smoothingType;
        this.state.smoothingType = ev.target.value;
        const needsRebuild = (prev === "none") !== (ev.target.value === "none");
        if (needsRebuild) {
            this._destroyDetailCharts();
            requestAnimationFrame(() => {
                this._createDetailCharts();
            });
        } else {
            this._updateDetailCharts();
            this._updateFullscreenChart();
        }
    }

    onSmoothingWeightChange(ev) {
        this.state.smoothingWeight = parseFloat(ev.target.value);
        this._updateDetailCharts();
        this._updateFullscreenChart();
    }

    toggleOutliers() {
        this.state.ignoreOutliers = !this.state.ignoreOutliers;
        this._updateDetailCharts();
        this._updateFullscreenChart();
    }

    onSearchChange(ev) {
        this.state.searchQuery = ev.target.value;
    }

    onStateFilterChange(ev) {
        this.state.stateFilter = ev.target.value;
    }

    onSortChange(ev) {
        this.state.sortBy = ev.target.value;
    }

    _fmtMetric(val) {
        if (val == null) return "\u2014";
        return typeof val === "number" ? val.toFixed(4) : String(val);
    }

    _fmtShort(val) {
        if (val == null) return "\u2014";
        if (typeof val !== "number") return String(val);
        if (Math.abs(val) < 0.001) return val.toExponential(2);
        return val.toFixed(3);
    }

    getKpi(key) {
        void this.state.kpiTick;
        if (!this.metrics || !this.metrics[key] || !this.metrics[key].length) return "\u2014";
        const arr = this.metrics[key].filter(v => v != null);
        if (!arr.length) return "\u2014";
        return this._fmtShort(arr[arr.length - 1]);
    }

    async uploadWeights() {
        const jobId = this.state.selectedRun?.id;
        if (!jobId) return;
        this.state.weightUploading = true;
        this.state.weightError = null;
        try {
            if (!this.state.weightId) {
                const wr = await rpc("/rl_gym/weights/create", {
                    values: { job_id: jobId, name: this.state.selectedRun.name + "-weights", format: "safetensors" },
                });
                this.state.weightId = wr.id;
            }
            const result = await rpc("/rl_gym/weights/upload", { weight_id: this.state.weightId });
            if (result.error) {
                this.state.weightError = result.error;
            } else {
                this.state.weightS3Url = result.s3_url;
            }
        } catch (e) {
            this.state.weightError = e.message || "Upload failed";
        }
        this.state.weightUploading = false;
    }

    copyWeightUrl() {
        if (this.state.weightS3Url) {
            navigator.clipboard.writeText(this.state.weightS3Url).catch(() => {});
        }
    }

    async runInference() {
        if (!this.state.inferencePrompt.trim() || this.state.inferenceRunning) return;
        this.state.inferenceRunning = true;
        this.state.inferenceResponse = null;
        try {
            const result = await rpc("/rl_gym/inference/run", {
                job_id: this.state.selectedRun?.id,
                prompt: this.state.inferencePrompt,
                max_tokens: this.state.inferenceMaxTokens,
                temperature: this.state.inferenceTemperature,
            });
            this.state.inferenceResponse = result;
        } catch (e) {
            this.state.inferenceResponse = { response: "Error: " + (e.message || "Inference failed"), tokens_used: 0 };
        }
        this.state.inferenceRunning = false;
    }
}

registry.category("actions").add("rl_gym_dashboard.runs_dashboard", RunsDashboard);
