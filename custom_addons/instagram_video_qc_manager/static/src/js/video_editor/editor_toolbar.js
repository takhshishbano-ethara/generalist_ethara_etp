/** @odoo-module **/
import { Component } from "@odoo/owl";

/**
 * Left sidebar: editing tools, aspect presets, filter sliders, transform
 * buttons, audio toggle, and (when in crop mode) numeric crop inputs.
 */
export class EditorToolbar extends Component {
    static template = "instagram_video_qc_manager.EditorToolbar";
    static props = {
        mode: String,
        aspect: String,
        // ``config`` is the ACTIVE SLOT's config — owns the crop rectangle
        // for the slot the user is currently editing.
        config: Object,
        // ``sharedConfig`` owns the per-version filters / transform /
        // audio (brightness, contrast, saturation, rotate, mute) that
        // are applied to BOTH slots.
        sharedConfig: { type: Object, optional: true },
        videoEl: { optional: true },
        onModeChange: Function,
        onAspectChange: Function,
        onFilterChange: Function,
        onCropInput: Function,
        onRotate: Function,
        onToggleMute: Function,
    };

    aspectPresets = ["free", "1:1", "9:16", "16:9", "4:5"];
    modes = [
        { key: "trim", icon: "fa-scissors", label: "Trim" },
        { key: "crop", icon: "fa-crop", label: "Crop" },
        { key: "filter", icon: "fa-sliders", label: "Filters" },
        { key: "compare", icon: "fa-columns", label: "Compare" },
    ];

    onSlider(field, ev) {
        this.props.onFilterChange(field, parseFloat(ev.target.value));
    }
}
