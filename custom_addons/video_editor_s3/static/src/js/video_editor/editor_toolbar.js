/** @odoo-module **/

import { Component } from "@odoo/owl";

const RESOLUTION_PRESETS = [
    { key: "original", label: "Original (no resize)", w: 0, h: 0 },
    { key: "2160p",    label: "4K UHD (3840 × 2160)", w: 3840, h: 2160 },
    { key: "1440p",    label: "1440p (2560 × 1440)",  w: 2560, h: 1440 },
    { key: "1080p",    label: "1080p (1920 × 1080)",  w: 1920, h: 1080 },
    { key: "720p",     label: "720p (1280 × 720)",    w: 1280, h: 720 },
    { key: "480p",     label: "480p (854 × 480)",     w: 854,  h: 480 },
    { key: "360p",     label: "360p (640 × 360)",     w: 640,  h: 360 },
    { key: "240p",     label: "240p (426 × 240)",     w: 426,  h: 240 },
    { key: "custom",   label: "Custom…",              w: null, h: null },
];

export class EditorToolbar extends Component {
    static template = "video_editor_s3.EditorToolbar";
    static props = {
        mode: String,
        config: Object,
        duration: { type: Number, optional: true },
        disabled: { type: Boolean, optional: true },
        onChangeMode: Function,
        onResetSlot: Function,
        onToggleMute: Function,
        onUpdateFilter: Function,
        onUpdateRotate: Function,
        onUpdateResize: Function,
        onTrimChange: Function,
        onUpdateCrop: Function,
    };

    get filterValue() {
        return this.props.config.filter || { brightness: 0, contrast: 1, saturation: 1 };
    }

    get rotate() {
        return this.props.config.rotate || 0;
    }

    get resize() {
        return this.props.config.resize || null;
    }

    get isMuted() {
        return !!this.props.config.mute;
    }

    setMode(mode) {
        if (!this.props.disabled) {
            this.props.onChangeMode(mode);
        }
    }

    onBrightness(ev) {
        this.props.onUpdateFilter({ ...this.filterValue, brightness: parseFloat(ev.target.value) });
    }
    onContrast(ev) {
        this.props.onUpdateFilter({ ...this.filterValue, contrast: parseFloat(ev.target.value) });
    }
    onSaturation(ev) {
        this.props.onUpdateFilter({ ...this.filterValue, saturation: parseFloat(ev.target.value) });
    }
    onRotate(deg) {
        this.props.onUpdateRotate(deg);
    }
    onResizePreset(preset) {
        this.props.onUpdateResize(preset);
    }
    onMute() {
        this.props.onToggleMute();
    }
    onReset(field) {
        this.props.onResetSlot(field);
    }

    formatTime(seconds) {
        const total = Math.max(0, seconds || 0);
        const m = Math.floor(total / 60);
        const rem = total - m * 60;
        const s = Math.floor(rem);
        const ms = Math.round((rem - s) * 1000);
        return `${m.toString().padStart(2, "0")}:${s.toString().padStart(2, "0")}.${ms.toString().padStart(3, "0")}`;
    }

    get trimStart() {
        return (this.props.config.trim && this.props.config.trim.start) || 0;
    }

    get trimEnd() {
        return (this.props.config.trim && this.props.config.trim.end) || 0;
    }

    get trimDuration() {
        return Math.max(0, this.trimEnd - this.trimStart);
    }

    _parts(seconds) {
        const total = Math.max(0, seconds || 0);
        const min = Math.floor(total / 60);
        const rem = total - min * 60;
        const sec = Math.floor(rem);
        const ms = Math.round((rem - sec) * 1000);
        return { min, sec, ms };
    }

    get startParts() { return this._parts(this.trimStart); }
    get endParts() { return this._parts(this.trimEnd); }

    get resolutionPresets() {
        return RESOLUTION_PRESETS;
    }

    get resizeWidth() {
        const r = this.props.config.resize;
        return (r && r.w) || 0;
    }

    get resizeHeight() {
        const r = this.props.config.resize;
        return (r && r.h) || 0;
    }

    get resizePresetKey() {
        const r = this.props.config.resize;
        if (!r || !r.w || !r.h) return "original";
        const match = RESOLUTION_PRESETS.find((p) => p.w === r.w && p.h === r.h);
        return match ? match.key : "custom";
    }

    _evenize(v) {
        const n = Math.max(2, Math.round((parseFloat(v) || 0) / 2) * 2);
        return n;
    }

    onSelectResolution(ev) {
        if (this.props.disabled) return;
        const key = ev.target.value;
        const preset = RESOLUTION_PRESETS.find((p) => p.key === key);
        if (!preset || key === "original") {
            this.props.onUpdateResize(null);
            return;
        }
        if (key === "custom") {
            const cur = this.props.config.resize;
            const w = (cur && cur.w) || 1920;
            const h = (cur && cur.h) || 1080;
            this.props.onUpdateResize({ w: this._evenize(w), h: this._evenize(h) });
            return;
        }
        this.props.onUpdateResize({ w: preset.w, h: preset.h });
    }

    get crop() {
        return this.props.config.crop || null;
    }

    get cropX() { return (this.crop && this.crop.x) || 0; }
    get cropY() { return (this.crop && this.crop.y) || 0; }
    get cropW() { return (this.crop && this.crop.w) || 0; }
    get cropH() { return (this.crop && this.crop.h) || 0; }

    onCropDimChange(field, ev) {
        if (this.props.disabled) return;
        const v = parseInt(ev.target.value, 10) || 0;
        const cur = this.crop || { w: 0, h: 0, x: 0, y: 0 };
        this.props.onUpdateCrop({ ...cur, [field]: v });
    }

    onCustomResize(dim, ev) {
        if (this.props.disabled) return;
        const v = this._evenize(ev.target.value);
        const cur = this.props.config.resize || { w: 1920, h: 1080 };
        this.props.onUpdateResize({ ...cur, [dim]: v });
    }

    onTrimPartChange(which, unit, ev) {
        if (this.props.disabled) return;
        const raw = parseFloat(ev.target.value);
        const value = Number.isFinite(raw) && raw >= 0 ? raw : 0;
        const parts = which === "start" ? { ...this.startParts } : { ...this.endParts };
        parts[unit] = value;
        let seconds = parts.min * 60 + parts.sec + parts.ms / 1000;
        const dur = Math.max(0, this.props.duration || 0);
        if (dur > 0) seconds = Math.min(seconds, dur);
        seconds = Math.max(0, Math.round(seconds * 1000) / 1000);
        const trim = { ...(this.props.config.trim || { start: 0, end: dur }) };
        if (which === "start") {
            trim.start = seconds;
            if (trim.end <= trim.start) trim.end = Math.min(dur || trim.start + 0.001, trim.start + 0.001);
        } else {
            trim.end = seconds;
            if (trim.start >= trim.end) trim.start = Math.max(0, trim.end - 0.001);
        }
        this.props.onTrimChange(trim);
    }
}
