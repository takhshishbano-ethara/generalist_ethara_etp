/** @odoo-module **/

export function smoothEma(data, alpha) {
    if (!data.length) return [];
    const out = new Array(data.length);
    out[0] = data[0];
    for (let i = 1; i < data.length; i++) {
        if (data[i] == null) {
            out[i] = out[i - 1];
        } else if (out[i - 1] == null) {
            out[i] = data[i];
        } else {
            out[i] = alpha * data[i] + (1 - alpha) * out[i - 1];
        }
    }
    return out;
}

export function smoothTwema(steps, data, alpha) {
    if (!data.length) return [];
    const w = Math.min(Math.sqrt(alpha), 0.999);
    const range = (steps[steps.length - 1] - steps[0]) || 1;
    const out = new Array(data.length);
    let lastY = 0;
    let debiasW = 0;
    let initialized = false;
    for (let i = 0; i < data.length; i++) {
        const y = data[i];
        if (y == null) { out[i] = i > 0 ? out[i - 1] : null; continue; }
        if (!initialized) { lastY = y; debiasW = 1; out[i] = y; initialized = true; continue; }
        const dx = ((steps[i] - steps[i - 1]) / range) * 100;
        const adj = Math.pow(w, dx);
        lastY = lastY * adj + y;
        debiasW = debiasW * adj + 1;
        out[i] = lastY / debiasW;
    }
    return out;
}

export function smoothGaussian(data, alpha) {
    if (!data.length) return [];
    const sigma = alpha * 10;
    if (sigma < 0.1) return [...data];
    const radius = Math.ceil(sigma * 3);
    const kernel = [];
    let kSum = 0;
    for (let j = -radius; j <= radius; j++) {
        const v = Math.exp(-(j * j) / (2 * sigma * sigma));
        kernel.push(v);
        kSum += v;
    }
    for (let j = 0; j < kernel.length; j++) kernel[j] /= kSum;
    const n = data.length;
    const out = new Array(n);
    for (let i = 0; i < n; i++) {
        let sum = 0, wt = 0;
        for (let j = -radius; j <= radius; j++) {
            let idx = i + j;
            if (idx < 0) idx = -idx;
            if (idx >= n) idx = 2 * n - idx - 2;
            if (idx < 0 || idx >= n) continue;
            if (data[idx] == null) continue;
            const kv = kernel[j + radius];
            sum += data[idx] * kv;
            wt += kv;
        }
        out[i] = wt > 0 ? sum / wt : data[i];
    }
    return out;
}

export function smoothRunningAvg(data, alpha) {
    if (!data.length) return [];
    const window = Math.max(1, Math.round(alpha * 40 + 1));
    const n = data.length;
    const out = new Array(n);
    for (let i = 0; i < n; i++) {
        let sum = 0, count = 0;
        const start = Math.max(0, i - Math.floor(window / 2));
        const end = Math.min(n - 1, i + Math.floor(window / 2));
        for (let j = start; j <= end; j++) {
            if (data[j] != null) { sum += data[j]; count++; }
        }
        out[i] = count > 0 ? sum / count : data[i];
    }
    return out;
}

export function applySmoothing(steps, data, type, alpha) {
    if (type === "none" || !data.length) return [...data];
    if (type === "ema") return smoothEma(data, alpha);
    if (type === "twema") return smoothTwema(steps, data, alpha);
    if (type === "gaussian") return smoothGaussian(data, alpha);
    if (type === "running") return smoothRunningAvg(data, alpha);
    return [...data];
}

