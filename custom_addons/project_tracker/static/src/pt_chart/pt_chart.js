/** @odoo-module */
import { Component, useRef, useEffect, onWillStart, onWillUnmount } from "@odoo/owl";
import { loadBundle } from "@web/core/assets";

/**
 * A thin OWL wrapper around Chart.js (Odoo's bundled "web.chartjs_lib"), so the
 * dashboard can drop in line / bar / doughnut charts without each one repeating
 * the load-bundle + lifecycle boilerplate.
 *
 * Props:
 *   - type:    Chart.js chart type ("line" | "bar" | "doughnut" | ...)
 *   - data:    Chart.js data object (labels + datasets)
 *   - options: optional Chart.js options (merged over sensible responsive defaults)
 *   - onClick: optional (index, datasetIndex) => void, for the clicked element
 *
 * The chart is rebuilt whenever `data`/`type` change and destroyed on unmount,
 * mirroring core's graph_renderer so there are no leaked canvases.
 */
export class PtChart extends Component {
    static template = "project_tracker.PtChart";
    static props = {
        type: String,
        data: Object,
        options: { type: Object, optional: true },
        onClick: { type: Function, optional: true },
        class: { type: String, optional: true },
    };

    setup() {
        this.canvasRef = useRef("canvas");
        this.chart = null;
        onWillStart(() => loadBundle("web.chartjs_lib"));
        useEffect(
            () => this._render(),
            () => [this.props.data, this.props.type]
        );
        onWillUnmount(() => this._destroy());
    }

    _destroy() {
        if (this.chart) {
            this.chart.destroy();
            this.chart = null;
        }
    }

    _render() {
        const canvas = this.canvasRef.el;
        if (!canvas || !window.Chart) {
            return;
        }
        this._destroy();
        const options = {
            responsive: true,
            maintainAspectRatio: false,
            ...(this.props.options || {}),
        };
        if (this.props.onClick) {
            options.onClick = (_ev, elements) => {
                if (elements && elements.length) {
                    this.props.onClick(elements[0].index, elements[0].datasetIndex);
                }
            };
        }
        this.chart = new window.Chart(canvas, {
            type: this.props.type,
            data: this.props.data,
            options,
        });
    }
}
