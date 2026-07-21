/** @odoo-module */

import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { Component, useState, onWillStart } from "@odoo/owl";

const DEFAULT_TRAJECTORIES_URL = "https://github.com/EtharaOrion/kang-samples.git";
const DEFAULT_DATASET_URL = "https://huggingface.co/datasets/ethara/kang";

export class KangShowcase extends Component {
    static template = "kang_dashboard.KangShowcase";
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
                    ["kang_dashboard.trajectories_url", ""]
                ),
                this.orm.call(
                    "ir.config_parameter", "get_param",
                    ["kang_dashboard.dataset_url", ""]
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

registry.category("actions").add("kang_dashboard.showcase", KangShowcase);