export function createCrosshairPlugin(chartRegistry, crosshairState) {
    let _rafId = null;
    return {
        id: "rlCrosshair",
        afterEvent(chart, args) {
            const evt = args.event;
            if (evt.type === "mousemove") {
                const xScale = chart.scales.x;
                if (!xScale) return;
                crosshairState.step = xScale.getValueForPixel(evt.x);
                crosshairState.sourceId = chart.id;
                if (!_rafId) {
                    _rafId = requestAnimationFrame(() => {
                        _rafId = null;
                        for (const c of chartRegistry) {
                            if (c !== chart && !c._destroyed) c.draw();
                        }
                    });
                }
            } else if (evt.type === "mouseout") {
                crosshairState.step = null;
                crosshairState.sourceId = null;
                for (const c of chartRegistry) {
                    if (c !== chart && !c._destroyed) c.draw();
                }
            }
        },
        afterDraw(chart) {
            if (crosshairState.step == null) return;
            const xScale = chart.scales.x;
            if (!xScale) return;
            const px = xScale.getPixelForValue(crosshairState.step);
            if (px < xScale.left || px > xScale.right) return;
            const ctx = chart.ctx;
            ctx.save();
            ctx.beginPath();
            ctx.setLineDash([4, 3]);
            ctx.strokeStyle = "rgba(148,163,184,0.5)";
            ctx.lineWidth = 1;
            ctx.moveTo(px, chart.chartArea.top);
            ctx.lineTo(px, chart.chartArea.bottom);
            ctx.stroke();
            ctx.restore();
        },
    };
}

export function createDragZoomPlugin(zoomState) {
    return {
        id: "rlDragZoom",
        afterInit(chart) {
            const canvas = chart.canvas;
            let dragging = false;
            let startX = null;
            let endX = null;
            chart._dragZoom = { dragging: false, startPx: 0, endPx: 0 };

            const onMouseDown = (e) => {
                if (e.button !== 0) return;
                const xScale = chart.scales.x;
                if (!xScale) return;
                const rect = canvas.getBoundingClientRect();
                const px = e.clientX - rect.left;
                if (px < xScale.left || px > xScale.right) return;
                dragging = true;
                startX = xScale.getValueForPixel(px);
                chart._dragZoom.dragging = true;
                chart._dragZoom.startPx = px;
                chart._dragZoom.endPx = px;
            };

            const onMouseMove = (e) => {
                if (!dragging) return;
                const rect = canvas.getBoundingClientRect();
                const px = e.clientX - rect.left;
                const xScale = chart.scales.x;
                endX = xScale.getValueForPixel(px);
                chart._dragZoom.endPx = px;
                chart.draw();
            };

            const finishDrag = () => {
                if (!dragging) return;
                dragging = false;
                chart._dragZoom.dragging = false;
                if (startX != null && endX != null && Math.abs(endX - startX) > 1) {
                    const mn = Math.min(startX, endX);
                    const mx = Math.max(startX, endX);
                    chart.options.scales.x.min = mn;
                    chart.options.scales.x.max = mx;
                    zoomState[chart.canvas.dataset.chartId] = { min: mn, max: mx };
                    chart.update("none");
                }
                startX = null;
                endX = null;
            };

            const onMouseLeave = () => {
                if (dragging) {
                    dragging = false;
                    chart._dragZoom.dragging = false;
                    startX = null;
                    endX = null;
                    chart.draw();
                }
            };

            const onDblClick = () => {
                delete chart.options.scales.x.min;
                delete chart.options.scales.x.max;
                const cid = chart.canvas.dataset.chartId;
                if (cid) delete zoomState[cid];
                chart.update("none");
            };

            canvas.addEventListener("mousedown", onMouseDown);
            canvas.addEventListener("mousemove", onMouseMove);
            canvas.addEventListener("mouseup", finishDrag);
            canvas.addEventListener("mouseleave", onMouseLeave);
            canvas.addEventListener("dblclick", onDblClick);

            chart._dragZoomListeners = [
                ["mousedown", onMouseDown], ["mousemove", onMouseMove],
                ["mouseup", finishDrag], ["mouseleave", onMouseLeave],
                ["dblclick", onDblClick],
            ];
        },
        beforeDestroy(chart) {
            if (chart._dragZoomListeners) {
                const canvas = chart.canvas;
                for (const [evt, fn] of chart._dragZoomListeners) {
                    canvas.removeEventListener(evt, fn);
                }
                delete chart._dragZoomListeners;
            }
        },
        afterDraw(chart) {
            const dz = chart._dragZoom;
            if (!dz || !dz.dragging) return;
            const ctx = chart.ctx;
            const top = chart.chartArea.top;
            const bottom = chart.chartArea.bottom;
            const left = Math.min(dz.startPx, dz.endPx);
            const width = Math.abs(dz.endPx - dz.startPx);
            ctx.save();
            ctx.fillStyle = "rgba(59,130,246,0.12)";
            ctx.fillRect(left, top, width, bottom - top);
            ctx.strokeStyle = "rgba(59,130,246,0.4)";
            ctx.lineWidth = 1;
            ctx.strokeRect(left, top, width, bottom - top);
            ctx.restore();
        },
    };
}

