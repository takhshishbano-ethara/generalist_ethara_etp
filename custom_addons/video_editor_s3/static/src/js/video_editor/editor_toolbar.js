/** @odoo-module **/

import { Component } from "@odoo/owl";

export class EditorToolbar extends Component {
    static template = "video_editor_s3.EditorToolbar";
    static props = {
        mode: String,
        config: Object,
        disabled: { type: Boolean, optional: true },
        onChangeMode: Function,
        onResetSlot: Function,
        onToggleMute: Function,
        onUpdateFilter: Function,
        onUpdateRotate: Function,
        onUpdateResize: Function,
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
}
