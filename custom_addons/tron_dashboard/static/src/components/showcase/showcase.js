/** @odoo-module */

import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { Component, useState, onWillStart } from "@odoo/owl";

const DEFAULT_TRAJECTORIES_URL = "https://example.com/tron-repo";
const DEFAULT_DATASET_URL = "https://example.com/tron-dataset";

export class TronShowcase extends Component {
    static template = "tron_dashboard.TronShowcase";
    static props = ["*"];

    setup() {
        this.orm = useService("orm");
        this.state = useState({
            trajectoriesUrl: DEFAULT_TRAJECTORIES_URL,
            datasetUrl: DEFAULT_DATASET_URL,
        });

        onWillStart(async () => {
            const [trajUrl, dsUrl] = await Promise.all([
                this.orm.call(
                    "ir.config_parameter", "get_param",
                    ["tron_dashboard.trajectories_url", ""]
                ),
                this.orm.call(
                    "ir.config_parameter", "get_param",
                    ["tron_dashboard.dataset_url", ""]
                ),
            ]);
            if (trajUrl) {
                this.state.trajectoriesUrl = trajUrl;
            }
            if (dsUrl) {
                this.state.datasetUrl = dsUrl;
            }
        });
    }
}

registry.category("actions").add("tron_dashboard.showcase", TronShowcase);