export const CHART_DEFS = [
    { id: "loss_overview", section: 0, title: "Policy Loss (GTPO Surrogate)", lines: [{ key: "loss", label: "Policy Loss", color: "#3b82f6" }] },
    { id: "reward_overview", section: 0, title: "Reward ± σ", lines: [{ key: "reward", label: "Reward", color: "#10b981" }], stdBand: "reward_std" },
    { id: "policy_loss", section: 1, title: "Policy Loss (Detail)", lines: [{ key: "policy_loss", label: "Policy Loss", color: "#f59e0b" }] },
    { id: "loss_composite", section: 1, title: "Loss + Entropy", lines: [{ key: "loss", label: "Policy Loss", color: "#3b82f6" }, { key: "entropy", label: "Entropy", color: "#22d3ee" }] },
    { id: "reward_detail", section: 2, title: "Mean Reward ± σ", lines: [{ key: "reward_mean", label: "Mean Reward", color: "#10b981" }], stdBand: "reward_std" },
    { id: "advantage", section: 2, title: "Advantage", lines: [{ key: "advantage_mean", label: "Advantage", color: "#a78bfa" }] },
    { id: "best_reward", section: 2, title: "Best Reward", lines: [{ key: "best_reward", label: "Best Reward", color: "#fbbf24" }] },
    { id: "kl_div", section: 3, title: "KL Divergence", lines: [{ key: "kl_divergence", label: "KL Divergence", color: "#f472b6" }] },
    { id: "clip_frac", section: 3, title: "Clip Fraction", lines: [{ key: "clip_fraction", label: "Clip Fraction", color: "#fb923c" }] },
    { id: "entropy", section: 3, title: "Entropy", lines: [{ key: "entropy", label: "Entropy", color: "#22d3ee" }] },
    { id: "lr_schedule", section: 4, title: "Learning Rate", lines: [{ key: "learning_rate", label: "Learning Rate", color: "#818cf8" }] },
    { id: "grad_norm", section: 4, title: "Gradient Norm", lines: [{ key: "gradient_norm", label: "Gradient Norm", color: "#f87171" }], refLine: { value: 1.0, label: "Clip", color: "#64748b" } },
    { id: "throughput", section: 5, title: "Throughput", lines: [{ key: "tokens_per_second", label: "Tokens/sec", color: "#34d399" }, { key: "samples_per_second", label: "Samples/sec", color: "#60a5fa" }] },
    { id: "gpu_mem", section: 5, title: "GPU Memory %", lines: [{ key: "gpu_memory_used", label: "GPU Mem %", color: "#fbbf24" }] },
    { id: "gpu_util", section: 5, title: "GPU Utilization", lines: [{ key: "gpu_utilization", label: "GPU Util %", color: "#f97316" }] },
    { id: "cpu_usage", section: 5, title: "CPU Usage", lines: [{ key: "cpu_percent", label: "CPU %", color: "#06b6d4" }] },
    { id: "memory_usage", section: 5, title: "Memory Usage", lines: [{ key: "memory_percent", label: "Memory %", color: "#8b5cf6" }] },
];

export const WANDB_COLORS = [
    '#B1B4B9', '#58D3DB', '#5ED6A4', '#FCA36F', '#FF7A88',
    '#7DB1FA', '#BBE06B', '#FFCF4D', '#E180FF', '#B199FF',
];
