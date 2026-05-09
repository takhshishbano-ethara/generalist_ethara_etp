/** @odoo-module **/
import { Component, useState, useRef, onWillStart, onMounted, onWillUnmount } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { loadBundle } from "@web/core/assets";

function mulberry32(seed) {
    return function () {
        seed |= 0;
        seed = (seed + 0x6d2b79f5) | 0;
        let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
        t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
        return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
}

function gaussianNoise(rng) {
    const u1 = rng();
    const u2 = rng();
    return Math.sqrt(-2.0 * Math.log(u1 + 1e-10)) * Math.cos(2.0 * Math.PI * u2);
}

function ema(values, alpha = 0.05) {
    const result = [values[0]];
    for (let i = 1; i < values.length; i++) {
        result.push(alpha * values[i] + (1 - alpha) * result[i - 1]);
    }
    return result;
}

const ALGORITHMS = ["PPO", "SAC", "TD3", "A2C", "DDPG"];
const ENVIRONMENTS = [
    "HalfCheetah-v4",
    "Hopper-v4",
    "Walker2d-v4",
    "Ant-v4",
    "Humanoid-v4",
    "Swimmer-v4",
];

class RlGymDashboard extends Component {
    static template = "rl_gym_dashboard.Dashboard";
    static props = ["*"];

    setup() {
        this.rewardChartRef = useRef("rewardChart");
        this.lossChartRef = useRef("lossChart");
        this.explorationChartRef = useRef("explorationChart");
        this.gradientChartRef = useRef("gradientChart");

        this.charts = {};
        this.intervalId = null;
        this.simData = {
            rewards: [],
            rewardsSmoothed: [],
            policyLoss: [],
            valueLoss: [],
            entropyLoss: [],
            entropy: [],
            gradientNorm: [],
            labels: [],
            lossLabels: [],
        };

        this.state = useState({
            totalEpisodes: 0,
            totalSteps: 0,
            currentReward: 0,
            bestReward: -Infinity,
            avgReward: 0,
            fps: 0,
            trainingTime: "00:00:00",
            algorithm: "PPO",
            environment: "HalfCheetah-v4",
            learningRate: "3.00e-4",
            entropyCoeff: 0.01,
            clipRange: 0.2,
            recentEpisodes: [],
        });

        onWillStart(async () => {
            await loadBundle("web.chartjs_lib");
        });

        onMounted(() => {
            this._initSimulation();
            this._createCharts();
            this.intervalId = setInterval(() => this._tick(), 2000);
        });

        onWillUnmount(() => {
            if (this.intervalId) {
                clearInterval(this.intervalId);
            }
            Object.values(this.charts).forEach((c) => c.destroy());
        });
    }

    _initSimulation() {
        const sessionSeed = Math.floor(Date.now() / 60000);
        this.rng = mulberry32(sessionSeed);

        const algoIdx = Math.floor(this.rng() * ALGORITHMS.length);
        const envIdx = Math.floor(this.rng() * ENVIRONMENTS.length);
        this.state.algorithm = ALGORITHMS[algoIdx];
        this.state.environment = ENVIRONMENTS[envIdx];

        this.sessionStartTime = Date.now() - 200 * 2000;
        this.currentEpisode = 0;
        this.currentStep = 0;
        this.maxReward = -Infinity;
        this.baseLR = 3e-4;

        const historyLength = 200;
        for (let i = 0; i < historyLength; i++) {
            this._generateDataPoint(i);
        }

        this._updateKPIs();
    }

    _generateDataPoint(episodeIndex) {
        const progress = episodeIndex / 300;
        const convergenceFactor = 1 - Math.exp(-3 * progress);

        // Reward: starts ~-200, converges toward ~2500-4000 range
        const targetReward = 3000 + this.rng() * 1000;
        const baseReward = -200 + (targetReward + 200) * convergenceFactor;
        const noiseScale = 150 * (1 - convergenceFactor * 0.7);
        const reward = baseReward + gaussianNoise(this.rng) * noiseScale;

        // Policy loss: starts ~0.5, converges toward 0
        const policyLoss =
            0.5 * (1 - convergenceFactor) * (0.8 + 0.4 * this.rng()) +
            gaussianNoise(this.rng) * 0.02;

        // Value loss: starts ~1.0, converges toward 0.01
        const valueLoss =
            1.0 * Math.exp(-4 * progress) * (0.7 + 0.6 * this.rng()) +
            Math.abs(gaussianNoise(this.rng) * 0.005);

        // Entropy: starts ~2.0, decreases toward ~0.3
        const entropy = 2.0 * (1 - convergenceFactor * 0.85) + gaussianNoise(this.rng) * 0.05;

        // Gradient norm: ~0.5-2.0 with spikes
        let gradNorm = 0.8 + this.rng() * 0.8 + Math.abs(gaussianNoise(this.rng) * 0.3);
        if (this.rng() < 0.03) {
            gradNorm += 3 + this.rng() * 5;
        }

        this.simData.rewards.push(reward);
        this.simData.policyLoss.push(Math.max(0, policyLoss));
        this.simData.valueLoss.push(Math.max(0.001, valueLoss));
        this.simData.entropyLoss.push(Math.max(0.05, entropy));
        this.simData.entropy.push(Math.max(0.05, entropy));
        this.simData.gradientNorm.push(Math.max(0.1, gradNorm));

        this.currentEpisode++;
        this.currentStep += 2048 + Math.floor(this.rng() * 512) - 256;

        this.simData.labels.push(this.currentEpisode);
        this.simData.lossLabels.push(Math.round(this.currentStep / 1000) + "k");

        if (reward > this.maxReward) {
            this.maxReward = reward;
        }

        this.simData.rewardsSmoothed = ema(this.simData.rewards, 0.08);
    }

    _tick() {
        this._generateDataPoint(this.simData.rewards.length);

        const maxPoints = 200;
        if (this.simData.rewards.length > maxPoints) {
            this.simData.rewards.shift();
            this.simData.rewardsSmoothed.shift();
            this.simData.policyLoss.shift();
            this.simData.valueLoss.shift();
            this.simData.entropyLoss.shift();
            this.simData.entropy.shift();
            this.simData.gradientNorm.shift();
            this.simData.labels.shift();
            this.simData.lossLabels.shift();
        }

        this._updateCharts();
        this._updateKPIs();
    }

    _updateKPIs() {
        const rewards = this.simData.rewards;
        const last100 = rewards.slice(-100);

        this.state.totalEpisodes = this.currentEpisode;
        this.state.totalSteps = this.currentStep;
        this.state.currentReward = Math.round(rewards[rewards.length - 1] || 0);
        this.state.bestReward = Math.round(this.maxReward);
        this.state.avgReward = Math.round(
            last100.reduce((a, b) => a + b, 0) / (last100.length || 1)
        );
        this.state.fps = Math.round(900 + gaussianNoise(this.rng) * 80);
        if (this.state.fps < 600) this.state.fps = 600;
        if (this.state.fps > 1400) this.state.fps = 1400;

        const elapsed = Date.now() - this.sessionStartTime;
        const hours = Math.floor(elapsed / 3600000);
        const minutes = Math.floor((elapsed % 3600000) / 60000);
        const seconds = Math.floor((elapsed % 60000) / 1000);
        this.state.trainingTime = `${String(hours).padStart(2, "0")}:${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;

        const progress = this.currentEpisode / 300;
        const currentLR = this.baseLR * Math.max(0.01, 1 - progress * 0.8);
        this.state.learningRate = currentLR.toExponential(2);

        const episodeLength = 500 + Math.floor(this.rng() * 500);
        const newEpisode = {
            episode: this.currentEpisode,
            reward: Math.round(rewards[rewards.length - 1] || 0),
            length: episodeLength,
            timestamp: this.state.trainingTime,
        };

        const recent = [newEpisode, ...this.state.recentEpisodes];
        this.state.recentEpisodes = recent.slice(0, 10);
    }

    _createCharts() {
        const Chart = globalThis.Chart;

        const baseOptions = {
            responsive: true,
            maintainAspectRatio: false,
            animation: { duration: 800 },
            interaction: { intersect: false, mode: "index" },
            plugins: {
                legend: {
                    display: false,
                },
            },
            scales: {
                x: {
                    grid: { color: "rgba(148, 163, 184, 0.08)" },
                    ticks: { color: "#94a3b8", maxTicksLimit: 8, font: { size: 10 } },
                    border: { color: "rgba(148, 163, 184, 0.15)" },
                },
                y: {
                    grid: { color: "rgba(148, 163, 184, 0.08)" },
                    ticks: { color: "#94a3b8", font: { size: 10 } },
                    border: { color: "rgba(148, 163, 184, 0.15)" },
                },
            },
        };

        this.charts.reward = new Chart(this.rewardChartRef.el, {
            type: "line",
            data: {
                labels: [...this.simData.labels],
                datasets: [
                    {
                        label: "Raw Reward",
                        data: [...this.simData.rewards],
                        borderColor: "rgba(16, 185, 129, 0.25)",
                        backgroundColor: "rgba(16, 185, 129, 0.03)",
                        borderWidth: 1,
                        pointRadius: 0,
                        tension: 0.1,
                        fill: true,
                    },
                    {
                        label: "Smoothed Reward (EMA)",
                        data: [...this.simData.rewardsSmoothed],
                        borderColor: "#10b981",
                        backgroundColor: "rgba(16, 185, 129, 0.1)",
                        borderWidth: 2.5,
                        pointRadius: 0,
                        tension: 0.3,
                        fill: true,
                    },
                ],
            },
            options: {
                ...baseOptions,
                plugins: {
                    ...baseOptions.plugins,
                    legend: { display: true, labels: { color: "#94a3b8", boxWidth: 12, font: { size: 11 } } },
                },
                scales: {
                    ...baseOptions.scales,
                    y: {
                        ...baseOptions.scales.y,
                        title: { display: true, text: "Reward", color: "#64748b", font: { size: 11 } },
                    },
                    x: {
                        ...baseOptions.scales.x,
                        title: { display: true, text: "Episode", color: "#64748b", font: { size: 11 } },
                    },
                },
            },
        });

        this.charts.loss = new Chart(this.lossChartRef.el, {
            type: "line",
            data: {
                labels: [...this.simData.lossLabels],
                datasets: [
                    {
                        label: "Policy Loss",
                        data: [...this.simData.policyLoss],
                        borderColor: "#3b82f6",
                        borderWidth: 1.8,
                        pointRadius: 0,
                        tension: 0.3,
                    },
                    {
                        label: "Value Loss",
                        data: [...this.simData.valueLoss],
                        borderColor: "#f59e0b",
                        borderWidth: 1.8,
                        pointRadius: 0,
                        tension: 0.3,
                    },
                    {
                        label: "Entropy",
                        data: [...this.simData.entropyLoss],
                        borderColor: "#8b5cf6",
                        borderWidth: 1.8,
                        pointRadius: 0,
                        tension: 0.3,
                    },
                ],
            },
            options: {
                ...baseOptions,
                plugins: {
                    legend: { display: true, labels: { color: "#94a3b8", boxWidth: 10, font: { size: 10 } } },
                },
                scales: {
                    ...baseOptions.scales,
                    y: {
                        ...baseOptions.scales.y,
                        title: { display: true, text: "Loss", color: "#64748b", font: { size: 11 } },
                    },
                    x: {
                        ...baseOptions.scales.x,
                        title: { display: true, text: "Steps", color: "#64748b", font: { size: 11 } },
                    },
                },
            },
        });

        this.charts.exploration = new Chart(this.explorationChartRef.el, {
            type: "line",
            data: {
                labels: [...this.simData.labels],
                datasets: [
                    {
                        label: "Entropy",
                        data: [...this.simData.entropy],
                        borderColor: "#06b6d4",
                        backgroundColor: "rgba(6, 182, 212, 0.08)",
                        borderWidth: 2,
                        pointRadius: 0,
                        tension: 0.3,
                        fill: true,
                    },
                ],
            },
            options: {
                ...baseOptions,
                scales: {
                    ...baseOptions.scales,
                    y: {
                        ...baseOptions.scales.y,
                        title: { display: true, text: "Entropy", color: "#64748b", font: { size: 11 } },
                        min: 0,
                    },
                    x: {
                        ...baseOptions.scales.x,
                        title: { display: true, text: "Episode", color: "#64748b", font: { size: 11 } },
                    },
                },
            },
        });

        const gradientThreshold = Array(this.simData.gradientNorm.length).fill(10.0);
        this.charts.gradient = new Chart(this.gradientChartRef.el, {
            type: "line",
            data: {
                labels: [...this.simData.lossLabels],
                datasets: [
                    {
                        label: "Gradient Norm",
                        data: [...this.simData.gradientNorm],
                        borderColor: "#f43f5e",
                        backgroundColor: "rgba(244, 63, 94, 0.06)",
                        borderWidth: 1.8,
                        pointRadius: 0,
                        tension: 0.2,
                        fill: true,
                    },
                    {
                        label: "Clip Threshold",
                        data: gradientThreshold,
                        borderColor: "rgba(244, 63, 94, 0.4)",
                        borderWidth: 1.5,
                        borderDash: [6, 4],
                        pointRadius: 0,
                        tension: 0,
                        fill: false,
                    },
                ],
            },
            options: {
                ...baseOptions,
                plugins: {
                    legend: { display: true, labels: { color: "#94a3b8", boxWidth: 10, font: { size: 10 } } },
                },
                scales: {
                    ...baseOptions.scales,
                    y: {
                        ...baseOptions.scales.y,
                        title: { display: true, text: "Norm", color: "#64748b", font: { size: 11 } },
                        min: 0,
                    },
                    x: {
                        ...baseOptions.scales.x,
                        title: { display: true, text: "Steps", color: "#64748b", font: { size: 11 } },
                    },
                },
            },
        });
    }

    _updateCharts() {
        const rewardChart = this.charts.reward;
        rewardChart.data.labels = [...this.simData.labels];
        rewardChart.data.datasets[0].data = [...this.simData.rewards];
        rewardChart.data.datasets[1].data = [...this.simData.rewardsSmoothed];
        rewardChart.update("none");

        const lossChart = this.charts.loss;
        lossChart.data.labels = [...this.simData.lossLabels];
        lossChart.data.datasets[0].data = [...this.simData.policyLoss];
        lossChart.data.datasets[1].data = [...this.simData.valueLoss];
        lossChart.data.datasets[2].data = [...this.simData.entropyLoss];
        lossChart.update("none");

        const explorationChart = this.charts.exploration;
        explorationChart.data.labels = [...this.simData.labels];
        explorationChart.data.datasets[0].data = [...this.simData.entropy];
        explorationChart.update("none");

        const gradientChart = this.charts.gradient;
        gradientChart.data.labels = [...this.simData.lossLabels];
        gradientChart.data.datasets[0].data = [...this.simData.gradientNorm];
        gradientChart.data.datasets[1].data = Array(this.simData.gradientNorm.length).fill(10.0);
        gradientChart.update("none");
    }

    formatNumber(num) {
        if (num >= 1e6) return (num / 1e6).toFixed(2) + "M";
        if (num >= 1e3) return (num / 1e3).toFixed(1) + "k";
        return String(num);
    }
}

registry.category("actions").add("rl_gym_dashboard.main", RlGymDashboard);
