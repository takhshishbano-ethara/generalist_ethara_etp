/** @odoo-module **/
import { Component, useRef, useState } from "@odoo/owl";

/**
 * Pure-OWL timeline track with draggable start/end handles and a play cursor.
 * Bubbles trim updates up to the parent via the `onTrimChange` callback prop.
 */
export class EditorTimeline extends Component {
    static template = "instagram_video_qc_manager.EditorTimeline";
    static props = {
        duration: Number,
        currentTime: Number,
        trim: Object,
        onTrimChange: Function,
        onSeek: Function,
    };

    setup() {
        this.trackRef = useRef("track");
        this.state = useState({ dragging: null });
    }

    get startPct() {
        return this._pct(this.props.trim.start);
    }
    get endPct() {
        return this._pct(this.props.trim.end || this.props.duration);
    }
    get cursorPct() {
        return this._pct(this.props.currentTime);
    }
    _pct(t) {
        const d = this.props.duration || 1;
        return Math.min(100, Math.max(0, (t / d) * 100));
    }

    _timeAtX(clientX) {
        const rect = this.trackRef.el.getBoundingClientRect();
        const ratio = Math.min(1, Math.max(0, (clientX - rect.left) / rect.width));
        return ratio * (this.props.duration || 0);
    }

    onMouseDown(handle, ev) {
        ev.preventDefault();
        this.state.dragging = handle;
        const move = (e) => this._drag(e);
        const up = () => {
            this.state.dragging = null;
            window.removeEventListener("mousemove", move);
            window.removeEventListener("mouseup", up);
        };
        window.addEventListener("mousemove", move);
        window.addEventListener("mouseup", up);
    }

    _drag(ev) {
        const t = this._timeAtX(ev.clientX);
        const trim = { ...this.props.trim };
        if (this.state.dragging === "start") {
            trim.start = Math.min(t, (trim.end || this.props.duration) - 0.1);
        } else if (this.state.dragging === "end") {
            trim.end = Math.max(t, trim.start + 0.1);
        }
        this.props.onTrimChange(trim);
    }

    onTrackClick(ev) {
        if (this.state.dragging) return;
        this.props.onSeek(this._timeAtX(ev.clientX));
    }
}
