/** @odoo-module **/

import { registry } from "@web/core/registry";
import { standardFieldProps } from "@web/views/fields/standard_field_props";
import { Component } from "@odoo/owl";

export class CrowleyVideoPlayer extends Component {
    static template = "crowley.VideoPlayer";
    static props = { ...standardFieldProps };

    get videoUrl() {
        return this.props.record.data[this.props.name] || "";
    }
}

const crowleyVideoPlayer = {
    component: CrowleyVideoPlayer,
    displayName: "Crowley Video Player",
    supportedTypes: ["char"],
    extractProps: () => ({}),
};

registry.category("fields").add("crowley_video_player", crowleyVideoPlayer);
