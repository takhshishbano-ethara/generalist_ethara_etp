/** @odoo-module **/

import { registry } from "@web/core/registry";
import { standardFieldProps } from "@web/views/fields/standard_field_props";
import { Component } from "@odoo/owl";

export class T2AVVideoPlayer extends Component {
    static template = "t2av.VideoPlayer";
    static props = { ...standardFieldProps };

    get videoUrl() {
        return this.props.record.data[this.props.name] || "";
    }
}

const t2avVideoPlayer = {
    component: T2AVVideoPlayer,
    displayName: "T2AV Video Player",
    supportedTypes: ["char"],
    extractProps: () => ({}),
};

registry.category("fields").add("t2av_video_player", t2avVideoPlayer);
