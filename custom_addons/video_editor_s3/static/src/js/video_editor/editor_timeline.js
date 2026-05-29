/** @odoo-module **/

import { Component, useRef, onMounted, onWillUnmount } from "@odoo/owl";

export class EditorTimeline extends Component {
    static template = "video_editor_s3.EditorTimeline";
    static props = {
        duration: Number,
        currentTime: Number,
        trim: Object,
        onTrimChange: Function,
        onSeek: Function,
        disabled: { type: Boolean, optional: true },
    };

    setup() {
        this.barRef = useRef("bar");
        this._mouseMove = this._onMouseMove.bind(this);
        this._mouseUp = this._onMouseUp.bind(this);
        this._activeHandle = null;
        onMounted(() => {
            window.addEventListener("mousemove", this._mouseMove);
            window.addEventListener("mouseup", this._mouseUp);
        });
        onWillUnmount(() => {
            window.removeEventListener("mousemove", this._mouseMove);
            window.removeEventListener("mouseup", this._mouseUp);
        });
    }

    get duration() {
        return Math.max(0.001, this.props.duration || 0);
    }

    get trim() {
        return this.props.trim || { start: 0, end: this.duration };
    }

    _pct(seconds) {
        return Math.max(0, Math.min(100, (seconds / this.duration) * 100));
    }

    get startPct() { return this._pct(this.trim.start); }
    get endPct() { return this._pct(this.trim.end); }
    get cursorPct() { return this._pct(this.props.currentTime || 0); }

    _eventToSeconds(ev) {
        const rect = this.barRef.el.getBoundingClientRect();
        const x = Math.max(0, Math.min(rect.width, ev.clientX - rect.left));
        return (x / rect.width) * this.duration;
    }

    onBarClick(ev) {
        if (this.props.disabled) return;
        if (ev.target.classList.contains("o_trim_handle")) return;
        const seconds = this._eventToSeconds(ev);
        this.props.onSeek(seconds);
    }

    onHandleDown(which, ev) {
        if (this.props.disabled) return;
        ev.preventDefault();
        ev.stopPropagation();
        this._activeHandle = which;
    }

    _onMouseMove(ev) {
        if (!this._activeHandle || !this.barRef.el) return;
        const seconds = this._eventToSeconds(ev);
        const trim = { ...this.trim };
        if (this._activeHandle === "start") {
            trim.start = Math.min(seconds, trim.end - 0.05);
            trim.start = Math.max(0, trim.start);
        } else if (this._activeHandle === "end") {
            trim.end = Math.max(seconds, trim.start + 0.05);
            trim.end = Math.min(this.duration, trim.end);
        }
        this.props.onTrimChange(trim);
    }

    _onMouseUp() {
        this._activeHandle = null;
    }

    formatTime(seconds) {
        const s = Math.max(0, Math.floor(seconds || 0));
        const m = Math.floor(s / 60);
        const r = s % 60;
        return `${m}:${r.toString().padStart(2, "0")}`;
    }
}
